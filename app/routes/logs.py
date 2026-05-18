"""Logs route — view recent API check history per monitor."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user

router = APIRouter()


@router.get("/logs/{monitor_id}", response_class=HTMLResponse)
async def monitor_logs(monitor_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    manager = request.app.state.manager
    templates = request.app.state.templates

    monitor = manager.get_monitor(monitor_id)
    if not monitor:
        return HTMLResponse("Monitor not found", status_code=404)

    logs = manager.get_check_logs(monitor_id)

    return templates.TemplateResponse(
        request,
        "logs.html",
        {"user": user, "monitor": monitor, "logs": logs},
    )
