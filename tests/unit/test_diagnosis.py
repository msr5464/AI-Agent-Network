"""Behaviour tests for shared/diagnosis.py — one case per verdict.

The engine decides whether the healing agent is allowed to edit code, so the two
failure modes that matter are asymmetric and both are covered here:

  * a false stop blocks a fix that would have worked  (test_locator_stale_*)
  * a false LOCATOR_STALE lets it guess at a selector on a page it never reached
    (test_wrong_page_*, test_error_state_*, test_precondition_*)

Every fixture is written against the same page-object source, so the difference
between verdicts is always the DOM, never the code being repaired.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import diagnosis, page_identity
from tests import fixtures as fx


def _page_objects():
    return [
        {"path": "/repo/app/web/DashboardPage.java", "snippet": fx.DASHBOARD_PAGE_SOURCE},
        {"path": "/repo/app/web/HomePage.java", "snippet": fx.HOME_PAGE_SOURCE},
    ]


def _diagnose(snapshot_text, tmp_path, failed_selector="img[class*='avatar'] >> nth=0",
              execution_log="", page_objects=None, **issue_kwargs):
    snapshot = tmp_path / "failure.html"
    snapshot.write_text(snapshot_text, encoding="utf-8")
    issue = fx.issue(dom_snapshot=str(snapshot), execution_log=execution_log,
                     failed_selector=failed_selector, **issue_kwargs)
    evidence = diagnosis.collect(
        issue, workspace=None,
        page_objects=_page_objects() if page_objects is None else page_objects)
    return diagnosis.diagnose(evidence), evidence


class TestWrongPage:
    def test_zero_coverage_with_rival_is_wrong_page(self, tmp_path):
        verdict, evidence = _diagnose(fx.DASHBOARD_LOGGED_OUT, tmp_path)
        assert verdict["verdict"] == "WRONG_PAGE"
        assert verdict["confidence"] == "HIGH"
        assert verdict["actionable"] is False
        assert evidence["expected_coverage"]["matched"] == 0
        assert evidence["expected_coverage"]["evaluable"] == 2

    def test_names_the_page_actually_reached(self, tmp_path):
        verdict, _ = _diagnose(fx.DASHBOARD_LOGGED_OUT, tmp_path)
        assert any("HomePage" in reason for reason in verdict["reasons"])

    def test_state_markers_are_reported(self, tmp_path):
        _, evidence = _diagnose(fx.DASHBOARD_LOGGED_OUT, tmp_path)
        assert {m["marker"] for m in evidence["markers"]} >= {"logged-out"}


class TestLocatorStale:
    """The must-not-regress case: a genuine locator break still gets fixed."""

    def test_surviving_siblings_mean_the_locator_is_stale(self, tmp_path):
        verdict, _ = _diagnose(fx.DASHBOARD_RENAMED, tmp_path)
        assert verdict["verdict"] == "LOCATOR_STALE"
        assert verdict["actionable"] is True
        assert verdict["action"] == "edit the selector"

    def test_healthy_page_is_never_diagnosed_as_wrong(self, tmp_path):
        # Same page, nothing broken at all — must not be called WRONG_PAGE.
        verdict, _ = _diagnose(fx.DASHBOARD_OK, tmp_path,
                               failed_selector="img[class*='avatar'] >> nth=0")
        assert verdict["verdict"] != "WRONG_PAGE"


class TestPresentButNotVisible:
    def test_element_in_dom_is_never_a_stale_locator(self, tmp_path):
        verdict, evidence = _diagnose(fx.DASHBOARD_COVERED, tmp_path)
        assert verdict["verdict"] == "BLOCKED"
        assert evidence["failing_selector_matches"] == 1
        # Replacing a selector that already matches can only weaken the test.
        assert verdict["verdict"] != "LOCATOR_STALE"


class TestAbstention:
    def test_no_snapshot_abstains(self, tmp_path):
        issue = fx.issue(failed_selector="img[class*='avatar']")
        verdict = diagnosis.diagnose(diagnosis.collect(issue, page_objects=_page_objects()))
        assert verdict["verdict"] == diagnosis.ABSTAIN
        assert "no DOM snapshot" in verdict["reasons"][0]

    def test_unevaluable_page_object_abstains(self, tmp_path):
        # Every locator is XPath. Unevaluable must not be read as "absent",
        # which would otherwise look exactly like WRONG_PAGE.
        verdict, evidence = _diagnose(
            fx.DASHBOARD_LOGGED_OUT, tmp_path, page_object="OpaquePage",
            failed_selector="xpath=//div[@id='header']",
            page_objects=[{"path": "/repo/app/web/OpaquePage.java",
                           "snippet": fx.OPAQUE_PAGE_SOURCE}])
        assert verdict["verdict"] == diagnosis.ABSTAIN
        assert evidence["expected_coverage"]["evaluable"] == 0

    def test_unknown_page_object_abstains(self, tmp_path):
        verdict, _ = _diagnose(fx.DASHBOARD_LOGGED_OUT, tmp_path,
                               page_object="NotAPageAnywhere", page_objects=[])
        assert verdict["verdict"] == diagnosis.ABSTAIN


class TestRouting:
    def test_stop_verdicts_are_not_actionable(self):
        for verdict in diagnosis.STOP:
            assert verdict not in diagnosis.ACTIONS

    def test_every_actionable_verdict_names_its_action(self):
        assert all(action for action in diagnosis.ACTIONS.values())

    def test_only_a_stale_locator_authorises_an_edit(self):
        # Anything else the agent can diagnose, it reports. Widening this set is a
        # decision about what the agent is allowed to change, not a refactor.
        assert set(diagnosis.ACTIONS) == {"LOCATOR_STALE"}

    def test_describe_marks_stop_verdicts(self, tmp_path):
        verdict, evidence = _diagnose(fx.DASHBOARD_LOGGED_OUT, tmp_path)
        assert "not a locator problem" in diagnosis.describe(verdict, evidence)[0]


class TestExpectedPageObject:
    @pytest.mark.parametrize("issue_field,text", [
        ("error_message", "Failed to load Element Locator@x in DashboardPage"),
        ("stack_trace", "DashboardPage.java:19"),
        ("root_cause", "could not find the widget in CheckoutPage"),
    ])
    def test_recovered_from_each_field(self, issue_field, text):
        expected = "CheckoutPage" if "Checkout" in text else "DashboardPage"
        assert diagnosis.expected_page_object({issue_field: text}) == expected

    def test_absent_when_nothing_names_one(self):
        assert diagnosis.expected_page_object({"error_message": "boom"}) == ""
