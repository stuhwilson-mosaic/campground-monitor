"""Dashboard route — authenticated view of all monitors."""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    manager = request.app.state.manager
    templates = request.app.state.templates

    monitors = manager.list_monitors()
    today = date.today().isoformat()

    running_count = sum(1 for m in monitors if m.get("status") == "running")
    paused_count = sum(1 for m in monitors if m.get("status") == "paused")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "monitors": monitors,
            "running_count": running_count,
            "paused_count": paused_count,
            "today": today,
        },
    )


@router.get("/guide", response_class=HTMLResponse)
async def guide(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "guide.html", {"user": user})
