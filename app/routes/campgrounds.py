"""Authenticated campground discovery and read-only calendar endpoints."""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import get_current_user_record
from app.availability import AvailabilityError, validate_dates

router = APIRouter()


def _campground(request: Request, facility_id: str) -> dict:
    campground = request.app.state.catalog.get_campground(facility_id)
    if campground is None:
        raise HTTPException(status_code=404, detail="Campground not found.")
    return campground


def _dates(check_in: str = "", check_out: str = "") -> tuple[str, str]:
    start = check_in or date.today().isoformat()
    try:
        end = check_out or (date.fromisoformat(start) + timedelta(days=2)).isoformat()
        return validate_dates(start, end)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/campgrounds", response_class=HTMLResponse)
async def browse_campgrounds(request: Request, q: str = Query("", max_length=120), state: str = Query("", max_length=5)):
    user = get_current_user_record(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    catalog = request.app.state.catalog
    results = catalog.search_campgrounds(query=q, state=state)
    return request.app.state.templates.TemplateResponse(request, "campgrounds.html", {
        "user": user.username, "is_admin": user.role == "admin", "campgrounds": results[:60],
        "states": [item["code"] for item in catalog.get_states()], "query": q, "state": state, "truncated": len(results) > 60,
    })


@router.get("/campgrounds/{facility_id}", response_class=HTMLResponse)
async def campground_page(request: Request, facility_id: str, check_in: str = "", check_out: str = ""):
    user = get_current_user_record(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    facility = _campground(request, facility_id)
    start, end = _dates(check_in, check_out)
    return request.app.state.templates.TemplateResponse(request, "availability.html", {
        "user": user.username, "is_admin": user.role == "admin",
        "seed": {"facility": facility, "check_in": start, "check_out": end,
                 "today": date.today().isoformat(), "max_date": (date.today() + timedelta(days=365)).isoformat()},
    })


@router.get("/api/campgrounds/{facility_id}/availability")
async def campground_availability(request: Request, facility_id: str, check_in: str = "", check_out: str = ""):
    if not get_current_user_record(request):
        raise HTTPException(status_code=401, detail="Sign in to view availability.")
    facility = _campground(request, facility_id)
    start, end = _dates(check_in, check_out)
    catalog = request.app.state.catalog

    def load():
        metadata = catalog.get_campsite_metadata(facility_id)
        view = request.app.state.availability.get_view(facility_id, start, end, metadata)
        return {"facility": {**facility, "map_url": metadata["map_url"]}, **view}

    try:
        async with request.app.state.availability_slots:
            data = await asyncio.to_thread(load)
    except AvailabilityError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "30"})
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
