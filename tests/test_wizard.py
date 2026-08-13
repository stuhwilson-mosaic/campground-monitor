"""Tests for wizard routes and HTMX filter endpoints (Task 8)."""
import csv
import importlib
import os

import pytest
from fastapi.testclient import TestClient


# ── Wizard-specific RIDB fixture ───────────────────────────────────────────────

def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


@pytest.fixture
def wizard_ridb_dir(tmp_path):
    """
    Minimal RIDB data for wizard tests.

    Rec area 10 "Test National Park" (state CA, org 128 = NPS) contains:
      - 11 Pine Camp     (Campground, reservable, enabled)
      - 12 Oak Camp      (Campground, reservable, enabled)
      - 13 Wilderness Permit  (Permit, reservable, enabled)
    """
    ridb = tmp_path / "ridb_wizard"
    ridb.mkdir()

    # Facilities
    fac_headers = [
        "FacilityID", "LegacyFacilityID", "OrgFacilityID", "ParentOrgID",
        "ParentRecAreaID", "FacilityName", "FacilityDescription",
        "FacilityTypeDescription", "FacilityUseFeeDescription",
        "FacilityDirections", "FacilityPhone", "FacilityEmail",
        "FacilityReservationURL", "FacilityMapURL", "FacilityAdaAccess",
        "FacilityAccessibilityText", "FacilityLongitude", "FacilityLatitude",
        "Keywords", "StayLimit", "Reservable", "Enabled", "LastUpdatedDate",
    ]
    facilities = [
        ["11", "", "", "128", "10", "Pine Camp", "Pine Camp desc.", "Campground",
         "", "", "", "", "", "", "No", "", "-119.0", "37.0", "", "", "True", "True",
         "2024-01-01"],
        ["12", "", "", "128", "10", "Oak Camp", "Oak Camp desc.", "Campground",
         "", "", "", "", "", "", "No", "", "-119.1", "37.1", "", "", "True", "True",
         "2024-01-01"],
        ["13", "", "", "128", "10", "Wilderness Permit", "Permit desc.", "Permit",
         "", "", "", "", "", "", "No", "", "-119.2", "37.2", "", "", "True", "True",
         "2024-01-01"],
    ]
    _write_csv(ridb / "Facilities_API_v1.csv", fac_headers, facilities)

    # FacilityAddresses — all in CA
    addr_headers = [
        "FacilityAddressID", "FacilityID", "FacilityAddressType",
        "FacilityStreetAddress1", "FacilityStreetAddress2",
        "FacilityStreetAddress3", "City", "AddressStateCode", "PostalCode",
        "AddressCountryCode", "LastUpdatedDate",
    ]
    addresses = [
        ["201", "11", "Default", "", "", "", "Somewhere", "CA", "95000", "USA", "2024-01-01"],
        ["202", "12", "Default", "", "", "", "Somewhere", "CA", "95000", "USA", "2024-01-01"],
        ["203", "13", "Default", "", "", "", "Somewhere", "CA", "95000", "USA", "2024-01-01"],
    ]
    _write_csv(ridb / "FacilityAddresses_API_v1.csv", addr_headers, addresses)

    # RecAreas
    ra_headers = [
        "RecAreaID", "OrgRecAreaID", "ParentOrgID", "RecAreaName",
        "RecAreaDescription", "RecAreaUseFeeDescription", "RecAreaDirections",
        "RecAreaPhone", "RecAreaEmail", "RecAreaReservationURL",
        "RecAreaMapURL", "RecAreaLongitude", "RecAreaLatitude", "StayLimit",
        "Keywords", "Reservable", "Enabled", "LastUpdatedDate",
    ]
    recareas = [
        ["10", "", "128", "Test National Park", "A test park.", "", "", "", "", "",
         "", "-119.0", "37.0", "", "", "True", "True", "2024-01-01"],
    ]
    _write_csv(ridb / "RecAreas_API_v1.csv", ra_headers, recareas)

    # RecAreaFacilities
    _write_csv(
        ridb / "RecAreaFacilities_API_v1.csv",
        ["RecAreaID", "FacilityID"],
        [["10", "11"], ["10", "12"], ["10", "13"]],
    )

    # OrgEntities
    _write_csv(
        ridb / "OrgEntities_API_v1.csv",
        ["EntityID", "OrgID", "EntityType"],
        [
            ["10", "128", "RecArea"],
            ["11", "128", "Facility"],
            ["12", "128", "Facility"],
            ["13", "128", "Facility"],
        ],
    )

    # Organizations
    org_headers = [
        "OrgID", "OrgType", "OrgName", "OrgImageURL", "OrgURLText",
        "OrgURLAddress", "OrgAbbrevName", "OrgJurisdictionType", "OrgParentID",
        "LastUpdatedDate",
    ]
    _write_csv(
        ridb / "Organizations_API_v1.csv",
        org_headers,
        [["128", "Federal", "National Park Service", "", "NPS",
          "https://www.nps.gov", "NPS", "Federal", "0", "2024-01-01"]],
    )

    return str(ridb)


@pytest.fixture
def authed_client(monkeypatch, tmp_path, wizard_ridb_dir):
    """TestClient with auth session already established."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    os.environ["AUTH_USERNAME"] = "stu"
    os.environ["AUTH_PASSWORD"] = "testpass"
    os.environ["DATA_DIR"] = data_dir
    os.environ["RIDB_DIR"] = wizard_ridb_dir

    import app.config as cfg_mod
    importlib.reload(cfg_mod)

    import app.auth as auth_mod
    importlib.reload(auth_mod)

    from app.main import create_app
    application = create_app()
    client = TestClient(application, follow_redirects=False)

    # Log in
    resp = client.post("/login", data={"username": "stu", "password": "testpass"})
    assert resp.status_code == 303, f"Login failed: {resp.status_code}"

    client.app_ref = application
    return client


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_wizard_page_renders(authed_client):
    resp = authed_client.get("/monitors/new")
    assert resp.status_code == 200


def test_api_agencies_by_state(authed_client):
    resp = authed_client.get("/api/agencies?state=CA")
    assert resp.status_code == 200
    assert "NPS" in resp.text or "National Park" in resp.text


def test_api_rec_areas(authed_client):
    resp = authed_client.get("/api/rec-areas?state=CA&org=128")
    assert resp.status_code == 200
    assert "Test National Park" in resp.text


def test_api_rec_areas_search(authed_client):
    resp = authed_client.get("/api/rec-areas?state=CA&org=128&q=test")
    assert resp.status_code == 200
    assert "Test National Park" in resp.text


def test_api_facilities(authed_client):
    resp = authed_client.get("/api/facilities?rec_area=10")
    assert resp.status_code == 200
    assert "Pine Camp" in resp.text and "Oak Camp" in resp.text


def test_api_facilities_filter_by_type(authed_client):
    resp = authed_client.get("/api/facilities?rec_area=10&type=Permit")
    assert resp.status_code == 200
    assert "Wilderness Permit" in resp.text
    assert "Pine Camp" not in resp.text


def test_create_monitor_via_api(authed_client, tmp_path):
    import json, os
    # tmp_data_dir is DATA_DIR from the authed_client fixture (tmp_path / "data")
    tmp_data_dir = str(tmp_path / "data")
    resp = authed_client.post("/api/monitors", json={
        "name": "Test Trip",
        "rec_area_name": "Test National Park",
        "facility_ids": ["100", "101"],
        "facility_names": {"100": "Pine Camp", "101": "Oak Camp"},
        "check_in": "2026-06-01",
        "check_out": "2026-06-03",
        "poll_interval_seconds": 300,
        "notify_email": False,
        "notify_ntfy": True,
        "email_to": "",
        "ntfy_topic": "test-topic",
        "status": "stopped",
    })
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert "id" in data
    # Verify persisted
    with open(os.path.join(tmp_data_dir, "monitors.json")) as f:
        stored = json.load(f)
    assert len(stored["monitors"]) == 1
    assert stored["monitors"][0]["name"] == "Test Trip"
    assert len(stored["monitors"][0]["facilities"]) == 2


def test_step3_offers_optional_nights_field(authed_client):
    """Permit monitors record trip length as metadata."""
    resp = authed_client.get("/monitors/new/step3")
    assert resp.status_code == 200
    assert 'id="nights"' in resp.text


def test_step3_does_not_guess_permit_mode_by_some(authed_client):
    """Regression: `.some(t => t === "Permit")` flipped a MIXED selection to
    permit mode, so the campground facilities never got dates and the monitor
    raised ValueError on every cycle forever. Step 3 must read the single type.

    Matches the exact old expression rather than a bare ".some(", so that
    prose mentioning the old bug does not trip the test.
    """
    resp = authed_client.get("/monitors/new/step3")
    assert "Object.values(wizardData.facility_types || {}).some(" not in resp.text
    # and it derives the type from the selection instead
    assert "wizardData.facility_ids" in resp.text


def test_step2_has_a_type_lock_hint(authed_client):
    """Step 2 explains why other-type facilities are greyed out."""
    resp = authed_client.get(
        "/monitors/new/step2?rec_area_id=2991&rec_area_name=Yosemite"
    )
    assert resp.status_code == 200
    assert 'id="facility-type-hint"' in resp.text


# ── Per-user defaults prefill the wizard ──────────────────────────────────────
#
# The values already lived in users.json and were already written back on every
# create (api.py). The wizard just never read them: step 4 was handed
# `default_email` and never rendered it, and the stored ntfy topic and poll
# interval were not read at all.

def _set_defaults(client, **defaults):
    client.app_ref.state.user_store.update_defaults("stu", defaults)


def test_step4_prefills_stored_email_default(authed_client):
    _set_defaults(authed_client, email_to="preset@example.com", ntfy_topic="")
    resp = authed_client.get("/monitors/new/step4")
    assert 'value="preset@example.com"' in resp.text


def test_step4_prefills_stored_ntfy_topic_default(authed_client):
    _set_defaults(authed_client, email_to="", ntfy_topic="my-preset-topic")
    resp = authed_client.get("/monitors/new/step4")
    assert "my-preset-topic" in resp.text


def test_step3_prefills_stored_poll_interval_default(authed_client):
    _set_defaults(authed_client, poll_interval_seconds=900)
    resp = authed_client.get("/monitors/new/step3")
    assert '<option value="900" selected>' in resp.text


def test_step3_falls_back_to_five_minutes_without_a_default(authed_client):
    _set_defaults(authed_client, poll_interval_seconds=None)
    resp = authed_client.get("/monitors/new/step3")
    assert '<option value="300" selected>' in resp.text


def test_step4_renders_empty_for_a_user_with_no_defaults(authed_client):
    """Users with blank defaults must see blank fields, not the word None."""
    _set_defaults(authed_client, email_to="", ntfy_topic="")
    resp = authed_client.get("/monitors/new/step4")
    assert "None" not in resp.text


# ── Global park search endpoint ───────────────────────────────────────────────

def test_rec_areas_search_works_without_state_or_agency(authed_client):
    """The whole point: find a park without drilling down first."""
    resp = authed_client.get("/api/rec-areas?q=test")
    assert resp.status_code == 200
    assert "Test National Park" in resp.text
    assert "Select a state and agency" not in resp.text


def test_rec_areas_without_query_still_needs_state_and_agency(authed_client):
    """No query and no drill-down must not dump the whole catalog."""
    resp = authed_client.get("/api/rec-areas")
    assert resp.status_code == 200
    assert "Test National Park" not in resp.text


def test_rec_areas_drilldown_still_works(authed_client):
    """The original state+org path is unchanged."""
    resp = authed_client.get("/api/rec-areas?state=CA&org=128")
    assert resp.status_code == 200
    assert "Test National Park" in resp.text


def test_rec_areas_rows_use_data_attributes_not_inline_onclick(authed_client):
    """Inline onclick interpolated a third-party name into a JS string literal."""
    resp = authed_client.get("/api/rec-areas?q=test")
    assert "data-rec-area-id=" in resp.text
    assert "onclick=" not in resp.text


def test_rec_areas_search_reports_state(authed_client):
    resp = authed_client.get("/api/rec-areas?q=test")
    assert "CA" in resp.text


# ── Clone: wizard prefill spine ───────────────────────────────────────────────

def test_seed_from_monitor_maps_every_field():
    """Pure function, no HTTP. Names and types are keyed by facility id."""
    from app.routes.wizard import seed_from_monitor
    seed = seed_from_monitor({
        "id": "m1", "name": "Conness1", "rec_area_name": "Yosemite National Park",
        "facilities": [{"id": "445859", "name": "Yosemite Wilderness",
                        "type": "Permit", "division_ids": ["44585955"]}],
        "check_in": "", "check_out": "", "entry_date": "2026-08-14",
        "party_size": 2, "nights": 3, "poll_interval_seconds": 300,
        "enable_ntfy": True, "ntfy_topic": "t", "enable_email": False, "email_to": "",
    })
    assert seed["facility_ids"] == ["445859"]
    assert seed["facility_names"] == {"445859": "Yosemite Wilderness"}
    assert seed["facility_types"] == {"445859": "Permit"}
    assert seed["selected_divisions"] == {"445859": ["44585955"]}
    assert seed["entry_date"] == "2026-08-14"
    assert seed["nights"] == 3
    assert seed["name"] == "Conness1 copy"


def test_seed_omits_divisions_when_absent():
    from app.routes.wizard import seed_from_monitor
    seed = seed_from_monitor({
        "name": "c", "facilities": [{"id": "1", "name": "Camp", "type": "Campground"}],
    })
    assert seed["selected_divisions"] == {}


def test_clone_opens_the_wizard_at_step_3_prefilled(authed_client):
    mgr = authed_client.app_ref.state.manager
    mgr.add_monitor({
        "id": "clone-me", "owner": "stu", "name": "Original", "status": "stopped",
        "rec_area_name": "Test National Park",
        "facilities": [{"id": "11", "name": "Pine Camp", "type": "Campground"}],
        "check_in": "2026-09-01", "check_out": "2026-09-03", "stats": {},
    })
    resp = authed_client.get("/monitors/new?from=clone-me")
    assert resp.status_code == 200
    assert "/monitors/new/step3" in resp.text     # opens at dates
    assert "Pine Camp" in resp.text               # seeded
    assert "Original copy" in resp.text


def test_plain_wizard_still_opens_at_step_1(authed_client):
    resp = authed_client.get("/monitors/new")
    assert resp.status_code == 200
    assert "/monitors/new/step1" in resp.text


def test_clone_of_unknown_monitor_falls_back_to_empty_wizard(authed_client):
    resp = authed_client.get("/monitors/new?from=does-not-exist")
    assert resp.status_code == 200
    assert "/monitors/new/step1" in resp.text


def test_clone_escapes_a_name_that_could_close_the_script_block(authed_client):
    """A facility name containing </script> must not break out of the seed."""
    mgr = authed_client.app_ref.state.manager
    mgr.add_monitor({
        "id": "evil", "owner": "stu", "name": "x", "status": "stopped",
        "rec_area_name": "P",
        "facilities": [{"id": "9", "name": "</script><b>pwn</b>", "type": "Campground"}],
        "check_in": "2026-09-01", "check_out": "2026-09-02", "stats": {},
    })
    resp = authed_client.get("/monitors/new?from=evil")
    assert resp.status_code == 200
    assert "</script><b>pwn</b>" not in resp.text
    assert "<\/script>" in resp.text


# ── Favorites ─────────────────────────────────────────────────────────────────

FAV = {
    "label": "Tuolumne pair",
    "rec_area_id": "10",
    "rec_area_name": "Test National Park",
    "facilities": [{"id": "11", "name": "Pine Camp", "type": "Campground"}],
}


def test_create_favorite(authed_client):
    resp = authed_client.post("/api/favorites", json=FAV)
    assert resp.status_code == 200
    assert authed_client.app_ref.state.user_store.list_favorites("stu")


def test_favorite_requires_a_label(authed_client):
    resp = authed_client.post("/api/favorites", json={**FAV, "label": "  "})
    assert resp.status_code == 422


def test_favorite_requires_a_facility(authed_client):
    resp = authed_client.post("/api/favorites", json={**FAV, "facilities": []})
    assert resp.status_code == 422


def test_favorite_cannot_mix_facility_types(authed_client):
    """A favorite seeds a monitor, and a monitor watches one type."""
    resp = authed_client.post("/api/favorites", json={**FAV, "facilities": [
        {"id": "11", "name": "Pine Camp", "type": "Campground"},
        {"id": "13", "name": "Wilderness Permit", "type": "Permit"},
    ]})
    assert resp.status_code == 422


def test_delete_favorite(authed_client):
    fid = authed_client.post("/api/favorites", json=FAV).json()["id"]
    assert authed_client.delete(f"/api/favorites/{fid}").status_code == 200
    assert authed_client.app_ref.state.user_store.list_favorites("stu") == []


def test_delete_unknown_favorite_404s(authed_client):
    assert authed_client.delete("/api/favorites/nope").status_code == 404


def test_favorite_opens_the_wizard_at_step_3_prefilled(authed_client):
    fid = authed_client.post("/api/favorites", json=FAV).json()["id"]
    resp = authed_client.get(f"/monitors/new?favorite={fid}")
    assert resp.status_code == 200
    assert "/monitors/new/step3" in resp.text
    assert "Pine Camp" in resp.text


def test_unknown_favorite_falls_back_to_empty_wizard(authed_client):
    resp = authed_client.get("/monitors/new?favorite=nope")
    assert resp.status_code == 200
    assert "/monitors/new/step1" in resp.text


def test_seed_from_favorite_keeps_divisions():
    from app.routes.wizard import seed_from_favorite
    seed = seed_from_favorite({
        "rec_area_id": "2991", "rec_area_name": "Yosemite",
        "facilities": [{"id": "445859", "name": "Wilderness", "type": "Permit",
                        "division_ids": ["44585955"]}],
    })
    assert seed["selected_divisions"] == {"445859": ["44585955"]}
    assert seed["facility_names"] == {"445859": "Wilderness"}
    # A favorite is a place, not a trip: no dates come along.
    assert "check_in" not in seed


def test_dashboard_shows_favorites(authed_client):
    authed_client.post("/api/favorites", json=FAV)
    resp = authed_client.get("/dashboard")
    assert "Tuolumne pair" in resp.text


def test_dashboard_renders_nothing_extra_without_favorites(authed_client):
    resp = authed_client.get("/dashboard")
    assert "favorites-strip" not in resp.text


# ── Clone walks the full wizard ───────────────────────────────────────────────

def test_clone_opens_at_step_1_not_step_3(authed_client):
    """Cloning should walk every step so nothing is created unreviewed."""
    authed_client.app_ref.state.manager.add_monitor({
        "id": "walk-me", "owner": "stu", "name": "Original", "status": "stopped",
        "rec_area_name": "Test National Park",
        "facilities": [{"id": "11", "name": "Pine Camp", "type": "Campground"}],
        "check_in": "2030-09-01", "check_out": "2030-09-03", "stats": {},
    })
    resp = authed_client.get("/monitors/new?from=walk-me")
    assert resp.status_code == 200
    assert "/monitors/new/step1" in resp.text
    assert "Pine Camp" in resp.text          # still seeded
    assert "Original copy" in resp.text


def test_favorite_still_opens_at_step_3(authed_client):
    """A favorite is a settled selection, so it keeps skipping ahead."""
    fid = authed_client.post("/api/favorites", json=FAV).json()["id"]
    resp = authed_client.get(f"/monitors/new?favorite={fid}")
    assert "/monitors/new/step3" in resp.text


def test_clone_seed_derives_state_and_agency(authed_client):
    """A monitor never stored state/org, so step 1 needs them from the catalog."""
    from app.routes.wizard import seed_from_monitor
    catalog = authed_client.app_ref.state.catalog
    seed = seed_from_monitor(
        {"name": "m", "facilities": [{"id": "11", "name": "Pine Camp",
                                      "type": "Campground"}]},
        catalog,
    )
    assert seed["state"] == "CA"
    assert seed["org"] == "128"


def test_seed_survives_a_facility_missing_from_the_catalog(authed_client):
    """RIDB is a point-in-time export; a facility can disappear from it."""
    from app.routes.wizard import seed_from_monitor
    seed = seed_from_monitor(
        {"name": "m", "facilities": [{"id": "does-not-exist", "name": "?",
                                      "type": "Campground"}]},
        authed_client.app_ref.state.catalog,
    )
    assert "state" not in seed
    assert seed["facility_ids"] == ["does-not-exist"]


def test_seed_without_a_catalog_still_works(authed_client):
    from app.routes.wizard import seed_from_monitor
    seed = seed_from_monitor({"name": "m", "facilities": []})
    assert seed["facility_ids"] == []
