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

    # Counts describe ALL of the user's monitors, deliberately computed before
    # the filter is applied. Counting the filtered subset would make the
    # summary bar contradict itself the moment "show only running" is on.
    running_count = sum(1 for m in monitors if m.get("status") == "running")
    stopped_count = len(monitors) - running_count
    total_count = len(monitors)

    # Running first, newest first within each group. Two passes because the two
    # keys sort in opposite directions and Python's sort is stable: ordering by
    # created_at descending first, then by status, preserves the date order
    # inside each status group. The secondary key matters because without it
    # the order is whatever monitors.json happens to hold, which shifts as rows
    # are rewritten every poll cycle.
    monitors = sorted(monitors, key=lambda m: m.get("created_at") or "", reverse=True)
    monitors = sorted(monitors, key=lambda m: 0 if m.get("status") == "running" else 1)

    running_only = request.query_params.get("running") == "1"
    if running_only:
        monitors = [m for m in monitors if m.get("status") == "running"]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user.username,
            "is_admin": is_admin,
            "monitors": monitors,
            "running_count": running_count,
            "stopped_count": stopped_count,
            "total_count": total_count,
            "running_only": running_only,
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
