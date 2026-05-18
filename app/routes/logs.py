"""Logs route — view recent API check history per monitor."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth import require_monitor_access

router = APIRouter()


@router.get("/logs/{monitor_id}", response_class=HTMLResponse)
async def monitor_logs(monitor_id: str, request: Request):
    user, monitor = require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    templates = request.app.state.templates
    logs = manager.get_check_logs(monitor_id)
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"user": user.username, "is_admin": user.role == "admin", "monitor": monitor, "logs": logs},
    )
