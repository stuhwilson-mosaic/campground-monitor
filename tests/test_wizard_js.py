"""Behavioural tests for the wizard's client-side selection state.

The facility-selection logic lives in JavaScript, so a Python-only suite cannot
reach it. These render the real page, extract its scripts, and execute them in
node against a minimal DOM stub.

This exists because converting wizardData.facility_names from a parallel array
to a dict keyed by facility id was the riskiest edit in the reuse project: get
it wrong and monitors are created with numeric ids where names should be, which
looks like bad data rather than an error.
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node not available")

DOM_STUB = """
function mkEl() {
  return {
    value: "", textContent: "", disabled: false, checked: false,
    dataset: {}, style: {},
    classList: { toggle(){}, add(){}, remove(){}, contains(){ return false; } },
    closest(){ return null; }, remove(){}, appendChild(){}, replaceChildren(){},
    querySelectorAll(){ return []; }, addEventListener(){},
  };
}
const _els = {};
const _all = [];          // every element registered for querySelectorAll
const _handlers = {};        // type -> [fn]
function mkNamed(id, cls, dataset) {
  const el = mkEl();
  el.id = id || "";
  el.className = cls || "";
  el.dataset = dataset || {};
  el._classes = new Set((cls || "").split(" ").filter(Boolean));
  el.classList = {
    add: (c) => el._classes.add(c),
    remove: (c) => el._classes.delete(c),
    toggle: () => {},
    contains: (c) => el._classes.has(c),
  };
  // Real closest(): walks self then parents by class selector.
  el.closest = (sel) => {
    const want = sel.replace(/^\./, "");
    let n = el;
    while (n) { if (n._classes && n._classes.has(want)) return n; n = n._parent; }
    return null;
  };
  if (id) _els[id] = el;
  _all.push(el);
  return el;
}
globalThis.mkNamed = mkNamed;
globalThis.fireClick = function (target) {
  (_handlers.click || []).forEach((h) => h({ target: target }));
};
// Drives the htmx restore path, which is where cloned state is re-applied.
globalThis.fireEvent = function (type, target) {
  (_handlers[type] || []).forEach((h) => h({ target: target }));
};
globalThis.document = {
  getElementById(id){ return _els[id] || null; },
  querySelectorAll(sel){
    const want = sel.replace(/^\./, "");
    return _all.filter((e) => e._classes && e._classes.has(want));
  },
  querySelector(){ return null; },
  createElement(){ return mkEl(); },
  addEventListener(type, fn){ (_handlers[type] = _handlers[type] || []).push(fn); },
  body: mkEl(),
};
globalThis.window = globalThis;
globalThis.htmx = { ajax(){} };
globalThis.navigator = { clipboard: { writeText(){ return Promise.resolve(); } } };
globalThis.alert = function(){};
"""

CHECKBOX = """
function cb(id, name, type, checked) {
  return { value: id, checked: checked, dataset: { name: name, type: type },
           closest(){ return null; } };
}
"""


@pytest.fixture
def wizard_js(tmp_path, sample_ridb_dir):
    """Render /monitors/new and return its concatenated inline scripts."""
    os.environ["AUTH_USERNAME"] = "stu"
    os.environ["AUTH_PASSWORD"] = "pw"
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["RIDB_DIR"] = sample_ridb_dir
    os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

    import app.config as cfg
    importlib.reload(cfg)
    import app.auth as auth
    importlib.reload(auth)
    from app.main import create_app

    client = TestClient(create_app(), follow_redirects=False)
    client.post("/login", data={"username": "stu", "password": "pw"})
    html = client.get("/monitors/new").text
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


def run_js(wizard_js, body):
    """Execute the wizard scripts plus `body` in node; return parsed stdout."""
    script = f"{DOM_STUB}\n{wizard_js}\n{CHECKBOX}\n{body}"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


def test_selecting_facilities_keys_names_by_id(wizard_js):
    out = run_js(wizard_js, """
        updateFacilitySelection(cb("11", "Pine Camp", "Campground", true));
        updateFacilitySelection(cb("12", "Oak Camp", "Campground", true));
        console.log(JSON.stringify({
          ids: wizardData.facility_ids, names: wizardData.facility_names,
        }));
    """)
    assert out["ids"] == ["11", "12"]
    assert out["names"] == {"11": "Pine Camp", "12": "Oak Camp"}


def test_unchecking_one_of_two_same_named_facilities_keeps_the_other(wizard_js):
    """The desync bug: removal used to filter the names array BY VALUE.

    Two facilities sharing a name meant unchecking one dropped BOTH name
    entries while dropping only one id, so every later facility was submitted
    under the wrong name.
    """
    out = run_js(wizard_js, """
        updateFacilitySelection(cb("11", "Group Camp", "Campground", true));
        updateFacilitySelection(cb("12", "Group Camp", "Campground", true));
        updateFacilitySelection(cb("11", "Group Camp", "Campground", false));
        console.log(JSON.stringify({
          ids: wizardData.facility_ids, names: wizardData.facility_names,
        }));
    """)
    assert out["ids"] == ["12"]
    assert out["names"] == {"12": "Group Camp"}, (
        "the surviving facility lost its name, so it would be submitted under "
        "its numeric id"
    )


def test_unchecking_removes_only_that_facility(wizard_js):
    out = run_js(wizard_js, """
        updateFacilitySelection(cb("11", "Pine Camp", "Campground", true));
        updateFacilitySelection(cb("12", "Oak Camp", "Campground", true));
        updateFacilitySelection(cb("11", "Pine Camp", "Campground", false));
        console.log(JSON.stringify({
          ids: wizardData.facility_ids, names: wizardData.facility_names,
        }));
    """)
    assert out["ids"] == ["12"]
    assert out["names"] == {"12": "Oak Camp"}


def test_type_lock_records_the_first_selected_type(wizard_js):
    """A monitor watches one facility type; the lock derives from selection."""
    out = run_js(wizard_js, """
        updateFacilitySelection(cb("11", "Pine Camp", "Campground", true));
        console.log(JSON.stringify({ locked: currentLockedType() }));
    """)
    assert out["locked"] == "Campground"


def test_type_lock_clears_when_selection_empties(wizard_js):
    out = run_js(wizard_js, """
        updateFacilitySelection(cb("11", "Pine Camp", "Campground", true));
        updateFacilitySelection(cb("11", "Pine Camp", "Campground", false));
        console.log(JSON.stringify({ locked: currentLockedType() }));
    """)
    assert out["locked"] is None


def test_clicking_a_park_selects_it_and_enables_next(wizard_js):
    """Regression: clicking a park in step 1 did nothing and blocked the wizard.

    selectRecArea read the implicit global `event` and touched
    event.currentTarget.classList. That worked under the old inline onclick,
    where currentTarget was the row. Once the handler became a delegated
    listener on `document`, currentTarget was `document`, whose classList is
    undefined, so the function threw before reaching the code that enables the
    Next button. rec_area_id was set but the user could not continue.
    """
    out = run_js(wizard_js, """
        mkNamed("step1-next-btn", "btn", {}).disabled = true;
        var row = mkNamed("", "rec-area-item",
                          { recAreaId: "2991", recAreaName: "Yosemite National Park" });
        fireClick(row);
        console.log(JSON.stringify({
          id: wizardData.rec_area_id,
          name: wizardData.rec_area_name,
          nextEnabled: document.getElementById("step1-next-btn").disabled === false,
          highlighted: row.classList.contains("selected"),
        }));
    """)
    assert out["id"] == "2991"
    assert out["name"] == "Yosemite National Park"
    assert out["nextEnabled"], "Next stayed disabled, so the wizard is blocked"
    assert out["highlighted"]


def test_clicking_outside_a_park_row_is_ignored(wizard_js):
    out = run_js(wizard_js, """
        mkNamed("step1-next-btn", "btn", {}).disabled = true;
        fireClick(mkNamed("", "some-other-thing", {}));
        console.log(JSON.stringify({ id: wizardData.rec_area_id || "" }));
    """)
    assert out["id"] == ""


def test_cloned_facilities_enable_step_2_next(wizard_js):
    """Regression: cloning left you stranded on step 2.

    The seed puts facilities in wizardData and the htmx restore ticks their
    checkboxes, but ticking a box in code does not fire onchange. The selected
    count and the Next button are driven by wizardData, so they were never
    updated and Next stayed disabled with facilities visibly selected.
    """
    out = run_js(wizard_js, """
        var list = mkNamed("facility-list", "", {});
        var next = mkNamed("step2-next-btn", "btn", {});
        next.disabled = true;
        mkNamed("selected-facility-count", "", {});

        // What a clone arrives with: seeded selection, fresh DOM.
        wizardData.facility_ids = ["11", "12"];
        wizardData.facility_types = { "11": "Campground", "12": "Campground" };
        fireEvent("htmx:afterSwap", list);

        console.log(JSON.stringify({
          nextEnabled: document.getElementById("step2-next-btn").disabled === false,
          count: document.getElementById("selected-facility-count").textContent,
        }));
    """)
    assert out["nextEnabled"], "Next stayed disabled, so the clone is stuck on step 2"
    assert out["count"] == "2 selected"


def test_step_2_next_stays_disabled_with_nothing_selected(wizard_js):
    out = run_js(wizard_js, """
        var list = mkNamed("facility-list", "", {});
        var next = mkNamed("step2-next-btn", "btn", {});
        next.disabled = false;
        mkNamed("selected-facility-count", "", {});

        wizardData.facility_ids = [];
        fireEvent("htmx:afterSwap", list);

        console.log(JSON.stringify({
          nextDisabled: document.getElementById("step2-next-btn").disabled === true,
        }));
    """)
    assert out["nextDisabled"]
