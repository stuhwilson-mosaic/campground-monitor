"""Wizard routes — steps 1-5: location, facility, dates, notifications, review."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user, get_current_user_record, require_monitor_access

router = APIRouter()


def seed_from_monitor(monitor: dict) -> dict:
    """Build wizard prefill state from an existing monitor.

    Dates are carried over deliberately: cloning is overwhelmingly used to
    shift a window by a day or two, so an editable starting value beats an
    empty field.

    Pure function over a dict, so it is testable without HTTP.
    """
    facilities = monitor.get("facilities") or []
    facility_ids = [f["id"] for f in facilities if f.get("id")]

    return {
        "rec_area_name": monitor.get("rec_area_name", ""),
        "facility_ids": facility_ids,
        # Both keyed by facility id, matching the wizard's internal shape.
        "facility_names": {f["id"]: f.get("name", f["id"]) for f in facilities if f.get("id")},
        "facility_types": {f["id"]: f.get("type", "Campground") for f in facilities if f.get("id")},
        "selected_divisions": {
            f["id"]: f["division_ids"] for f in facilities
            if f.get("id") and f.get("division_ids")
        },
        "name": f"{monitor.get('name', 'Monitor')} copy",
        "check_in": monitor.get("check_in", ""),
        "check_out": monitor.get("check_out", ""),
        "entry_date": monitor.get("entry_date", ""),
        "party_size": monitor.get("party_size", 1),
        "nights": monitor.get("nights"),
        "poll_interval_seconds": monitor.get("poll_interval_seconds", 300),
        "enable_ntfy": bool(monitor.get("enable_ntfy")),
        "ntfy_topic": monitor.get("ntfy_topic", ""),
        "enable_email": bool(monitor.get("enable_email")),
        "email_to": monitor.get("email_to", ""),
    }


def seed_from_favorite(favorite: dict) -> dict:
    """Build wizard prefill state from a saved facility selection.

    Same shape as seed_from_monitor, minus the dates and notification settings:
    a favorite is a place, not a trip. Those come from the user's defaults.
    """
    facilities = favorite.get("facilities") or []
    return {
        "rec_area_id": favorite.get("rec_area_id", ""),
        "rec_area_name": favorite.get("rec_area_name", ""),
        "facility_ids": [f["id"] for f in facilities if f.get("id")],
        "facility_names": {f["id"]: f.get("name", f["id"]) for f in facilities if f.get("id")},
        "facility_types": {f["id"]: f.get("type", "Campground") for f in facilities if f.get("id")},
        "selected_divisions": {
            f["id"]: f["division_ids"] for f in facilities
            if f.get("id") and f.get("division_ids")
        },
    }


def _seed_json(seed: dict) -> str:
    """Serialize a seed for embedding in a <script type="application/json">.

    Escaping "</" is what stops a facility name containing a literal
    "</script>" from closing the block early.
    """
    return json.dumps(seed or {}).replace("</", "<\\/")


def _templates(request: Request):
    return request.app.state.templates


def _catalog(request: Request):
    return request.app.state.catalog


@router.get("/monitors/new", response_class=HTMLResponse)
async def wizard_new(request: Request):
    """Render the wizard shell.

    With no query params this is the plain 5-step flow starting at step 1.
    `?from=<monitor_id>` clones an existing monitor: the facility selection is
    already settled, so it opens at step 3 (dates) with everything prefilled.
    """
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    record = get_current_user_record(request)
    is_admin = bool(record and record.role == "admin")
    catalog = _catalog(request)
    states = catalog.get_states()

    seed: dict = {}
    start_step = 1

    clone_from = request.query_params.get("from", "")
    if clone_from:
        try:
            # Owner-or-admin. A non-owner gets a 403 rather than a silent
            # fallback, since that is a real authorization answer.
            _, monitor = require_monitor_access(request, clone_from)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # An id that no longer exists is not worth an error page; drop the
            # user into an empty wizard instead.
            monitor = None
        if monitor:
            seed = seed_from_monitor(monitor)
            start_step = 3

    favorite_id = request.query_params.get("favorite", "")
    if favorite_id and not seed:
        # Only the caller's own favorites are visible, so an id belonging to
        # someone else simply is not found.
        favorite = request.app.state.user_store.get_favorite(user, favorite_id)
        if favorite:
            seed = seed_from_favorite(favorite)
            start_step = 3

    return _templates(request).TemplateResponse(
        request,
        "wizard.html",
        {
            "user": user,
            "is_admin": is_admin,
            "states": states,
            "seed_json": _seed_json(seed),
            "start_step": start_step,
        },
    )


@router.get("/monitors/new/step1", response_class=HTMLResponse)
async def wizard_step1(request: Request):
    """Return step 1 partial (used for back navigation)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    record = get_current_user_record(request)
    is_admin = bool(record and record.role == "admin")
    catalog = _catalog(request)
    states = catalog.get_states()
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step1.html",
        {"is_admin": is_admin, "states": states},
    )


@router.get("/monitors/new/step2", response_class=HTMLResponse)
async def wizard_step2(request: Request):
    """Return step 2 partial (facility selection for a chosen rec area)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    record = get_current_user_record(request)
    is_admin = bool(record and record.role == "admin")
    rec_area_id = request.query_params.get("rec_area_id", "")
    rec_area_name = request.query_params.get("rec_area_name", "")
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step2.html",
        {"is_admin": is_admin, "rec_area_id": rec_area_id, "rec_area_name": rec_area_name},
    )


@router.get("/monitors/new/step3", response_class=HTMLResponse)
async def wizard_step3(request: Request):
    """Return step 3 partial (monitor name, dates, poll interval)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    record = get_current_user_record(request)
    is_admin = bool(record and record.role == "admin")
    defaults = dict(record.defaults) if record else {}
    # Falls back to 5 minutes, which is what the select hardcoded before.
    default_poll_interval = defaults.get("poll_interval_seconds") or 300
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step3.html",
        {"is_admin": is_admin, "default_poll_interval": default_poll_interval},
    )


@router.get("/monitors/new/step4", response_class=HTMLResponse)
async def wizard_step4(request: Request):
    """Return step 4 partial (notification settings)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    record = get_current_user_record(request)
    is_admin = bool(record and record.role == "admin")
    user_store = request.app.state.user_store
    manager = request.app.state.manager
    user_record = user_store.get(user)
    defaults = dict(user_record.defaults) if user_record else {}
    # ntfy topics: union of this user's monitors only (so other users' topics don't leak).
    monitors = manager.list_monitors(owner=user)
    ntfy_topics = list({m.get("ntfy_topic", "") for m in monitors if m.get("ntfy_topic")})
    default_email = defaults.get("email_to", "")
    # The stored ntfy default was never read; the dropdown was built purely from
    # topics already used on this user's monitors.
    default_ntfy_topic = defaults.get("ntfy_topic", "")
    if default_ntfy_topic and default_ntfy_topic not in ntfy_topics:
        ntfy_topics.insert(0, default_ntfy_topic)
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step4.html",
        {
            "is_admin": is_admin,
            "ntfy_topics": ntfy_topics,
            "default_email": default_email,
            "default_ntfy_topic": default_ntfy_topic,
        },
    )


@router.get("/monitors/new/step5", response_class=HTMLResponse)
async def wizard_step5(request: Request):
    """Return step 5 partial (review and create)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    record = get_current_user_record(request)
    is_admin = bool(record and record.role == "admin")
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step5.html",
        {"is_admin": is_admin},
    )
