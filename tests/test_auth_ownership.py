"""Tests for owner-or-admin gating on monitor routes."""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_users(tmp_path, sample_ridb_dir):
    """Build the app with stu (admin) + bob (user), each owning one monitor."""
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

    # Seed: stu (admin) already created via bootstrap. Add bob.
    application.state.user_store.add(username="bob", password="bobpw", role="user")
    # Two monitors, one per owner.
    application.state.manager.add_monitor({
        "id": "stu-mon", "name": "stu's monitor", "owner": "stu", "status": "stopped",
        "facilities": [], "stats": {},
    })
    application.state.manager.add_monitor({
        "id": "bob-mon", "name": "bob's monitor", "owner": "bob", "status": "stopped",
        "facilities": [], "stats": {},
    })
    return application


def _login(client, username, password):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 303
    return resp.cookies


def test_regular_user_403s_on_others_monitor_start(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.post("/api/monitors/stu-mon/start")
    assert resp.status_code == 403


def test_regular_user_can_start_own_monitor(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.post("/api/monitors/bob-mon/start")
    assert resp.status_code == 200


def test_admin_can_start_any_monitor(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/api/monitors/bob-mon/start")
    assert resp.status_code == 200


def test_regular_user_403s_on_others_monitor_delete(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.delete("/api/monitors/stu-mon")
    assert resp.status_code == 403


def test_regular_user_403s_on_others_monitor_logs(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.get("/logs/stu-mon")
    assert resp.status_code == 403


def test_unknown_monitor_returns_404(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/api/monitors/does-not-exist/start")
    assert resp.status_code == 404


def test_dashboard_filters_for_regular_user(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    # Names may be HTML-escaped (apostrophe → &#39;) in Jinja2 autoescape context.
    assert "bob" in resp.text and "monitor" in resp.text
    assert "bob-mon" in resp.text
    assert "stu-mon" not in resp.text


def test_dashboard_shows_all_for_admin_with_chips(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "bob-mon" in resp.text
    assert "stu-mon" in resp.text
    # Owner chip rendered for admin view.
    assert "owner-chip" in resp.text


def test_admin_link_visible_only_to_admin(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    bob_dash = client.get("/dashboard").text
    assert 'href="/admin"' not in bob_dash

    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "stu", "pw")
    stu_dash = client.get("/dashboard").text
    assert 'href="/admin"' in stu_dash


def test_cloning_another_users_monitor_403s(app_with_users):
    """?from= must not become a way to read someone else's facility selection."""
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.get("/monitors/new?from=stu-mon")
    assert resp.status_code == 403


def test_cloning_own_monitor_is_allowed(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.get("/monitors/new?from=bob-mon")
    assert resp.status_code == 200


def test_admin_can_clone_any_monitor(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.get("/monitors/new?from=bob-mon")
    assert resp.status_code == 200


def test_editing_another_users_monitor_dates_403s(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.post("/api/monitors/stu-mon/dates",
                       json={"check_in": "2030-01-01", "check_out": "2030-01-03"})
    assert resp.status_code == 403


def test_edit_page_for_another_users_monitor_403s(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    assert client.get("/monitors/stu-mon/edit").status_code == 403


def test_activity_page_is_admin_only(app_with_users):
    client = TestClient(app_with_users, follow_redirects=False)
    _login(client, "bob", "bobpw")
    assert client.get("/admin/activity").status_code == 403
