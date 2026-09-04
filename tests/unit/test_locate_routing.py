"""Who owns a broken locator: Locate, Fix, or nobody.

Locate records why it stopped, and Fix used to discard that entirely — in shadow
mode `locate_resolution()` returns None immediately, so Fix restarted blind and
re-derived a worse answer than the one already proved. Routing on the verdict is
what turns two searchers into a first line and an escalation.

Two verdicts are deliberate refusals that Fix must honour rather than override.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_fix_module():
    """01_fix.py is not an importable name, so load it by path.

    It reads AUDIT_DIR and HANDOFF_FILE at import time; neither is touched by the
    pure functions under test, so placeholders are enough.
    """
    import os
    os.environ.setdefault("AUDIT_DIR", str(ROOT / "tests" / "fixtures"))
    os.environ.setdefault("HANDOFF_FILE", str(ROOT / "tests" / "fixtures" / "none.json"))
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_fix.py"
    spec = importlib.util.spec_from_file_location("healing_fix", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["healing_fix"] = module
    spec.loader.exec_module(module)
    return module


fix = pytest.importorskip("bs4") and _load_fix_module()


class TestLocateRoute:
    def test_a_proved_heal_is_consumed(self):
        route, note = fix.locate_route({
            "verdict": "HEALED", "new_locator": "#summary img[alt='PencilSimple']",
            "strategy": "scoped-by-ancestor", "score": 0.932, "verification": "WEAK"})
        assert route == "consume"
        assert "PencilSimple" in note

    @pytest.mark.parametrize("classification", [
        "ASSERTION_LOCATOR",   # healing it hides a real regression
        "UNSTABLE_LOCATOR",    # healed repeatedly; needs a test id
        "MISBOUND",            # resolves to the wrong element
    ])
    def test_deliberate_refusals_are_honoured_not_overridden(self, classification):
        route, note = fix.locate_route(
            {"verdict": "NO_HEAL", "classification": classification})
        assert route == "defer"
        assert note

    @pytest.mark.parametrize("verdict,classification", [
        ("NO_BASELINE", ""),          # nothing recorded to compare against
        ("NO_DECLARATION", ""),       # no page object declares it
        ("NO_HEAL", "NO_STABLE_LOCATOR"),   # found it, could not express it
        ("NO_HEAL", "LOW_CONFIDENCE"),      # ambiguous or unverified
        ("NO_HEAL", ""),
    ])
    def test_everything_else_is_fixs_to_own(self, verdict, classification):
        route, _ = fix.locate_route(
            {"verdict": verdict, "classification": classification,
             "reason": "no recorded good run"})
        assert route == "own"

    def test_no_locate_result_means_fix_owns_it(self):
        route, note = fix.locate_route({})
        assert route == "own"
        assert "did not run" in note

    def test_the_refusal_reason_is_carried_forward(self):
        """The reason reaches the prompt, so the model does not retry the same ground."""
        _, note = fix.locate_route(
            {"verdict": "NO_HEAL", "classification": "NO_STABLE_LOCATOR",
             "reason": "found the element but could not express it uniquely"})
        assert "could not express it uniquely" in note


class TestLocateOutcome:
    def test_matches_on_the_failing_selector(self, monkeypatch):
        monkeypatch.setattr(fix, "load_locate_resolutions", lambda: [
            {"failed_selector": "#a", "verdict": "NO_BASELINE"},
            {"failed_selector": "#b", "verdict": "HEALED"}])
        assert fix.locate_outcome({"failed_selector": "#b"})["verdict"] == "HEALED"

    def test_an_unrelated_selector_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(fix, "load_locate_resolutions", lambda: [
            {"failed_selector": "#a", "verdict": "HEALED"}])
        assert fix.locate_outcome({"failed_selector": "#zzz"}) == {}

    def test_no_failing_selector_returns_nothing(self):
        assert fix.locate_outcome({}) == {}

    def test_reported_in_shadow_mode_too(self, monkeypatch):
        """locate_resolution() withholds in shadow because it APPLIES an answer.
        locate_outcome() only reports one, so it must not withhold."""
        monkeypatch.setattr(fix, "LOCATE_MODE", "shadow")
        monkeypatch.setattr(fix, "load_locate_resolutions", lambda: [
            {"failed_selector": "#b", "verdict": "HEALED", "new_expression": "x"}])
        ctx = {"failed_selector": "#b"}
        assert fix.locate_resolution(ctx) is None      # unchanged
        assert fix.locate_outcome(ctx)["verdict"] == "HEALED"
