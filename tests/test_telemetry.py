"""Tests for the SQLite telemetry store.

Storing raw response bodies is not an option: measured against the live API,
a campground month is 496 KB and a permit month is 145 KB, which at the current
polling rate would be ~6.4 GB/day. Only errors keep an excerpt.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.telemetry import BODY_EXCERPT_LIMIT, TelemetryStore, truncate_body


@pytest.fixture
def store(tmp_path):
    s = TelemetryStore(str(tmp_path))
    yield s
    s.close()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ── Schema ────────────────────────────────────────────────────────────────────

def test_schema_is_created_on_a_fresh_file(store):
    assert store.get_checks("anything") == []
    assert store.get_events() == []


def test_reopening_an_existing_db_is_idempotent(tmp_path):
    a = TelemetryStore(str(tmp_path))
    a.record_check(monitor_id="m", facility_id="f", url="http://x")
    a.close()
    b = TelemetryStore(str(tmp_path))   # must not fail or wipe
    try:
        assert len(b.get_checks("m")) == 1
    finally:
        b.close()


# ── Check rows ────────────────────────────────────────────────────────────────

def test_record_and_read_back_a_check(store):
    store.record_check(
        monitor_id="m1", facility_id="232450", facility_name="UPPER PINES",
        kind="campground", url="http://x", params={"start_date": "2026-08-01"},
        http_status=200, duration_ms=417, response_bytes=508267,
        status="ok", sites_found=3, new_alerts=1, alerted=True,
        result=[{"site": "A1"}],
    )
    row = store.get_checks("m1")[0]
    assert row["facility_name"] == "UPPER PINES"
    assert row["http_status"] == 200
    assert row["response_bytes"] == 508267
    assert row["alerted"] == 1


def test_one_row_per_http_request(store):
    """A cross-month stay makes two calls and must produce two rows."""
    for month in ("2026-03-01", "2026-04-01"):
        store.record_check(monitor_id="m1", facility_id="f", url="http://x",
                           params={"start_date": month})
    assert len(store.get_checks("m1")) == 2


def test_successful_responses_store_no_body(store):
    """The entire point of the design: bodies are 496 KB each."""
    store.record_check(monitor_id="m1", facility_id="f", url="http://x",
                       status="ok", body_excerpt="a" * 5000)
    assert store.get_checks("m1")[0]["body_excerpt"] is None


def test_error_rows_keep_a_body_excerpt(store):
    store.record_check(monitor_id="m1", facility_id="f", url="http://x",
                       status="error", error="429 Too Many Requests",
                       body_excerpt="<html>rate limited</html>")
    row = store.get_checks("m1")[0]
    assert "rate limited" in row["body_excerpt"]
    assert row["error"].startswith("429")


def test_body_excerpt_is_truncated(store):
    store.record_check(monitor_id="m1", facility_id="f", url="http://x",
                       status="error", body_excerpt="x" * (BODY_EXCERPT_LIMIT * 3))
    assert len(store.get_checks("m1")[0]["body_excerpt"]) <= BODY_EXCERPT_LIMIT


def test_truncate_body_handles_bytes_and_bad_encoding():
    assert truncate_body(None) is None
    assert truncate_body(b"\xff\xfe not utf8") is not None
    assert len(truncate_body(b"y" * 99999)) <= BODY_EXCERPT_LIMIT


def test_checks_come_back_newest_first(store):
    store.record_check(monitor_id="m1", facility_id="old", url="u", ts=_iso(2))
    store.record_check(monitor_id="m1", facility_id="new", url="u", ts=_iso(0))
    assert [r["facility_id"] for r in store.get_checks("m1")] == ["new", "old"]


def test_checks_are_scoped_to_their_monitor(store):
    store.record_check(monitor_id="m1", facility_id="a", url="u")
    store.record_check(monitor_id="m2", facility_id="b", url="u")
    assert len(store.get_checks("m1")) == 1


def test_pagination(store):
    for i in range(5):
        store.record_check(monitor_id="m1", facility_id=str(i), url="u", ts=_iso(5 - i))
    assert len(store.get_checks("m1", limit=2)) == 2
    assert len(store.get_checks("m1", limit=2, offset=4)) == 1
    assert store.count_checks("m1") == 5


def test_errors_only_filter(store):
    store.record_check(monitor_id="m1", facility_id="a", url="u", status="ok")
    store.record_check(monitor_id="m1", facility_id="b", url="u", status="error",
                       error="boom")
    assert len(store.get_checks("m1", errors_only=True)) == 1
    assert store.count_checks("m1", errors_only=True) == 1


# ── Retention ─────────────────────────────────────────────────────────────────

def test_prune_removes_only_rows_past_the_cutoff(store):
    store.record_check(monitor_id="m1", facility_id="old", url="u", ts=_iso(40))
    store.record_check(monitor_id="m1", facility_id="fresh", url="u", ts=_iso(1))

    removed = store.prune_checks(older_than_days=30)

    assert removed == 1
    assert [r["facility_id"] for r in store.get_checks("m1")] == ["fresh"]


def test_prune_leaves_the_audit_log_alone(store):
    """Audit history is small and is the thing you would most regret losing."""
    store.record_event(event="login", username="stu", ts=_iso(400))
    store.prune_checks(older_than_days=1)
    assert len(store.get_events()) == 1


# ── Audit log ─────────────────────────────────────────────────────────────────

def test_record_and_filter_events(store):
    store.record_event(event="login", username="stu", ip="1.2.3.4")
    store.record_event(event="monitor.stop", username="bob", target_id="m1")

    assert len(store.get_events()) == 2
    assert len(store.get_events(username="stu")) == 1
    assert len(store.get_events(event="monitor.stop")) == 1
    assert store.count_events(username="bob") == 1


def test_system_events_have_no_username(store):
    """Distinguishes the auto-stop rule from a person clicking Stop."""
    store.record_event(event="monitor.stop", username=None, target_id="m1",
                       detail={"cause": "trip window expired"})
    row = store.get_events()[0]
    assert row["username"] is None
    assert "trip window" in row["detail"]
