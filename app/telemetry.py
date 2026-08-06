"""SQLite-backed telemetry: per-request check logs and a user activity audit.

Why SQLite when everything else here is JSON: monitors.json works because it is
small and rewritten whole at low frequency. Telemetry is ~16,000 append-only
rows a day needing indexed reads and cheap range deletes, which is the opposite
profile. `sqlite3` is in the standard library, so this adds no dependency.

Why summaries and not response bodies: measured against the live API on
2026-08-05, a campground month response is 496 KB and a permit month is 145 KB.
At the current polling rate, storing bodies would cost ~6.4 GB/day. Metadata
plus the parsed outcome is ~500 bytes a row, or ~8 MB/day. Bodies are captured
only for errors, where the bytes are actually diagnostic.

Threading: sqlite3 is synchronous and the poll loop is async, so EVERY call
into this class must be wrapped in asyncio.to_thread by the caller. Calling it
inline would block the event loop, which is exactly the defect the alert path
had. The connection is opened with check_same_thread=False and guarded by a
lock, which is safe because to_thread may run calls on different workers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Errors are the only case where the raw body earns its storage.
BODY_EXCERPT_LIMIT = 8 * 1024
DEFAULT_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT    NOT NULL,
  monitor_id     TEXT    NOT NULL,
  facility_id    TEXT    NOT NULL,
  facility_name  TEXT,
  kind           TEXT    NOT NULL,
  url            TEXT    NOT NULL,
  params         TEXT,
  http_status    INTEGER,
  duration_ms    INTEGER,
  response_bytes INTEGER,
  status         TEXT    NOT NULL,
  error          TEXT,
  body_excerpt   TEXT,
  sites_found    INTEGER,
  new_alerts     INTEGER,
  alerted        INTEGER,
  result         TEXT
);
CREATE INDEX IF NOT EXISTS idx_check_monitor_ts ON check_log(monitor_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_check_ts         ON check_log(ts);

CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  username    TEXT,
  event       TEXT NOT NULL,
  target_id   TEXT,
  target_name TEXT,
  detail      TEXT,
  ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(username, ts DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate_body(body, limit: int = BODY_EXCERPT_LIMIT) -> str | None:
    """Clip a response body to `limit` BYTES, tolerating non-UTF8 payloads.

    A 429 from a CDN often returns HTML, not JSON, and may not decode cleanly,
    so never assume the error body is text we can parse.
    """
    if body is None:
        return None
    if isinstance(body, str):
        body = body.encode("utf-8", errors="replace")
    return body[:limit].decode("utf-8", errors="replace")


class TelemetryStore:
    """Append-only telemetry. All methods are blocking; call via to_thread."""

    def __init__(self, data_dir: str, filename: str = "telemetry.db"):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, filename)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets the web UI read while the poll loop writes.
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Telemetry does not warrant a full fsync per commit.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Check log ─────────────────────────────────────────────────────────────

    def record_check(
        self,
        *,
        monitor_id: str,
        facility_id: str,
        facility_name: str = "",
        kind: str = "campground",
        url: str = "",
        params: dict | None = None,
        http_status: int | None = None,
        duration_ms: int | None = None,
        response_bytes: int | None = None,
        status: str = "ok",
        error: str | None = None,
        body_excerpt: str | None = None,
        sites_found: int | None = None,
        new_alerts: int | None = None,
        alerted: bool | None = None,
        result: object = None,
        ts: str | None = None,
    ) -> int:
        """Insert one row per HTTP REQUEST, not per check.

        A cross-month campground stay makes two calls, and a permit check may
        also fetch division metadata, so the request count in the UI matches
        what actually hit the network.
        """
        if status == "ok":
            # Successful responses are never stored; that is the whole point.
            body_excerpt = None
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO check_log
                   (ts, monitor_id, facility_id, facility_name, kind, url, params,
                    http_status, duration_ms, response_bytes, status, error,
                    body_excerpt, sites_found, new_alerts, alerted, result)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts or _now(), monitor_id, facility_id, facility_name, kind, url,
                    json.dumps(params or {}), http_status, duration_ms, response_bytes,
                    status, error, truncate_body(body_excerpt), sites_found,
                    new_alerts, int(bool(alerted)) if alerted is not None else None,
                    json.dumps(result) if result is not None else None,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_checks(
        self, monitor_id: str, *, limit: int = 100, offset: int = 0,
        errors_only: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM check_log WHERE monitor_id = ?"
        args: list = [monitor_id]
        if errors_only:
            sql += " AND status = 'error'"
        sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def count_checks(self, monitor_id: str, *, errors_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM check_log WHERE monitor_id = ?"
        if errors_only:
            sql += " AND status = 'error'"
        with self._lock:
            return self._conn.execute(sql, (monitor_id,)).fetchone()["n"]

    def prune_checks(self, older_than_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Delete check rows past the retention window. Returns rows removed.

        Callers should log the count: a prune that silently stops running is
        otherwise discovered as a full disk.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM check_log WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ── Audit log ─────────────────────────────────────────────────────────────

    def record_event(
        self, *, event: str, username: str | None = None,
        target_id: str | None = None, target_name: str | None = None,
        detail: object = None, ip: str | None = None, ts: str | None = None,
    ) -> int:
        """Record a user or system action.

        `username` is None for system-initiated events (auto-stop, resume at
        boot), which is what distinguishes "the rule stopped it" from "a person
        stopped it".
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO audit_log (ts, username, event, target_id, target_name, detail, ip)
                   VALUES (?,?,?,?,?,?,?)""",
                (ts or _now(), username, event, target_id, target_name,
                 json.dumps(detail) if detail is not None else None, ip),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_events(
        self, *, limit: int = 100, offset: int = 0,
        username: str | None = None, event: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        args: list = []
        if username:
            sql += " AND username = ?"
            args.append(username)
        if event:
            sql += " AND event = ?"
            args.append(event)
        sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def count_events(self, *, username: str | None = None, event: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM audit_log WHERE 1=1"
        args: list = []
        if username:
            sql += " AND username = ?"
            args.append(username)
        if event:
            sql += " AND event = ?"
            args.append(event)
        with self._lock:
            return self._conn.execute(sql, args).fetchone()["n"]


# ── Request-side helpers ──────────────────────────────────────────────────────

def client_ip(request) -> str | None:
    """Best-effort client IP.

    Every request arrives through the cloudflared sidecar, so the socket peer is
    always the container address (172.21.0.x). The forwarded header is the only
    thing carrying the real client.
    """
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", None)


async def audit(request, event: str, **kwargs) -> None:
    """Record a user action. Never raises.

    An audit write must not be able to prevent the thing it describes: a broken
    telemetry database should not stop anyone logging in or stopping a monitor.
    Runs in a thread because sqlite3 is synchronous.
    """
    store = getattr(request.app.state, "telemetry", None)
    if store is None:
        return
    try:
        kwargs.setdefault("ip", client_ip(request))
        await asyncio.to_thread(store.record_event, event=event, **kwargs)
    except Exception:
        log.exception("audit: failed to record %s", event)
