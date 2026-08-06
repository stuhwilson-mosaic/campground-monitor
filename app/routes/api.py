"""API action endpoints — HTMX monitor start/stop/delete + catalog filters."""
import asyncio
import uuid
from datetime import date, datetime
from html import escape
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, model_validator

from app.auth import get_current_user, require_monitor_access
from app.telemetry import audit

router = APIRouter(prefix="/api")


# ── Monitor validation ────────────────────────────────────────────────────────
#
# Split into two functions on purpose. Creating a monitor needs both; editing an
# existing monitor's dates already knows the type and needs only the second.
# Keeping them separate stops the two paths from drifting apart.


def resolve_facility_type(facility_ids: list[str], facility_types: dict[str, str]) -> str:
    """Return the single facility type for a monitor.

    Raises ValueError if the selection is empty or mixes types. Mixed selections
    are unserviceable: _run_check routes per facility, so whichever half lacks
    its dates calls its check function with empty strings and raises on every
    cycle forever.
    """
    if not facility_ids:
        return _fail("Select at least one facility.")

    kinds = {facility_types.get(fid, "Campground") for fid in facility_ids}
    if len(kinds) > 1:
        return _fail(
            "A monitor watches either campgrounds or wilderness permits, not both. "
            f"Got: {', '.join(sorted(kinds))}. Create one monitor for each."
        )
    return kinds.pop()


def validate_dates_for_type(
    kind: str,
    check_in: str,
    check_out: str,
    entry_date: str,
    nights: Optional[int] = None,
) -> None:
    """Validate the date fields that matter for `kind`. Raises ValueError."""
    if kind == "Permit":
        if not entry_date:
            _fail("Permit monitors need an entry date.")
        if nights is not None and nights < 1:
            _fail("Nights must be at least 1.")
        return

    if not check_in or not check_out:
        _fail("Campground monitors need both a check-in and a check-out date.")
    # ISO YYYY-MM-DD compares correctly as a string, which is how the wizard
    # already does it. A zero- or negative-length stay makes dates_needed()
    # return [], and all([]) is True, so every site would report available.
    if check_out <= check_in:
        _fail("Check-out must be after check-in.")


def _fail(message: str):
    raise ValueError(message)


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
    user, monitor = require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.start_monitor(monitor_id)
    await audit(request, "monitor.start", username=user.username,
                target_id=monitor_id, target_name=monitor.get("name"))
    return _card_response(request, monitor_id)


@router.post("/monitors/{monitor_id}/stop", response_class=HTMLResponse)
async def stop_monitor(monitor_id: str, request: Request):
    user, monitor = require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.stop_monitor(monitor_id)
    await audit(request, "monitor.stop", username=user.username,
                target_id=monitor_id, target_name=monitor.get("name"))
    return _card_response(request, monitor_id)


@router.delete("/monitors/{monitor_id}", response_class=HTMLResponse)
async def delete_monitor(monitor_id: str, request: Request):
    user, monitor = require_monitor_access(request, monitor_id)
    manager = request.app.state.manager
    await manager.stop_monitor(monitor_id)
    # Capture the name before the row is gone.
    await audit(request, "monitor.delete", username=user.username,
                target_id=monitor_id, target_name=monitor.get("name"))
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
    nights: Optional[int] = None  # permit-only, metadata only (see below)
    facility_types: dict[str, str] = {}
    selected_divisions: dict[str, list[str]] = {}  # {facility_id: [division_id, ...]}
    # {facility_id: {division_id: name}} — captured by the wizard's trailhead
    # picker so the dashboard can name trailheads without an API call.
    selected_division_names: dict[str, dict[str, str]] = {}

    @model_validator(mode="after")
    def _check_type_and_dates(self):
        """Reject monitors the poll loop could never service.

        `nights` never reaches check_permit: the recreation.gov permit API has
        no duration dimension at all (a day/division record is just
        quota_usage_by_member_daily plus is_walkup), because Yosemite wilderness
        quota applies to the entry trailhead on the entry date only. It is
        recorded for display and for what to actually book.
        """
        kind = resolve_facility_type(self.facility_ids, self.facility_types)
        validate_dates_for_type(
            kind, self.check_in, self.check_out, self.entry_date, self.nights
        )
        return self


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
            div_names = body.selected_division_names.get(fid) or {}
            if div_names:
                fac["division_names"] = div_names
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
        "nights": body.nights,
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

    await audit(request, "monitor.create", username=user,
                target_id=monitor_id, target_name=body.name)
    return JSONResponse({"id": monitor_id, "status": "ok"})


# ── Edit a monitor's dates ────────────────────────────────────────────────────


# The intervals the wizard offers. Validated against this set rather than
# accepting any integer: a 1-second interval would hammer recreation.gov, and
# the May logs already show 429s at 15s.
ALLOWED_POLL_INTERVALS = [15, 30, 60, 120, 300, 600, 900, 1800]


class UpdateDatesRequest(BaseModel):
    check_in: str = ""
    check_out: str = ""
    entry_date: str = ""
    nights: Optional[int] = None
    poll_interval_seconds: Optional[int] = None

    @model_validator(mode="after")
    def _check_interval(self):
        if (self.poll_interval_seconds is not None
                and self.poll_interval_seconds not in ALLOWED_POLL_INTERVALS):
            raise ValueError(
                "Check frequency must be one of: "
                + ", ".join(str(i) for i in ALLOWED_POLL_INTERVALS)
            )
        return self


@router.post("/monitors/{monitor_id}/dates")
async def update_monitor_dates(monitor_id: str, body: UpdateDatesRequest, request: Request):
    """Change an existing monitor's dates.

    Requires the monitor to be stopped. Editing a live monitor was
    rejected in favour of this: it removes every question about a poll cycle
    running mid-change, at the cost of two extra clicks.
    """
    user, monitor = require_monitor_access(request, monitor_id)
    manager = request.app.state.manager

    if monitor.get("status") == "running":
        return JSONResponse(
            {"error": "Stop the monitor before editing its dates."},
            status_code=409,
        )

    # Same rules as creation, via the same functions, so the two cannot drift.
    kind = resolve_facility_type(
        [f["id"] for f in monitor.get("facilities", []) if f.get("id")],
        {f["id"]: f.get("type", "Campground")
         for f in monitor.get("facilities", []) if f.get("id")},
    )
    try:
        validate_dates_for_type(
            kind, body.check_in, body.check_out, body.entry_date, body.nights
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    fields: dict[str, Any]
    if kind == "Permit":
        fields = {"entry_date": body.entry_date, "nights": body.nights,
                  "check_in": "", "check_out": ""}
    else:
        fields = {"check_in": body.check_in, "check_out": body.check_out,
                  "entry_date": "", "nights": None}
    if body.poll_interval_seconds is not None:
        # The poll loop re-reads config each cycle, so this applies on the next
        # tick. Editing is stop-first anyway, so it takes effect on restart.
        fields["poll_interval_seconds"] = body.poll_interval_seconds
    manager.update_monitor(monitor_id, **fields)

    # The dedup ledger refers to the old date window; keeping it would suppress
    # the first alert under the new dates.
    manager.clear_notified(monitor_id)
    await audit(request, "monitor.edit", username=user.username,
                target_id=monitor_id, target_name=monitor.get("name"),
                detail={"kind": kind})
    return JSONResponse({"status": "ok"})


# ── Favorites ─────────────────────────────────────────────────────────────────


class CreateFavoriteRequest(BaseModel):
    label: str
    rec_area_id: str = ""
    rec_area_name: str = ""
    facilities: list[dict[str, Any]] = []

    @model_validator(mode="after")
    def _check(self):
        if not self.label.strip():
            raise ValueError("A favorite needs a label.")
        if not self.facilities:
            raise ValueError("A favorite needs at least one facility.")
        kinds = {f.get("type", "Campground") for f in self.facilities}
        if len(kinds) > 1:
            # Same rule as monitors: a favorite seeds a monitor, and a monitor
            # watches one facility type.
            raise ValueError("A favorite cannot mix campgrounds and permits.")
        return self


@router.post("/favorites")
async def create_favorite(body: CreateFavoriteRequest, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    store = request.app.state.user_store
    fav = store.add_favorite(user, body.model_dump())
    if fav is None:
        return JSONResponse({"error": "Unknown user"}, status_code=404)
    return JSONResponse({"id": fav["id"], "status": "ok"})


@router.delete("/favorites/{favorite_id}")
async def delete_favorite(favorite_id: str, request: Request):
    """Delete one of the CALLING user's favorites.

    Scoped to the caller by construction: the store only ever looks inside that
    user's record, so a foreign id is simply not found.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    store = request.app.state.user_store
    if not store.delete_favorite(user, favorite_id):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return HTMLResponse("")


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
    """Return HTML list items for rec areas matching state/org/query.

    A query searches the whole catalog and ignores state/org. Without one, the
    state+org drill-down still applies exactly as before. Search has to work
    unfiltered because facilities with no address row land in the "Other" state
    bucket and are otherwise unreachable: 87 of 149 permits in the real export.
    """
    user = get_current_user(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    catalog = request.app.state.catalog

    query = (q or "").strip()
    if query:
        areas = catalog.search_all_rec_areas(query)
    elif state and org:
        areas = catalog.get_rec_areas(state, org)
    else:
        return HTMLResponse(
            '<li class="rec-area-empty">Search for a park, or pick a state and agency.</li>'
        )

    if not areas:
        return HTMLResponse('<li class="rec-area-empty">No parks found.</li>')

    # Park names come from a third-party CSV and reach the browser verbatim.
    # The old markup interpolated the name into an inline onclick and defended
    # only against apostrophes, so a name containing a quote or angle bracket
    # broke the element. Data attributes plus escaping remove the whole class
    # of problem; the click is handled by a delegated listener in wizard.html.
    parts = []
    for ra in areas:
        states = ", ".join(ra.get("states", []))
        suffix = f" &middot; {escape(states)}" if states else ""
        parts.append(
            f'<li class="rec-area-item" '
            f'data-rec-area-id="{escape(ra["id"], quote=True)}" '
            f'data-rec-area-name="{escape(ra["name"], quote=True)}">'
            f'{escape(ra["name"])} '
            f'<span class="rec-area-count">({ra["facility_count"]} sites{suffix})</span>'
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
