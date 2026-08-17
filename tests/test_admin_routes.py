"""Tests for /admin user-management routes."""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_app(tmp_path, sample_ridb_dir):
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
    return create_app()


def _login(client, u, p):
    resp = client.post("/login", data={"username": u, "password": p})
    assert resp.status_code == 303


def test_admin_index_blocks_non_admin(admin_app):
    admin_app.state.user_store.add(username="bob", password="bobpw", role="user")
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "bob", "bobpw")
    resp = client.get("/admin")
    assert resp.status_code == 403


def test_admin_index_renders_for_admin(admin_app):
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "stu" in resp.text


def test_admin_can_create_user(admin_app):
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post(
        "/admin/users",
        data={"username": "alice", "password": "alicepw", "role": "user"},
    )
    assert resp.status_code in (200, 303)
    assert admin_app.state.user_store.get("alice") is not None


def test_admin_create_rejects_duplicate(admin_app):
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    client.post("/admin/users", data={"username": "alice", "password": "p", "role": "user"})
    resp = client.post("/admin/users", data={"username": "alice", "password": "p", "role": "user"})
    assert resp.status_code == 409


def test_admin_create_rejects_bad_username(admin_app):
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post(
        "/admin/users",
        data={"username": "has spaces", "password": "p", "role": "user"},
    )
    assert resp.status_code == 400


def test_admin_can_reset_password(admin_app):
    admin_app.state.user_store.add(username="bob", password="oldpw", role="user")
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/bob/password", data={"new_password": "newpw"})
    assert resp.status_code == 303
    assert admin_app.state.user_store.verify("bob", "newpw") is not None


def test_admin_can_change_role(admin_app):
    admin_app.state.user_store.add(username="bob", password="p", role="user")
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/bob/role", data={"role": "admin"})
    assert resp.status_code == 303
    assert admin_app.state.user_store.get("bob").role == "admin"


def test_admin_cannot_demote_last_admin(admin_app):
    """stu is the only admin; demoting must 409."""
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/stu/role", data={"role": "user"})
    assert resp.status_code == 409


def test_admin_can_delete_user_reassign_monitors(admin_app):
    admin_app.state.user_store.add(username="bob", password="p", role="user")
    admin_app.state.manager.add_monitor(
        {"id": "bob-mon", "owner": "bob", "status": "stopped", "facilities": [], "stats": {}}
    )
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/bob/delete", data={"monitors": "reassign"})
    assert resp.status_code == 303
    assert admin_app.state.user_store.get("bob") is None
    assert admin_app.state.manager.get_monitor("bob-mon")["owner"] == "stu"


def test_admin_can_delete_user_and_monitors(admin_app):
    admin_app.state.user_store.add(username="bob", password="p", role="user")
    admin_app.state.manager.add_monitor(
        {"id": "bob-mon", "owner": "bob", "status": "stopped", "facilities": [], "stats": {}}
    )
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/bob/delete", data={"monitors": "delete"})
    assert resp.status_code == 303
    assert admin_app.state.user_store.get("bob") is None
    assert admin_app.state.manager.get_monitor("bob-mon") is None


def test_admin_cannot_delete_self(admin_app):
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/stu/delete", data={"monitors": "reassign"})
    assert resp.status_code == 409


def test_admin_cannot_delete_last_admin(admin_app):
    """Even if we added a second user — stu is still the only admin, so delete must 409."""
    admin_app.state.user_store.add(username="bob", password="p", role="user")
    client = TestClient(admin_app, follow_redirects=False)
    _login(client, "stu", "pw")
    resp = client.post("/admin/users/stu/delete", data={"monitors": "delete"})
    assert resp.status_code == 409
