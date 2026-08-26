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
           flaky=None, page_object="DashboardPage"):
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
            # The baseline records userMenu present and the avatar never present,
            # so nothing that used to work has gone missing.
            baseline=fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 0}))
        assert verdict["verdict"] == "ELEMENT_GONE"
        assert verdict["actionable"] is False

    def test_near_miss_it_was_present_on_the_last_good_run(self, tmp_path):
        # Present before, absent now, everything else unchanged: a renamed element.
        verdict, _ = _setup(
            tmp_path, fx.DASHBOARD_RENAMED, _page_objects(),
            context=fx.context(coverage=self.RIGHT_PAGE),
            baseline=fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 1}))
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
