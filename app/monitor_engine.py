"""
Monitor engine — core check/notify logic for campground availability.

Extracted and parameterized from monitor.py so it can run in Docker (Linux)
without winsound and without reading module-level globals.
"""
import calendar
import logging
import smtplib
import time
from datetime import date, timedelta
from email.mime.text import MIMEText

import requests

from app.config import RECGOV_BASE_URL, RECGOV_HEADERS

# Cache of {facility_id: {division_id: name}} to avoid repeated permitcontent calls
_permit_division_cache: dict[str, dict[str, str]] = {}

log = logging.getLogger(__name__)


def dates_needed(check_in: str, check_out: str) -> list[str]:
    """Return night-dates required for a stay (check-in through night before check-out).

    Args:
        check_in:  ISO date string, e.g. "2026-04-10"
        check_out: ISO date string, e.g. "2026-04-12"

    Returns:
        List of date-time strings in recreation.gov format, e.g.
        ["2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z"]
    """
    start = date.fromisoformat(check_in)
    end = date.fromisoformat(check_out)
    dates = []
    current = start
    while current < end:
        dates.append(f"{current.isoformat()}T00:00:00Z")
        current += timedelta(days=1)
    return dates


def _months_needed(check_in: str, check_out: str) -> list[str]:
    """Return distinct first-of-month strings needed to cover the date range.

    Args:
        check_in:  ISO date string, e.g. "2026-03-30"
        check_out: ISO date string, e.g. "2026-04-02"

    Returns:
        List of first-of-month strings in recreation.gov API format, e.g.
        ["2026-03-01T00:00:00.000Z", "2026-04-01T00:00:00.000Z"]
    """
    start = date.fromisoformat(check_in)
    # check_out is exclusive — last night needed is the day before check_out
    end = date.fromisoformat(check_out) - timedelta(days=1)

    months = []
    current = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)

    while current <= end_month:
        months.append(f"{current.isoformat()}T00:00:00.000Z")
        # Advance to next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return months


def _request(url: str, params: dict, meta: list | None = None, timeout: int = 30):
    """GET with the standard headers, optionally recording request metadata.

    When `meta` is a list, one record is appended per call: url, params, status,
    duration, response size, and any error. It is a LIST rather than a dict
    because one logical check can make several HTTP calls (a cross-month stay
    fetches two months; a permit check may also fetch division metadata).

    Callers that pass nothing, notably monitor.py, are unaffected.
    """
    started = time.monotonic()
    record: dict = {"url": url, "params": dict(params or {})}
    try:
        resp = requests.get(url, headers=RECGOV_HEADERS, params=params, timeout=timeout)
        record["http_status"] = resp.status_code
        record["response_bytes"] = len(resp.content)
        resp.raise_for_status()
        return resp
    except Exception as exc:
        record["error"] = str(exc)
        # A non-2xx still has a body, and that body is the diagnostic.
        body = getattr(getattr(exc, "response", None), "content", None)
        if body is not None:
            record["body"] = body
        raise
    finally:
        record["duration_ms"] = int((time.monotonic() - started) * 1000)
        if meta is not None:
            meta.append(record)


def check_campground(
    facility_id: str, check_in: str, check_out: str, meta: list | None = None
) -> list[dict]:
    """Check a campground for sites available for all nights of the stay.

    Makes one API call per calendar month spanned by the stay. Cross-month stays
    merge availabilities from all month responses before evaluating sites.

    Args:
        facility_id: Recreation.gov facility ID, e.g. "232447"
        check_in:    ISO date string, e.g. "2026-04-10"
        check_out:   ISO date string, e.g. "2026-04-12"

    Returns:
        List of dicts for each fully-available site:
        [{"site_id", "site", "loop", "type", "max_people"}, ...]

    Raises:
        ValueError: if the stay covers no nights (check_out <= check_in).
    """
    needed = dates_needed(check_in, check_out)
    if not needed:
        # Without this guard the availability test below is
        # `all(<empty generator>)`, which is True, so EVERY site would be
        # reported available and the monitor would alert on the whole
        # campground. The API layer rejects this too, but monitor.py calls
        # straight into here with unvalidated module constants.
        raise ValueError(
            f"check_out ({check_out}) must be after check_in ({check_in}); "
            "the stay covers no nights"
        )
    months = _months_needed(check_in, check_out)
    url = f"{RECGOV_BASE_URL}/{facility_id}/month"

    # Collect availabilities keyed by site_id across all months.
    # site_meta holds the static info (site name, loop, etc.) from first encounter.
    site_avail: dict[str, dict[str, str]] = {}
    site_meta: dict[str, dict] = {}

    for month_start in months:
        resp = _request(url, {"start_date": month_start}, meta=meta)
        data = resp.json()

        for site_id, site_info in data.get("campsites", {}).items():
            # Merge availability dates into our running map
            if site_id not in site_avail:
                site_avail[site_id] = {}
                site_meta[site_id] = site_info
            site_avail[site_id].update(site_info.get("availabilities", {}))

    # Evaluate which sites have ALL needed nights as "Available"
    available = []
    for site_id, avail in site_avail.items():
        if all(avail.get(d) == "Available" for d in needed):
            info = site_meta[site_id]
            available.append({
                "site_id": site_id,
                "site": info.get("site", "?"),
                "loop": info.get("loop", "?"),
                "type": info.get("campsite_type", "?"),
                "max_people": info.get("max_num_people", "?"),
            })

    return available


def get_permit_divisions(facility_id: str) -> dict[str, str]:
    """Return division names for a permit facility, using a module-level cache.

    Fetches from the permitcontent API on first call for a given facility_id;
    subsequent calls return the cached result.

    Args:
        facility_id: Recreation.gov permit facility ID, e.g. "445859"

    Returns:
        Dict mapping division_id to division name, e.g.
        {"44585901": "Alder Creek", "44585902": "Aspen Valley"}
    """
    if facility_id in _permit_division_cache:
        return _permit_division_cache[facility_id]

    url = f"https://www.recreation.gov/api/permitcontent/{facility_id}"
    resp = requests.get(url, headers=RECGOV_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    divisions = {
        div_id: div_info.get("name", div_id)
        for div_id, div_info in data.get("payload", {}).get("divisions", {}).items()
    }
    _permit_division_cache[facility_id] = divisions
    return divisions


def check_permit(
    facility_id: str,
    entry_date: str,
    party_size: int = 1,
    division_ids: list[str] | None = None,
    meta: list | None = None,
) -> list[dict]:
    """Check a wilderness permit facility for available divisions on a given date.

    Fetches availability for the full calendar month containing entry_date, then
    filters to divisions with remaining >= party_size for that specific date.

    Args:
        facility_id:  Recreation.gov permit facility ID, e.g. "445859"
        entry_date:   ISO date string for the desired entry date, e.g. "2026-06-15"
        party_size:   Minimum remaining spots required (default 1)
        division_ids: Optional list of division IDs to monitor. If None or empty,
                      all divisions are checked.

    Returns:
        List of dicts for each available division:
        [{"division_id", "division_name", "remaining", "total", "is_walkup"}, ...]
    """
    target = date.fromisoformat(entry_date)
    first_of_month = date(target.year, target.month, 1)
    last_day = calendar.monthrange(target.year, target.month)[1]
    last_of_month = date(target.year, target.month, last_day)

    url = f"https://www.recreation.gov/api/permitinyo/{facility_id}/availabilityv2"
    resp = _request(
        url,
        {
            "start_date": first_of_month.isoformat(),
            "end_date": last_of_month.isoformat(),
            "commercial_acct": "false",
        },
        meta=meta,
    )
    resp.raise_for_status()
    data = resp.json()

    divisions = get_permit_divisions(facility_id)

    date_key = entry_date  # API keys payload by "YYYY-MM-DD"
    day_data = data.get("payload", {}).get(date_key, {})

    available = []
    for division_id, quota in day_data.items():
        if division_ids and division_id not in division_ids:
            continue
        usage = quota.get("quota_usage_by_member_daily", {})
        remaining = usage.get("remaining", 0)
        total = usage.get("total", 0)
        is_walkup = quota.get("is_walkup", False)
        if remaining >= party_size:
            available.append({
                "division_id": division_id,
                "division_name": divisions.get(division_id, division_id),
                "remaining": remaining,
                "total": total,
                "is_walkup": is_walkup,
            })

    return available


def send_ntfy(
    topic: str,
    title: str,
    body: str,
    click_url: str = "",
    priority: str = "urgent",
    tags: str = "tent,warning",
) -> bool:
    """Send a push notification via ntfy.sh.

    Args:
        topic:     ntfy topic name, e.g. "yosemite-camp-stuhw"
        title:     Notification title
        body:      Notification body text
        click_url: Optional URL opened when notification is tapped
        priority:  ntfy priority level (default "urgent")
        tags:      Comma-separated ntfy tag string (default "tent,warning")

    Returns:
        True on success, False on failure.
    """
    try:
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": tags,
        }
        if click_url:
            headers["Click"] = click_url

        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("ntfy notification sent to topic '%s'", topic)
        return True
    except Exception as exc:
        log.error("Failed to send ntfy notification: %s", exc)
        return False


SMTP_TIMEOUT_SECONDS = 20


def send_email(to: str, subject: str, body: str, smtp_config: dict) -> bool:
    """Send a notification email via SMTP with STARTTLS.

    Args:
        to:          Recipient address
        subject:     Email subject line
        body:        Plain-text email body
        smtp_config: Dict with keys: server, port, from_addr, password

    Returns:
        True on success, False on failure.
    """
    from_addr = smtp_config["from_addr"]

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    try:
        # An explicit timeout is essential: smtplib defaults to
        # socket._GLOBAL_DEFAULT_TIMEOUT (block forever), and nothing here calls
        # socket.setdefaulttimeout. A hung SMTP host would otherwise stall the
        # caller indefinitely.
        with smtplib.SMTP(
            smtp_config["server"], smtp_config["port"], timeout=SMTP_TIMEOUT_SECONDS
        ) as server:
            server.starttls()
            server.login(from_addr, smtp_config["password"])
            server.sendmail(from_addr, [to], msg.as_string())
        log.info("Email sent to %s", to)
        return True
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        return False
