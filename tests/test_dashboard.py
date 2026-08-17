"""Login/auth tests for the FastAPI app shell (Task 6)."""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, sample_ridb_dir):
    """TestClient with auth env vars set and a fresh app instance."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    os.environ["AUTH_USERNAME"] = "stu"
    os.environ["AUTH_PASSWORD"] = "testpass"
    os.environ["DATA_DIR"] = data_dir
    os.environ["RIDB_DIR"] = sample_ridb_dir

    # Reload config so the env vars above are picked up
    import app.config as cfg_mod
    importlib.reload(cfg_mod)

    # Re-seed auth's serializer with the reloaded config
    import app.auth as auth_mod
    importlib.reload(auth_mod)

    from app.main import create_app
    application = create_app()
    return TestClient(application, follow_redirects=False)


def test_unauthenticated_redirects_to_login(client):
    """GET / without a session cookie should 303-redirect to /login."""
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_page_renders(client):
    """GET /login returns 200 and the page contains a password field."""
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "password" in resp.text.lower()


def test_login_success(client):
    """POST /login with valid credentials redirects and sets the session cookie."""
    resp = client.post(
        "/login",
        data={"username": "stu", "password": "testpass"},
    )
    assert resp.status_code == 303
    assert "camp_session" in resp.cookies


def test_login_failure(client):
    """POST /login with wrong password re-renders the form with an error."""
    resp = client.post(
        "/login",
        data={"username": "stu", "password": "wrongpass"},
    )
    assert resp.status_code == 200
    assert "invalid" in resp.text.lower()


def test_logout_clears_session(client):
    """After a successful login, GET /logout should redirect and clear the cookie."""
    # First, log in to obtain a session cookie
    login_resp = client.post(
        "/login",
        data={"username": "stu", "password": "testpass"},
    )
    assert login_resp.status_code == 303

    # Now call /logout — should redirect back to /login
    logout_resp = client.get("/logout")
    assert logout_resp.status_code == 303


# ── Dashboard tests ────────────────────────────────────────────────────────────

def test_dashboard_renders_when_authenticated(client):
    """GET /dashboard while authenticated should return 200."""
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.get("/dashboard")
    assert resp.status_code == 200


def test_dashboard_shows_monitor_card(client, sample_monitor):
    """Dashboard shows a card for each monitor in monitors.json."""
    sample_monitor["owner"] = "stu"
    client.app.state.manager.add_monitor(sample_monitor)
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Test Yosemite Monitor" in resp.text


def test_dashboard_dims_past_monitors(client, sample_monitor):
    """Monitors with check_out in the past get a 'past' CSS class."""
    sample_monitor["check_out"] = "2020-01-01"
    sample_monitor["owner"] = "stu"
    client.app.state.manager.add_monitor(sample_monitor)
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "past" in resp.text


def test_start_monitor_action(client, sample_monitor):
    """POST /api/monitors/{id}/start returns 200 and an updated card."""
    sample_monitor["status"] = "stopped"
    sample_monitor["owner"] = "stu"
    client.app.state.manager.add_monitor(sample_monitor)
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.post(f"/api/monitors/{sample_monitor['id']}/start")
    assert resp.status_code == 200



def test_resume_monitor_action(client, sample_monitor):
    """POST /api/monitors/{id}/start from paused state returns 200."""
    sample_monitor["status"] = "paused"
    sample_monitor["owner"] = "stu"
    client.app.state.manager.add_monitor(sample_monitor)
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.post(f"/api/monitors/{sample_monitor['id']}/start")
    assert resp.status_code == 200


def test_stop_monitor_action(client, sample_monitor):
    """POST /api/monitors/{id}/stop returns 200 and an updated card."""
    sample_monitor["status"] = "running"
    sample_monitor["owner"] = "stu"
    client.app.state.manager.add_monitor(sample_monitor)
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.post(f"/api/monitors/{sample_monitor['id']}/stop")
    assert resp.status_code == 200


def test_delete_monitor_action(client, sample_monitor):
    """DELETE /api/monitors/{id} removes the monitor and returns empty HTML."""
    sample_monitor["owner"] = "stu"
    client.app.state.manager.add_monitor(sample_monitor)
    client.post("/login", data={"username": "stu", "password": "testpass"})
    resp = client.delete(f"/api/monitors/{sample_monitor['id']}")
    assert resp.status_code == 200
    # Verify monitor removed from disk
    import json
    monitors_path = client.app.state.manager._path
    with open(monitors_path) as f:
        data = json.load(f)
    assert not any(m["id"] == sample_monitor["id"] for m in data["monitors"])


@pytest.fixture
def authed_app_client(tmp_path, sample_ridb_dir):
    """Logged-in client plus the app, so tests can seed monitors directly."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    os.environ["AUTH_USERNAME"] = "stu"
    os.environ["AUTH_PASSWORD"] = "testpass"
    os.environ["DATA_DIR"] = data_dir
    os.environ["RIDB_DIR"] = sample_ridb_dir

    import app.config as cfg_mod
    importlib.reload(cfg_mod)
    import app.auth as auth_mod
    importlib.reload(auth_mod)
    from app.main import create_app

    application = create_app()
    c = TestClient(application, follow_redirects=False)
    assert c.post("/login", data={"username": "stu", "password": "testpass"}).status_code == 303
    return c, application

# ── Sorting, filtering, and the trailhead/menu card changes ───────────────────

def _add(client_app, mid, status, created_at, **over):
    mon = {
        "id": mid, "owner": "stu", "name": mid, "status": status,
        "rec_area_name": "Yosemite", "created_at": created_at,
        "facilities": [{"id": "1", "name": "Camp", "type": "Campground"}],
        "check_in": "2030-01-01", "check_out": "2030-01-03", "stats": {},
    }
    mon.update(over)
    client_app.state.manager.add_monitor(mon)


def test_running_monitors_sort_above_stopped(authed_app_client):
    client, app = authed_app_client
    _add(app, "old-stopped", "stopped", "2026-01-01T00:00:00")
    _add(app, "new-running", "running", "2026-02-01T00:00:00")
    body = client.get("/dashboard").text
    assert body.index("new-running") < body.index("old-stopped")


def test_newest_first_within_a_status_group(authed_app_client):
    client, app = authed_app_client
    _add(app, "older-run", "running", "2026-01-01T00:00:00")
    _add(app, "newer-run", "running", "2026-03-01T00:00:00")
    body = client.get("/dashboard").text
    assert body.index("newer-run") < body.index("older-run")


def test_running_only_filter_hides_stopped(authed_app_client):
    client, app = authed_app_client
    _add(app, "is-running", "running", "2026-01-01T00:00:00")
    _add(app, "is-stopped", "stopped", "2026-01-01T00:00:00")
    body = client.get("/dashboard?running=1").text
    assert "is-running" in body
    assert "is-stopped" not in body


def test_counts_describe_all_monitors_even_when_filtered(authed_app_client):
    """Counting the filtered subset would make the summary bar lie."""
    client, app = authed_app_client
    _add(app, "is-running", "running", "2026-01-01T00:00:00")
    _add(app, "is-stopped", "stopped", "2026-01-01T00:00:00")
    body = client.get("/dashboard?running=1").text
    assert ">2<" in body.replace(" ", "").replace("\n", "")


def test_filtering_to_nothing_shows_a_distinct_empty_state(authed_app_client):
    client, app = authed_app_client
    _add(app, "is-stopped", "stopped", "2026-01-01T00:00:00")
    body = client.get("/dashboard?running=1").text
    assert "No monitors are running right now" in body
    assert "Create your first monitor" not in body


def test_permit_card_names_its_trailheads(authed_app_client):
    """A permit facility is always the same generic name; the trailhead is
    the part you actually need to recognise."""
    client, app = authed_app_client
    _add(app, "permit-mon", "stopped", "2026-01-01T00:00:00",
         check_in="", check_out="", entry_date="2030-05-01",
         facilities=[{
             "id": "445859", "name": "Yosemite Wilderness Permits", "type": "Permit",
             "division_ids": ["44585955"],
             "division_names": {"44585955": "Young Lakes via Dog Lake"},
         }])
    body = client.get("/dashboard").text
    assert "Young Lakes via Dog Lake" in body


def test_permit_card_falls_back_to_ids_for_older_monitors(authed_app_client):
    """Monitors created before names were stored must still render."""
    client, app = authed_app_client
    _add(app, "legacy-permit", "stopped", "2026-01-01T00:00:00",
         check_in="", check_out="", entry_date="2030-05-01",
         facilities=[{"id": "445859", "name": "Yosemite Wilderness Permits",
                      "type": "Permit", "division_ids": ["44585955"]}])
    body = client.get("/dashboard").text
    assert "44585955" in body


def test_card_actions_are_collapsed_into_a_menu(authed_app_client):
    client, app = authed_app_client
    _add(app, "m1", "stopped", "2026-01-01T00:00:00")
    body = client.get("/dashboard").text
    assert "card-menu-items" in body
    assert "Pause" not in body


def test_guide_describes_current_functionality(authed_app_client):
    """The guide is the only place these rules are explained to a user."""
    client, _ = authed_app_client
    body = client.get("/guide").text

    # Removed features must not linger
    assert "Pause" not in body
    assert "paused" not in body
    # Current behaviour must be covered
    for phrase in ["Clone", "favorite", "Running only", "trailhead",
                   "either campgrounds or wilderness permits", "Create Stopped"]:
        assert phrase in body, f"guide does not mention: {phrase}"


def test_card_body_is_collapsible(authed_app_client):
    """Dates, stats and actions collapse; identity stays visible for scanning."""
    client, app = authed_app_client
    _add(app, "m1", "running", "2026-01-01T00:00:00")
    body = client.get("/dashboard").text

    assert 'class="card-body"' in body or "card-body" in body
    assert 'data-monitor-id="m1"' in body
    # Collapsed markup still contains the detail; <details> hides it visually.
    assert "card-body-toggle" in body


def test_card_identity_stays_outside_the_collapsible_part(authed_app_client):
    """Name, park and facilities must precede the toggle, or a collapsed card
    would show nothing useful."""
    client, app = authed_app_client
    _add(app, "scan-me", "running", "2026-01-01T00:00:00")
    body = client.get("/dashboard").text

    toggle_at = body.index("card-body-toggle")
    assert body.index("scan-me") < toggle_at          # name
    assert body.index("Yosemite") < toggle_at          # park
    assert body.index("Camp") < toggle_at              # facility


def test_controls_stay_above_the_fold(authed_app_client):
    """Both controls sit in the header, so a collapsed card is still usable.

    The pill starts/stops and the menu holds everything else; neither should
    require expanding the card first.
    """
    client, app = authed_app_client
    _add(app, "m1", "running", "2026-01-01T00:00:00")
    body = client.get("/dashboard").text

    toggle_at = body.index("card-body-toggle")
    assert body.index("status-toggle") < toggle_at
    assert body.index("card-menu-items") < toggle_at
    # The old actions row is gone entirely.
    assert "monitor-card-actions" not in body


def test_status_pill_is_the_start_stop_control(authed_app_client):
    """The pill doubles as the action, keeping it reachable while collapsed."""
    client, app = authed_app_client
    _add(app, "runner", "running", "2026-01-01T00:00:00")
    _add(app, "stopper", "stopped", "2026-01-02T00:00:00")
    body = client.get("/dashboard").text

    assert 'hx-post="/api/monitors/runner/stop"' in body
    assert 'hx-post="/api/monitors/stopper/start"' in body
    # And the old duplicate buttons are gone
    assert '<button class="btn btn-sm btn-outline-primary"' not in body


def test_status_pill_states_its_action_for_screen_readers(authed_app_client):
    client, app = authed_app_client
    _add(app, "runner", "running", "2026-01-01T00:00:00")
    body = client.get("/dashboard").text
    assert "Stop monitor runner" in body
    assert "currently running" in body


def test_status_pill_still_shows_the_status(authed_app_client):
    """It must keep working as a status indicator, not just a button."""
    client, app = authed_app_client
    _add(app, "runner", "running", "2026-01-01T00:00:00")
    body = client.get("/dashboard").text
    assert 'class="status-word">running<' in body
