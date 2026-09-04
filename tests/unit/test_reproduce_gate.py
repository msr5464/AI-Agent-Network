"""Tests for the diagnosis gate wired into 00_reproduce.py.

Two behaviours matter here and neither is about the engine itself:

  * shadow mode must change nothing. It exists so the verdicts can be measured
    against real outcomes before they are allowed to refuse work.
  * an inferred trace selector must stop short-circuiting the shape check.
    `_polled_to_death` synthesises one whenever a wait loop ran out of patience,
    which is equally what a page that never loaded produces.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agents" / "test-healing-agent"


@pytest.fixture(scope="module")
def reproduce(tmp_path_factory):
    """Import the step module under its own `lib` package.

    Both agents ship a top-level `lib`, so whichever test imports first owns
    `sys.modules["lib"]` and the other agent's imports then fail. The import is
    therefore bracketed: `lib*` is evicted, this agent's directory goes to the
    front of the path, and both are restored afterwards so the tests that run
    next are unaffected either way.
    """
    os.environ.setdefault("AUDIT_DIR", str(tmp_path_factory.mktemp("audit")))

    saved_path = list(sys.path)
    saved_modules = {name: module for name, module in sys.modules.items()
                     if name == "lib" or name.startswith("lib.")}
    for name in saved_modules:
        del sys.modules[name]
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(AGENT))
    try:
        spec = importlib.util.spec_from_file_location(
            "reproduce_step", AGENT / "actions" / "00_reproduce.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in [n for n in sys.modules
                     if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


class TestInferredSelectorDemotion:
    def test_a_real_trace_error_still_means_locator(self, reproduce):
        shape, reason = reproduce.classify_failure_shape(
            "some unrecognisable output", trace_selector="#submit",
            trace_selector_inferred=False)
        assert shape == "LOCATOR"
        assert "#submit" in reason

    def test_an_inferred_selector_no_longer_short_circuits(self, reproduce):
        # Same selector, but synthesised from repeated polling. With no other
        # locator signal in the text there is nothing to conclude, and the old
        # code would have said LOCATOR with high confidence.
        shape, _ = reproduce.classify_failure_shape(
            "some unrecognisable output", trace_selector="#submit",
            trace_selector_inferred=True)
        assert shape == "UNKNOWN"

    def test_an_inferred_selector_still_defers_to_infra_signals(self, reproduce):
        shape, _ = reproduce.classify_failure_shape(
            "Communications link failure", trace_selector="#submit",
            trace_selector_inferred=True)
        assert shape == "INFRA_DB"

    def test_genuine_locator_wording_is_unaffected(self, reproduce):
        shape, _ = reproduce.classify_failure_shape(
            "Failed to load Element Locator@#x in SomePage",
            trace_selector="#x", trace_selector_inferred=True)
        assert shape == "LOCATOR"


class TestWaitBudget:
    def test_reads_the_frameworks_configured_timeout(self, reproduce, tmp_path):
        (tmp_path / "parameters").mkdir()
        (tmp_path / "parameters/config.properties").write_text(
            "endExecutionOnFailure=false\nObjectWaitTime=30\n")
        assert reproduce.wait_budget_seconds(tmp_path) == 30

    def test_absent_config_is_unknown_not_zero(self, reproduce, tmp_path):
        assert reproduce.wait_budget_seconds(tmp_path) is None


class TestSkipReasonMapping:
    def test_recoverable_causes_keep_the_handoff_queued(self):
        from shared import diagnosis
        # The environment will come back; leaving the work queued is right.
        assert diagnosis.skip_reason("ENV_UNREACHABLE") == "infra"

    def test_deterministic_causes_consume_the_handoff(self):
        from shared import diagnosis
        # Re-running this nightly produces the identical diagnosis forever.
        assert diagnosis.skip_reason("WRONG_PAGE") == "diagnosed"

    def test_actionable_verdicts_are_not_skips(self):
        from shared import diagnosis
        assert diagnosis.skip_reason("LOCATOR_STALE") == "no-work"


class TestRelabel:
    """A verdict that is not allowed to act is not allowed to relabel either.

    `root_cause_category` is what the fix step keys off, so rewriting it in
    shadow mode let an unenforced verdict reach the model as the classification
    and suppress the page-object lookup that would have contradicted it.
    """

    def _issue(self):
        return {"root_cause_category": "ELEMENT_NOT_FOUND",
                "recommended_action": "Update the broken locator"}

    def test_shadow_mode_changes_nothing(self, reproduce):
        issue = self._issue()
        reproduce.relabel(issue, {"verdict": "WRONG_PAGE",
                                  "remediation": "fix what happens before it"}, "shadow")
        assert issue["root_cause_category"] == "ELEMENT_NOT_FOUND"
        assert issue["recommended_action"] == "Update the broken locator"

    def test_enforce_applies_the_verdict(self, reproduce):
        issue = self._issue()
        reproduce.relabel(issue, {"verdict": "WRONG_PAGE",
                                  "remediation": "fix what happens before it"}, "enforce")
        assert issue["root_cause_category"] == "WRONG_PAGE"
        assert issue["recommended_action"] == "fix what happens before it"

    def test_a_stale_locator_maps_to_the_category_the_fixer_knows(self, reproduce):
        issue = dict(self._issue(), root_cause_category="TIMEOUT")
        reproduce.relabel(issue, {"verdict": "LOCATOR_STALE"}, "enforce")
        assert issue["root_cause_category"] == "ELEMENT_NOT_FOUND"

    def test_an_abstention_relabels_nothing(self, reproduce):
        issue = self._issue()
        reproduce.relabel(issue, {"verdict": "INSUFFICIENT_EVIDENCE"}, "enforce")
        assert issue["root_cause_category"] == "ELEMENT_NOT_FOUND"


class TestGateDecision:
    """Shadow must be inert; enforce must stop; FORCE must always win."""

    STOP_VERDICT = {"verdict": "WRONG_PAGE", "confidence": "HIGH",
                    "reasons": ["0 of 2 locators match"]}
    FIX_VERDICT = {"verdict": "LOCATOR_STALE", "confidence": "HIGH",
                   "reasons": ["1 of 2 locators match"]}

    def test_shadow_leaves_the_shape_untouched(self, reproduce):
        shape, reason, note = reproduce.gate_decision(
            "LOCATOR", "matched locator signal", self.STOP_VERDICT, "shadow", False)
        assert (shape, reason) == ("LOCATOR", "matched locator signal")
        assert "would have stopped" in note

    def test_enforce_replaces_the_shape_with_the_verdict(self, reproduce):
        shape, reason, note = reproduce.gate_decision(
            "LOCATOR", "matched locator signal", self.STOP_VERDICT, "enforce", False)
        assert shape == "WRONG_PAGE"
        assert reason == "0 of 2 locators match"
        assert note == ""

    def test_force_overrides_enforce(self, reproduce):
        shape, _, note = reproduce.gate_decision(
            "LOCATOR", "r", self.STOP_VERDICT, "enforce", True)
        assert shape == "LOCATOR"
        assert "FORCE=true" in note

    def test_actionable_verdicts_never_gate(self, reproduce):
        for mode in ("shadow", "enforce"):
            shape, _, note = reproduce.gate_decision(
                "LOCATOR", "r", self.FIX_VERDICT, mode, False)
            assert (shape, note) == ("LOCATOR", "")

    def test_abstention_never_gates(self, reproduce):
        shape, _, note = reproduce.gate_decision(
            "LOCATOR", "r", {"verdict": "INSUFFICIENT_EVIDENCE"}, "enforce", False)
        assert (shape, note) == ("LOCATOR", "")

    def test_a_failed_diagnosis_never_gates(self, reproduce):
        # collect() raising must leave the pre-existing behaviour in charge.
        shape, _, note = reproduce.gate_decision("LOCATOR", "r", {}, "enforce", False)
        assert (shape, note) == ("LOCATOR", "")
