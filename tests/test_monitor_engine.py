"""Tests for app.monitor_engine — TDD for check/notify logic."""
import smtplib
from unittest.mock import MagicMock, patch

import pytest
import requests

import app.monitor_engine as engine
from app.monitor_engine import (
    _months_needed,
    _permit_division_cache,
    check_campground,
    check_permit,
    dates_needed,
    get_permit_divisions,
    send_email,
    send_ntfy,
)


# ─────────────────────────────────────────────────────────────
# dates_needed
# ─────────────────────────────────────────────────────────────

def test_dates_needed_single_night():
    """Single-night stay returns one date."""
    result = dates_needed("2026-04-10", "2026-04-11")
    assert result == ["2026-04-10T00:00:00Z"]


def test_dates_needed_two_nights():
    """Two-night stay returns two dates."""
    result = dates_needed("2026-04-10", "2026-04-12")
    assert result == [
        "2026-04-10T00:00:00Z",
        "2026-04-11T00:00:00Z",
    ]


def test_dates_needed_cross_month():
    """Stay spanning month boundary returns all nights in correct format."""
    result = dates_needed("2026-03-30", "2026-04-02")
    assert result == [
        "2026-03-30T00:00:00Z",
        "2026-03-31T00:00:00Z",
        "2026-04-01T00:00:00Z",
    ]


# ─────────────────────────────────────────────────────────────
# _months_needed
# ─────────────────────────────────────────────────────────────

def test_months_needed_single_month():
    """Stay within one month returns one first-of-month."""
    result = _months_needed("2026-04-10", "2026-04-12")
    assert result == ["2026-04-01T00:00:00.000Z"]


def test_months_needed_cross_month():
    """Stay spanning two months returns both first-of-month values."""
    result = _months_needed("2026-03-30", "2026-04-02")
    assert result == [
        "2026-03-01T00:00:00.000Z",
        "2026-04-01T00:00:00.000Z",
    ]


# ─────────────────────────────────────────────────────────────
# check_campground
# ─────────────────────────────────────────────────────────────

def _make_api_response(site_availabilities: dict) -> dict:
    """Build a minimal recreation.gov API response dict."""
    campsites = {}
    for site_id, avail_map in site_availabilities.items():
        campsites[site_id] = {
            "availabilities": avail_map,
            "site": site_id,
            "loop": "UPPER LOOP",
            "campsite_type": "STANDARD NONELECTRIC",
            "max_num_people": 6,
        }
    return {"campsites": campsites}


def test_check_campground_rejects_zero_night_stay():
    """A zero-night stay must raise, not report the whole campground available.

    dates_needed() returns [] when check_out <= check_in, and `all([])` is True,
    so the availability test at the heart of check_campground would pass for
    every single site. The API layer validates this too, but monitor.py (the
    CLI) calls this function directly with unvalidated module constants, so the
    guard has to live here as well.
    """
    with patch("app.monitor_engine.requests.get") as mock_get:
        with pytest.raises(ValueError):
            check_campground("232447", "2026-04-10", "2026-04-10")

    assert not mock_get.called, "must fail before issuing a request"


def test_check_campground_rejects_reversed_dates():
    with patch("app.monitor_engine.requests.get") as mock_get:
        with pytest.raises(ValueError):
            check_campground("232447", "2026-04-12", "2026-04-10")

    assert not mock_get.called


def test_check_campground_available():
    """Returns matching sites when all needed nights are Available."""
    avail = {
        "2026-04-10T00:00:00Z": "Available",
        "2026-04-11T00:00:00Z": "Available",
    }
    api_resp = _make_api_response({"site-001": avail})

    mock_resp = MagicMock()
    mock_resp.json.return_value = api_resp

    with patch("app.monitor_engine.requests.get", return_value=mock_resp) as mock_get:
        result = check_campground("232447", "2026-04-10", "2026-04-12")

    assert len(result) == 1
    assert result[0]["site_id"] == "site-001"
    assert result[0]["site"] == "site-001"
    assert result[0]["loop"] == "UPPER LOOP"
    assert result[0]["type"] == "STANDARD NONELECTRIC"
    assert result[0]["max_people"] == 6
    # One API call (single month)
    assert mock_get.call_count == 1


def test_check_campground_none_available():
    """Returns empty list when no sites have all nights Available."""
    avail = {
        "2026-04-10T00:00:00Z": "Reserved",
        "2026-04-11T00:00:00Z": "Available",
    }
    api_resp = _make_api_response({"site-001": avail})

    mock_resp = MagicMock()
    mock_resp.json.return_value = api_resp

    with patch("app.monitor_engine.requests.get", return_value=mock_resp):
        result = check_campground("232447", "2026-04-10", "2026-04-12")

    assert result == []


def test_check_campground_cross_month():
    """Cross-month stay makes two API calls and merges availabilities."""
    # March response has March nights; April response has April nights
    march_avail = {
        "2026-03-30T00:00:00Z": "Available",
        "2026-03-31T00:00:00Z": "Available",
    }
    april_avail = {
        "2026-04-01T00:00:00Z": "Available",
    }

    march_resp = MagicMock()
    march_resp.json.return_value = _make_api_response({"site-001": march_avail})

    april_resp = MagicMock()
    april_resp.json.return_value = _make_api_response({"site-001": april_avail})

    with patch(
        "app.monitor_engine.requests.get", side_effect=[march_resp, april_resp]
    ) as mock_get:
        result = check_campground("232447", "2026-03-30", "2026-04-02")

    # Two API calls (one per month)
    assert mock_get.call_count == 2
    # Site available across both months
    assert len(result) == 1
    assert result[0]["site_id"] == "site-001"


def test_check_campground_cross_month_partial_unavailable():
    """Cross-month stay excludes a site that's not available on all nights."""
    march_avail = {
        "2026-03-30T00:00:00Z": "Available",
        "2026-03-31T00:00:00Z": "Reserved",   # blocked in March
    }
    april_avail = {
        "2026-04-01T00:00:00Z": "Available",
    }

    march_resp = MagicMock()
    march_resp.json.return_value = _make_api_response({"site-001": march_avail})

    april_resp = MagicMock()
    april_resp.json.return_value = _make_api_response({"site-001": april_avail})

    with patch("app.monitor_engine.requests.get", side_effect=[march_resp, april_resp]):
        result = check_campground("232447", "2026-03-30", "2026-04-02")

    assert result == []


# ─────────────────────────────────────────────────────────────
# send_ntfy
# ─────────────────────────────────────────────────────────────

def test_send_ntfy_success():
    """Returns True when ntfy POST succeeds."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None

    with patch("app.monitor_engine.requests.post", return_value=mock_resp) as mock_post:
        result = send_ntfy(
            topic="test-topic",
            title="Test Alert",
            body="Site available",
            click_url="https://www.recreation.gov/camping/campgrounds/123",
        )

    assert result is True
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "https://ntfy.sh/test-topic" in call_kwargs[0]
    headers_sent = call_kwargs[1]["headers"]
    assert headers_sent["Title"] == "Test Alert"
    assert headers_sent["Priority"] == "urgent"
    assert "Click" in headers_sent


def test_send_ntfy_failure():
    """Returns False when ntfy POST raises an exception."""
    with patch(
        "app.monitor_engine.requests.post",
        side_effect=requests.exceptions.ConnectionError("no network"),
    ):
        result = send_ntfy(
            topic="test-topic",
            title="Test Alert",
            body="Site available",
        )

    assert result is False


# ─────────────────────────────────────────────────────────────
# send_email
# ─────────────────────────────────────────────────────────────

SMTP_CONFIG = {
    "server": "smtp.gmail.com",
    "port": 587,
    "from_addr": "from@example.com",
    "password": "secret",
}


def test_send_email_success():
    """Returns True when SMTP send succeeds."""
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with patch("app.monitor_engine.smtplib.SMTP", return_value=mock_smtp):
        result = send_email(
            to="to@example.com",
            subject="Campsite Alert",
            body="Site A is available",
            smtp_config=SMTP_CONFIG,
        )

    assert result is True
    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once_with("from@example.com", "secret")
    mock_smtp.sendmail.assert_called_once()


def test_send_email_failure():
    """Returns False when SMTP raises an exception."""
    with patch(
        "app.monitor_engine.smtplib.SMTP",
        side_effect=smtplib.SMTPException("connection refused"),
    ):
        result = send_email(
            to="to@example.com",
            subject="Campsite Alert",
            body="Site A is available",
            smtp_config=SMTP_CONFIG,
        )

    assert result is False


# ─────────────────────────────────────────────────────────────
# get_permit_divisions
# ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_permit_cache():
    """Clear the permit division cache before each test to avoid cross-test pollution."""
    engine._permit_division_cache.clear()
    yield
    engine._permit_division_cache.clear()


def _make_permitcontent_response(divisions: dict) -> dict:
    """Build a minimal permitcontent API response dict."""
    return {
        "payload": {
            "name": "Test Wilderness Permits",
            "divisions": {
                div_id: {"name": name, "type": "Entry Point"}
                for div_id, name in divisions.items()
            },
        }
    }


def test_get_permit_divisions_returns_id_name_map():
    """Returns a dict mapping division IDs to names."""
    api_resp = _make_permitcontent_response({
        "44585901": "Alder Creek",
        "44585902": "Aspen Valley",
    })
    mock_resp = MagicMock()
    mock_resp.json.return_value = api_resp

    with patch("app.monitor_engine.requests.get", return_value=mock_resp):
        result = get_permit_divisions("445859")

    assert result == {"44585901": "Alder Creek", "44585902": "Aspen Valley"}


def test_get_permit_divisions_caches_result():
    """Second call for the same facility_id does not make another HTTP request."""
    api_resp = _make_permitcontent_response({"44585901": "Alder Creek"})
    mock_resp = MagicMock()
    mock_resp.json.return_value = api_resp

    with patch("app.monitor_engine.requests.get", return_value=mock_resp) as mock_get:
        get_permit_divisions("445859")
        get_permit_divisions("445859")

    assert mock_get.call_count == 1


def test_get_permit_divisions_caches_per_facility():
    """Different facility IDs each make their own HTTP request."""
    resp_a = MagicMock()
    resp_a.json.return_value = _make_permitcontent_response({"111": "Trail A"})
    resp_b = MagicMock()
    resp_b.json.return_value = _make_permitcontent_response({"222": "Trail B"})

    with patch(
        "app.monitor_engine.requests.get", side_effect=[resp_a, resp_b]
    ) as mock_get:
        result_a = get_permit_divisions("111111")
        result_b = get_permit_divisions("222222")

    assert mock_get.call_count == 2
    assert result_a == {"111": "Trail A"}
    assert result_b == {"222": "Trail B"}


# ─────────────────────────────────────────────────────────────
# check_permit
# ─────────────────────────────────────────────────────────────

def _make_permit_availability_response(date_key: str, divisions: dict) -> dict:
    """Build a minimal permitinyo availability API response dict.

    Args:
        date_key:  ISO date string, e.g. "2026-06-15"
        divisions: {division_id: {"total": N, "remaining": M, "is_walkup": bool}}
    """
    day_data = {}
    for div_id, quota in divisions.items():
        day_data[div_id] = {
            "quota_usage_by_member_daily": {
                "total": quota["total"],
                "remaining": quota["remaining"],
            },
            "is_walkup": quota.get("is_walkup", False),
        }
    return {"payload": {date_key: day_data}}


def test_check_permit_available():
    """Returns divisions with remaining >= party_size."""
    avail_resp = _make_permit_availability_response("2026-06-15", {
        "44585901": {"total": 18, "remaining": 12, "is_walkup": False},
        "44585902": {"total": 10, "remaining": 3, "is_walkup": False},
    })
    content_resp = _make_permitcontent_response({
        "44585901": "Alder Creek",
        "44585902": "Aspen Valley",
    })

    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp
    content_mock = MagicMock()
    content_mock.json.return_value = content_resp

    # First call is permitinyo (availability), second is permitcontent (divisions)
    with patch(
        "app.monitor_engine.requests.get", side_effect=[avail_mock, content_mock]
    ):
        result = check_permit("445859", "2026-06-15", party_size=1)

    assert len(result) == 2
    ids = {r["division_id"] for r in result}
    assert ids == {"44585901", "44585902"}

    alder = next(r for r in result if r["division_id"] == "44585901")
    assert alder["division_name"] == "Alder Creek"
    assert alder["remaining"] == 12
    assert alder["total"] == 18
    assert alder["is_walkup"] is False


def test_check_permit_filters_by_party_size():
    """Excludes divisions where remaining < party_size."""
    avail_resp = _make_permit_availability_response("2026-06-15", {
        "44585901": {"total": 18, "remaining": 12},
        "44585902": {"total": 10, "remaining": 1},
    })
    content_resp = _make_permitcontent_response({
        "44585901": "Alder Creek",
        "44585902": "Aspen Valley",
    })

    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp
    content_mock = MagicMock()
    content_mock.json.return_value = content_resp

    with patch(
        "app.monitor_engine.requests.get", side_effect=[avail_mock, content_mock]
    ):
        result = check_permit("445859", "2026-06-15", party_size=5)

    assert len(result) == 1
    assert result[0]["division_id"] == "44585901"


def test_check_permit_none_available():
    """Returns empty list when no divisions meet the party_size threshold."""
    avail_resp = _make_permit_availability_response("2026-06-15", {
        "44585901": {"total": 18, "remaining": 0},
    })
    content_resp = _make_permitcontent_response({"44585901": "Alder Creek"})

    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp
    content_mock = MagicMock()
    content_mock.json.return_value = content_resp

    with patch(
        "app.monitor_engine.requests.get", side_effect=[avail_mock, content_mock]
    ):
        result = check_permit("445859", "2026-06-15", party_size=1)

    assert result == []


def test_check_permit_uses_correct_month_range():
    """Availability API is called with first and last day of the entry date's month."""
    avail_resp = _make_permit_availability_response("2026-06-15", {})
    content_resp = _make_permitcontent_response({})

    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp
    content_mock = MagicMock()
    content_mock.json.return_value = content_resp

    with patch(
        "app.monitor_engine.requests.get", side_effect=[avail_mock, content_mock]
    ) as mock_get:
        check_permit("445859", "2026-06-15")

    avail_call_kwargs = mock_get.call_args_list[0][1]
    params = avail_call_kwargs["params"]
    assert params["start_date"] == "2026-06-01"
    assert params["end_date"] == "2026-06-30"
    assert params["commercial_acct"] == "false"


def test_check_permit_month_range_february_leap_year():
    """Correctly computes last day of February in a leap year."""
    avail_resp = _make_permit_availability_response("2028-02-15", {})
    content_resp = _make_permitcontent_response({})

    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp
    content_mock = MagicMock()
    content_mock.json.return_value = content_resp

    with patch(
        "app.monitor_engine.requests.get", side_effect=[avail_mock, content_mock]
    ) as mock_get:
        check_permit("445859", "2028-02-15")

    params = mock_get.call_args_list[0][1]["params"]
    assert params["end_date"] == "2028-02-29"


def test_check_permit_uses_cached_divisions():
    """check_permit uses cached divisions and does not re-fetch permitcontent."""
    # Pre-populate cache
    engine._permit_division_cache["445859"] = {"44585901": "Cached Trail"}

    avail_resp = _make_permit_availability_response("2026-06-15", {
        "44585901": {"total": 5, "remaining": 2},
    })
    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp

    with patch("app.monitor_engine.requests.get", return_value=avail_mock) as mock_get:
        result = check_permit("445859", "2026-06-15", party_size=1)

    # Only one HTTP call (availability); permitcontent not fetched due to cache
    assert mock_get.call_count == 1
    assert result[0]["division_name"] == "Cached Trail"


def test_check_permit_filters_by_division_ids():
    """check_permit only returns divisions matching the division_ids filter."""
    engine._permit_division_cache["445859"] = {
        "44585901": "Alder Creek",
        "44585902": "Aspen Valley",
        "44585903": "Base Line Camp Road",
    }

    avail_resp = _make_permit_availability_response("2026-06-15", {
        "44585901": {"total": 18, "remaining": 12},
        "44585902": {"total": 2, "remaining": 2},
        "44585903": {"total": 15, "remaining": 10},
    })
    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp

    with patch("app.monitor_engine.requests.get", return_value=avail_mock):
        result = check_permit("445859", "2026-06-15", party_size=1,
                              division_ids=["44585901", "44585903"])

    assert len(result) == 2
    names = {r["division_name"] for r in result}
    assert names == {"Alder Creek", "Base Line Camp Road"}


def test_check_permit_empty_division_ids_returns_all():
    """check_permit with division_ids=[] returns all available divisions."""
    engine._permit_division_cache["445859"] = {
        "44585901": "Alder Creek",
        "44585902": "Aspen Valley",
    }

    avail_resp = _make_permit_availability_response("2026-06-15", {
        "44585901": {"total": 18, "remaining": 12},
        "44585902": {"total": 2, "remaining": 2},
    })
    avail_mock = MagicMock()
    avail_mock.json.return_value = avail_resp

    with patch("app.monitor_engine.requests.get", return_value=avail_mock):
        result = check_permit("445859", "2026-06-15", party_size=1,
                              division_ids=[])

    assert len(result) == 2


def test_send_email_passes_a_socket_timeout():
    """smtplib.SMTP blocks forever without an explicit timeout.

    The default is socket._GLOBAL_DEFAULT_TIMEOUT and nothing calls
    socket.setdefaulttimeout, so a hung SMTP host would stall the caller
    indefinitely.
    """
    with patch("app.monitor_engine.smtplib.SMTP") as mock_smtp:
        send_email("a@b.c", "subj", "body", {
            "server": "smtp.example.com", "port": 587,
            "from_addr": "x@y.z", "password": "pw",
        })

    assert mock_smtp.called
    assert "timeout" in mock_smtp.call_args.kwargs, (
        "smtplib.SMTP called without timeout=; a hung host blocks forever"
    )
    assert mock_smtp.call_args.kwargs["timeout"] > 0
