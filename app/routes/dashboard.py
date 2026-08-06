"""Dashboard route — authenticated view of monitors (filtered or all for admin)."""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user_record

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user_record(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    manager = request.app.state.manager
    templates = request.app.state.templates

    is_admin = user.role == "admin"
    monitors = manager.list_monitors() if is_admin else manager.list_monitors(owner=user.username)
    today = date.today().isoformat()
    running_count = sum(1 for m in monitors if m.get("status") == "running")
    paused_count = sum(1 for m in monitors if m.get("status") == "paused")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user.username,
            "is_admin": is_admin,
            "monitors": monitors,
            "running_count": running_count,
            "paused_count": paused_count,
            "today": today,
            # Always the caller's own, even for an admin: favorites are a
            # personal shortcut, not something to administer.
            "favorites": request.app.state.user_store.list_favorites(user.username),
        },
    )


@router.get("/guide", response_class=HTMLResponse)
async def guide(request: Request):
    user = get_current_user_record(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "guide.html", {"user": user.username, "is_admin": user.role == "admin"}
    )
