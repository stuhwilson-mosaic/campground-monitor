"""Shared pytest fixtures for campground-monitor tests."""
import csv
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest


# ── Live-data guard ───────────────────────────────────────────────────────────
#
# data/monitors.json is rewritten by the running container every 12-18 seconds.
# A test run pointed at it would race those writes, and app/config.py defaults
# DATA_DIR to "./data", so simply forgetting to set it aims the whole suite at
# live state. That is not hypothetical: during the May 2026 multi-user rollout
# an unguarded run seeded the real data/users.json with the default password.

LIVE_DATA_DIR = (Path(__file__).resolve().parent.parent / "data").resolve()


def resolves_to_live_data_dir(path) -> bool:
    """True if `path` would land on the repo's real data/ directory.

    An empty or missing value counts as live, because config.DATA_DIR then
    falls back to "./data", which resolves against the repo root in a normal run.
    """
    if not path:
        return True
    try:
        return Path(path).resolve() == LIVE_DATA_DIR
    except (OSError, ValueError):
        return False


def pytest_configure(config):
    """Guarantee DATA_DIR is safe before a single test is collected."""
    explicit = os.environ.get("DATA_DIR")
    if not explicit:
        # Unset would fall back to the live ./data. Redirect somewhere harmless
        # rather than failing, so a bare `pytest` run is safe by default.
        os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cm-test-data-")
        return
    if resolves_to_live_data_dir(explicit):
        raise pytest.UsageError(
            f"DATA_DIR points at the live data directory ({LIVE_DATA_DIR}). "
            "The test suite writes monitors.json and users.json and would race "
            "the running container. Set DATA_DIR to a scratch directory, or "
            "unset it entirely to get an automatic temp directory."
        )


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Return a string path to a temporary data directory."""
    d = tmp_path / "data"
    d.mkdir()
    return str(d)


@pytest.fixture
def sample_monitor():
    """Return a sample monitor config dict with all fields populated.

    Dates are relative to today on purpose. Hardcoded dates turn this into a
    time bomb: once the window falls into the past, `_trip_window_expired()`
    fires on the first poll-loop iteration and the monitor auto-stops before
    any check runs, so every test that asserts a check happened fails at once.
    That is exactly what happened to five tests here in 2026-08.

    Tests that deliberately exercise auto-stop override these with past dates.
    """
    check_in = date.today() + timedelta(days=30)
    return {
        "id": "test-monitor-001",
        "owner": "stu",
        "name": "Test Yosemite Monitor",
        "facility_id": "232450",
        "facility_name": "UPPER PINES",
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=3)).isoformat(),
        "interval": 5,
        "enable_email": False,
        "enable_ntfy": True,
        "ntfy_topic": "yosemite-camp-test",
        "email_to": "",
        "active": True,
        "created_at": "2026-03-19T00:00:00Z",
    }


@pytest.fixture
def sample_ridb_dir(tmp_path):
    """
    Create a minimal RIDB CSV directory with test data mirroring real RIDB structure.

    Contains 6 facilities:
      - 2 valid campgrounds (Reservable=True, Enabled=True, FacilityTypeDescription=Campground)
      - 1 non-reservable campground (Reservable=False)
      - 1 disabled campground (Enabled=False)
      - 1 permit-only facility (FacilityTypeDescription=Permit)
      - 1 visitor center (FacilityTypeDescription=Visitor Center)

    Returns the str path to the directory.
    """
    ridb = tmp_path / "RIDBFullExport_V1_CSV"
    ridb.mkdir()

    # ── Facilities.csv ────────────────────────────────────────────────────────
    facility_headers = [
        "FacilityID", "LegacyFacilityID", "OrgFacilityID", "ParentOrgID",
        "ParentRecAreaID", "FacilityName", "FacilityDescription",
        "FacilityTypeDescription", "FacilityUseFeeDescription",
        "FacilityDirections", "FacilityPhone", "FacilityEmail",
        "FacilityReservationURL", "FacilityMapURL", "FacilityAdaAccess",
        "FacilityAccessibilityText", "FacilityLongitude", "FacilityLatitude",
        "Keywords", "StayLimit", "Reservable", "Enabled", "LastUpdatedDate",
    ]
    facilities = [
        # Valid campground 1
        ["232450", "70926", "70926", "128", "2991", "UPPER PINES",
         "Upper Pines Campground in Yosemite Valley.", "Campground", "",
         "Drive to Yosemite Valley.", "209-372-0200", "",
         "https://www.recreation.gov/camping/campgrounds/232450", "", "Yes", "",
         "-119.5593", "37.7388", "camping,yosemite", "7 nights", "True", "True",
         "2024-01-01"],
        # Valid campground 2
        ["232447", "70928", "70928", "128", "2991", "LOWER PINES",
         "Lower Pines Campground in Yosemite Valley.", "Campground", "",
         "Drive to Yosemite Valley.", "209-372-0200", "",
         "https://www.recreation.gov/camping/campgrounds/232447", "", "Yes", "",
         "-119.5572", "37.7381", "camping,yosemite", "7 nights", "True", "True",
         "2024-01-01"],
        # Non-reservable campground (should be excluded by reservable filter)
        ["999001", "", "", "128", "2991", "WALK-IN ONLY",
         "Walk-in only, no reservations.", "Campground", "", "", "", "", "", "",
         "No", "", "-119.600", "37.740", "", "", "False", "True", "2024-01-01"],
        # Disabled campground (should be excluded by enabled filter)
        ["999002", "", "", "128", "2991", "CLOSED CAMP",
         "Closed for renovation.", "Campground", "", "", "", "", "", "", "No",
         "", "-119.601", "37.741", "", "", "True", "False", "2024-01-01"],
        # Permit facility (should be excluded by type filter)
        ["999003", "", "", "128", "2991", "HALF DOME PERMIT",
         "Half Dome cables permit.", "Permit", "", "", "", "", "", "", "No", "",
         "-119.531", "37.746", "", "", "True", "True", "2024-01-01"],
        # Visitor center (should be excluded by type filter)
        ["999004", "", "", "128", "2991", "VALLEY VISITOR CENTER",
         "Yosemite Valley Visitor Center.", "Visitor Center", "", "", "", "", "",
         "", "No", "", "-119.598", "37.748", "", "", "False", "True",
         "2024-01-01"],
        # Reservable permit in CA belonging to an org with NO campgrounds.
        # This is the only way to catch permits being dropped from the agency
        # join: any org that also owns a campground would show up regardless.
        ["999005", "", "", "999", "2991", "WILDERNESS PERMIT",
         "Backcountry wilderness permit.", "Permit", "", "", "", "", "", "",
         "No", "", "-119.540", "37.750", "", "", "True", "True", "2024-01-01"],
    ]
    _write_csv(ridb / "Facilities_API_v1.csv", facility_headers, facilities)

    # ── FacilityAddresses.csv ─────────────────────────────────────────────────
    addr_headers = [
        "FacilityAddressID", "FacilityID", "FacilityAddressType",
        "FacilityStreetAddress1", "FacilityStreetAddress2",
        "FacilityStreetAddress3", "City", "AddressStateCode", "PostalCode",
        "AddressCountryCode", "LastUpdatedDate",
    ]
    addresses = [
        ["10001", "232450", "Default", "Yosemite Valley", "", "", "Yosemite Village", "CA", "95389", "USA", "2024-01-01"],
        ["10002", "232447", "Default", "Yosemite Valley", "", "", "Yosemite Village", "CA", "95389", "USA", "2024-01-01"],
        ["10003", "999005", "Default", "Wilderness Office", "", "", "Yosemite Village", "CA", "95389", "USA", "2024-01-01"],
    ]
    _write_csv(ridb / "FacilityAddresses_API_v1.csv", addr_headers, addresses)

    # ── RecAreas.csv ──────────────────────────────────────────────────────────
    recarea_headers = [
        "RecAreaID", "OrgRecAreaID", "ParentOrgID", "RecAreaName",
        "RecAreaDescription", "RecAreaUseFeeDescription", "RecAreaDirections",
        "RecAreaPhone", "RecAreaEmail", "RecAreaReservationURL",
        "RecAreaMapURL", "RecAreaLongitude", "RecAreaLatitude", "StayLimit",
        "Keywords", "Reservable", "Enabled", "LastUpdatedDate",
    ]
    recareas = [
        ["2991", "", "128", "Yosemite National Park", "Yosemite NP.", "", "",
         "209-372-0200", "", "https://www.recreation.gov", "", "-119.538", "37.865",
         "", "yosemite,national park", "True", "True", "2024-01-01"],
    ]
    _write_csv(ridb / "RecAreas_API_v1.csv", recarea_headers, recareas)

    # ── RecAreaFacilities.csv ─────────────────────────────────────────────────
    raf_headers = ["RecAreaID", "FacilityID"]
    raf_rows = [
        ["2991", "232450"],
        ["2991", "232447"],
        ["2991", "999001"],
        ["2991", "999002"],
        ["2991", "999003"],
        ["2991", "999004"],
        ["2991", "999005"],
    ]
    _write_csv(ridb / "RecAreaFacilities_API_v1.csv", raf_headers, raf_rows)

    # ── OrgEntities.csv ───────────────────────────────────────────────────────
    orgentity_headers = ["EntityID", "OrgID", "EntityType"]
    orgentity_rows = [
        ["2991", "128", "RecArea"],
        ["232450", "128", "Facility"],
        ["232447", "128", "Facility"],
        # EntityType "Permit" — real RIDB data uses this for permit facilities,
        # and ridb.py's entity-type allowlist used to omit it, so every permit
        # lost its agency and vanished from the wizard's drill-down.
        ["999005", "999", "Permit"],
    ]
    _write_csv(ridb / "OrgEntities_API_v1.csv", orgentity_headers, orgentity_rows)

    # ── Organizations.csv ─────────────────────────────────────────────────────
    org_headers = [
        "OrgID", "OrgType", "OrgName", "OrgImageURL", "OrgURLText",
        "OrgURLAddress", "OrgAbbrevName", "OrgJurisdictionType", "OrgParentID",
        "LastUpdatedDate",
    ]
    org_rows = [
        ["128", "Federal", "National Park Service", "", "NPS", "https://www.nps.gov",
         "NPS", "Federal", "0", "2024-01-01"],
        # Owns only the permit above, never a campground.
        ["999", "Federal", "US Forest Service", "", "USFS", "https://www.fs.usda.gov",
         "USFS", "Federal", "0", "2024-01-01"],
    ]
    _write_csv(ridb / "Organizations_API_v1.csv", org_headers, org_rows)

    return str(ridb)


def _write_csv(path, headers, rows):
    """Write a CSV file with the given headers and rows."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
