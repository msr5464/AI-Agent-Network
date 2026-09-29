"""Behaviour tests for the three verdicts that were previously unreachable.

Each has a fixture that must produce it and a near-miss that must not. The
near-misses are the point: `PRIOR_STEP_FAILED` and `WRONG_PAGE` are the same
observation at different resolutions, and `ELEMENT_GONE` and `LOCATOR_STALE`
differ only by what a baseline recorded. Getting either boundary wrong is worse
than not drawing it.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import diagnosis
from tests import fixtures as fx


def _setup(tmp_path, snapshot, page_objects, *, context=None, baseline=None,
           execution_log="", failed_selector="img[class*='avatar'] >> nth=0",
           flaky=None, page_object="DashboardPage", baseline_not_after=""):
    """Lay artefacts out the way the framework does, then diagnose."""
    dom = tmp_path / "dom"
    dom.mkdir(exist_ok=True)
    snap = dom / "verifySomething_120000.html"
    snap.write_text(snapshot, encoding="utf-8")
    if context is not None:
        (dom / "verifySomething_120000.context.json").write_text(json.dumps(context))
    if baseline is not None:
        folder = tmp_path / "test-output" / "baselines"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{page_object}.json").write_text(json.dumps(baseline))

    issue = fx.issue(dom_snapshot=str(snap), execution_log=execution_log,
                     failed_selector=failed_selector, page_object=page_object)
    if flaky is not None:
        issue["flaky_tests"] = flaky
    if baseline_not_after:
        issue["baseline_not_after"] = baseline_not_after
    evidence = diagnosis.collect(issue, workspace=tmp_path, page_objects=page_objects)
    return diagnosis.diagnose(evidence), evidence


def _page_objects():
    return [
        {"path": "/repo/app/web/DashboardPage.java", "snippet": fx.DASHBOARD_PAGE_SOURCE},
        {"path": "/repo/app/web/HomePage.java", "snippet": fx.HOME_PAGE_SOURCE},
    ]


CLICKED = ("[00:00:10] ACTION: Clicking Sign In button\n"
           "[00:00:41] Failed to load Element Locator@img[class*='avatar'] in DashboardPage\n")
NAVIGATED = ("[00:00:10] ACTION: Navigating to: https://app.example.com/\n"
             "[00:00:41] Failed to load Element Locator@img[class*='avatar'] in DashboardPage\n")


class TestPriorStepFailed:
    def test_a_click_that_moved_nothing(self, tmp_path):
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_LOGGED_OUT, _page_objects(),
            context=fx.context(navigation=["https://app.example.com/"]),
            execution_log=CLICKED)
        assert verdict["verdict"] == "PRIOR_STEP_FAILED"
        assert verdict["actionable"] is False

    def test_near_miss_the_page_did_navigate(self, tmp_path):
        # Same failure, but the flow moved. That is the more general answer.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_LOGGED_OUT, _page_objects(),
            context=fx.context(navigation=["https://app.example.com/",
                                           "https://app.example.com/next"]),
            execution_log=CLICKED)
        assert verdict["verdict"] == "WRONG_PAGE"

    def test_near_miss_the_last_action_was_not_an_interaction(self, tmp_path):
        # Navigating somewhere and finding the wrong page is not a failed click.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_LOGGED_OUT, _page_objects(),
            context=fx.context(navigation=["https://app.example.com/"]),
            execution_log=NAVIGATED)
        assert verdict["verdict"] == "WRONG_PAGE"


class TestElementGone:
    """Right page, but this element was never on it — even when the test passed."""

    RIGHT_PAGE = {"matched": 1, "evaluable": 2,
                  "details": {"avatarWidget": 0, "userMenu": 1}}

    def test_absent_on_the_last_good_run_too(self, tmp_path):
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE),
            # The avatar matched in an earlier run and matches nothing now, and
            # the last good run already found nothing: it was on the page once
            # and has since been taken off it.
            baseline=fx.baseline_record(
                coverage={"userMenu": 1, "avatarWidget": 0},
                last_seen={"userMenu": "2026-08-01T00:00:00",
                           "avatarWidget": "2026-05-04T00:00:00"}))
        assert verdict["verdict"] == "ELEMENT_GONE"
        assert verdict["actionable"] is False

    def test_a_selector_that_never_matched_is_a_locator_fix_not_a_removal(self, tmp_path):
        """The bug this pins: a passing test promotes counts for every locator on
        every page it loaded, including ones it never went near. A selector that
        has simply always been wrong was therefore written down as matching
        nothing on a run that passed, and read back as proof the element had been
        removed from the product — "confirm with the team before changing the
        test", for a one-word typo in a selector. Never having matched is what
        separates the two, and `lastSeen` is where that lives.
        """
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE),
            baseline=fx.baseline_record(
                coverage={"userMenu": 1, "avatarWidget": 0},
                last_seen={"userMenu": "2026-08-01T00:00:00"}))
        assert verdict["verdict"] == "LOCATOR_STALE"
        assert verdict["actionable"] is True

    def test_a_baseline_older_than_lastseen_abstains(self, tmp_path):
        """Baselines written before the framework recorded `lastSeen` cannot tell
        removal from a selector that never worked, so the verdict that stops a
        locator edit must not rest on them. The next green run writes the history
        and the verdict becomes available again.
        """
        record = fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 0})
        record.pop("lastSeen")
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE), baseline=record)
        assert verdict["verdict"] != "ELEMENT_GONE"

    def test_near_miss_it_was_present_on_the_last_good_run(self, tmp_path):
        # Present before, absent now, everything else unchanged: a renamed element.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE),
            baseline=fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 1}))
        assert verdict["verdict"] == "LOCATOR_STALE"
        assert verdict["actionable"] is True

    def test_a_baseline_written_after_the_failure_is_not_the_last_good_run(self, tmp_path):
        """The bug this pins: a fix landed, a sibling test went green, and the
        baseline it promoted recorded every locator on the page — including the
        broken one it never touched. The next attempt read that zero as proof
        the element had been removed. Pinning the cutoff to the ORIGINAL failure
        makes a record written during the repair inadmissible.
        """
        verdict, evidence = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE),
            baseline=fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 0}),
            # The baseline is stamped 2026-08-01; the run began before that.
            baseline_not_after="2026-07-01T00:00:00")
        assert evidence["baseline"]["available"] is False
        assert verdict["verdict"] != "ELEMENT_GONE"

    def test_a_baseline_that_never_measured_the_element_will_not_guess(self, tmp_path):
        # The rule used to fire when `vanished` was merely absent — and it is
        # absent whenever no per-locator counts could be compared, which is the
        # common case. "Absent on the last good run too" has to be read off that
        # run, not inferred from the silence.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE),
            baseline=fx.baseline_record(coverage={"userMenu": 1}))
        assert verdict["verdict"] == "LOCATOR_STALE"
        assert verdict["actionable"] is True

    def test_without_a_baseline_it_will_not_guess(self, tmp_path):
        # From one run, "removed" and "the selector was always wrong" are the same
        # picture. Claiming either would be inventing evidence.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE))
        assert verdict["verdict"] == "LOCATOR_STALE"


class TestFlakyTransient:
    """Reached only when every deterministic rule has declined."""

    FLAKY = [{"test_name": "SomeTest.verifySomething", "failure_count": 4,
              "last_days": 10, "in_current_run": True}]

    def test_intermittent_history_with_no_structural_cause(self, tmp_path):
        # A page object with too few evaluable locators to judge: the structural
        # rules abstain, so history is all that is left.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_OK,
            [{"path": "/repo/OpaquePage.java", "snippet": fx.OPAQUE_PAGE_SOURCE}],
            page_object="OpaquePage", failed_selector="xpath=//div[@id='header']",
            flaky=self.FLAKY)
        assert verdict["verdict"] == "FLAKY_TRANSIENT"

    def test_a_structural_cause_always_wins(self, tmp_path):
        # Same flaky history, but the page is plainly the wrong one. "It works
        # sometimes" must never pre-empt an explanation.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_LOGGED_OUT, _page_objects(), flaky=self.FLAKY)
        assert verdict["verdict"] == "WRONG_PAGE"

    def test_no_history_means_no_verdict(self, tmp_path):
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_OK,
            [{"path": "/repo/OpaquePage.java", "snippet": fx.OPAQUE_PAGE_SOURCE}],
            page_object="OpaquePage", failed_selector="xpath=//div[@id='header']")
        assert verdict["verdict"] == diagnosis.ABSTAIN

    def test_a_single_failure_is_an_incident_not_a_pattern(self, tmp_path):
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_OK,
            [{"path": "/repo/OpaquePage.java", "snippet": fx.OPAQUE_PAGE_SOURCE}],
            page_object="OpaquePage", failed_selector="xpath=//div[@id='header']",
            flaky=[{"test_name": "SomeTest.verifySomething", "failure_count": 1,
                    "last_days": 10}])
        assert verdict["verdict"] == diagnosis.ABSTAIN
