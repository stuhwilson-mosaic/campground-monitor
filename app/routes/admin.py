"""Admin routes — user management. Admin-only."""
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_admin


router = APIRouter(prefix="/admin")
_USERNAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _validate_username(username: str) -> str:
    norm = (username or "").strip().lower()
    if not _USERNAME_RE.match(norm):
        raise HTTPException(status_code=400, detail="invalid username (a-z, 0-9, _, -, 1-32 chars)")
    return norm


def _counts_by_owner(manager) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in manager.list_monitors():
        owner = m.get("owner", "")
        counts[owner] = counts.get(owner, 0) + 1
    return counts


@router.get("", response_class=HTMLResponse)
async def admin_index(request: Request):
    admin = require_admin(request)
    user_store = request.app.state.user_store
    manager = request.app.state.manager
    templates = request.app.state.templates
    counts = _counts_by_owner(manager)
    users = [
        {
            "username": u.username,
            "role": u.role,
            "created_at": u.created_at,
            "monitor_count": counts.get(u.username, 0),
        }
        for u in user_store.list_users()
    ]
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"user": admin.username, "is_admin": True, "users": users},
    )


@router.post("/users")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
):
    require_admin(request)
    norm = _validate_username(username)
    user_store = request.app.state.user_store
    if user_store.get(norm) is not None:
        raise HTTPException(status_code=409, detail="user already exists")
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be admin or user")
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    user_store.add(username=norm, password=password, role=role)
    return RedirectResponse(url="/admin", status_code=303)
