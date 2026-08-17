"""Tests for app.ridb — RIDBCatalog class."""
import pytest
from app.ridb import RIDBCatalog


# ── Fixture shortcut ───────────────────────────────────────────────────────────

@pytest.fixture
def catalog(sample_ridb_dir):
    """Return a loaded RIDBCatalog from sample data."""
    return RIDBCatalog(sample_ridb_dir)


# ── Load-time filtering ────────────────────────────────────────────────────────

def test_load_filters_to_reservable_enabled(catalog):
    """Only Reservable+Enabled facilities with allowed types survive load."""
    # 4 survive: UPPER PINES (campground), LOWER PINES (campground),
    # HALF DOME PERMIT (permit), WILDERNESS PERMIT (permit, USFS-owned)
    # — all reservable+enabled
    # Excluded: WALK-IN ONLY (not reservable), CLOSED CAMP (not enabled),
    # VALLEY VISITOR CENTER (wrong type)
    assert len(catalog._facilities) == 4
    surviving_ids = {f["FacilityID"] for f in catalog._facilities.values()}
    assert "232450" in surviving_ids   # UPPER PINES
    assert "232447" in surviving_ids   # LOWER PINES
    assert "999003" in surviving_ids   # HALF DOME PERMIT
    assert "999001" not in surviving_ids  # not reservable
    assert "999002" not in surviving_ids  # not enabled
    assert "999004" not in surviving_ids  # wrong type


# ── get_states ─────────────────────────────────────────────────────────────────

def test_get_states(catalog):
    """get_states returns sorted list; 'Other' sorts last."""
    states = catalog.get_states()
    # CA has 2 campgrounds (UPPER/LOWER PINES with addresses)
    # HALF DOME PERMIT has no address → "Other"
    assert isinstance(states, list)
    codes = [s["code"] for s in states]
    assert "CA" in codes
    assert "Other" in codes
    # Other must sort last
    assert codes[-1] == "Other"
    # Each entry has code and count
    # UPPER PINES, LOWER PINES, WILDERNESS PERMIT (HALF DOME PERMIT has no
    # address row, so it falls through to "Other")
    ca = next(s for s in states if s["code"] == "CA")
    assert ca["count"] == 3
    other = next(s for s in states if s["code"] == "Other")
    assert other["count"] == 1


# ── get_agencies ───────────────────────────────────────────────────────────────

def test_get_agencies_by_state(catalog):
    """get_agencies returns agencies that have facilities in that state."""
    agencies = catalog.get_agencies("CA")
    # NPS (owns the campgrounds) and USFS (owns only a permit). Sorted by name.
    assert len(agencies) == 2
    nps = agencies[0]
    assert nps["id"] == "128"
    assert nps["name"] == "National Park Service"
    assert nps["abbrev"] == "NPS"


def test_get_agencies_unknown_state_returns_empty(catalog):
    """get_agencies returns empty list for unknown state."""
    assert catalog.get_agencies("ZZ") == []


# ── get_rec_areas ──────────────────────────────────────────────────────────────

def test_get_rec_areas(catalog):
    """get_rec_areas returns rec areas with facility counts for state+org."""
    rec_areas = catalog.get_rec_areas("CA", "128")
    assert len(rec_areas) == 1
    ra = rec_areas[0]
    assert ra["id"] == "2991"
    assert ra["name"] == "Yosemite National Park"
    # Only the 2 CA campgrounds are linked (UPPER PINES + LOWER PINES)
    assert ra["facility_count"] == 2


# ── get_facilities ─────────────────────────────────────────────────────────────

def test_get_facilities_by_rec_area(catalog):
    """get_facilities returns all facilities for a rec area when no type filter."""
    facilities = catalog.get_facilities("2991")
    fac_ids = {f["id"] for f in facilities}
    # All 3 surviving facilities are linked to rec area 2991
    assert "232450" in fac_ids
    assert "232447" in fac_ids
    assert "999003" in fac_ids
    # Each entry has required fields
    for f in facilities:
        assert "id" in f
        assert "name" in f
        assert "type" in f
        assert "lon" in f
        assert "lat" in f


def test_get_facilities_filtered_by_type(catalog):
    """get_facilities filters by facility_type when provided."""
    campgrounds = catalog.get_facilities("2991", facility_type="Campground")
    camp_ids = {f["id"] for f in campgrounds}
    assert "232450" in camp_ids
    assert "232447" in camp_ids
    assert "999003" not in camp_ids  # Permit, not Campground

    permits = catalog.get_facilities("2991", facility_type="Permit")
    permit_ids = {f["id"] for f in permits}
    assert "999003" in permit_ids
    assert "232450" not in permit_ids


# ── search_rec_areas ───────────────────────────────────────────────────────────

def test_search_rec_areas(catalog):
    """search_rec_areas does case-insensitive substring match on name."""
    results = catalog.search_rec_areas("CA", "128", "yosemite")
    assert len(results) == 1
    assert results[0]["name"] == "Yosemite National Park"

    # Case insensitive
    results_upper = catalog.search_rec_areas("CA", "128", "YOSEMITE")
    assert len(results_upper) == 1

    # No match
    no_match = catalog.search_rec_areas("CA", "128", "yellowstone")
    assert no_match == []


# ── unknown_state_bucket ───────────────────────────────────────────────────────

def test_unknown_state_bucket(catalog):
    """Facilities with no address row are bucketed under 'Other'."""
    # HALF DOME PERMIT (999003) has no address entry
    agencies_other = catalog.get_agencies("Other")
    # 999003 has no OrgEntity mapping either, so no agency
    # The "Other" bucket still appears in states though
    states = catalog.get_states()
    other = next((s for s in states if s["code"] == "Other"), None)
    assert other is not None
    assert other["count"] == 1


def test_get_agencies_includes_permit_only_orgs(catalog):
    """An agency whose only facility is a permit must still be reachable.

    _FACILITY_ENTITY_TYPES omitted "Permit", so permit facilities never picked
    up an org id, and get_agencies skips anything with a falsy _org_id. The
    result: permits were unreachable through the wizard's
    state -> agency -> park -> facility funnel, which is the headline feature.
    """
    ids = {a["id"] for a in catalog.get_agencies("CA")}
    assert "999" in ids, (
        "an org that owns only a permit is missing from get_agencies, so its "
        "permits cannot be reached in the wizard"
    )


def test_permit_facility_gets_an_org_id(catalog):
    """The underlying join, asserted directly."""
    assert catalog._facilities["999005"].get("_org_id") == "999"


# ── Global park search ─────────────────────────────────────────────────────────
#
# search_rec_areas() only filters WITHIN an already drilled-down state+org.
# Permits in particular are unreachable that way when they have no address row
# (87 of 149 in the real export land in state "Other"), so search must work
# without a state or an agency.

def test_search_all_finds_a_park_by_name_fragment(catalog):
    hits = catalog.search_all_rec_areas("yosem")
    assert any(h["name"] == "Yosemite National Park" for h in hits)


def test_search_all_is_case_insensitive(catalog):
    assert catalog.search_all_rec_areas("YOSEMITE")
    assert catalog.search_all_rec_areas("yosemite")


def test_search_all_matches_keywords(catalog):
    """Keywords let a landmark find its park."""
    hits = catalog.search_all_rec_areas("national park")
    assert any(h["name"] == "Yosemite National Park" for h in hits)


def test_search_all_reports_states_for_disambiguation(catalog):
    """RecArea rows carry no state column, so it comes from the facilities."""
    hit = catalog.search_all_rec_areas("yosemite")[0]
    assert "CA" in hit["states"]


def test_search_all_includes_facility_count(catalog):
    hit = catalog.search_all_rec_areas("yosemite")[0]
    assert hit["facility_count"] >= 1


def test_search_all_returns_nothing_for_no_match(catalog):
    assert catalog.search_all_rec_areas("zzzznotapark") == []


def test_search_all_respects_the_limit(catalog):
    assert len(catalog.search_all_rec_areas("a", limit=1)) <= 1


def test_search_all_ignores_blank_queries(catalog):
    """A blank query must not dump the entire catalog into the page."""
    assert catalog.search_all_rec_areas("") == []
    assert catalog.search_all_rec_areas("   ") == []
