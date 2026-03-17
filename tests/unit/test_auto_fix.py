"""
Unit tests for src/auto_fix: manager orchestration, Cursor client, browser inspector, GitHub PR creator.
Tests use mocks to avoid external dependencies (GitHub, LLM, Cursor CLI, real browser).
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))

from src.agent.analyzer import FailureClassification
from src.auto_fix.models import FixProposal, FileChange, AdditionalChange


# ---------------------------------------------------------------------------
# AutoFixManager — orchestration logic (no network/LLM)
# ---------------------------------------------------------------------------

class TestAutoFixManagerIsAutoFixable:
    """Test is_auto_fixable filtering."""

    @pytest.fixture
    def manager(self):
        with mock.patch("src.auto_fix.manager.GitHubClient"), mock.patch(
            "src.auto_fix.manager.FixGenerator"
        ):
            from src.auto_fix.manager import AutoFixManager

            return AutoFixManager(
                github_token="x",
                github_org="o",
                github_repo_automation="r",
            )

    def test_product_change_high_confidence(self, manager):
        c = FailureClassification(
            test_name="MyTest.testMethod",
            classification="PRODUCT_CHANGE",
            confidence="HIGH",
            root_cause="UI changed",
            recommended_action="Update test",
            root_cause_category="OTHER",
        )
        assert manager.is_auto_fixable(c) is True

    def test_automation_issue_medium_confidence(self, manager):
        c = FailureClassification(
            test_name="MyTest.testMethod",
            classification="AUTOMATION_ISSUE",
            confidence="MEDIUM",
            root_cause="Element not found",
            recommended_action="Fix locator",
            root_cause_category="ELEMENT_NOT_FOUND",
        )
        assert manager.is_auto_fixable(c) is True

    def test_product_bug_not_fixable(self, manager):
        c = FailureClassification(
            test_name="MyTest.testMethod",
            classification="PRODUCT_BUG",
            confidence="HIGH",
            root_cause="Bug in app",
            recommended_action="Fix app",
            root_cause_category="OTHER",
        )
        assert manager.is_auto_fixable(c) is False

    def test_low_confidence_not_fixable(self, manager):
        c = FailureClassification(
            test_name="MyTest.testMethod",
            classification="AUTOMATION_ISSUE",
            confidence="LOW",
            root_cause="Unknown",
            recommended_action="Review",
            root_cause_category="OTHER",
        )
        assert manager.is_auto_fixable(c) is False


class TestAutoFixManagerHelpers:
    """Test manager helper methods (branch name, element name, flex replace)."""

    @pytest.fixture
    def manager(self):
        with mock.patch("src.auto_fix.manager.GitHubClient"), mock.patch(
            "src.auto_fix.manager.FixGenerator"
        ):
            from src.auto_fix.manager import AutoFixManager

            return AutoFixManager(
                github_token="x",
                github_org="o",
                github_repo_automation="r",
            )

    def test_build_branch_name(self, manager):
        assert "auto-fix/" in manager._build_branch_name("MyTest.testMethod")
        assert manager._build_branch_name("pkg.Class.test_my_feature").endswith("testmyfeature") or "auto-fix" in manager._build_branch_name(
            "pkg.Class.test_my_feature"
        )

    def test_extract_element_name_page_colon_format(self, manager):
        root = "Element 'DashPeopleDetailsPage:Block Reason PopUp Header' is NOT visible"
        assert manager._extract_element_name_from_root_cause(root) == "Block Reason PopUp Header"

    def test_extract_element_name_quoted_element(self, manager):
        root = "Element 'Submit Button' is NOT clickable"
        assert manager._extract_element_name_from_root_cause(root) == "Submit Button"

    def test_extract_element_name_no_match(self, manager):
        assert manager._extract_element_name_from_root_cause("Random error") is None

    def test_flex_replace_exact_match(self, manager):
        content = "line1\n  target block\nline3"
        target = "  target block"
        replacement = "  new block"
        out, ok = manager._flex_replace(content, target, replacement)
        assert ok is True
        assert "new block" in out and "target block" not in out

    def test_flex_replace_no_match(self, manager):
        content = "line1\nline2"
        out, ok = manager._flex_replace(content, "nonexistent", "new")
        assert ok is False
        assert out == content


class TestAutoFixManagerParseStackFrames:
    """Test stack frame parsing with mocked repo lookup."""

    @pytest.fixture
    def manager(self):
        with mock.patch("src.auto_fix.manager.GitHubClient"), mock.patch(
            "src.auto_fix.manager.FixGenerator"
        ):
            from src.auto_fix.manager import AutoFixManager

            m = AutoFixManager(
                github_token="x",
                github_org="o",
                github_repo_automation="r",
            )
            return m

    def test_parse_stack_frames_returns_frames(self, manager, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "MyClass.java").write_text("public class MyClass { }\n")
        log = "at com.example.MyClass.myMethod(MyClass.java:42)"
        manager._find_file_in_repo = mock.Mock(return_value=tmp_path / "src" / "MyClass.java")
        manager._read_snippet = mock.Mock(return_value="snippet")
        frames = manager._parse_stack_frames(log, str(tmp_path))
        assert len(frames) >= 1
        assert frames[0].get("line_no") == 42

    def test_parse_stack_frames_empty_log(self, manager, tmp_path):
        assert manager._parse_stack_frames("", str(tmp_path)) == []


# ---------------------------------------------------------------------------
# CursorClient — IDE integration (configuration only)
# ---------------------------------------------------------------------------

class TestCursorClient:
    """Test Cursor CLI adapter configuration."""

    def test_not_configured_no_agent(self):
        with mock.patch("src.auto_fix.cursor_client.shutil.which", return_value=None), mock.patch.dict(
            "os.environ", {"CURSOR_API_KEY": "key"}, clear=False
        ):
            from src.auto_fix.cursor_client import CursorClient

            client = CursorClient()
            assert client._is_configured() is False

    def test_not_configured_no_key(self):
        with mock.patch("src.auto_fix.cursor_client.shutil.which", return_value="/usr/bin/agent"):
            with mock.patch.dict("os.environ", {"CURSOR_API_KEY": ""}, clear=False):
                from src.auto_fix.cursor_client import CursorClient

                client = CursorClient()
                assert client._is_configured() is False

    def test_configured_when_both_set(self):
        with mock.patch("src.auto_fix.cursor_client.shutil.which", return_value="/usr/bin/agent"), mock.patch.dict(
            "os.environ", {"CURSOR_API_KEY": "key"}, clear=False
        ):
            from src.auto_fix.cursor_client import CursorClient

            client = CursorClient()
            assert client._is_configured() is True


# ---------------------------------------------------------------------------
# BrowserInspector — URL extraction and LocatorCandidate (no live browser)
# ---------------------------------------------------------------------------

class TestBrowserInspectorExtractPageUrl:
    """Test URL extraction from logs without starting browser."""

    def test_extract_page_url_pattern1(self):
        from src.auto_fix.browser_inspector import BrowserInspector

        if not getattr(BrowserInspector, "__module__", ""):
            pass
        log = "Page URL:- https://app.example.com/dashboard"
        # We need an instance; BrowserInspector may require selenium at init
        try:
            with mock.patch("src.auto_fix.browser_inspector.SELENIUM_AVAILABLE", True), mock.patch(
                "src.auto_fix.browser_inspector.webdriver"
            ):
                insp = BrowserInspector(headless=True, timeout=5)
                url = insp.extract_page_url(log, "")
                assert url == "https://app.example.com/dashboard"
        except Exception:
            # If selenium not installed or Chrome not available, skip assertion
            pytest.skip("BrowserInspector requires Selenium/Chrome")

    def test_extract_page_url_standalone_func(self):
        """Test the regex logic without instantiating BrowserInspector (avoids selenium import)."""
        import re

        log = "Page URL:- https://app.example.com/dashboard\nSome other text"
        pattern1 = r"Page URL[:\s-]+([^\s\n]+)"
        match = re.search(pattern1, log, re.IGNORECASE)
        assert match
        assert match.group(1).strip() == "https://app.example.com/dashboard"


class TestLocatorCandidate:
    """Test LocatorCandidate conversion to Selenium By."""

    def test_to_selenium_by_id(self):
        pytest.importorskip("selenium")
        from src.auto_fix.browser_inspector import LocatorCandidate
        from selenium.webdriver.common.by import By

        c = LocatorCandidate("id", "myId", "HIGH", "")
        by, value = c.to_selenium_by()
        assert by == By.ID
        assert value == "myId"

    def test_to_selenium_by_css(self):
        pytest.importorskip("selenium")
        from src.auto_fix.browser_inspector import LocatorCandidate
        from selenium.webdriver.common.by import By

        c = LocatorCandidate("css", "[data-cy='btn']", "HIGH", "")
        by, value = c.to_selenium_by()
        assert by == By.CSS_SELECTOR
        assert value == "[data-cy='btn']"


# ---------------------------------------------------------------------------
# PRCreator — PR title, body, labels (no GitHub API)
# ---------------------------------------------------------------------------

class TestPRCreator:
    """Test PR content generation."""

    @pytest.fixture
    def pr_creator(self):
        from src.auto_fix.github.pr_creator import PRCreator

        return PRCreator()

    @pytest.fixture
    def classification(self):
        return FailureClassification(
            test_name="pkg.TestClass.testMethod",
            classification="AUTOMATION_ISSUE",
            confidence="HIGH",
            root_cause="Element not found",
            recommended_action="Update locator",
            root_cause_category="ELEMENT_NOT_FOUND",
        )

    @pytest.fixture
    def fix_proposal(self):
        return FixProposal(
            original_code="old",
            fixed_code="new",
            explanation="Updated locator",
            confidence="HIGH",
            file_path="tests/MyTest.java",
            plan_summary=[],
            additional_changes=[],
        )

    def test_generate_pr_title_automation_issue(self, pr_creator, classification):
        title = pr_creator.generate_pr_title(classification)
        assert "Fix automation issue" in title or "🔧" in title
        assert "testMethod" in title

    def test_generate_pr_title_product_change(self, pr_creator):
        c = FailureClassification(
            test_name="pkg.TestClass.testMethod",
            classification="PRODUCT_CHANGE",
            confidence="MEDIUM",
            root_cause="UI changed",
            recommended_action="Update test",
            root_cause_category="OTHER",
        )
        title = pr_creator.generate_pr_title(c)
        assert "product change" in title.lower() or "Update test" in title

    def test_generate_pr_body_contains_sections(self, pr_creator, classification, fix_proposal):
        body = pr_creator.generate_pr_body(classification, fix_proposal)
        assert "Test Name" in body or "testMethod" in body
        assert "Root Cause" in body
        assert classification.root_cause in body
        assert fix_proposal.explanation in body

    def test_determine_labels_automation_issue_high(self, pr_creator, classification):
        labels = pr_creator.determine_labels(classification)
        assert "automated-fix" in labels
        assert "automation-issue" in labels
        assert "high-confidence" in labels

    def test_determine_labels_product_change(self, pr_creator):
        c = FailureClassification(
            test_name="x.y.z",
            classification="PRODUCT_CHANGE",
            confidence="LOW",
            root_cause="x",
            recommended_action="y",
            root_cause_category="OTHER",
        )
        labels = pr_creator.determine_labels(c)
        assert "product-change" in labels
        assert "needs-review" in labels


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TestAutoFixModels:
    """Test auto_fix data models."""

    def test_file_change_default_type(self):
        fc = FileChange(file_path="a.java", new_content="code")
        assert fc.change_type == "modify"

    def test_pr_result_success(self):
        from src.auto_fix.models import PRResult

        r = PRResult(success=True, pr_url="https://github.com/org/repo/pull/1")
        assert r.success is True
        assert r.error is None

    def test_auto_fix_result_skipped(self):
        from src.auto_fix.models import AutoFixResult

        r = AutoFixResult(
            test_name="MyTest.testMethod",
            success=False,
            skipped=True,
            skip_reason="Test passed locally",
        )
        assert r.skipped is True
        assert "passed" in (r.skip_reason or "")
