"""Authentication — credential check, signed session cookies, FastAPI deps."""
from __future__ import annotations

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException

from app import config
from app.user_store import User, UserStore

COOKIE_NAME = "camp_session"
_serializer = URLSafeTimedSerializer(config.SESSION_SECRET)


def check_credentials(store: UserStore, username: str, password: str) -> bool:
    return store.verify(username, password) is not None


def create_session_cookie(username: str) -> str:
    return _serializer.dumps({"user": username})


def verify_session_cookie(cookie: str | None, max_age: int | None = None) -> str | None:
    if not cookie:
        return None
    effective_max_age = max_age if max_age is not None else config.SESSION_MAX_AGE
    if effective_max_age <= 0:
        effective_max_age = -1
    try:
        data = _serializer.loads(cookie, max_age=effective_max_age)
        return data.get("user")
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request) -> str | None:
    cookie = request.cookies.get(COOKIE_NAME)
    return verify_session_cookie(cookie)


def _user_store(request: Request) -> UserStore:
    return request.app.state.user_store


def get_current_user_record(request: Request) -> User | None:
    username = get_current_user(request)
    if not username:
        return None
    return _user_store(request).get(username)


def require_auth(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(request: Request) -> User:
    rec = get_current_user_record(request)
    if rec is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if rec.role != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    return rec


def require_monitor_access(request: Request, monitor_id: str) -> tuple[User, dict]:
    """Look up the monitor and check the caller can access it.

    Returns (user, monitor). Raises:
      303 -> /login if not authenticated
      404 if monitor missing
      403 if caller is not admin and not the owner
    """
    rec = get_current_user_record(request)
    if rec is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    monitor = request.app.state.manager.get_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    if rec.role != "admin" and monitor.get("owner") != rec.username:
        raise HTTPException(status_code=403, detail="not your monitor")
    return rec, monitor
