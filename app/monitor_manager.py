"""Monitor Manager — JSON persistence and async task lifecycle.

Manages a collection of campground monitors:
- Persists monitor configs to {data_dir}/monitors.json using atomic writes.
- Owns asyncio tasks that run each monitor's poll loop.
- Tracks per-monitor stats (checks, alerts, errors, runtime).
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.monitor_engine import check_campground, check_permit, send_email, send_ntfy
from app.config import EMAIL_FROM, EMAIL_PASSWORD, SMTP_PORT, SMTP_SERVER

log = logging.getLogger(__name__)

_AUTO_STOP_GRACE = timedelta(hours=36)


def _trip_window_expired(config: dict, now: datetime | None = None) -> bool:
    """Return True if the monitor's trip window ended more than the grace period ago.

    Uses ``check_out`` for campground monitors and ``entry_date`` for permits.
    The end of the window is taken to be UTC midnight at the start of the day
    after the end date (so a ``check_out`` of 2026-05-10 ends at 2026-05-11 00:00Z),
    and we require strictly more than ``_AUTO_STOP_GRACE`` past that point before
    signaling. Missing or malformed dates return False (never auto-stop on bad data).
    """
    end_str = config.get("check_out") or config.get("entry_date") or ""
    if not end_str:
        return False
    try:
        end_date = date.fromisoformat(end_str)
    except (TypeError, ValueError):
        return False
    end_of_window = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    current = now or datetime.now(timezone.utc)
    return (current - end_of_window) > _AUTO_STOP_GRACE


class MonitorManager:
    """Manage campground monitors: persistence + async task lifecycle."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, "monitors.json")
        self._tasks: dict[str, asyncio.Task] = {}
        self._paused: set[str] = set()
        # Notified sites per monitor: monitor_id -> {(facility_id, site_id): notified_epoch}
        self._notified: dict[str, dict[tuple[str, str], float]] = {}
        # Track start time for runtime calculation: monitor_id -> epoch
        self._start_times: dict[str, float] = {}
        # Re-alert after this many seconds even if site is still available
        self.notify_cooldown_seconds = 15 * 60  # 15 minutes
        # Per-monitor check log: monitor_id -> deque of log entries (newest first)
        self._check_logs: dict[str, deque[dict]] = {}
        self._max_log_entries = 20

        os.makedirs(data_dir, exist_ok=True)
        if not os.path.exists(self._path):
            self._write({"monitors": []})

    # ── Internal I/O ──────────────────────────────────────────────────────────

    def _read(self) -> dict:
        with open(self._path, encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        """Atomic write: write to temp file then os.replace() to target."""
        dir_ = os.path.dirname(self._path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def list_monitors(self) -> list[dict]:
        """Return all monitor configs."""
        return self._read()["monitors"]

    def get_monitor(self, monitor_id: str) -> dict | None:
        """Return a single monitor config by ID, or None if not found."""
        for m in self.list_monitors():
            if m["id"] == monitor_id:
                return m
        return None

    def add_monitor(self, config: dict) -> dict:
        """Add a new monitor config and persist to disk.

        Args:
            config: Monitor config dict; must include an "id" field.

        Returns:
            The added config dict (a copy).
        """
        data = self._read()
        entry = dict(config)
        # Set a default status if not provided
        entry.setdefault("status", "stopped")
        data["monitors"].append(entry)
        self._write(data)
        return dict(entry)

    def update_monitor(self, monitor_id: str, **fields) -> dict | None:
        """Update fields on an existing monitor and persist to disk.

        Args:
            monitor_id: ID of the monitor to update.
            **fields:   Key/value pairs to set.

        Returns:
            Updated monitor dict, or None if not found.
        """
        data = self._read()
        for monitor in data["monitors"]:
            if monitor["id"] == monitor_id:
                monitor.update(fields)
                self._write(data)
                return dict(monitor)
        return None

    def delete_monitor(self, monitor_id: str) -> bool:
        """Delete a monitor by ID.

        Returns:
            True if deleted, False if not found.
        """
        data = self._read()
        before = len(data["monitors"])
        data["monitors"] = [m for m in data["monitors"] if m["id"] != monitor_id]
        if len(data["monitors"]) == before:
            return False
        self._write(data)
        return True

    # ── Async Task Lifecycle ──────────────────────────────────────────────────

    async def start_monitor(self, monitor_id: str) -> None:
        """Start the poll loop for a monitor.

        Creates an asyncio.Task for the poll loop and updates status to "running".
        No-ops if already running.
        """
        if monitor_id in self._tasks and not self._tasks[monitor_id].done():
            return  # Already running

        self._paused.discard(monitor_id)
        self._start_times[monitor_id] = time.time()

        # Update status and stats
        monitor = self.get_monitor(monitor_id)
        if monitor is None:
            log.error("start_monitor: monitor %s not found", monitor_id)
            return
        stats = monitor.get("stats", {})
        stats["last_started_at"] = datetime.now(timezone.utc).isoformat()
        self.update_monitor(monitor_id, status="running", stats=stats)

        task = asyncio.create_task(
            self._poll_loop(monitor_id),
            name=f"poll-{monitor_id}",
        )
        self._tasks[monitor_id] = task

    async def pause_monitor(self, monitor_id: str) -> None:
        """Pause a running monitor (skips check cycles, stays alive)."""
        self._paused.add(monitor_id)
        self.update_monitor(monitor_id, status="paused")

    async def stop_monitor(self, monitor_id: str, *, preserve_status: bool = False) -> None:
        """Cancel the poll task.

        If preserve_status is True, leave the persisted status unchanged (used during
        graceful shutdown so monitors auto-resume on next startup). Otherwise mark
        the monitor as "stopped" (used for user-initiated stops).
        """
        task = self._tasks.pop(monitor_id, None)
        # Skip cancel/await when called from inside the task being stopped
        # (e.g. the poll loop auto-stopping itself), to avoid self-deadlock.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._paused.discard(monitor_id)

        # Accumulate runtime
        monitor = self.get_monitor(monitor_id)
        stats = (monitor or {}).get("stats", {})
        start_t = self._start_times.pop(monitor_id, None)
        if start_t:
            stats["total_runtime_seconds"] = stats.get("total_runtime_seconds", 0) + int(time.time() - start_t)

        if preserve_status:
            self.update_monitor(monitor_id, stats=stats)
        else:
            stats["last_stopped_at"] = datetime.now(timezone.utc).isoformat()
            self.update_monitor(monitor_id, status="stopped", stats=stats)

    async def resume_all(self) -> None:
        """Start all monitors whose persisted status is "running" (called at startup)."""
        for monitor in self.list_monitors():
            if monitor.get("status") == "running":
                await self.start_monitor(monitor["id"])

    async def stop_all(self) -> None:
        """Cancel all running tasks on shutdown, preserving status="running" so
        resume_all() restarts them on the next startup."""
        for monitor_id in list(self._tasks.keys()):
            await self.stop_monitor(monitor_id, preserve_status=True)

    # ── Check Logs ─────────────────────────────────────────────────────────────

    def get_check_logs(self, monitor_id: str) -> list[dict]:
        """Return the last N check log entries for a monitor (newest first)."""
        return list(self._check_logs.get(monitor_id, []))

    def _append_log(self, monitor_id: str, entry: dict) -> None:
        """Append a log entry to the monitor's ring buffer."""
        if monitor_id not in self._check_logs:
            self._check_logs[monitor_id] = deque(maxlen=self._max_log_entries)
        self._check_logs[monitor_id].appendleft(entry)

    # ── Test Alert ──────────────────────────────────────────────────────────────

    def send_test_alert(self, monitor_id: str) -> dict[str, bool]:
        """Send a test notification through the monitor's configured channels.

        Returns dict with results: {"ntfy": True/False, "email": True/False}
        """
        config = self.get_monitor(monitor_id)
        if config is None:
            return {"ntfy": False, "email": False}

        name = config.get("name", "Unknown Monitor")
        body = (
            f"This is a test alert from Campground Monitor.\n"
            f"Monitor: {name}\n"
            f"Dates: {config.get('check_in', '?')} to {config.get('check_out', '?')}\n\n"
            f"If you're seeing this, notifications are working!"
        )
        title = f"Test Alert: {name}"

        results: dict[str, bool] = {"ntfy": False, "email": False}

        if config.get("enable_ntfy") or config.get("notify_ntfy"):
            topic = config.get("ntfy_topic", "")
            if topic:
                results["ntfy"] = send_ntfy(topic=topic, title=title, body=body, priority="default", tags="white_check_mark,test_tube")

        if config.get("enable_email") or config.get("notify_email"):
            email_to = config.get("email_to", "")
            if email_to:
                smtp_config = {
                    "server": SMTP_SERVER,
                    "port": SMTP_PORT,
                    "from_addr": EMAIL_FROM,
                    "password": EMAIL_PASSWORD,
                }
                results["email"] = send_email(to=email_to, subject=title, body=body, smtp_config=smtp_config)

        return results

    # ── Poll Loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self, monitor_id: str) -> None:
        """Infinite poll loop for a single monitor."""
        while True:
            if monitor_id in self._paused:
                await asyncio.sleep(5)
                continue

            config = self.get_monitor(monitor_id)
            if config is None:
                log.warning("_poll_loop: monitor %s disappeared, stopping loop", monitor_id)
                return

            if _trip_window_expired(config):
                log.info(
                    "_poll_loop: monitor %s trip window ended >%dh ago, auto-stopping",
                    monitor_id,
                    int(_AUTO_STOP_GRACE.total_seconds() // 3600),
                )
                await self.stop_monitor(monitor_id)
                return

            await self._run_check(config)

            # interval field is seconds (used in tests); fall back to poll_interval_seconds
            interval = config.get("interval") or config.get("poll_interval_seconds", 300)
            await asyncio.sleep(interval)

    async def _run_check(self, config: dict) -> None:
        """Run one availability check cycle for a monitor config.

        Iterates over all facilities in the monitor, calls check_campground for each
        (with 2s pause between), filters already-notified sites, and dispatches alerts.
        Updates monitor state and stats.
        """
        monitor_id = config["id"]
        stats = config.get("stats", {})

        # Ensure notified dict exists for this monitor
        if monitor_id not in self._notified:
            self._notified[monitor_id] = {}

        check_in = config.get("check_in", "")
        check_out = config.get("check_out", "")
        entry_date = config.get("entry_date", "")
        party_size = config.get("party_size", 1)

        # Support both multi-facility (wizard) and single-facility (legacy) configs
        facilities = config.get("facilities", [])
        if not facilities:
            fid = config.get("facility_id", "")
            fname = config.get("facility_name", fid)
            if fid:
                facilities = [{"id": fid, "name": fname}]

        # Accept monitors that have either check_in+check_out (campground) or entry_date (permit)
        if not facilities or (not entry_date and (not check_in or not check_out)):
            log.warning("_run_check: monitor %s missing required fields, skipping", monitor_id)
            return

        stats["total_checks"] = stats.get("total_checks", 0) + 1
        found_any = False
        now = time.time()

        # Collect all currently available site keys this cycle for pruning
        currently_available: set[tuple[str, str]] = set()

        for i, facility in enumerate(facilities):
            facility_id = facility["id"]
            facility_name = facility.get("name", facility_id)
            is_permit = facility.get("type") == "Permit"

            if i > 0:
                await asyncio.sleep(2)  # polite pause between facilities

            if is_permit:
                log_entry: dict[str, Any] = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    "entry_date": entry_date,
                    "party_size": party_size,
                }
            else:
                log_entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    "check_in": check_in,
                    "check_out": check_out,
                }

            try:
                if is_permit:
                    div_ids = facility.get("division_ids") or None
                    sites = await asyncio.to_thread(
                        check_permit, facility_id, entry_date,
                        party_size=party_size, division_ids=div_ids,
                    )
                else:
                    sites = await asyncio.to_thread(check_campground, facility_id, check_in, check_out)
            except Exception as exc:
                log.error("_run_check: %s failed for monitor %s: %s", facility_name, monitor_id, exc)
                stats["total_errors"] = stats.get("total_errors", 0) + 1
                log_entry.update({"status": "error", "error": str(exc), "sites_found": 0, "alerted": False})
                self._append_log(monitor_id, log_entry)
                continue

            for s in sites:
                site_key = s.get("division_id") or s.get("site_id")
                currently_available.add((facility_id, site_key))

            # A site is "new" if it's not in the notified dict OR its cooldown has expired
            new_sites = [
                s for s in sites
                if (facility_id, s.get("division_id") or s.get("site_id")) not in self._notified[monitor_id]
                or (now - self._notified[monitor_id].get((facility_id, s.get("division_id") or s.get("site_id")), 0)) >= self.notify_cooldown_seconds
            ]

            if new_sites:
                found_any = True
                await self._send_alerts(config, facility_id, facility_name, new_sites)
                for s in new_sites:
                    site_key = s.get("division_id") or s.get("site_id")
                    self._notified[monitor_id][(facility_id, site_key)] = now

            if is_permit:
                log_entry.update({
                    "status": "ok",
                    "sites_found": len(sites),
                    "new_alerts": len(new_sites),
                    "alerted": len(new_sites) > 0,
                    "sites": [
                        {"site": s["division_name"], "remaining": s["remaining"], "total": s["total"]}
                        for s in sites
                    ],
                })
            else:
                log_entry.update({
                    "status": "ok",
                    "sites_found": len(sites),
                    "new_alerts": len(new_sites),
                    "alerted": len(new_sites) > 0,
                    "sites": [
                        {"site": s["site"], "loop": s.get("loop", "?"), "type": s.get("type", "?")}
                        for s in sites
                    ],
                })
            self._append_log(monitor_id, log_entry)

            if not new_sites and sites:
                log.info("  %s: %d site(s) still available (already notified)", facility_name, len(sites))
            elif not sites:
                log.info("  %s: no availability", facility_name)

        # Prune sites that are no longer available (so we re-alert if they come back)
        stale_keys = [k for k in self._notified[monitor_id] if k not in currently_available]
        for k in stale_keys:
            del self._notified[monitor_id][k]

        if found_any:
            stats["total_alerts"] = stats.get("total_alerts", 0) + 1

        self.update_monitor(monitor_id, stats=stats)

    async def _send_alerts(self, config: dict, facility_id: str, facility_name: str, sites: list[dict]) -> None:
        """Build and dispatch notifications for newly-found available sites.

        Sends ntfy and/or email based on config flags.
        Permit and campground sites produce different alert formats.
        """
        monitor_id = config["id"]

        if sites and "division_id" in sites[0]:
            # Permit alert
            entry_date = config.get("entry_date", "")
            party_size = config.get("party_size", 1)
            lines = [
                f"  {s['division_name']}: {s['remaining']}/{s['total']} available"
                for s in sites
            ]
            body = (
                f"Permit available at {facility_name}!\n"
                f"Entry date: {entry_date} | Party size: {party_size}\n"
                f"Entry points ({len(sites)}):\n" + "\n".join(lines)
            )
            title = f"Permit Alert: {facility_name}"
            click_url = (
                f"https://www.recreation.gov/permits/{facility_id}"
                f"/registration/detailed-availability?date={entry_date}&type=overnight-permit"
            )
        else:
            # Campground alert
            check_in = config.get("check_in", "")
            check_out = config.get("check_out", "")
            lines = [f"  Site {s['site']} (loop: {s['loop']}, type: {s['type']}, max: {s['max_people']})" for s in sites]
            body = (
                f"Campsite available at {facility_name}!\n"
                f"Dates: {check_in} → {check_out}\n"
                f"Sites found ({len(sites)}):\n" + "\n".join(lines)
            )
            title = f"Campsite Alert: {facility_name}"
            click_url = f"https://www.recreation.gov/camping/campgrounds/{facility_id}"

        if config.get("enable_ntfy"):
            topic = config.get("ntfy_topic", "")
            if topic:
                send_ntfy(topic=topic, title=title, body=body, click_url=click_url)
            else:
                log.warning("_send_alerts: enable_ntfy set but no ntfy_topic for monitor %s", monitor_id)

        if config.get("enable_email"):
            email_to = config.get("email_to", "")
            if email_to:
                smtp_config = {
                    "server": SMTP_SERVER,
                    "port": SMTP_PORT,
                    "from_addr": EMAIL_FROM,
                    "password": EMAIL_PASSWORD,
                }
                send_email(to=email_to, subject=title, body=body, smtp_config=smtp_config)
            else:
                log.warning("_send_alerts: enable_email set but no email_to for monitor %s", monitor_id)
