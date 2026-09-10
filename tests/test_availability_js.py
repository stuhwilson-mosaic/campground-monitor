"""Run the availability page's real date, status, and filter logic in Node."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
SCRIPT = Path(__file__).resolve().parents[1] / "app/static/availability.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def run_js(body, timezone="America/Los_Angeles"):
    assert SCRIPT.exists(), "Availability page behavior has not been implemented"
    script = f"const ui = require({json.dumps(str(SCRIPT))});\n{body}"
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=15,
        env={**os.environ, "TZ": timezone},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_only_available_is_counted_as_bookable_and_original_status_is_preserved():
    result = run_js("""
        const statuses = ['Available', 'Reserved', 'Not Available', 'Closed',
            'Cutoff', 'Not Reservable', 'First Come First Served', 'Walk Up',
            'Unknown', 'New upstream status'];
        console.log(JSON.stringify(statuses.map(ui.statusInfo)));
    """)
    assert [status["available"] for status in result] == [True] + [False] * 9
    assert len({status["code"] for status in result[:9]}) == 9
    assert result[-1]["label"] == "New upstream status"
    assert result[-1]["kind"] == "unknown"


def test_missing_status_is_unknown_not_available():
    result = run_js("console.log(JSON.stringify(ui.statusInfo(null)));")
    assert result["label"] == "Unknown"
    assert result["available"] is False


def test_changed_available_labels_do_not_disagree_with_the_server_stay_rule():
    result = run_js("console.log(JSON.stringify(['available', 'AVAILABLE', ' Available '].map(ui.statusInfo)));")
    assert [status["available"] for status in result] == [False, False, False]
    assert [status["kind"] for status in result] == ["unknown", "unknown", "unknown"]
    assert [status["label"] for status in result] == ["available", "AVAILABLE", " Available "]


def test_unrecognized_status_labels_cannot_match_inherited_object_properties():
    result = run_js("console.log(JSON.stringify(['constructor', '__proto__'].map(ui.statusInfo)));")
    assert [status.get("kind") for status in result] == ["unknown", "unknown"]
    assert [status["label"] for status in result] == ["constructor", "__proto__"]


def test_filters_combine_and_keep_unknown_accessibility_separate():
    result = run_js("""
        const sites = [
            {id:'a', loop:'North', type:'Tent', accessible:true, available_for_stay:true},
            {id:'b', loop:'North', type:'Tent', accessible:null, available_for_stay:true},
            {id:'c', loop:'North', type:'RV', accessible:false, available_for_stay:true},
            {id:'d', loop:'South', type:'Tent', accessible:true, available_for_stay:false},
        ];
        const ids = filters => ui.filterSites(sites, filters).map(site => site.id);
        console.log(JSON.stringify({
            combined: ids({loop:'North', type:'Tent', accessibility:'yes', availableOnly:true}),
            unknown: ids({accessibility:'unknown'}),
            no: ids({accessibility:'no'}),
            available: ids({availableOnly:true}),
            all: ids({}),
        }));
    """)
    assert result == {
        "combined": ["a"], "unknown": ["b"], "no": ["c"],
        "available": ["a", "b", "c"], "all": ["a", "b", "c", "d"],
    }


def test_daily_counts_do_not_turn_split_sites_into_a_full_stay():
    result = run_js("""
        const sites = [
            {id:'a', available_for_stay:false,
                availability:{'2026-09-30':'Available', '2026-10-01':'Reserved'}},
            {id:'b', available_for_stay:false,
                availability:{'2026-09-30':'Not Available', '2026-10-01':'Available'}},
            {id:'c', available_for_stay:true, lat:null, lon:null,
                availability:{'2026-09-30':'Available', '2026-10-01':'Available'}},
        ];
        console.log(JSON.stringify(ui.summarizeSites(
            ui.filterSites(sites, {}), ['2026-09-30', '2026-10-01', '2026-10-02'])));
    """)
    assert result == {"total": 3, "fullStay": 1, "daily": {
        "2026-09-30": 2, "2026-10-01": 2, "2026-10-02": 0,
    }}


@pytest.mark.parametrize("timezone", ["America/Los_Angeles", "Pacific/Auckland"])
def test_date_arithmetic_keeps_calendar_days_across_month_and_dst(timezone):
    result = run_js("""
        console.log(JSON.stringify({
            month: ui.addDays('2026-09-30', 1),
            dst: ui.addDays('2026-11-01', 1),
            nights: ui.stayNights('2026-10-31', '2026-11-02'),
            label: ui.formatDay('2026-09-10', {month:'short', day:'numeric'}),
            shifted: ui.shiftStay('2026-09-30', '2026-10-03', 7, '2026-09-10', '2027-09-10'),
        }));
    """, timezone=timezone)
    assert result == {
        "month": "2026-10-01", "dst": "2026-11-02", "nights": 2,
        "label": "Sep 10", "shifted": {"check_in": "2026-10-07", "check_out": "2026-10-10"},
    }


def test_invalid_stays_and_navigation_past_boundaries_are_rejected():
    result = run_js("""
        const validate = (start, end) => ui.validateStay(start, end, '2026-09-10', '2027-09-10');
        console.log(JSON.stringify({
            valid: validate('2026-09-10', '2026-10-11'),
            invalid: [validate('2026-09-10', '2026-09-10'),
                validate('2026-09-10', '2026-10-12'),
                validate('2026-09-09', '2026-09-11'),
                validate('2027-09-09', '2027-09-11'),
                validate('2027-02-30', '2027-03-02')].map(Boolean),
            previous: ui.shiftStay('2026-09-15', '2026-09-17', -7, '2026-09-10', '2027-09-10'),
            next: ui.shiftStay('2027-09-02', '2027-09-04', 7, '2026-09-10', '2027-09-10'),
        }));
    """)
    assert result == {"valid": None, "invalid": [True] * 5, "previous": None, "next": None}


def test_a_new_request_aborts_and_invalidates_the_previous_response():
    result = run_js("""
        const gate = ui.createRequestGate();
        const first = gate.start();
        const initiallyCurrent = first.isCurrent();
        const second = gate.start();
        console.log(JSON.stringify({initiallyCurrent,
            firstAborted:first.signal.aborted, firstCurrent:first.isCurrent(),
            secondAborted:second.signal.aborted, secondCurrent:second.isCurrent()}));
    """)
    assert result == {"initiallyCurrent": True, "firstAborted": True,
                      "firstCurrent": False, "secondAborted": False, "secondCurrent": True}


def test_revealing_mobile_calendar_scrolls_selected_row_after_layout():
    result = run_js("""
        const frames = [];
        global.window = {requestAnimationFrame(callback) { frames.push(callback); }};
        let laidOut = false;
        const buttons = Object.fromEntries(['calendar', 'map'].map(name => [name, {
            setAttribute(attribute, value) { this[attribute] = value; }
        }]));
        const app = {dataset:{mobileView:'map'},
            querySelector(selector) { return buttons[selector.slice(6)]; }};
        const container = {scrollTop:0,
            get clientHeight() { return laidOut ? 300 : 0; },
            getBoundingClientRect() { return {top:laidOut ? 100 : 0}; }};
        const row = {
            get offsetHeight() { return laidOut ? 40 : 0; },
            getBoundingClientRect() { return {top:laidOut ? 800 : 0}; }};
        const scrollSelection = () => ui.scrollRowIntoView(container, row, laidOut ? 80 : 0);
        scrollSelection(); // Selecting a marker while Calendar is display:none.
        ui.showMobileView(app, 'calendar', () => { throw Error('Map callback on Calendar'); }, scrollSelection);
        const beforeLayout = container.scrollTop;
        const scheduled = frames.length;
        laidOut = true;
        frames.forEach(callback => callback());
        console.log(JSON.stringify({beforeLayout, scheduled, afterLayout:container.scrollTop,
            view:app.dataset.mobileView, calendarPressed:buttons.calendar['aria-pressed'],
            mapPressed:buttons.map['aria-pressed']}));
    """)
    assert result == {"beforeLayout": 0, "scheduled": 1, "afterLayout": 620,
                      "view": "calendar", "calendarPressed": "true", "mapPressed": "false"}
