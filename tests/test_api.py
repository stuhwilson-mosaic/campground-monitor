"""Validation tests for POST /api/monitors.

Before this, CreateMonitorRequest validated nothing beyond field types. The
wizard's JavaScript was the only guard, so any client that posted directly
could create a monitor the poll loop could never service.
"""
import importlib
import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

FUTURE = date.today() + timedelta(days=30)
CHECK_IN = FUTURE.isoformat()
CHECK_OUT = (FUTURE + timedelta(days=2)).isoformat()
ENTRY_DATE = FUTURE.isoformat()


@pytest.fixture
def client(tmp_path, sample_ridb_dir):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    os.environ["AUTH_USERNAME"] = "stu"
    os.environ["AUTH_PASSWORD"] = "pw"
    os.environ["DATA_DIR"] = data_dir
    os.environ["RIDB_DIR"] = sample_ridb_dir

    import app.config as cfg_mod
    importlib.reload(cfg_mod)
    import app.auth as auth_mod
    importlib.reload(auth_mod)
    from app.main import create_app

    application = create_app()
    c = TestClient(application, follow_redirects=False)
    assert c.post("/login", data={"username": "stu", "password": "pw"}).status_code == 303
    c.app_ref = application
    return c


def campground_payload(**overrides):
    payload = {
        "name": "Test campground monitor",
        "rec_area_name": "Yosemite National Park",
        "facility_ids": ["232450"],
        "facility_names": {"232450": "UPPER PINES"},
        "facility_types": {"232450": "Campground"},
        "selected_divisions": {},
        "check_in": CHECK_IN,
        "check_out": CHECK_OUT,
        "entry_date": "",
        "status": "stopped",
    }
    payload.update(overrides)
    return payload


def permit_payload(**overrides):
    payload = {
        "name": "Test permit monitor",
        "rec_area_name": "Yosemite National Park",
        "facility_ids": ["445859"],
        "facility_names": {"445859": "Yosemite Wilderness"},
        "facility_types": {"445859": "Permit"},
        "selected_divisions": {"445859": ["44585955"]},
        "check_in": "",
        "check_out": "",
        "entry_date": ENTRY_DATE,
        "party_size": 2,
        "status": "stopped",
    }
    payload.update(overrides)
    return payload


# ── Single facility type ──────────────────────────────────────────────────────

def test_mixed_facility_types_is_rejected(client):
    """A campground and a permit in one monitor cannot both be serviced.

    _run_check routes per facility, so the campground half would call
    check_campground(fid, "", "") and raise ValueError on every cycle forever.
    """
    resp = client.post("/api/monitors", json=campground_payload(
        facility_ids=["232450", "445859"],
        facility_names={"232450": "UPPER PINES", "445859": "Yosemite Wilderness"},
        facility_types={"232450": "Campground", "445859": "Permit"},
    ))
    assert resp.status_code == 422


def test_single_type_multi_facility_is_accepted(client):
    """Two campgrounds in one monitor stays legal."""
    resp = client.post("/api/monitors", json=campground_payload(
        facility_ids=["232450", "232447"],
        facility_names={"232450": "UPPER PINES", "232447": "LOWER PINES"},
        facility_types={"232450": "Campground", "232447": "Campground"},
    ))
    assert resp.status_code == 200


def test_empty_facility_list_is_rejected(client):
    resp = client.post("/api/monitors", json=campground_payload(
        facility_ids=[], facility_names={}, facility_types={},
    ))
    assert resp.status_code == 422


# ── Campground dates ──────────────────────────────────────────────────────────

def test_checkout_equal_to_checkin_is_rejected(client):
    """Zero-night stay makes dates_needed() empty, and all([]) is True.

    Every campsite would then report available and the monitor would fire a
    false alert naming the whole campground.
    """
    resp = client.post("/api/monitors", json=campground_payload(
        check_in=CHECK_IN, check_out=CHECK_IN,
    ))
    assert resp.status_code == 422


def test_checkout_before_checkin_is_rejected(client):
    resp = client.post("/api/monitors", json=campground_payload(
        check_in=CHECK_OUT, check_out=CHECK_IN,
    ))
    assert resp.status_code == 422


def test_campground_without_dates_is_rejected(client):
    resp = client.post("/api/monitors", json=campground_payload(
        check_in="", check_out="",
    ))
    assert resp.status_code == 422


# ── Permit dates and nights ───────────────────────────────────────────────────

def test_permit_without_entry_date_is_rejected(client):
    resp = client.post("/api/monitors", json=permit_payload(entry_date=""))
    assert resp.status_code == 422


def test_permit_nights_zero_is_rejected(client):
    resp = client.post("/api/monitors", json=permit_payload(nights=0))
    assert resp.status_code == 422


def test_permit_nights_is_optional(client):
    resp = client.post("/api/monitors", json=permit_payload())
    assert resp.status_code == 200


def test_permit_nights_round_trips(client):
    resp = client.post("/api/monitors", json=permit_payload(nights=3))
    assert resp.status_code == 200
    monitor_id = resp.json()["id"]

    stored = client.app_ref.state.manager.get_monitor(monitor_id)
    assert stored["nights"] == 3


def test_campground_monitor_stores_no_nights(client):
    """nights is permit-only; a campground monitor must not carry a value."""
    resp = client.post("/api/monitors", json=campground_payload())
    assert resp.status_code == 200

    stored = client.app_ref.state.manager.get_monitor(resp.json()["id"])
    assert stored.get("nights") is None


# ── Editing dates ─────────────────────────────────────────────────────────────

def _make_monitor(client, status="stopped", **over):
    mon = {
        "id": "edit-me", "owner": "stu", "name": "Editable", "status": status,
        "rec_area_name": "Yosemite",
        "facilities": [{"id": "232450", "name": "UPPER PINES", "type": "Campground"}],
        "check_in": CHECK_IN, "check_out": CHECK_OUT, "entry_date": "", "stats": {},
    }
    mon.update(over)
    client.app_ref.state.manager.add_monitor(mon)
    return mon["id"]


def test_edit_dates_on_a_stopped_monitor(client):
    mid = _make_monitor(client)
    new_out = (FUTURE + timedelta(days=5)).isoformat()
    resp = client.post(f"/api/monitors/{mid}/dates",
                       json={"check_in": CHECK_IN, "check_out": new_out})
    assert resp.status_code == 200
    assert client.app_ref.state.manager.get_monitor(mid)["check_out"] == new_out


def test_edit_dates_on_a_running_monitor_409s(client):
    mid = _make_monitor(client, status="running")
    resp = client.post(f"/api/monitors/{mid}/dates",
                       json={"check_in": CHECK_IN, "check_out": CHECK_OUT})
    assert resp.status_code == 409


def test_edit_dates_rejects_an_invalid_window(client):
    """Same validation as create, through the same shared function."""
    mid = _make_monitor(client)
    resp = client.post(f"/api/monitors/{mid}/dates",
                       json={"check_in": CHECK_IN, "check_out": CHECK_IN})
    assert resp.status_code == 422


def test_edit_dates_clears_the_dedup_ledger(client):
    """Stale entries would suppress the first alert under the new dates."""
    mid = _make_monitor(client)
    mgr = client.app_ref.state.manager
    mgr._notified[mid] = {("232450", "site-1"): 1.0}

    new_out = (FUTURE + timedelta(days=5)).isoformat()
    client.post(f"/api/monitors/{mid}/dates",
                json={"check_in": CHECK_IN, "check_out": new_out})

    assert not mgr._notified.get(mid), "dedup ledger survived a date change"


def test_edit_permit_dates_and_nights(client):
    mid = _make_monitor(
        client, id="edit-permit",
        facilities=[{"id": "445859", "name": "Wilderness", "type": "Permit"}],
        check_in="", check_out="", entry_date=ENTRY_DATE,
    )
    resp = client.post(f"/api/monitors/{mid}/dates",
                       json={"entry_date": ENTRY_DATE, "nights": 4})
    assert resp.status_code == 200
    stored = client.app_ref.state.manager.get_monitor(mid)
    assert stored["nights"] == 4


def test_edit_page_renders_for_a_stopped_monitor(client):
    mid = _make_monitor(client)
    resp = client.get(f"/monitors/{mid}/edit")
    assert resp.status_code == 200
    # Retitled from "Edit dates" once frequency became editable too.
    assert "Edit monitor" in resp.text


def test_deleting_a_monitor_drops_its_in_memory_state(client):
    """delete_monitor used to leak _notified and _check_logs until restart."""
    mid = _make_monitor(client)
    mgr = client.app_ref.state.manager
    mgr._notified[mid] = {("232450", "s"): 1.0}
    mgr._append_log(mid, {"status": "ok"})

    assert mgr.delete_monitor(mid) is True
    assert mid not in mgr._notified
    assert mid not in mgr._check_logs


# ── Check drill-down page ─────────────────────────────────────────────────────

def test_logs_page_renders_request_detail(client):
    mid = _make_monitor(client)
    client.app_ref.state.telemetry.record_check(
        monitor_id=mid, facility_id="232450", facility_name="UPPER PINES",
        kind="campground",
        url="https://www.recreation.gov/api/camps/availability/campground/232450/month",
        params={"start_date": "2026-08-01"}, http_status=200,
        duration_ms=417, response_bytes=508267, status="ok", sites_found=2,
    )
    resp = client.get(f"/logs/{mid}")
    assert resp.status_code == 200
    assert "UPPER PINES" in resp.text
    assert "HTTP 200" in resp.text
    assert "417ms" in resp.text
    assert "availability/campground/232450/month" in resp.text


def test_logs_page_shows_error_body_excerpt(client):
    mid = _make_monitor(client)
    client.app_ref.state.telemetry.record_check(
        monitor_id=mid, facility_id="232450", url="http://x", status="error",
        error="429 Too Many Requests", body_excerpt="rate limit exceeded",
    )
    resp = client.get(f"/logs/{mid}")
    assert "429 Too Many Requests" in resp.text
    assert "rate limit exceeded" in resp.text


def test_logs_errors_only_filter(client):
    mid = _make_monitor(client)
    tel = client.app_ref.state.telemetry
    tel.record_check(monitor_id=mid, facility_id="ok-one", url="u", status="ok")
    tel.record_check(monitor_id=mid, facility_id="bad-one", url="u",
                     status="error", error="boom")

    assert "ok-one" in client.get(f"/logs/{mid}").text
    assert "ok-one" not in client.get(f"/logs/{mid}?errors=1").text
    assert "bad-one" in client.get(f"/logs/{mid}?errors=1").text


def test_logs_page_empty_state(client):
    mid = _make_monitor(client)
    assert "No checks recorded yet" in client.get(f"/logs/{mid}").text


def test_logs_page_survives_a_bad_page_param(client):
    mid = _make_monitor(client)
    assert client.get(f"/logs/{mid}?page=abc").status_code == 200
    assert client.get(f"/logs/{mid}?page=99999").status_code == 200


# ── Audit log ─────────────────────────────────────────────────────────────────

def _events(client, **kw):
    return client.app_ref.state.telemetry.get_events(**kw)


def test_login_is_audited(client):
    assert any(e["event"] == "login" and e["username"] == "stu"
               for e in _events(client))


def test_logout_is_audited(client):
    client.get("/logout")
    assert any(e["event"] == "logout" for e in _events(client))


def test_monitor_lifecycle_is_audited(client):
    mid = _make_monitor(client)
    client.post(f"/api/monitors/{mid}/start")
    client.post(f"/api/monitors/{mid}/stop")

    kinds = [e["event"] for e in _events(client)]
    assert "monitor.start" in kinds
    assert "monitor.stop" in kinds
    started = next(e for e in _events(client) if e["event"] == "monitor.start")
    assert started["username"] == "stu"
    assert started["target_id"] == mid


def test_monitor_create_and_delete_are_audited(client):
    mid = client.post("/api/monitors", json=campground_payload()).json()["id"]
    client.delete(f"/api/monitors/{mid}")
    kinds = [e["event"] for e in _events(client)]
    assert "monitor.create" in kinds
    assert "monitor.delete" in kinds


def test_deleted_monitor_keeps_its_name_in_the_audit_row(client):
    """Recorded before the row is removed, or the name would be lost."""
    mid = _make_monitor(client)
    client.delete(f"/api/monitors/{mid}")
    row = next(e for e in _events(client) if e["event"] == "monitor.delete")
    assert row["target_name"] == "Editable"


def test_edit_dates_is_audited(client):
    mid = _make_monitor(client)
    new_out = (FUTURE + timedelta(days=5)).isoformat()
    client.post(f"/api/monitors/{mid}/dates",
                json={"check_in": CHECK_IN, "check_out": new_out})
    assert any(e["event"] == "monitor.edit" for e in _events(client))


def test_activity_page_renders_for_admin(client):
    mid = _make_monitor(client)
    client.post(f"/api/monitors/{mid}/start")
    resp = client.get("/admin/activity")
    assert resp.status_code == 200
    assert "monitor.start" in resp.text


def test_activity_page_filters(client):
    """Filtering to logins must drop the monitor rows.

    Asserts on the monitor NAME rather than the event string: every event type
    also appears as an <option> in the filter dropdown, so searching for
    "monitor.start" would match the form regardless of the filter.
    """
    mid = _make_monitor(client)
    client.post(f"/api/monitors/{mid}/start")

    assert "Editable" in client.get("/admin/activity").text
    assert "Editable" not in client.get("/admin/activity?event=login").text


def test_audit_failure_does_not_break_the_action(client, monkeypatch):
    """Telemetry must never be able to stop a monitor action from working."""
    mid = _make_monitor(client)

    def boom(*a, **kw):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(client.app_ref.state.telemetry, "record_event", boom)
    resp = client.post(f"/api/monitors/{mid}/start")
    assert resp.status_code == 200


# ── Edit: frequency + reference block ─────────────────────────────────────────

def test_edit_changes_the_poll_interval(client):
    mid = _make_monitor(client)
    resp = client.post(f"/api/monitors/{mid}/dates", json={
        "check_in": CHECK_IN, "check_out": CHECK_OUT, "poll_interval_seconds": 900,
    })
    assert resp.status_code == 200
    assert client.app_ref.state.manager.get_monitor(mid)["poll_interval_seconds"] == 900


def test_edit_rejects_an_interval_outside_the_allowed_set(client):
    """A 1-second interval would hammer recreation.gov; 429s already happen at 15s."""
    mid = _make_monitor(client)
    resp = client.post(f"/api/monitors/{mid}/dates", json={
        "check_in": CHECK_IN, "check_out": CHECK_OUT, "poll_interval_seconds": 1,
    })
    assert resp.status_code == 422


def test_edit_leaves_the_interval_alone_when_omitted(client):
    mid = _make_monitor(client, poll_interval_seconds=600)
    client.post(f"/api/monitors/{mid}/dates",
                json={"check_in": CHECK_IN, "check_out": CHECK_OUT})
    assert client.app_ref.state.manager.get_monitor(mid)["poll_interval_seconds"] == 600


def test_edit_page_shows_read_only_reference(client):
    mid = _make_monitor(client, enable_ntfy=True, ntfy_topic="my-topic")
    body = client.get(f"/monitors/{mid}/edit").text
    assert "not editable here" in body
    assert "UPPER PINES" in body       # the facility
    assert "Yosemite" in body          # the park
    assert "my-topic" in body          # notification target


def test_edit_page_reference_names_trailheads_for_permits(client):
    mid = _make_monitor(
        client, id="permit-edit",
        facilities=[{"id": "445859", "name": "Yosemite Wilderness Permits",
                     "type": "Permit", "division_ids": ["44585955"],
                     "division_names": {"44585955": "Young Lakes via Dog Lake"}}],
        check_in="", check_out="", entry_date=ENTRY_DATE,
    )
    body = client.get(f"/monitors/{mid}/edit").text
    assert "Young Lakes via Dog Lake" in body


def test_edit_page_offers_the_frequency_selector(client):
    mid = _make_monitor(client, poll_interval_seconds=900)
    body = client.get(f"/monitors/{mid}/edit").text
    assert 'id="poll-interval"' in body
    assert '<option value="900" selected>' in body
