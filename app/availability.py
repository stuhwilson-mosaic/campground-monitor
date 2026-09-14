"""Read-only campground calendars with a bounded cache shared by browser users."""
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.monitor_engine import _months_needed, fetch_campground_month

log = logging.getLogger(__name__)


class AvailabilityError(Exception):
    """No sufficiently recent, trustworthy calendar can be served."""


def validate_dates(check_in: str, check_out: str, *, today: date | None = None) -> tuple[str, str]:
    """Bound public calendar requests before any outbound work."""
    today = today or date.today()
    try:
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (check_in, check_out)):
            raise ValueError
        start, end = date.fromisoformat(check_in), date.fromisoformat(check_out)
    except (ValueError, TypeError):
        raise ValueError("Enter valid check-in and check-out dates.") from None
    if start < today:
        raise ValueError("Check-in must be today or later.")
    if not 1 <= (end - start).days <= 31:
        raise ValueError("Choose a stay of 1 to 31 nights.")
    if end > today + timedelta(days=365):
        raise ValueError("Choose dates within the next year.")
    return start.isoformat(), end.isoformat()


@dataclass
class _Snapshot:
    sites: dict | None
    checked: float
    fetched_at: str
    retry_after: float = 0


def _calendar_sites(payload: dict) -> dict:
    """Reject changed/error payloads rather than treating them as sold-out inventory."""
    if not isinstance(payload, dict) or not isinstance(payload.get("campsites"), dict):
        raise AvailabilityError("Unexpected campground response")
    sites = {}
    for sid, row in payload["campsites"].items():
        if not isinstance(row, dict) or not isinstance(row.get("availabilities"), dict):
            raise AvailabilityError("Unexpected campsite response")
        if not str(sid).isdigit() or str(row.get("hide_external", False)).lower() == "true":
            continue
        # Keep only fields used by the browser; avoid retaining rate/rule payloads.
        statuses = {}
        for day, status in row["availabilities"].items():
            try:
                iso_day = date.fromisoformat(day[:10]).isoformat()
            except (TypeError, ValueError):
                raise AvailabilityError("Unexpected calendar date") from None
            statuses[iso_day] = status[:100] if isinstance(status, str) and status else "Unknown"
        sites[str(sid)] = {
            "name": str(row.get("site") or sid),
            "loop": str(row.get("loop") or ""),
            "type": str(row.get("campsite_type") or "Unknown"),
            "availability": statuses,
        }
    return sites


class AvailabilityService:
    """Coalesce concurrent reads, throttle failures, and label bounded stale data.

    The condition protects cache bookkeeping only. HTTP runs outside the lock,
    and callers run this synchronous service through asyncio.to_thread.
    Monitor polling keeps its existing cadence and bypasses the browser cache.
    """
    def __init__(self, *, fetcher=None, clock=time.monotonic, max_entries=64):
        self._fetcher = fetcher or (
            lambda fid, start: fetch_campground_month(fid, start, timeout=12)
        )
        self._clock = clock
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str], _Snapshot] = OrderedDict()
        self._inflight: set[tuple[str, str]] = set()
        self._condition = threading.Condition()

    def _month(self, facility_id: str, month_start: str) -> tuple[_Snapshot, bool]:
        key = (facility_id, month_start)
        with self._condition:
            while True:
                now = self._clock()
                old = self._cache.get(key)
                if old is not None:
                    self._cache.move_to_end(key)
                    if old.sites is not None and now - old.checked < 90:
                        return old, False
                    if now < old.retry_after:
                        if old.sites is not None and now - old.checked < 900:
                            return old, True
                        raise AvailabilityError("Availability is temporarily unavailable. Try again shortly.")
                if key not in self._inflight and len(self._inflight) < 4:
                    self._inflight.add(key)
                    break
                self._condition.wait()

        error = None
        try:
            sites = _calendar_sites(self._fetcher(facility_id, month_start))
            fresh = _Snapshot(sites, self._clock(), datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            error = exc
            fresh = old or _Snapshot(None, self._clock(), "")
            fresh.retry_after = self._clock() + 30
            log.warning("Campground calendar refresh failed for %s (%s)", facility_id, type(exc).__name__)
        finally:
            with self._condition:
                # All ordinary upstream errors have a negative-cache entry.
                if 'fresh' in locals():
                    self._cache[key] = fresh
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._max_entries:
                        self._cache.popitem(last=False)
                self._inflight.discard(key)
                self._condition.notify_all()
        if error and (fresh.sites is None or self._clock() - fresh.checked >= 900):
            raise AvailabilityError("Availability is temporarily unavailable. Try again shortly.") from error
        return fresh, error is not None

    def get_view(self, facility_id: str, check_in: str, check_out: str, metadata: dict) -> dict:
        start, end = date.fromisoformat(check_in), date.fromisoformat(check_out)
        nights = (end - start).days
        if not 1 <= nights <= 31:
            raise ValueError("Choose a stay of 1 to 31 nights.")
        dates = [(start + timedelta(days=i)).isoformat() for i in range(max(14, nights))]
        visible_end = (date.fromisoformat(dates[-1]) + timedelta(days=1)).isoformat()
        merged, fetched, stale = {}, [], False
        for month_start in _months_needed(check_in, visible_end):
            snapshot, was_stale = self._month(facility_id, month_start)
            stale = stale or was_stale
            fetched.append(snapshot.fetched_at)
            for sid, row in snapshot.sites.items():
                if sid not in merged:
                    details = metadata.get("sites", {}).get(sid, {})
                    merged[sid] = {
                        "id": sid, "name": row["name"], "loop": row["loop"], "type": row["type"],
                        "accessible": details.get("accessible"),
                        "lat": details.get("lat"), "lon": details.get("lon"),
                        "booking_url": f"https://www.recreation.gov/camping/campsites/{sid}",
                        "availability": {},
                    }
                merged[sid]["availability"].update(row["availability"])
        for site in merged.values():
            site["availability"] = {day: site["availability"].get(day, "Unknown") for day in dates}
            site["available_for_stay"] = all(site["availability"][day] == "Available" for day in dates[:nights])
        sites = sorted(merged.values(), key=lambda s: (
            s["loop"].casefold(), [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", s["name"])], s["id"]
        ))
        return {
            "check_in": check_in, "check_out": check_out, "dates": dates, "sites": sites,
            "fetched_at": min(fetched), "stale": stale,
            "notice": "Refresh failed. Showing previously checked availability; confirm on Recreation.gov." if stale else "",
        }
