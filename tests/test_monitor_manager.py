"""Tests for app.monitor_manager — TDD for JSON persistence and async task lifecycle."""
import asyncio
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.monitor_manager import MonitorManager, _trip_window_expired


# ─────────────────────────────────────────────────────────────
# Persistence tests
# ─────────────────────────────────────────────────────────────

def test_load_empty_creates_default(tmp_data_dir):
    """MonitorManager creates monitors.json with default structure when missing."""
    mm = MonitorManager(tmp_data_dir)
    monitors_path = os.path.join(tmp_data_dir, "monitors.json")

    assert os.path.exists(monitors_path)
    with open(monitors_path) as f:
        data = json.load(f)

    assert "monitors" in data
    assert data["monitors"] == []


def test_add_monitor(tmp_data_dir, sample_monitor):
    """add_monitor returns the monitor and it appears in list_monitors."""
    mm = MonitorManager(tmp_data_dir)
    added = mm.add_monitor(sample_monitor)

    assert added["id"] == sample_monitor["id"]
    monitors = mm.list_monitors()
    assert len(monitors) == 1
    assert monitors[0]["id"] == sample_monitor["id"]


def test_add_monitor_persists_to_disk(tmp_data_dir, sample_monitor):
    """Monitor added by one instance is visible to a fresh instance from same dir."""
    mm1 = MonitorManager(tmp_data_dir)
    mm1.add_monitor(sample_monitor)

    mm2 = MonitorManager(tmp_data_dir)
    monitors = mm2.list_monitors()
    assert len(monitors) == 1
    assert monitors[0]["id"] == sample_monitor["id"]


def test_delete_monitor(tmp_data_dir, sample_monitor):
    """delete_monitor removes the monitor from the list."""
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    mm.delete_monitor(sample_monitor["id"])

    assert mm.list_monitors() == []


def test_update_monitor_status(tmp_data_dir, sample_monitor):
    """update_monitor can change the status field."""
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    mm.update_monitor(sample_monitor["id"], status="paused")

    monitor = mm.get_monitor(sample_monitor["id"])
    assert monitor["status"] == "paused"


def test_update_monitor_fields(tmp_data_dir, sample_monitor):
    """update_monitor can update multiple fields at once."""
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    mm.update_monitor(sample_monitor["id"], check_in="2026-08-01", check_out="2026-08-03")

    monitor = mm.get_monitor(sample_monitor["id"])
    assert monitor["check_in"] == "2026-08-01"
    assert monitor["check_out"] == "2026-08-03"



def test_atomic_write_survives(tmp_data_dir, sample_monitor):
    """Atomic write (temp + os.replace) leaves a valid JSON file even if checked mid-write."""
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    monitors_path = os.path.join(tmp_data_dir, "monitors.json")
    with open(monitors_path) as f:
        data = json.load(f)

    # File must be valid JSON with expected structure
    assert data["monitors"][0]["id"] == sample_monitor["id"]
    # No temp file should be left behind
    assert not os.path.exists(monitors_path + ".tmp")


# ─────────────────────────────────────────────────────────────
# Async poll loop tests
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_loop_calls_check(tmp_data_dir, sample_monitor):
    """After start_monitor, check_campground is called within one cycle."""
    monitor = {**sample_monitor, "interval": 0.1}  # 0.1s poll for fast test

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(monitor)

    available_site = {
        "site_id": "site-001",
        "site": "001",
        "loop": "UPPER",
        "type": "STANDARD NONELECTRIC",
        "max_people": 6,
    }

    with patch("app.monitor_manager.check_campground", return_value=[]) as mock_check:
        await mm.start_monitor(monitor["id"])
        await asyncio.sleep(0.3)
        await mm.stop_monitor(monitor["id"])

    assert mock_check.call_count >= 1



@pytest.mark.asyncio
async def test_notified_sites_prevents_duplicate(tmp_data_dir, sample_monitor):
    """A site found in the first cycle is not re-alerted in the second cycle."""
    monitor = {**sample_monitor, "interval": 0.1}

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(monitor)

    site = {
        "site_id": "site-dupe",
        "site": "D1",
        "loop": "LOOP A",
        "type": "STANDARD NONELECTRIC",
        "max_people": 6,
    }

    with patch("app.monitor_manager.check_campground", return_value=[site]), \
         patch("app.monitor_manager.send_ntfy", return_value=True) as mock_ntfy, \
         patch("app.monitor_manager.send_email", return_value=True):

        await mm.start_monitor(monitor["id"])
        await asyncio.sleep(0.35)
        await mm.stop_monitor(monitor["id"])

    # ntfy called for first discovery; NOT called again for same site on second cycle
    # With 3 cycles at 0.1s, send_ntfy should have been called exactly once for site-dupe
    assert mock_ntfy.call_count == 1


# ─────────────────────────────────────────────────────────────
# Dedup ledger: what may and may not re-arm an alert
#
# These drive _run_check directly rather than the poll loop so each cycle is a
# discrete, ordered step — the behaviour under test is "what happened on the
# PREVIOUS cycle", which sleep-based tests cannot pin down.
# ─────────────────────────────────────────────────────────────

_DEDUP_SITE = {
    "site_id": "site-flap",
    "site": "H017",
    "loop": "LOOP H",
    "type": "STANDARD NONELECTRIC",
    "max_people": 6,
}


async def _run_cycles(mm, monitor_id, outcomes):
    """Run one _run_check per entry in `outcomes`; returns the send_ntfy mock.

    Each outcome is either a list of sites to return or an Exception to raise,
    which is how a real cycle can end.
    """
    def _check(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with patch("app.monitor_manager.check_campground", side_effect=_check), \
         patch("app.monitor_manager.send_ntfy", return_value=True) as mock_ntfy:
        for _ in range(len(outcomes)):
            await mm._run_check(mm.get_monitor(monitor_id))
    return mock_ntfy


@pytest.mark.asyncio
async def test_failed_check_does_not_rearm_alert(tmp_data_dir, sample_monitor):
    """A facility that errors must not drop its sites from the notified ledger.

    Observed in production 2026-08-06: a 429 between two good cycles re-alerted
    all 37 sites 18 seconds after the previous alert. The error path skips the
    currently_available bookkeeping but the prune still ran, so a failed request
    was indistinguishable from "everything became unavailable".
    """
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    mock_ntfy = await _run_cycles(mm, sample_monitor["id"], [
        [_DEDUP_SITE],                              # discovered -> alert
        RuntimeError("429 Client Error: Too Many Requests"),
        [_DEDUP_SITE],                              # same site, still available
    ])

    assert mock_ntfy.call_count == 1


@pytest.mark.asyncio
async def test_single_missing_cycle_does_not_rearm_alert(tmp_data_dir, sample_monitor):
    """One cycle of absence is noise, not a booking; it must not re-alert.

    Observed 2026-08-11: site H017 dropped out of one 60-second response and
    came back on the next, producing a duplicate alert 3 minutes after the
    first — well inside the 15-minute cooldown.
    """
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    mock_ntfy = await _run_cycles(mm, sample_monitor["id"], [
        [_DEDUP_SITE],   # discovered -> alert
        [],              # single-cycle blip
        [_DEDUP_SITE],   # back again
    ])

    assert mock_ntfy.call_count == 1


@pytest.mark.asyncio
async def test_sustained_absence_rearms_alert(tmp_data_dir, sample_monitor):
    """A site that really goes away and later returns must alert again.

    The guard against over-correcting the two tests above: pruning is what
    makes a genuine book-then-cancel reach the user, so it must survive.
    """
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    mock_ntfy = await _run_cycles(mm, sample_monitor["id"], [
        [_DEDUP_SITE],   # discovered -> alert
        [],              # gone
        [],              # still gone -> now believed booked
        [_DEDUP_SITE],   # released -> alert again
    ])

    assert mock_ntfy.call_count == 2


@pytest.mark.asyncio
async def test_send_alerts_dispatches_to_ntfy_and_email(tmp_data_dir, sample_monitor):
    """_send_alerts fires both ntfy and email when both are enabled on the config."""
    monitor = {
        **sample_monitor,
        "interval": 0.1,
        "enable_ntfy": True,
        "enable_email": True,
        "email_to": "alert@example.com",
        "ntfy_topic": "test-topic",
    }

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(monitor)

    site = {
        "site_id": "site-alert",
        "site": "A1",
        "loop": "LOOP B",
        "type": "STANDARD NONELECTRIC",
        "max_people": 6,
    }

    with patch("app.monitor_manager.check_campground", return_value=[site]), \
         patch("app.monitor_manager.send_ntfy", return_value=True) as mock_ntfy, \
         patch("app.monitor_manager.send_email", return_value=True) as mock_email:

        await mm.start_monitor(monitor["id"])
        await asyncio.sleep(0.2)
        await mm.stop_monitor(monitor["id"])

    assert mock_ntfy.call_count >= 1
    assert mock_email.call_count >= 1


@pytest.mark.asyncio
async def test_permit_monitor_calls_check_permit(tmp_data_dir):
    """When a monitor has permit-type facilities, check_permit is called (not check_campground)."""
    permit_monitor = {
        "id": "test-permit-monitor-001",
        "name": "Yosemite Wilderness Permit Monitor",
        "facilities": [{"id": "445859", "name": "Yosemite Wilderness", "type": "Permit"}],
        # Relative, not hardcoded: a past entry_date auto-stops the monitor
        # before check_permit is ever called. See sample_monitor in conftest.
        "entry_date": (date.today() + timedelta(days=30)).isoformat(),
        "party_size": 2,
        "check_in": "",
        "check_out": "",
        "interval": 0.1,
        "enable_email": False,
        "enable_ntfy": False,
        "ntfy_topic": "",
        "email_to": "",
    }

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(permit_monitor)

    with patch("app.monitor_manager.check_permit", return_value=[]) as mock_permit, \
         patch("app.monitor_manager.check_campground", return_value=[]) as mock_campground:
        await mm.start_monitor(permit_monitor["id"])
        await asyncio.sleep(0.3)
        await mm.stop_monitor(permit_monitor["id"])

    assert mock_permit.call_count >= 1
    assert mock_campground.call_count == 0


@pytest.mark.asyncio
async def test_api_error_skips_cycle_gracefully(tmp_data_dir, sample_monitor):
    """If check_campground raises, the monitor stays running and retries next cycle."""
    monitor = {**sample_monitor, "interval": 0.1}

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(monitor)

    call_count = {"n": 0}

    def flaky_check(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("API timeout")
        return []

    with patch("app.monitor_manager.check_campground", side_effect=flaky_check):
        await mm.start_monitor(monitor["id"])
        await asyncio.sleep(0.35)
        await mm.stop_monitor(monitor["id"])

    # Should have retried after the error
    assert call_count["n"] >= 2

    # Monitor should still be in "running" state (not "error") after stop
    # (stop sets it to stopped, but it didn't crash)
    stored = mm.get_monitor(monitor["id"])
    assert stored["status"] == "stopped"


# ─────────────────────────────────────────────────────────────
# Auto-stop after trip window passes
# ─────────────────────────────────────────────────────────────

def test_trip_window_expired_campground_past():
    """Campground trip ended well beyond grace → True."""
    cfg = {"check_out": "2026-05-10"}
    now = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    assert _trip_window_expired(cfg, now=now) is True


def test_trip_window_expired_boundary_under_grace():
    """End-of-day was 35h ago — still inside the 36h grace → False."""
    cfg = {"check_out": "2026-05-10"}
    # end_of_window = 2026-05-11 00:00Z; 35h after = 2026-05-12 11:00Z
    now = datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc)
    assert _trip_window_expired(cfg, now=now) is False


def test_trip_window_expired_boundary_just_over_grace():
    """End-of-day was 36h01m ago → True."""
    cfg = {"check_out": "2026-05-10"}
    # end_of_window = 2026-05-11 00:00Z; 36h01m after = 2026-05-12 12:01Z
    now = datetime(2026, 5, 12, 12, 1, tzinfo=timezone.utc)
    assert _trip_window_expired(cfg, now=now) is True


def test_trip_window_expired_future():
    """Future check_out → False."""
    cfg = {"check_out": "2030-01-01"}
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    assert _trip_window_expired(cfg, now=now) is False


def test_trip_window_expired_permit_entry_date():
    """Permit monitor uses entry_date, not check_out."""
    cfg = {"entry_date": "2026-05-10", "check_out": ""}
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    assert _trip_window_expired(cfg, now=now) is True


def test_trip_window_expired_missing_dates():
    """Monitor with no end date → False (don't auto-stop on bad data)."""
    assert _trip_window_expired({}) is False
    assert _trip_window_expired({"check_out": ""}) is False


def test_trip_window_expired_malformed_date():
    """Unparseable date → False."""
    assert _trip_window_expired({"check_out": "tomorrow"}) is False


@pytest.mark.asyncio
async def test_poll_loop_auto_stops_when_trip_window_passed(tmp_data_dir, sample_monitor):
    """A monitor with check_out >24h in the past auto-stops without calling check_campground."""
    monitor = {
        **sample_monitor,
        "interval": 0.05,
        "check_in": "2024-01-01",  # well in the past
        "check_out": "2024-01-03",
    }

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(monitor)

    with patch("app.monitor_manager.check_campground", return_value=[]) as mock_check:
        await mm.start_monitor(monitor["id"])
        await asyncio.sleep(0.2)

    stored = mm.get_monitor(monitor["id"])
    assert stored["status"] == "stopped"
    assert mock_check.call_count == 0


@pytest.mark.asyncio
async def test_poll_loop_auto_stops_past_permit(tmp_data_dir):
    """A permit monitor with entry_date >24h past auto-stops without calling check_permit."""
    permit_monitor = {
        "id": "test-past-permit-001",
        "name": "Past Permit",
        "facilities": [{"id": "445859", "name": "Yosemite Wilderness", "type": "Permit"}],
        "entry_date": "2024-01-01",
        "party_size": 2,
        "check_in": "",
        "check_out": "",
        "interval": 0.05,
        "enable_email": False,
        "enable_ntfy": False,
    }

    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(permit_monitor)

    with patch("app.monitor_manager.check_permit", return_value=[]) as mock_permit:
        await mm.start_monitor(permit_monitor["id"])
        await asyncio.sleep(0.2)

    stored = mm.get_monitor(permit_monitor["id"])
    assert stored["status"] == "stopped"
    assert mock_permit.call_count == 0


def test_list_monitors_filters_by_owner(tmp_data_dir):
    from app.monitor_manager import MonitorManager
    mgr = MonitorManager(tmp_data_dir)
    mgr.add_monitor({"id": "m1", "owner": "stu"})
    mgr.add_monitor({"id": "m2", "owner": "alice"})
    mgr.add_monitor({"id": "m3", "owner": "stu"})
    assert [m["id"] for m in mgr.list_monitors(owner="stu")] == ["m1", "m3"]
    assert [m["id"] for m in mgr.list_monitors(owner="alice")] == ["m2"]
    # None means "no filter" (admin path).
    assert {m["id"] for m in mgr.list_monitors()} == {"m1", "m2", "m3"}


# ── Alert delivery: non-blocking, timed out, and honest about failure ──────────

@pytest.mark.asyncio
async def test_send_alerts_does_not_block_the_event_loop(tmp_data_dir, sample_monitor):
    """Senders must go through asyncio.to_thread like every other network call.

    _send_alerts used to call send_ntfy/send_email inline. Since smtplib.SMTP
    had no timeout, one hung SMTP host froze every monitor AND the web UI.
    """
    sample_monitor["enable_ntfy"] = True
    sample_monitor["ntfy_topic"] = "t"
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)

    loop = asyncio.get_running_loop()
    calling_threads = []

    def slow_send(*a, **kw):
        calling_threads.append(threading.current_thread())
        return True

    with patch("app.monitor_manager.send_ntfy", side_effect=slow_send):
        await mm._send_alerts(
            mm.get_monitor(sample_monitor["id"]), "232450", "UPPER PINES",
            [{"site_id": "s1", "site": "s1", "loop": "A", "type": "T", "max_people": 6}],
        )

    assert calling_threads, "send_ntfy was never called"
    assert calling_threads[0] is not threading.main_thread(), (
        "send_ntfy ran on the event loop thread; it must be wrapped in "
        "asyncio.to_thread so a hung host cannot freeze the process"
    )


@pytest.mark.asyncio
async def test_failed_alert_is_not_marked_as_delivered(tmp_data_dir, sample_monitor):
    """A sender returning False must not mark the site notified.

    Otherwise the 15-minute cooldown suppresses the retry and the alert is lost
    silently: you never learn the notification did not arrive.
    """
    sample_monitor["enable_ntfy"] = True
    sample_monitor["ntfy_topic"] = "t"
    sample_monitor["facilities"] = [
        {"id": "232450", "name": "UPPER PINES", "type": "Campground"}
    ]
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)
    mid = sample_monitor["id"]

    site = {"site_id": "s1", "site": "s1", "loop": "A", "type": "T", "max_people": 6}
    with patch("app.monitor_manager.check_campground", return_value=[site]), \
         patch("app.monitor_manager.send_ntfy", return_value=False):
        await mm._run_check(mm.get_monitor(mid))

    assert ("232450", "s1") not in mm._notified.get(mid, {}), (
        "a failed send marked the site as notified, so the cooldown will "
        "suppress the retry and the alert is lost"
    )


@pytest.mark.asyncio
async def test_successful_alert_is_marked_as_delivered(tmp_data_dir, sample_monitor):
    """The happy path must still record the notification (dedup still works)."""
    sample_monitor["enable_ntfy"] = True
    sample_monitor["ntfy_topic"] = "t"
    sample_monitor["facilities"] = [
        {"id": "232450", "name": "UPPER PINES", "type": "Campground"}
    ]
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)
    mid = sample_monitor["id"]

    site = {"site_id": "s1", "site": "s1", "loop": "A", "type": "T", "max_people": 6}
    with patch("app.monitor_manager.check_campground", return_value=[site]), \
         patch("app.monitor_manager.send_ntfy", return_value=True):
        await mm._run_check(mm.get_monitor(mid))

    assert ("232450", "s1") in mm._notified.get(mid, {})


# NOTE: the SMTP timeout test lives in tests/test_monitor_engine.py, which is
# where send_email is defined and imported.


# ── Pause/resume ordering ─────────────────────────────────────────────────────



# ── Telemetry integration ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_writes_one_telemetry_row_per_request(tmp_data_dir, sample_monitor):
    """Rows are per HTTP request, and the engine's meta list carries the detail."""
    from app.telemetry import TelemetryStore
    tel = TelemetryStore(tmp_data_dir)
    try:
        sample_monitor["facilities"] = [
            {"id": "232450", "name": "UPPER PINES", "type": "Campground"}
        ]
        mm = MonitorManager(tmp_data_dir, telemetry=tel)
        mm.add_monitor(sample_monitor)

        def fake_check(fid, ci, co, meta=None):
            if meta is not None:
                meta.append({"url": "http://x", "params": {"start_date": ci},
                             "http_status": 200, "duration_ms": 12,
                             "response_bytes": 500})
            return []

        with patch("app.monitor_manager.check_campground", side_effect=fake_check):
            await mm._run_check(mm.get_monitor(sample_monitor["id"]))

        rows = tel.get_checks(sample_monitor["id"])
        assert len(rows) == 1
        assert rows[0]["http_status"] == 200
        assert rows[0]["response_bytes"] == 500
        assert rows[0]["status"] == "ok"
        assert rows[0]["body_excerpt"] is None
    finally:
        tel.close()


@pytest.mark.asyncio
async def test_failed_check_is_recorded_as_an_error_row(tmp_data_dir, sample_monitor):
    from app.telemetry import TelemetryStore
    tel = TelemetryStore(tmp_data_dir)
    try:
        sample_monitor["facilities"] = [
            {"id": "232450", "name": "UPPER PINES", "type": "Campground"}
        ]
        mm = MonitorManager(tmp_data_dir, telemetry=tel)
        mm.add_monitor(sample_monitor)

        with patch("app.monitor_manager.check_campground",
                   side_effect=RuntimeError("429 Too Many Requests")):
            await mm._run_check(mm.get_monitor(sample_monitor["id"]))

        rows = tel.get_checks(sample_monitor["id"])
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert "429" in rows[0]["error"]
    finally:
        tel.close()


@pytest.mark.asyncio
async def test_check_history_survives_a_new_manager(tmp_data_dir, sample_monitor):
    """The old ring buffer was in-memory and lost every restart."""
    from app.telemetry import TelemetryStore
    tel = TelemetryStore(tmp_data_dir)
    try:
        sample_monitor["facilities"] = [
            {"id": "232450", "name": "UPPER PINES", "type": "Campground"}
        ]
        mm = MonitorManager(tmp_data_dir, telemetry=tel)
        mm.add_monitor(sample_monitor)
        with patch("app.monitor_manager.check_campground", return_value=[]):
            await mm._run_check(mm.get_monitor(sample_monitor["id"]))

        fresh = MonitorManager(tmp_data_dir, telemetry=tel)
        assert len(fresh.get_check_logs(sample_monitor["id"])) == 1
    finally:
        tel.close()


@pytest.mark.asyncio
async def test_manager_works_without_telemetry(tmp_data_dir, sample_monitor):
    """The CLI and older tests construct a manager with no database."""
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor(sample_monitor)
    with patch("app.monitor_manager.check_campground", return_value=[]):
        await mm._run_check(mm.get_monitor(sample_monitor["id"]))
    assert mm.get_check_logs(sample_monitor["id"])


def test_leftover_paused_status_is_normalized_to_stopped(tmp_data_dir, sample_monitor):
    """Pause was removed, so the file must stop carrying that status.

    Monitors saved before the removal can still read "paused". Leaving it there
    would mean monitors.json holds a state the code no longer understands, and
    the dashboard would render a status with no way to reach or leave it.
    """
    seed = MonitorManager(tmp_data_dir)
    seed.add_monitor({**sample_monitor, "status": "paused"})

    fresh = MonitorManager(tmp_data_dir)

    assert fresh.get_monitor(sample_monitor["id"])["status"] == "stopped"


def test_normalization_leaves_other_statuses_alone(tmp_data_dir, sample_monitor):
    seed = MonitorManager(tmp_data_dir)
    seed.add_monitor({**sample_monitor, "status": "running"})

    fresh = MonitorManager(tmp_data_dir)

    assert fresh.get_monitor(sample_monitor["id"])["status"] == "running"


def test_pause_is_gone(tmp_data_dir):
    """The API surface must not offer a pause any more."""
    mm = MonitorManager(tmp_data_dir)
    assert not hasattr(mm, "pause_monitor")
    assert not hasattr(mm, "_paused")


# ── Trailhead name backfill ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_resolves_missing_trailhead_names(tmp_data_dir):
    """Monitors saved before names were captured show raw ids on the dashboard.

    Cloning does not fix it either, since a clone copies whatever its source
    had, so the ids have to be resolved for the existing rows.
    """
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor({
        "id": "m1", "name": "Cath", "status": "stopped", "stats": {},
        "facilities": [{"id": "445859", "name": "Yosemite Wilderness Permits",
                        "type": "Permit", "division_ids": ["44585907", "44585945"]}],
    })

    with patch("app.monitor_manager.get_permit_divisions",
               return_value={"44585907": "Cathedral Lakes",
                             "44585945": "Sunrise Lakes",
                             "99999999": "Somewhere Else"}) as mock_res:
        updated = await mm.backfill_division_names()

    assert updated == 1
    mock_res.assert_called_once_with("445859")
    fac = mm.get_monitor("m1")["facilities"][0]
    # Only the trailheads this monitor watches, not the whole facility.
    assert fac["division_names"] == {"44585907": "Cathedral Lakes",
                                     "44585945": "Sunrise Lakes"}


@pytest.mark.asyncio
async def test_backfill_leaves_existing_names_alone(tmp_data_dir):
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor({
        "id": "m1", "name": "x", "status": "stopped", "stats": {},
        "facilities": [{"id": "445859", "name": "P", "type": "Permit",
                        "division_ids": ["1"], "division_names": {"1": "Mine"}}],
    })
    with patch("app.monitor_manager.get_permit_divisions") as mock_res:
        assert await mm.backfill_division_names() == 0
    assert not mock_res.called
    assert mm.get_monitor("m1")["facilities"][0]["division_names"] == {"1": "Mine"}


@pytest.mark.asyncio
async def test_backfill_survives_an_api_failure(tmp_data_dir):
    """A dashboard showing ids is far better than a broken startup."""
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor({
        "id": "m1", "name": "x", "status": "stopped", "stats": {},
        "facilities": [{"id": "445859", "name": "P", "type": "Permit",
                        "division_ids": ["44585907"]}],
    })
    with patch("app.monitor_manager.get_permit_divisions",
               side_effect=RuntimeError("recreation.gov is down")):
        assert await mm.backfill_division_names() == 0
    assert "division_names" not in mm.get_monitor("m1")["facilities"][0]


@pytest.mark.asyncio
async def test_backfill_ignores_campground_monitors(tmp_data_dir, sample_monitor):
    mm = MonitorManager(tmp_data_dir)
    mm.add_monitor({**sample_monitor,
                    "facilities": [{"id": "232450", "name": "C", "type": "Campground"}]})
    with patch("app.monitor_manager.get_permit_divisions") as mock_res:
        assert await mm.backfill_division_names() == 0
    assert not mock_res.called
