"""Tests for app.auth — credential check via UserStore, cookie signing, FastAPI deps."""
import importlib
import os
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth
from app.user_store import UserStore


@pytest.fixture
def reset_auth_module():
    """Force `app.auth` to re-import so the SESSION_SECRET picks up env changes."""
    yield
    sys.modules.pop("app.auth", None)


def _make_request(cookies: dict[str, str] | None = None, app_state=None) -> Request:
    """Build a minimal Starlette Request with the given cookies + app.state."""
    headers = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "app": _StubApp(app_state),
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


class _StubApp:
    def __init__(self, state):
        self.state = state or _StubState()


class _StubState:
    pass


# ── Credential checks ─────────────────────────────────────────────────────────

def test_check_credentials_valid(tmp_data_dir):
    store = UserStore(tmp_data_dir, bootstrap_username="alice", bootstrap_password="secret")
    assert auth.check_credentials(store, "alice", "secret") is True


def test_check_credentials_invalid(tmp_data_dir):
    store = UserStore(tmp_data_dir, bootstrap_username="alice", bootstrap_password="secret")
    assert auth.check_credentials(store, "alice", "wrong") is False
    assert auth.check_credentials(store, "bob", "secret") is False


# ── Session cookie round-trip ─────────────────────────────────────────────────

def test_session_cookie_roundtrip():
    cookie = auth.create_session_cookie("stu")
    assert auth.verify_session_cookie(cookie) == "stu"


def test_session_cookie_invalid():
    assert auth.verify_session_cookie("not.a.valid.cookie") is None
    assert auth.verify_session_cookie("") is None
    assert auth.verify_session_cookie(None) is None


def test_session_cookie_expired():
    cookie = auth.create_session_cookie("stu")
    assert auth.verify_session_cookie(cookie, max_age=0) is None


# ── get_current_user_record + require_admin + require_monitor_access ──────────

def test_get_current_user_record_returns_user(tmp_data_dir):
    store = UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    state = _StubState()
    state.user_store = store
    cookie = auth.create_session_cookie("stu")
    req = _make_request(cookies={auth.COOKIE_NAME: cookie}, app_state=state)
    rec = auth.get_current_user_record(req)
    assert rec is not None and rec.username == "stu" and rec.role == "admin"


def test_get_current_user_record_no_cookie(tmp_data_dir):
    state = _StubState()
    state.user_store = UserStore(tmp_data_dir)
    req = _make_request(app_state=state)
    assert auth.get_current_user_record(req) is None


def test_require_admin_blocks_non_admin(tmp_data_dir):
    store = UserStore(tmp_data_dir)
    store.add(username="bob", password="pw", role="user")
    state = _StubState()
    state.user_store = store
    cookie = auth.create_session_cookie("bob")
    req = _make_request(cookies={auth.COOKIE_NAME: cookie}, app_state=state)
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(req)
    assert exc.value.status_code == 403


def test_require_admin_allows_admin(tmp_data_dir):
    store = UserStore(tmp_data_dir, bootstrap_username="stu", bootstrap_password="pw")
    state = _StubState()
    state.user_store = store
    cookie = auth.create_session_cookie("stu")
    req = _make_request(cookies={auth.COOKIE_NAME: cookie}, app_state=state)
    user = auth.require_admin(req)
    assert user.username == "stu"
