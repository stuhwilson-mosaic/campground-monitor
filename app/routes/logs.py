"""Logs route — view recent API check history per monitor."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth import require_monitor_access

router = APIRouter()


@router.get("/monitors/{monitor_id}/edit", response_class=HTMLResponse)
async def monitor_edit(monitor_id: str, request: Request):
    """Small dedicated form for changing a monitor's dates.

    Not the wizard: only two fields change, and the wizard's five steps would
    be noise. Owner-or-admin, same gate as the logs page.
    """
    user, monitor = require_monitor_access(request, monitor_id)
    templates = request.app.state.templates
    facilities = monitor.get("facilities") or []
    is_permit = bool(facilities) and facilities[0].get("type") == "Permit"
    return templates.TemplateResponse(
        request,
        "monitor_edit.html",
        {
            "user": user.username,
            "is_admin": user.role == "admin",
            "monitor": monitor,
            "is_permit": is_permit,
        },
    )


PER_PAGE = 100


@router.get("/logs/{monitor_id}", response_class=HTMLResponse)
async def monitor_logs(monitor_id: str, request: Request):
    """Per-request check history for a monitor, newest first.

    Backed by SQLite, so it survives restarts. The previous in-memory ring
    buffer held 20 entries and was lost on every deploy.
    """
    user, monitor = require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    templates = request.app.state.templates

    errors_only = request.query_params.get("errors") == "1"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    total = manager.count_check_logs(monitor_id, errors_only=errors_only)
    page_count = max(1, -(-total // PER_PAGE))  # ceiling division
    page = min(page, page_count)

    logs = manager.get_check_logs(
        monitor_id,
        limit=PER_PAGE,
        offset=(page - 1) * PER_PAGE,
        errors_only=errors_only,
    )
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "user": user.username,
            "is_admin": user.role == "admin",
            "monitor": monitor,
            "logs": logs,
            "total": total,
            "page": page,
            "page_count": page_count,
            "per_page": PER_PAGE,
            "errors_only": errors_only,
        },
    )
