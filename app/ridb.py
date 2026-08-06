"""RIDB catalog loader — reads RIDB CSV exports and provides filtered lookups."""
import csv
import os
from typing import Optional


# Facility types we care about
_ALLOWED_TYPES = {"Campground", "Permit"}

# Entity types in OrgEntities that link a facility to its org.
# "Permit" belongs here: RIDB files permit facilities under that entity type,
# and omitting it left every permit with no org id. get_agencies() skips
# anything with a falsy org, so permits disappeared from the wizard's
# state -> agency -> park drill-down entirely. Yosemite (445859) only ever
# worked by accident, because get_facilities applies no org filter and the
# park's campgrounds pulled its rec area into the list anyway.
_FACILITY_ENTITY_TYPES = {"Campground", "Facility", "Permit"}

# Normalize messy AddressStateCode values (full names, all-caps, etc.) to 2-letter codes
_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
    "american samoa": "AS", "northern mariana islands": "MP",
}
# Also accept 2-letter codes already
_VALID_STATE_CODES = set(_STATE_NAME_TO_CODE.values())


def _normalize_state(raw: str) -> str:
    """Normalize a state code/name to a 2-letter code, or 'Other' if unrecognizable."""
    cleaned = raw.strip()
    if not cleaned:
        return "Other"
    upper = cleaned.upper()
    if upper in _VALID_STATE_CODES:
        return upper
    lower = cleaned.lower()
    if lower in _STATE_NAME_TO_CODE:
        return _STATE_NAME_TO_CODE[lower]
    return "Other"


def _read_csv(path: str) -> list[dict]:
    """Read a CSV file and return a list of dicts (one per row)."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class RIDBCatalog:
    """
    Loads RIDB CSV exports at construction time and exposes filtered queries.

    Filtering applied at load:
    - Only facilities with FacilityTypeDescription in {"Campground", "Permit"}
    - Only facilities where Reservable == "True" AND Enabled == "True"
    - Facilities with no address row are assigned state code "Other"
    """

    def __init__(self, ridb_dir: str) -> None:
        self._ridb_dir = ridb_dir
        self._load()

    # ── Internal load ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Read all CSVs and build in-memory indexes."""
        d = self._ridb_dir

        # Raw CSV loads
        raw_facilities = _read_csv(os.path.join(d, "Facilities_API_v1.csv"))
        raw_addresses = _read_csv(os.path.join(d, "FacilityAddresses_API_v1.csv"))
        raw_recareas = _read_csv(os.path.join(d, "RecAreas_API_v1.csv"))
        raw_raf = _read_csv(os.path.join(d, "RecAreaFacilities_API_v1.csv"))
        raw_orgentities = _read_csv(os.path.join(d, "OrgEntities_API_v1.csv"))
        raw_orgs = _read_csv(os.path.join(d, "Organizations_API_v1.csv"))

        # ── Build facility_id → state code map ────────────────────────────────
        # (use only the first address row per facility)
        fac_state: dict[str, str] = {}
        for row in raw_addresses:
            fid = row["FacilityID"]
            if fid not in fac_state:
                fac_state[fid] = _normalize_state(row.get("AddressStateCode", ""))

        # ── Filter facilities ──────────────────────────────────────────────────
        self._facilities: dict[str, dict] = {}
        for row in raw_facilities:
            fid = row["FacilityID"]
            if row.get("FacilityTypeDescription") not in _ALLOWED_TYPES:
                continue
            if row.get("Reservable", "").strip().lower() != "true":
                continue
            if row.get("Enabled", "").strip().lower() != "true":
                continue
            row["_state"] = fac_state.get(fid, "Other")
            self._facilities[fid] = row

        # ── Build org entity map: facility_id → org_id ────────────────────────
        fac_org: dict[str, str] = {}
        for row in raw_orgentities:
            if row.get("EntityType") in _FACILITY_ENTITY_TYPES:
                eid = row["EntityID"]
                if eid in self._facilities:
                    fac_org[eid] = row["OrgID"]

        # ── Build org map: org_id → org info ──────────────────────────────────
        self._orgs: dict[str, dict] = {
            row["OrgID"]: row for row in raw_orgs
        }

        # ── Build rec area map: rec_area_id → rec area info ───────────────────
        self._rec_areas: dict[str, dict] = {
            row["RecAreaID"]: row for row in raw_recareas
        }

        # ── Build rec area → list of facility_ids (filtered only) ─────────────
        self._ra_facilities: dict[str, list[str]] = {}
        for row in raw_raf:
            raid = row["RecAreaID"]
            fid = row["FacilityID"]
            if fid in self._facilities:
                self._ra_facilities.setdefault(raid, []).append(fid)

        # ── Attach org_id to each facility row ────────────────────────────────
        for fid, frow in self._facilities.items():
            frow["_org_id"] = fac_org.get(fid, "")

        # ── Build state → set of facility_ids ─────────────────────────────────
        self._state_facilities: dict[str, set[str]] = {}
        for fid, frow in self._facilities.items():
            state = frow["_state"]
            self._state_facilities.setdefault(state, set()).add(fid)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_states(self) -> list[dict]:
        """
        Return sorted list of {code, count} dicts for states that have
        at least one qualifying facility. "Other" always sorts last.
        """
        states = [
            {"code": code, "count": len(fids)}
            for code, fids in self._state_facilities.items()
        ]
        states.sort(key=lambda s: ("\xff", s["code"]) if s["code"] == "Other"
                    else ("", s["code"]))
        return states

    def get_agencies(self, state: str) -> list[dict]:
        """
        Return list of {id, name, abbrev} for agencies that have at least one
        facility in the given state (via OrgEntities join path).
        """
        fids = self._state_facilities.get(state, set())
        seen_org_ids: set[str] = set()
        result = []
        for fid in fids:
            org_id = self._facilities[fid].get("_org_id", "")
            if org_id and org_id not in seen_org_ids and org_id in self._orgs:
                seen_org_ids.add(org_id)
                org = self._orgs[org_id]
                result.append({
                    "id": org_id,
                    "name": org.get("OrgName", ""),
                    "abbrev": org.get("OrgAbbrevName", ""),
                })
        result.sort(key=lambda o: o["name"])
        return result

    def get_rec_areas(self, state: str, org_id: str) -> list[dict]:
        """
        Return list of {id, name, facility_count} for rec areas that contain
        at least one facility belonging to org_id within the given state.
        """
        # Collect facility IDs that match state + org
        matching_fids = {
            fid for fid, frow in self._facilities.items()
            if frow["_state"] == state and frow.get("_org_id") == org_id
        }

        result = []
        seen_ra_ids: set[str] = set()
        for raid, fids in self._ra_facilities.items():
            overlap = [fid for fid in fids if fid in matching_fids]
            if overlap and raid not in seen_ra_ids:
                seen_ra_ids.add(raid)
                ra = self._rec_areas.get(raid, {})
                result.append({
                    "id": raid,
                    "name": ra.get("RecAreaName", ""),
                    "facility_count": len(overlap),
                })
        result.sort(key=lambda r: r["name"])
        return result

    def search_rec_areas(self, state: str, org_id: str, query: str) -> list[dict]:
        """
        Return rec areas matching get_rec_areas(state, org_id) whose name
        contains query as a case-insensitive substring.
        """
        q = query.lower()
        return [
            ra for ra in self.get_rec_areas(state, org_id)
            if q in ra["name"].lower()
        ]

    def search_all_rec_areas(self, query: str, limit: int = 50) -> list[dict]:
        """Search every rec area by name or keyword, with no state or org filter.

        get_rec_areas/search_rec_areas both require a state AND an org, which
        makes them useless for facilities that have no address row: those land
        in the "Other" state bucket and never surface in the drill-down. In the
        real export that is 87 of 149 permits.

        Only rec areas that actually contain a qualifying facility are
        searched (555 of 3,643 in the current export), so every hit leads
        somewhere. Exact prefix matches sort ahead of interior matches.
        """
        q = (query or "").strip().lower()
        if not q:
            return []

        results = []
        for raid, fids in self._ra_facilities.items():
            if not fids:
                continue
            ra = self._rec_areas.get(raid)
            if not ra:
                continue

            name = ra.get("RecAreaName", "")
            haystack = f"{name}\n{ra.get('Keywords', '')}".lower()
            if q not in haystack:
                continue

            # RecArea rows have no state column, so derive it from the
            # facilities to disambiguate same-named parks.
            states = sorted({
                self._facilities[fid].get("_state", "")
                for fid in fids
                if fid in self._facilities
            } - {""})

            results.append({
                "id": raid,
                "name": name,
                "facility_count": len(fids),
                "states": states,
                # Sort key only; not part of the returned contract.
                "_rank": 0 if name.lower().startswith(q) else 1,
            })

        results.sort(key=lambda r: (r["_rank"], r["name"]))
        for r in results:
            del r["_rank"]
        return results[:limit]

    def get_facilities(
        self,
        rec_area_id: str,
        facility_type: Optional[str] = None,
    ) -> list[dict]:
        """
        Return list of {id, name, type, lon, lat} for all qualifying facilities
        in the given rec area, optionally filtered by FacilityTypeDescription.
        """
        fids = self._ra_facilities.get(rec_area_id, [])
        result = []
        for fid in fids:
            frow = self._facilities[fid]
            ftype = frow.get("FacilityTypeDescription", "")
            if facility_type is not None and ftype != facility_type:
                continue
            result.append({
                "id": fid,
                "name": frow.get("FacilityName", ""),
                "type": ftype,
                "lon": frow.get("FacilityLongitude", ""),
                "lat": frow.get("FacilityLatitude", ""),
            })
        result.sort(key=lambda f: f["name"])
        return result
