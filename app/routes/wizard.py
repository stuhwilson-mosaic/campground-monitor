"""Wizard routes — steps 1-5: location, facility, dates, notifications, review."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import get_current_user

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


def _catalog(request: Request):
    return request.app.state.catalog


@router.get("/monitors/new", response_class=HTMLResponse)
async def wizard_new(request: Request):
    """Render the wizard shell with step 1 loaded."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    catalog = _catalog(request)
    states = catalog.get_states()
    return _templates(request).TemplateResponse(
        request,
        "wizard.html",
        {"user": user, "states": states},
    )


@router.get("/monitors/new/step1", response_class=HTMLResponse)
async def wizard_step1(request: Request):
    """Return step 1 partial (used for back navigation)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    catalog = _catalog(request)
    states = catalog.get_states()
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step1.html",
        {"states": states},
    )


@router.get("/monitors/new/step2", response_class=HTMLResponse)
async def wizard_step2(request: Request):
    """Return step 2 partial (facility selection for a chosen rec area)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    rec_area_id = request.query_params.get("rec_area_id", "")
    rec_area_name = request.query_params.get("rec_area_name", "")
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step2.html",
        {"rec_area_id": rec_area_id, "rec_area_name": rec_area_name},
    )


@router.get("/monitors/new/step3", response_class=HTMLResponse)
async def wizard_step3(request: Request):
    """Return step 3 partial (monitor name, dates, poll interval)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step3.html",
        {},
    )


@router.get("/monitors/new/step4", response_class=HTMLResponse)
async def wizard_step4(request: Request):
    """Return step 4 partial (notification settings)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    user_store = request.app.state.user_store
    manager = request.app.state.manager
    user_record = user_store.get(user)
    defaults = dict(user_record.defaults) if user_record else {}
    # ntfy topics: union of this user's monitors only (so other users' topics don't leak).
    monitors = manager.list_monitors(owner=user)
    ntfy_topics = list({m.get("ntfy_topic", "") for m in monitors if m.get("ntfy_topic")})
    default_email = defaults.get("email_to", "")
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step4.html",
        {"ntfy_topics": ntfy_topics, "default_email": default_email},
    )


@router.get("/monitors/new/step5", response_class=HTMLResponse)
async def wizard_step5(request: Request):
    """Return step 5 partial (review and create)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _templates(request).TemplateResponse(
        request,
        "partials/wizard_step5.html",
        {},
    )
