"""What the retry budget is actually counting.

The bug this pins: `HEALING_RETRY_COUNT` capped *attempts*, and an attempt that
repaired one locator and moved the test on to the next one is recorded as a
failure — the run is still red. So a chain of broken locators spent its budget on
progress: six links, four attempts, and the run shipped half-repaired. A locator
that was genuinely stuck, meanwhile, burned all four verification runs.

The budget now counts attempts that moved nothing, under an absolute ceiling.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(retry_count="4", max_attempts="12"):
    os.environ.update({
        "HEALING_RETRY_COUNT": retry_count, "HEALING_MAX_ATTEMPTS": max_attempts,
        # A throwaway directory: importing the fix step writes metrics rows into
        # whatever AUDIT_DIR points at, and this assignment outlives the module —
        # pointing it at tests/fixtures edits the fixture tree on every run.
        "AUDIT_DIR": tempfile.mkdtemp(prefix="retry-budget-"),
        "HANDOFF_FILE": str(ROOT / "tests" / "fixtures" / "none.json"),
    })
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_fix.py"
    spec = importlib.util.spec_from_file_location("healing_fix_budget", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_fix_budget"] = mod
    spec.loader.exec_module(mod)
    return mod


fix = pytest.importorskip("bs4") and _load()


class TestProgressIsNotARetry:
    def test_an_attempt_that_advanced_resets_the_counter(self):
        assert fix.stuck_after(advanced=2, previous_stuck=3) == 0

    def test_an_attempt_that_moved_nothing_counts(self):
        assert fix.stuck_after(advanced=0, previous_stuck=1) == 2

    def test_a_chain_keeps_going_however_long_it_is(self):
        # Six links, every attempt red, every attempt progress.
        stuck = 0
        for attempt in range(1, 7):
            stuck = fix.stuck_after(advanced=1, previous_stuck=stuck)
            assert fix.retry_verdict("false", stuck, attempt) == "retry"


class TestStoppingConditions:
    def test_a_green_gate_stops(self):
        assert fix.retry_verdict("true", 0, 1).startswith("stop:")

    def test_a_skipped_gate_stops(self):
        assert fix.retry_verdict("skipped", 0, 1).startswith("stop:")

    def test_stuck_attempts_stop_at_the_retry_count(self):
        assert fix.retry_verdict("false", 3, 3) == "retry"
        assert "no progress" in fix.retry_verdict("false", 4, 4)

    def test_the_ceiling_stops_even_while_progressing(self):
        # Progress must not be able to spin forever.
        verdict = fix.retry_verdict("false", 0, 12)
        assert "ceiling" in verdict

    def test_the_ceiling_outranks_the_retry_count(self):
        # Reported as the ceiling, not as "no progress" — they are different
        # problems and the operator acts on them differently.
        assert "ceiling" in fix.retry_verdict("false", 9, 12)
