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


def _with_context(tmp_path, wait_error="", **context_kwargs):
    """Write a failure context and return the issue field that points at it."""
    payload = fx.context(**context_kwargs)
    if wait_error:
        payload["failure"]["waitError"] = wait_error
    path = tmp_path / "failure.context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {"failure_context": str(path)}


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


class TestAmbiguousLocator:
    """The bug this pins: a selector matching two elements was read as proof that
    the locator was fine.

    `button[type=\'submit\']` matched both "Login" and "Use OTP to Login" on the
    same form. Playwright throws a strict-mode violation the instant such a wait
    starts, so the wait ended in 28ms of a 30s budget — and the engine reported
    BLOCKED, "present but never became visible", "not a locator problem", with the
    remedy "dismiss what is over it". Every clause of that was wrong, and the one
    real defect (a selector matching two things) was the one thing not mentioned.
    """

    def test_two_live_matches_is_a_locator_defect(self, tmp_path):
        verdict, _ = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 2, "visible": True}],
                            elapsed_ms=28, budget_ms=30000))
        assert verdict["verdict"] == "AMBIGUOUS_LOCATOR"
        assert verdict["actionable"] is True
        assert verdict["action"] == "narrow the selector so it matches exactly one element"
        assert "AMBIGUOUS_LOCATOR" not in diagnosis.STOP

    def test_says_the_wait_never_ran_its_budget(self, tmp_path):
        verdict, _ = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 2, "visible": True}],
                            elapsed_ms=28, budget_ms=30000))
        assert any("28ms of a 30000ms budget" in reason for reason in verdict["reasons"])

    def test_strict_mode_violation_is_believed_over_a_snapshot_count(self, tmp_path):
        # No anchors to count on, so the match total comes from the saved markup —
        # which cannot see that the real locator was chained off a parent. The
        # framework naming the error is the evidence that settles it.
        verdict, evidence = _diagnose(
            fx.DASHBOARD_AMBIGUOUS, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@other", "count": 3,
                                      "visible": False}],
                            wait_error="PlaywrightException: strict mode violation: "
                                       "locator(\"img.avatar-user\") resolved to 2 "
                                       "elements"))
        assert evidence["matches_source"] == "snapshot"
        assert verdict["verdict"] == "AMBIGUOUS_LOCATOR"

    def test_a_snapshot_count_alone_does_not_convict(self, tmp_path):
        # Two document-wide matches say nothing about a locator that may be scoped
        # to a frame or chained off a parent. Without a live count or a named
        # strict-mode error, this rule must decline rather than guess.
        _, evidence = _diagnose(fx.DASHBOARD_AMBIGUOUS, tmp_path,
                                failed_selector="img.avatar-user")
        assert evidence["matches_source"] == "snapshot"
        verdict = diagnosis.diagnose(evidence)
        assert verdict["verdict"] != "AMBIGUOUS_LOCATOR"

    def test_one_match_is_never_ambiguous(self, tmp_path):
        verdict, _ = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 1, "visible": False}]))
        assert verdict["verdict"] != "AMBIGUOUS_LOCATOR"


class TestBlockedRequiresItsEvidence:
    """BLOCKED asserts two things. It may not be reached when either is refuted."""

    def test_declines_when_the_element_was_measured_visible(self, tmp_path):
        # "Never became visible" against a channel that looked and saw it visible.
        verdict, evidence = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 1, "visible": True}]))
        assert verdict["verdict"] != "BLOCKED"
        assert any("measured visible" in note for note in evidence["notes"])

    def test_declines_when_the_wait_did_not_time_out(self, tmp_path):
        verdict, evidence = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 1, "visible": False}],
                            elapsed_ms=28, budget_ms=30000))
        assert verdict["verdict"] != "BLOCKED"
        assert any("threw rather than timed out" in note for note in evidence["notes"])

    def test_still_fires_for_a_genuinely_hidden_element(self, tmp_path):
        # The case the rule was written for must survive both gates: the wait spent
        # its whole budget and the element was hidden at the end of it.
        verdict, _ = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 1, "visible": False}],
                            elapsed_ms=30029, budget_ms=30000))
        assert verdict["verdict"] == "BLOCKED"
        assert verdict["actionable"] is False

    def test_a_visible_anchor_is_reported_in_the_log(self, tmp_path):
        verdict, evidence = _diagnose(
            fx.DASHBOARD_COVERED, tmp_path, failed_selector="img.avatar-user",
            **_with_context(tmp_path, kind="ELEMENT_INTERACTION",
                            anchors=[{"selector": "Locator@img.avatar-user",
                                      "count": 2, "visible": True}],
                            elapsed_ms=28, budget_ms=30000))
        assert any("the element was visible" in line
                   for line in diagnosis.describe(verdict, evidence))


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


class TestEvidenceBelongsToThisFailure:
    """Where the numbers came from matters as much as what they say.

    A live measurement outranks a snapshot approximation, so anything wearing
    that badge is believed without argument. The badge has to be earned per
    failure: a context describing a different run, or a different page, or a
    different element, is read with exactly the authority of a real one.
    """

    def _context_beside(self, tmp_path, snapshot_text, payload, name="failure"):
        snapshot = tmp_path / f"{name}.html"
        snapshot.write_text(snapshot_text, encoding="utf-8")
        (tmp_path / f"{name}.context.json").write_text(json.dumps(payload))
        return snapshot

    def test_coverage_for_another_page_is_not_the_expected_page(self, tmp_path):
        # The failure names DashboardPage; the context measured LoginPage. Left
        # unchecked this reports "0 of 3 of its own locators match" about a page
        # nobody asked about, and the DOM is never consulted.
        snapshot = self._context_beside(
            tmp_path, fx.DASHBOARD_RENAMED,
            fx.context(page_object="LoginPage",
                       coverage={"matched": 0, "evaluable": 3,
                                 "details": {"user": 0, "pass": 0, "submit": 0}}))
        issue = fx.issue(dom_snapshot=str(snapshot),
                         failed_selector="img[class*='avatar'] >> nth=0")
        evidence = diagnosis.collect(issue, workspace=None,
                                     page_objects=_page_objects())
        assert evidence["expected_coverage"]["name"] == "DashboardPage"
        assert any("measured LoginPage" in note for note in evidence["notes"])
        # It is still evidence — about a rival page, which is what it describes.
        assert any(report["name"] == "LoginPage" for report in evidence["coverage"])

    def test_anchors_for_another_element_do_not_answer_for_this_one(self, tmp_path):
        # The page-load anchor counted a different element. Reading its zero as
        # the failing selector's count is how a covered element and an absent one
        # become indistinguishable.
        snapshot = self._context_beside(
            tmp_path, fx.DASHBOARD_COVERED,
            fx.context(anchors=[{"selector": "Locator@nav.sidebar",
                                 "count": 0, "visible": False}]))
        issue = fx.issue(dom_snapshot=str(snapshot),
                         failed_selector="img[class*='avatar'] >> nth=0")
        evidence = diagnosis.collect(issue, workspace=None,
                                     page_objects=_page_objects())
        # Measured from the DOM instead: the avatar really is present.
        assert evidence["failing_selector_matches"] == 1

    def test_anchors_for_this_element_are_used(self, tmp_path):
        snapshot = self._context_beside(
            tmp_path, fx.DASHBOARD_RENAMED,
            fx.context(anchors=[{"selector": "Locator@img[class*='avatar']",
                                 "count": 4, "visible": False}]))
        issue = fx.issue(dom_snapshot=str(snapshot),
                         failed_selector="img[class*='avatar'] >> nth=0")
        evidence = diagnosis.collect(issue, workspace=None,
                                     page_objects=_page_objects())
        assert evidence["failing_selector_matches"] == 4

    def test_a_context_from_another_run_is_not_read(self, tmp_path):
        # No context beside the snapshot, and the only file for the method is
        # days old. This is the failure that prompted the change.
        snapshot = tmp_path / "verifySomething_120000.html"
        snapshot.write_text(fx.DASHBOARD_RENAMED, encoding="utf-8")
        (tmp_path / "verifySomething_190032.context.json").write_text(json.dumps(
            fx.context(page_object="LoginPage", failedAt="2026-08-01T19:00:32")))
        issue = fx.issue(dom_snapshot=str(snapshot),
                         failed_selector="img[class*='avatar'] >> nth=0")
        evidence = diagnosis.collect(issue, workspace=None,
                                     page_objects=_page_objects())
        assert evidence["context"]["available"] is False
        assert any("another run" in note for note in evidence["notes"])
        assert diagnosis.diagnose(evidence)["verdict"] == "LOCATOR_STALE"


class TestPageObjectAttribution:
    """Naming the page object the failure is about.

    Only the page-load assertion says "in DashboardPage". Every interaction
    failure reports an element name and a selector and nothing about who owns
    them, so the coverage signal used to switch off entirely for the majority of
    failures — and a diagnosis with no expected page can only abstain.
    """

    CLICK_FAILURE = (
        "Failed to click on element 'Avatar' with locator: "
        "Locator@img[class*='avatar']: Error {\n  message='Timeout 30000ms exceeded.")

    def _workspace(self, tmp_path):
        folder = tmp_path / "src" / "main" / "java" / "app" / "web"
        folder.mkdir(parents=True)
        (folder / "DashboardPage.java").write_text(fx.DASHBOARD_PAGE_SOURCE)
        (folder / "HomePage.java").write_text(fx.HOME_PAGE_SOURCE)
        return tmp_path

    def test_the_owner_of_the_failing_selector(self, tmp_path):
        workspace = self._workspace(tmp_path)
        issue = {"error_message": self.CLICK_FAILURE, "execution_log": "",
                 "failed_selector": "img[class*='avatar']"}
        assert diagnosis.expected_page_object(issue, workspace) == "DashboardPage"

    def test_the_page_load_wording_still_wins(self, tmp_path):
        # Cheaper and more direct: it names the page object outright.
        issue = fx.issue(failed_selector="img[class*='avatar']")
        assert diagnosis.expected_page_object(issue) == "DashboardPage"

    def test_no_workspace_no_guess(self):
        issue = {"error_message": self.CLICK_FAILURE, "execution_log": "",
                 "failed_selector": "img[class*='avatar']"}
        assert diagnosis.expected_page_object(issue) == ""

    def test_an_interaction_failure_reaches_a_verdict(self, tmp_path):
        # End to end: the shape that used to abstain for want of a page object.
        workspace = self._workspace(tmp_path)
        snapshot = workspace / "failure.html"
        snapshot.write_text(fx.DASHBOARD_RENAMED, encoding="utf-8")
        issue = {"test_name": "app.tests.SomeTest.verifySomething",
                 "error_message": self.CLICK_FAILURE, "execution_log": "",
                 "stack_trace": "", "dom_snapshot": str(snapshot),
                 "failed_selector": "img[class*='avatar']"}
        evidence = diagnosis.collect(issue, workspace=workspace)
        assert evidence["expected_page_object"] == "DashboardPage"
        assert diagnosis.diagnose(evidence)["verdict"] == "LOCATOR_STALE"


class TestBaselineProvenance:
    """A baseline is only worth comparing against if it predates the failure."""

    def test_a_baseline_written_after_the_failure_is_ignored(self, tmp_path):
        # Baseline.record fires on every successful page-load anchor, including
        # the run that then fails and the probe that re-runs it minutes later. A
        # baseline stamped after the capture records the broken page under the
        # name of the last good one.
        folder = tmp_path / "test-output" / "baselines"
        folder.mkdir(parents=True)
        (folder / "DashboardPage.json").write_text(json.dumps(
            fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 0})))
        snapshot = tmp_path / "failure.html"
        snapshot.write_text(fx.DASHBOARD_RENAMED, encoding="utf-8")
        issue = fx.issue(dom_snapshot=str(snapshot),
                         failed_selector="img[class*='avatar'] >> nth=0")

        # The fixture snapshot is captured at 2026-08-26; the baseline records
        # 2026-08-01, so it genuinely is older and stands.
        evidence = diagnosis.collect(issue, workspace=tmp_path,
                                     page_objects=_page_objects())
        assert evidence["baseline"]["available"] is True

        (folder / "DashboardPage.json").write_text(json.dumps(
            fx.baseline_record(coverage={"userMenu": 1, "avatarWidget": 0})
            | {"recordedAt": "2026-08-26T00:05:00"}))
        evidence = diagnosis.collect(issue, workspace=tmp_path,
                                     page_objects=_page_objects())
        assert evidence["baseline"]["available"] is False
        assert any("not older than the failure" in n for n in evidence["notes"])
        assert diagnosis.diagnose(evidence)["verdict"] == "LOCATOR_STALE"


class TestRouting:
    def test_stop_verdicts_are_not_actionable(self):
        for verdict in diagnosis.STOP:
            assert verdict not in diagnosis.ACTIONS

    def test_every_actionable_verdict_names_its_action(self):
        assert all(action for action in diagnosis.ACTIONS.values())

    def test_only_locator_defects_authorise_an_edit(self):
        # Anything else the agent can diagnose, it reports. Widening this set is a
        # decision about what the agent is allowed to change, not a refactor.
        # Both members are defects in the selector with a fix in the selector: one
        # no longer matches its element, the other matches more than one.
        assert set(diagnosis.ACTIONS) == {"LOCATOR_STALE", "AMBIGUOUS_LOCATOR"}

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
