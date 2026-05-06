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
