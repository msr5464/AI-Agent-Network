"""Unit tests for shared/step_provenance.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import step_provenance

LOG = (
    "[00:00:33] STEP: Loading stored session from: Session.json\n"
    "[00:00:49] ACTION: Navigating to: https://app.example.com/\n"
    "[00:01:20] Failed to load Element Locator@img[class*='avatar'] in DashboardPage\n"
    "[00:01:20] ------------END OF EXECUTION------------\n"
)


class TestSummarize:
    def test_counts_steps_and_actions(self):
        summary = step_provenance.summarize(LOG)
        assert (summary["steps"], summary["actions"]) == (1, 1)
        assert summary["last_action"].startswith("Navigating to")

    def test_measures_the_gap_before_the_failure(self):
        assert step_provenance.summarize(LOG)["gap_before_failure_s"] == 31

    def test_recognises_a_wait_that_ran_the_clock_out(self):
        assert step_provenance.summarize(LOG, budget_s=30)["burned_full_budget"] is True

    def test_a_fast_failure_did_not_burn_the_budget(self):
        quick = ("[00:00:10] ACTION: Clicking submit\n"
                 "[00:00:12] Failed to load Element x in SomePage\n")
        assert step_provenance.summarize(quick, budget_s=30)["burned_full_budget"] is False

    def test_budget_unknown_leaves_the_judgement_unmade(self):
        assert step_provenance.summarize(LOG)["burned_full_budget"] is None

    def test_empty_log_is_unavailable_not_empty(self):
        # "we cannot see what it did" must stay distinct from "it did nothing".
        assert step_provenance.summarize("")["available"] is False

    def test_untimestamped_output_is_ignored(self):
        assert step_provenance.summarize("mvn output\nno timestamps")["available"] is False


class TestDescribe:
    def test_mentions_the_full_budget_when_it_was_burned(self):
        text = step_provenance.describe(step_provenance.summarize(LOG, budget_s=30))
        assert "full wait budget" in text

    def test_empty_when_nothing_was_parsed(self):
        assert step_provenance.describe(step_provenance.summarize("")) == ""
