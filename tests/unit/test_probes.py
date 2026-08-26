"""Tests for probe policy (shared/diagnosis.py) and execution (lib/probes.py).

A probe exists to stop the agent acting on an unmeasured hypothesis. The rule
that matters most is what happens when one *disagrees*: the verdict must fall
back to abstention rather than flip to its opposite, because a refutation says
the reasoning was wrong, not what the truth is.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import diagnosis


def _verdict(name, confidence="MEDIUM"):
    return {"verdict": name, "confidence": confidence, "reasons": ["because"],
            "actionable": name in diagnosis.ACTIONS,
            "action": diagnosis.ACTIONS.get(name, "")}


class TestNeedsProbe:
    def test_high_confidence_needs_no_probe(self):
        assert diagnosis.needs_probe(_verdict("WRONG_PAGE", "HIGH")) is False

    def test_medium_confidence_is_worth_measuring(self):
        assert diagnosis.needs_probe(_verdict("WRONG_PAGE", "MEDIUM")) is True

    def test_abstention_is_not_probed(self):
        assert diagnosis.needs_probe(_verdict(diagnosis.ABSTAIN, "LOW")) is False

    def test_locator_stale_is_not_probed(self):
        # The existing verify-then-revert loop already measures this one.
        assert diagnosis.needs_probe(_verdict("LOCATOR_STALE", "MEDIUM")) is False

    def test_every_probe_names_a_confirming_outcome(self):
        assert all(spec["confirms_when"] for spec in diagnosis.PROBES.values())


class TestApplyProbe:
    def test_confirmation_promotes_to_high(self):
        result = diagnosis.apply_probe(_verdict("WRONG_PAGE"), "same_dom")
        assert result["verdict"] == "WRONG_PAGE"
        assert result["confidence"] == "HIGH"
        assert result["probe"]["confirmed"] is True

    def test_refutation_abstains_rather_than_inverts(self):
        result = diagnosis.apply_probe(_verdict("WRONG_PAGE"), "different_dom")
        assert result["verdict"] == diagnosis.ABSTAIN
        assert result["actionable"] is False

    def test_a_passing_rerun_refutes_an_environment_verdict(self):
        result = diagnosis.apply_probe(_verdict("ENV_UNREACHABLE"), "passed")
        assert result["verdict"] == diagnosis.ABSTAIN

    def test_a_passing_rerun_confirms_flakiness(self):
        result = diagnosis.apply_probe(_verdict("FLAKY_TRANSIENT"), "passed")
        assert result["confidence"] == "HIGH"

    def test_a_larger_budget_that_passes_proves_the_fix(self):
        # The probe is the experiment: the change is demonstrated, not guessed.
        result = diagnosis.apply_probe(_verdict("TOO_SLOW"), "passed")
        assert result.get("proven") is True

    def test_inconclusive_leaves_the_verdict_alone(self):
        result = diagnosis.apply_probe(_verdict("WRONG_PAGE"), "inconclusive")
        assert result["verdict"] == "WRONG_PAGE"
        assert result["confidence"] == "MEDIUM"

    def test_reasons_record_what_the_probe_did(self):
        result = diagnosis.apply_probe(_verdict("WRONG_PAGE"), "same_dom")
        assert any("probe" in reason for reason in result["reasons"])
