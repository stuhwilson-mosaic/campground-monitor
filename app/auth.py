"""Authentication helpers — credential check, signed session cookies, FastAPI dep."""
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException

from app import config

COOKIE_NAME = "camp_session"
_serializer = URLSafeTimedSerializer(config.SESSION_SECRET)


def check_credentials(username: str, password: str) -> bool:
    return username == config.AUTH_USERNAME and password == config.AUTH_PASSWORD


def create_session_cookie(username: str) -> str:
    return _serializer.dumps({"user": username})


def verify_session_cookie(cookie: str | None, max_age: int | None = None) -> str | None:
    if not cookie:
        return None
    # max_age=0 means "expire immediately"; itsdangerous treats 0 as no limit,
    # so we normalise non-positive values to -1 which always raises SignatureExpired.
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


def require_auth(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user
