"""Patching tests: locating the declaration, the guards, and collisions.

These cover what the corpus run cannot reach — an edit that must be refused, and
a new locator that quietly steals another locator's element.
"""
import datetime as dt
import json, pathlib, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import yaml
from playwright.sync_api import sync_playwright

from shared import browser_mode
from shared import locator_capture as capture
from shared import locator_patch, locator_resolve as heal_mod

HERE = pathlib.Path(__file__).resolve().parent

# Fingerprints a real page, so it needs the framework's capture script.
pytestmark = pytest.mark.skipif(
    not capture.script_available(),
    reason=f"capture script not at {capture.script_path()} — set FRAMEWORK_DIR")
CONFIG = HERE.parent / "config" / "locator.yaml"
PO = HERE / "fixtures" / "pageobjects"
FIX = HERE / "fixtures"

# Two fields deliberately declaring the SAME selector: the edit must be located
# by field identity, because searching for the selector string cannot tell them
# apart and picking either one is a coin flip.
TWIN_SOURCE = """public class TwinPage {

    public Locator primaryAction() {
        return page.locator("#go");
    }

    public Locator secondaryAction() {
        return page.locator("#go");
    }
}
"""


def test_declaration_is_located_by_field_not_by_selector():
    edit, error = locator_patch.declaration_edit(
        TWIN_SOURCE, "secondaryAction", 'page.getByTestId("go-2")')
    assert not error and edit is not None
    assert "secondaryAction" in edit["old_string"]
    assert "primaryAction" not in edit["old_string"]

    updated, error = locator_patch.apply_to_source(
        TWIN_SOURCE, "secondaryAction", 'page.getByTestId("go-2")')
    assert not error, error
    assert updated.count('page.locator("#go")') == 1        # the twin is untouched
    assert 'page.getByTestId("go-2")' in updated


def test_apply_to_source_changes_exactly_one_line():
    source = (PO / "LoginPage.java").read_text()
    updated, error = locator_patch.apply_to_source(
        source, "usernameField", 'page.getByTestId("username")')
    assert not error, error
    changed = [(a, b) for a, b in zip(source.splitlines(), updated.splitlines()) if a != b]
    assert len(changed) == 1, f"expected a one-line edit, got {len(changed)}"
    assert source.count("public Locator") == updated.count("public Locator")


def test_apply_to_source_refuses_a_broader_selector():
    """The emit ladder drops scope when it can find nothing better, and a looser
    selector is how a wrong-page failure gets papered over into a pass."""
    source = (PO / "ProfilePage.java").read_text()
    updated, error = locator_patch.apply_to_source(source, "saveButton", 'page.locator("button")')
    assert updated is None
    assert "broaden" in error.lower(), error


def test_unknown_field_is_not_patched():
    source = (PO / "LoginPage.java").read_text()
    updated, error = locator_patch.apply_to_source(source, "noSuchField", "x")
    assert updated is None and "no declaration" in error


def test_no_op_edit_is_refused():
    source = (PO / "LoginPage.java").read_text()
    updated, error = locator_patch.apply_to_source(
        source, "usernameField", 'page.locator("#user-name")')
    assert updated is None and "already there" in error


def test_collisions_detects_two_locators_on_one_element():
    """saveButton and saveByText legitimately resolve to the same element, so
    healing one onto the other's selector must be reported rather than shipped:
    two tests would exercise one control while both still pass."""
    sources = {p.stem: p.read_text() for p in PO.glob("*.java")}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto((FIX / "v1/app.html").resolve().as_uri())
        problems = locator_patch.collisions(page, sources, "saveButton", "#save-btn")
        browser.close()
    assert problems, "expected a collision with ProfilePage#saveByText"
    assert any("saveByText" in p for p in problems), problems


def test_history_drives_the_circuit_breaker():
    """Three heals inside the window must stop the fourth — a locator that keeps
    moving needs a stable test id, not another heal."""
    cfg = yaml.safe_load(CONFIG.read_text())
    now = dt.datetime.now(dt.timezone.utc)
    baseline = {
        "locator_id": "X#y", "raw_locator": "#y", "action": "click", "element": {},
        "context": {"landmarks": []},
        "history": [{"healed_at": (now - dt.timedelta(days=i)).isoformat(),
                     "from": "#a", "to": "#b", "score": 0.9} for i in range(3)],
    }
    result = heal_mod.heal(page=None, baseline=baseline, cfg=cfg, url="about:blank")
    assert result.verdict == heal_mod.NO_HEAL
    assert result.classification == "UNSTABLE_LOCATOR"
    assert "test id" in result.reason


def test_baseline_adapter_maps_java_record_to_engine_shape():
    """The seam between one-file-per-page-object and one-baseline-per-locator."""
    record = {
        "urlShape": "https://example/app", "coverage": {"loginButton": 1},
        "fingerprints": {"loginButton": {"tag": "button", "accessible_name": "Login"}},
        "healHistory": {"loginButton": [{"healedAt": "2026-01-01T00:00:00+00:00",
                                         "to": "#new", "score": 0.9}]},
    }
    baseline = heal_mod.baseline_for("LoginPage", "loginButton", "#login-button", record)
    assert baseline["locator_id"] == "LoginPage#loginButton"
    assert baseline["element"]["accessible_name"] == "Login"
    assert len(baseline["history"]) == 1 and baseline["history"][0]["score"] == 0.9
    assert heal_mod.baseline_for("LoginPage", "absentField", "#x", record) is None


def test_assertion_locators_are_refused_from_real_source():
    """The guard must work off the code, not a flag nobody sets in production."""
    from shared import locator_assertions

    source = """
    public class CartPage {
        public void addItem() { click(addButton, "Add"); }
        public void verifyBadge() {
            AssertHelper.assertElementText(config, cartBadge, "1", "badge shows one");
        }
    }"""
    fields = locator_assertions.assertion_fields({"CartPage": source},
                                                 ["addButton", "cartBadge"])
    assert fields == {"cartBadge"}, fields

    cfg = yaml.safe_load(CONFIG.read_text())
    baseline = {"locator_id": "CartPage#cartBadge", "raw_locator": ".badge",
                "action": "click", "element": {}, "context": {"landmarks": []},
                "history": []}
    result = heal_mod.heal(page=None, baseline=baseline, cfg=cfg, url="about:blank",
                           assertion_fields=fields)
    assert result.classification == "ASSERTION_LOCATOR"
    assert "read by an assertion" in result.reason


def test_action_locators_are_not_refused():
    from shared import locator_assertions
    source = 'public class CartPage { public void add() { click(addButton, "Add"); } }'
    assert locator_assertions.assertion_fields({"CartPage": source}, ["addButton"]) == set()
