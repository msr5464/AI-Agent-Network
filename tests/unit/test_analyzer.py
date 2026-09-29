"""
Unit tests for agents/test-triaging-agent/lib/agent/analyzer.py.
Tests FailureClassification dataclass and helpers.
(TestAnalyzer / LangChain-based classifier was removed in favour of claude -p calls.)
"""

import sys
from pathlib import Path

import pytest

# Point at the agent's lib/ directory
_agent_dir = Path(__file__).resolve().parent.parent.parent / 'agents' / 'test-triaging-agent'
sys.path.insert(0, str(_agent_dir))

from lib.agent.analyzer import FailureClassification


class TestFailureClassification:
    """Test FailureClassification model and helpers."""

    def test_is_product_bug_true(self):
        fc = FailureClassification(
            test_name="MyTest.testFail",
            classification="PRODUCT_BUG",
            confidence="HIGH",
            root_cause="Expected 1 got 0",
            recommended_action="Fix app",
            root_cause_category="ASSERTION_FAILURE",
        )
        assert fc.is_product_bug() is True
        assert fc.is_automation_issue() is False

    def test_is_automation_issue_true(self):
        fc = FailureClassification(
            test_name="MyTest.testFail",
            classification="AUTOMATION_ISSUE",
            confidence="HIGH",
            root_cause="Element not found",
            recommended_action="Update locator",
            root_cause_category="ELEMENT_NOT_FOUND",
        )
        assert fc.is_automation_issue() is True
        assert fc.is_product_bug() is False

    def test_unknown_classification(self):
        fc = FailureClassification(
            test_name="MyTest.testFail",
            classification="UNKNOWN",
            confidence="LOW",
            root_cause="Parse failed",
            recommended_action="Manual review",
            root_cause_category="OTHER",
        )
        assert fc.is_product_bug() is False
        assert fc.is_automation_issue() is False

    def test_repr_product_bug(self):
        fc = FailureClassification(
            test_name="A.test", classification="PRODUCT_BUG",
            confidence="HIGH", root_cause="x", recommended_action="y",
            root_cause_category="OTHER",
        )
        assert "🐛" in repr(fc) and "PRODUCT_BUG" in repr(fc)

    def test_repr_automation_issue(self):
        fc = FailureClassification(
            test_name="A.test", classification="AUTOMATION_ISSUE",
            confidence="MEDIUM", root_cause="x", recommended_action="y",
            root_cause_category="OTHER",
        )
        assert "🔧" in repr(fc) and "AUTOMATION_ISSUE" in repr(fc)


class TestExtractElementNames:
    """Reading the element out of a failure the framework actually writes.

    Only the page-load assertion says "in DashboardPage". Interaction failures —
    the majority — name the element and its selector and nothing else, and used
    to match no pattern here at all: the fix step then logged "no page object
    matched" and Claude was asked to repair a locator it had never seen.
    """

    CLICK = ("Failed to click on element 'Edit Profile Summary button' with locator: "
             "Locator@#profile-section-profile-summary img[alt='mukesh']: Error {\n"
             "  message='Timeout 30000ms exceeded.")
    ENTER = ("Failed to enter data in element 'Profile Summary Text Area' with "
             "locator: Locator@textarea[placeholder='Craft a compelling summary']: Error {")

    def _names(self, text, category="ELEMENT_NOT_FOUND"):
        from shared.code_analyzer import CodeAnalyzer
        return CodeAnalyzer().extract_element_names(root_cause=text, category=category)

    def test_a_click_failure_yields_the_element_and_its_selector(self):
        names = self._names(self.CLICK)
        assert "Edit Profile Summary button" in names
        assert "#profile-section-profile-summary img[alt='mukesh']" in names

    def test_a_data_entry_failure_too(self):
        names = self._names(self.ENTER)
        assert "Profile Summary Text Area" in names
        assert "textarea[placeholder='Craft a compelling summary']" in names

    def test_the_selector_stops_before_the_exception_body(self):
        # "Locator@<selector>: Error {" — the trailing ": Error {" is not part of
        # the selector, and a selector with a colon in it would swallow the rest.
        assert not any("Error" in name for name in self._names(self.CLICK))

    def test_extraction_does_not_depend_on_the_classification(self):
        # It used to return nothing unless the failure was already classified
        # ELEMENT_NOT_FOUND or TIMEOUT, which made the evidence conditional on
        # the conclusion it feeds: a WRONG_PAGE verdict switched off the lookup
        # that would have shown it was a stale locator.
        assert self._names(self.CLICK, category="WRONG_PAGE")
        assert self._names(self.CLICK, category="")

    def test_the_page_load_wording_still_works(self):
        names = self._names("Failed to load Element Locator@.heading in ProductsPage")
        assert ".heading" in names
