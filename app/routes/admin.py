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


@router.post("/users/{username}/password")
async def admin_reset_password(
    request: Request,
    username: str,
    new_password: str = Form(...),
):
    require_admin(request)
    if not new_password:
        raise HTTPException(status_code=400, detail="password required")
    user_store = request.app.state.user_store
    if not user_store.update_password(username, new_password):
        raise HTTPException(status_code=404, detail="user not found")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{username}/role")
async def admin_change_role(
    request: Request,
    username: str,
    role: str = Form(...),
):
    admin = require_admin(request)
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be admin or user")
    user_store = request.app.state.user_store
    target = user_store.get(username)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Refuse to demote the last admin.
    if target.role == "admin" and role == "user" and user_store.admin_count() <= 1:
        raise HTTPException(status_code=409, detail="cannot demote the last admin")
    user_store.update_role(username, role)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{username}/delete")
async def admin_delete_user(
    request: Request,
    username: str,
    monitors: str = Form("reassign"),
):
    admin = require_admin(request)
    if monitors not in ("reassign", "delete"):
        raise HTTPException(status_code=400, detail="monitors must be 'reassign' or 'delete'")
    user_store = request.app.state.user_store
    manager = request.app.state.manager
    target = user_store.get(username)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.username == admin.username:
        raise HTTPException(status_code=409, detail="cannot delete yourself")
    if target.role == "admin" and user_store.admin_count() <= 1:
        raise HTTPException(status_code=409, detail="cannot delete the last admin")

    owned = [m["id"] for m in manager.list_monitors(owner=target.username)]
    if monitors == "reassign":
        for mid in owned:
            manager.update_monitor(mid, owner=admin.username)
    else:
        for mid in owned:
            await manager.stop_monitor(mid)
            manager.delete_monitor(mid)
    user_store.delete(target.username)
    return RedirectResponse(url="/admin", status_code=303)
