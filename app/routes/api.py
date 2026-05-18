"""API action endpoints — HTMX monitor start/pause/stop/delete + catalog filters."""
import asyncio
import uuid
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.auth import get_current_user, require_monitor_access

router = APIRouter(prefix="/api")


def _card_response(request: Request, monitor_id: str) -> HTMLResponse:
    """Render the monitor_card partial for the given monitor ID."""
    manager = request.app.state.manager
    templates = request.app.state.templates
    monitor = manager.get_monitor(monitor_id)
    today = date.today().isoformat()
    return templates.TemplateResponse(
        request,
        "partials/monitor_card.html",
        {"m": monitor, "today": today},
    )


@router.post("/monitors/{monitor_id}/start", response_class=HTMLResponse)
async def start_monitor(monitor_id: str, request: Request):
    require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.start_monitor(monitor_id)
    return _card_response(request, monitor_id)


@router.post("/monitors/{monitor_id}/pause", response_class=HTMLResponse)
async def pause_monitor(monitor_id: str, request: Request):
    require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.pause_monitor(monitor_id)
    return _card_response(request, monitor_id)


@router.post("/monitors/{monitor_id}/stop", response_class=HTMLResponse)
async def stop_monitor(monitor_id: str, request: Request):
    require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.stop_monitor(monitor_id)
    return _card_response(request, monitor_id)


@router.delete("/monitors/{monitor_id}", response_class=HTMLResponse)
async def delete_monitor(monitor_id: str, request: Request):
    require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.stop_monitor(monitor_id)
    manager.delete_monitor(monitor_id)
    return HTMLResponse("")


@router.post("/monitors/{monitor_id}/test-alert")
async def test_alert(monitor_id: str, request: Request):
    """Send a test notification through the monitor's configured channels."""
    require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    results = await asyncio.to_thread(manager.send_test_alert, monitor_id)
    return JSONResponse(results)


# ── Monitor creation ──────────────────────────────────────────────────────────

class CreateMonitorRequest(BaseModel):
    name: str
    rec_area_name: str
    facility_ids: list[str]
    facility_names: dict[str, str]
    check_in: str
    check_out: str
    poll_interval_seconds: int = 300
    notify_email: bool = False
    notify_ntfy: bool = True
    email_to: str = ""
    ntfy_topic: str = ""
    status: str = "stopped"
    entry_date: str = ""
    party_size: int = 1
    facility_types: dict[str, str] = {}
    selected_divisions: dict[str, list[str]] = {}  # {facility_id: [division_id, ...]}


@router.post("/monitors")
async def create_monitor(body: CreateMonitorRequest, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    manager = request.app.state.manager
    user_store = request.app.state.user_store
    monitor_id = str(uuid.uuid4())

    facilities: list[dict[str, Any]] = []
    for fid in body.facility_ids:
        fac: dict[str, Any] = {
            "id": fid,
            "name": body.facility_names.get(fid, fid),
            "type": body.facility_types.get(fid, "Campground"),
        }
        div_ids = body.selected_divisions.get(fid, [])
        if div_ids:
            fac["division_ids"] = div_ids
        facilities.append(fac)

    config: dict[str, Any] = {
        "id": monitor_id,
        "owner": user,
        "name": body.name,
        "rec_area_name": body.rec_area_name,
        "facilities": facilities,
        "check_in": body.check_in,
        "check_out": body.check_out,
        "entry_date": body.entry_date,
        "party_size": body.party_size,
        "poll_interval_seconds": body.poll_interval_seconds,
        "enable_ntfy": body.notify_ntfy,
        "ntfy_topic": body.ntfy_topic,
        "enable_email": body.notify_email,
        "email_to": body.email_to,
        "status": body.status,
        "created_at": datetime.utcnow().isoformat(),
        "last_check": None,
        "last_result": None,
        "notified_sites": [],
        "stats": {
            "total_checks": 0,
            "total_alerts": 0,
            "total_errors": 0,
            "last_started_at": None,
            "last_stopped_at": None,
            "total_runtime_seconds": 0,
        },
    }

    manager.add_monitor(config)

    if body.status == "running":
        await manager.start_monitor(monitor_id)

    # Defaults now live per-user, not on the manager.
    user_record = user_store.get(user)
    if user_record:
        defaults = dict(user_record.defaults)
        if body.email_to:
            defaults["email_to"] = body.email_to
        if body.ntfy_topic:
            defaults["ntfy_topic"] = body.ntfy_topic
        user_store.update_defaults(user, defaults)

    return JSONResponse({"id": monitor_id, "status": "ok"})


# ── Catalog filter endpoints (HTMX) ───────────────────────────────────────────


@router.get("/agencies", response_class=HTMLResponse)
async def api_agencies(request: Request, state: str = ""):
    """Return <option> elements for agencies in the given state."""
    user = get_current_user(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    catalog = request.app.state.catalog
    agencies = catalog.get_agencies(state) if state else []
    parts = ['<option value="">All agencies</option>']
    for ag in agencies:
        label = ag["abbrev"] if ag["abbrev"] else ag["name"]
        parts.append(
            f'<option value="{ag["id"]}">{label} — {ag["name"]}</option>'
        )
    return HTMLResponse("".join(parts))


@router.get("/rec-areas", response_class=HTMLResponse)
async def api_rec_areas(
    request: Request,
    state: str = "",
    org: str = "",
    q: Optional[str] = None,
):
    """Return HTML list items for rec areas matching state/org/query."""
    user = get_current_user(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    catalog = request.app.state.catalog
    if not state or not org:
        return HTMLResponse('<li class="rec-area-empty">Select a state and agency first.</li>')
    if q:
        areas = catalog.search_rec_areas(state, org, q)
    else:
        areas = catalog.get_rec_areas(state, org)
    if not areas:
        return HTMLResponse('<li class="rec-area-empty">No parks found.</li>')
    parts = []
    for ra in areas:
        parts.append(
            f'<li class="rec-area-item" '
            f'onclick="selectRecArea(\'{ra["id"]}\', \'{ra["name"].replace(chr(39), "")}\''
            f')">'
            f'{ra["name"]} '
            f'<span class="rec-area-count">({ra["facility_count"]} sites)</span>'
            f'</li>'
        )
    return HTMLResponse("".join(parts))


@router.get("/facilities", response_class=HTMLResponse)
async def api_facilities(
    request: Request,
    rec_area: str = "",
    type: Optional[str] = None,
):
    """Return HTML checkbox items for facilities in the given rec area."""
    user = get_current_user(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    catalog = request.app.state.catalog
    if not rec_area:
        return HTMLResponse('<p class="no-facilities">No rec area selected.</p>')
    facilities = catalog.get_facilities(rec_area, facility_type=type if type else None)
    if not facilities:
        return HTMLResponse('<p class="no-facilities">No facilities found.</p>')
    parts = []
    for fac in facilities:
        fac_id = fac["id"]
        fac_name = fac["name"]
        fac_type = fac["type"]
        parts.append(
            f'<label class="facility-item">'
            f'<input type="checkbox" name="facility_ids" value="{fac_id}" '
            f'data-name="{fac_name}" data-type="{fac_type}" '
            f'onchange="updateFacilitySelection(this)"> '
            f'{fac_name} <span class="facility-type">({fac_type})</span>'
            f'</label>'
        )
    return HTMLResponse("".join(parts))


# ── Permit division endpoints ─────────────────────────────────────────────────


@router.get("/permit-divisions", response_class=HTMLResponse)
async def api_permit_divisions(request: Request, facility_id: str = ""):
    """Return HTML checkboxes for permit trailheads/divisions."""
    from app.monitor_engine import get_permit_divisions

    user = get_current_user(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    if not facility_id:
        return HTMLResponse("")

    try:
        divisions = await asyncio.to_thread(get_permit_divisions, facility_id)
    except Exception as exc:
        return HTMLResponse(f'<p class="no-facilities">Could not load trailheads: {exc}</p>')

    if not divisions:
        return HTMLResponse('<p class="no-facilities">No trailheads found.</p>')

    sorted_divs = sorted(divisions.items(), key=lambda x: x[1])
    parts = [
        '<div class="division-filter">'
        '<input type="text" class="form-control division-search" '
        'placeholder="Filter trailheads..." '
        f'oninput="filterDivisions(this, \'{facility_id}\')">'
        '</div>'
    ]
    for div_id, div_name in sorted_divs:
        parts.append(
            f'<label class="facility-item division-item" data-division-name="{div_name.lower()}">'
            f'<input type="checkbox" value="{div_id}" '
            f'data-name="{div_name}" '
            f'onchange="updateDivisionSelection(this, \'{facility_id}\')"> '
            f'{div_name}'
            f'</label>'
        )
    return HTMLResponse("".join(parts))
