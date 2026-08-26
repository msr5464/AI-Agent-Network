"""Tests for the triaging agent's diagnosis integration.

Two failure modes, in opposite directions, both previously live:

  * ELEMENT_NOT_FOUND was the only category forwarded, so a page that was merely
    slow or covered — both fixable — stayed red forever under TIMEOUT;
  * and a wrong-page failure came through wearing the ELEMENT_NOT_FOUND label,
    so the healing agent was handed work no selector edit could do.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agents" / "test-triaging-agent"
sys.path.insert(0, str(ROOT))

from shared import diagnosis


@pytest.fixture(scope="module")
def classify(tmp_path_factory):
    """Import 03_classify with the triaging agent's own `lib` package."""
    tmp = tmp_path_factory.mktemp("tri")
    os.environ.setdefault("AUDIT_DIR", str(tmp))
    saved_path, saved = list(sys.path), {
        n: m for n, m in sys.modules.items() if n == "lib" or n.startswith("lib.")}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(AGENT))
    try:
        spec = importlib.util.spec_from_file_location(
            "classify_step", AGENT / "actions" / "03_classify.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in [n for n in sys.modules if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]
        sys.modules.update(saved)
        sys.path[:] = saved_path


class TestCategoryEnum:
    def test_legacy_categories_still_validate(self, classify):
        # Historical reports and category_rules.py key off these.
        for legacy in ("ELEMENT_NOT_FOUND", "TIMEOUT", "ASSERTION_FAILURE",
                       "ENVIRONMENT_ISSUE", "CODE_ISSUE", "OTHER"):
            assert legacy in classify.VALID_CATEGORIES

    def test_new_verdicts_validate(self, classify):
        for verdict in ("WRONG_PAGE", "DATA_PRECONDITION", "NOT_READY", "BLOCKED"):
            assert verdict in classify.VALID_CATEGORIES

    def test_an_unknown_category_is_still_coerced(self, classify):
        coerced = classify.validate_classification(
            {"classification": "AUTOMATION_ISSUE", "confidence": "HIGH",
             "root_cause_category": "MADE_UP"})
        assert coerced["root_cause_category"] == "OTHER"


class TestHandoffEligibility:
    """The filter 05_ship applies, restated so its intent is pinned down."""

    ACTIONABLE = set(diagnosis.ACTIONS) | {"ELEMENT_NOT_FOUND"}

    def _eligible(self, category, classification="AUTOMATION_ISSUE", confidence="HIGH"):
        return (classification == "AUTOMATION_ISSUE" and confidence == "HIGH"
                and category in self.ACTIONABLE and category not in diagnosis.STOP)

    def test_stale_locators_are_still_forwarded(self):
        assert self._eligible("ELEMENT_NOT_FOUND") is True
        assert self._eligible("LOCATOR_STALE") is True

    def test_timing_and_obstruction_causes_are_not_forwarded(self):
        # These are diagnosable but not fixable by editing a test: the framework
        # has no per-element wait budget, so "give it more time" would slow every
        # test in the suite and hide the fact that the page got slower. They stop
        # with a precise remediation instead of being handed to the fixer.
        for verdict in ("NOT_READY", "TOO_SLOW", "BLOCKED"):
            assert self._eligible(verdict) is False

    def test_stop_verdicts_are_never_forwarded(self):
        for verdict in diagnosis.STOP:
            assert self._eligible(verdict) is False

    def test_product_bugs_are_never_forwarded(self):
        assert self._eligible("ELEMENT_NOT_FOUND", classification="PRODUCT_BUG") is False

    def test_low_confidence_is_never_forwarded(self):
        assert self._eligible("ELEMENT_NOT_FOUND", confidence="MEDIUM") is False


class TestDiagnoseFailures:
    def test_abstentions_are_left_for_the_model(self, classify):
        # No artefacts at all, so the engine has nothing to say and must not
        # invent a verdict that would bypass classification.
        failures = [{"full_name": "a.b.C.d", "error_message": "boom",
                     "execution_log": "", "stack_trace": ""}]
        assert classify.diagnose_failures(failures, "") == {}

    def test_a_broken_failure_record_does_not_stop_the_step(self, classify):
        assert classify.diagnose_failures([{"full_name": "x"}], "") == {}
