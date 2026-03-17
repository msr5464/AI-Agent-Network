"""
Unit tests for ReportGenerator (src/reporters/report_generator.py).
Tests HTML report generation, save_report, save_autofix_tests_file, and recurring failures section.
"""

import tempfile
from pathlib import Path

import pytest

from src.agent.analyzer import FailureClassification
from src.parsers.models import TestSummary, TestResult, TestStatus
from src.reporters.report_generator import ReportGenerator


def _minimal_summary():
    return TestSummary(
        total=10,
        passed=8,
        failed=2,
        skipped=0,
        errors=0,
        duration_seconds=120.0,
    )


def _minimal_classification(test_name: str = "MyClass.testMethod", automation: bool = True):
    return FailureClassification(
        test_name=test_name,
        classification="AUTOMATION_ISSUE" if automation else "PRODUCT_BUG",
        confidence="HIGH",
        root_cause="Element not found",
        recommended_action="Update locator",
        root_cause_category="ELEMENT_NOT_FOUND",
    )


class TestReportGeneratorSaveReport:
    """Test ReportGenerator.save_report."""

    def test_save_report_writes_file(self):
        gen = ReportGenerator()
        html = "<html><body>Hello</body></html>"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.html"
            out = gen.save_report(html, str(path))
            assert Path(out).exists()
            assert Path(out).read_text(encoding="utf-8") == html

    def test_save_report_creates_parent_dirs(self):
        gen = ReportGenerator()
        html = "<html></html>"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "dir" / "report.html"
            out = gen.save_report(html, str(path))
            assert Path(out).exists()
            assert Path(out).read_text(encoding="utf-8") == html


class TestReportGeneratorSaveAutofixTestsFile:
    """Test ReportGenerator.save_autofix_tests_file."""

    def test_save_autofix_tests_file_creates_file_when_auto_fixable(self):
        gen = ReportGenerator()
        classifications = [
            _minimal_classification("ClassA.testOne", automation=True),
            _minimal_classification("ClassB.testTwo", automation=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = gen.save_autofix_tests_file(classifications, tmp, "MyReport-123")
            assert path is not None
            content = Path(path).read_text(encoding="utf-8")
            assert "ClassA.testOne" in content
            assert "ClassB.testTwo" in content
            assert "Auto-fixable" in content or "autofix" in content.lower()

    def test_save_autofix_tests_file_returns_none_when_no_auto_fixable(self):
        gen = ReportGenerator()
        classifications = [
            _minimal_classification("ClassA.testOne", automation=False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = gen.save_autofix_tests_file(classifications, tmp, "MyReport-123")
            assert path is None

    def test_save_autofix_tests_file_returns_none_when_empty(self):
        gen = ReportGenerator()
        with tempfile.TemporaryDirectory() as tmp:
            path = gen.save_autofix_tests_file([], tmp, "MyReport-123")
            assert path is None


class TestReportGeneratorGenerateHtmlReport:
    """Test ReportGenerator.generate_html_report return type and flaky section."""

    # report_dir must be a valid path string; ReportUrlBuilder.extract_project_job_from_path is called with it
    _REPORT_DIR = "/dummy/project/job/TestReport-1"

    def test_generate_html_report_returns_tuple_str_and_dict(self):
        gen = ReportGenerator()
        summary = _minimal_summary()
        html_content, test_api_map = gen.generate_html_report(
            summary,
            [],
            "TestReport-1",
            report_dir=self._REPORT_DIR,
        )
        assert isinstance(html_content, str)
        assert isinstance(test_api_map, dict)
        assert "<!DOCTYPE html>" in html_content or "<html" in html_content
        assert "AI-Generated" in html_content or "Automation Report" in html_content

    def test_generate_html_report_includes_flaky_section_with_recurring_failures(self):
        gen = ReportGenerator()
        summary = _minimal_summary()
        # Minimal recurring failure structure (as produced by AgentMemory.detect_recurring_failures)
        recurring_failures = [
            {
                "test_name": "MySuite.MyTest",
                "occurrences": 5,
                "history": [1, 0, 1, 0, 0, 1, 0, 0, 0, 1],
                "execution_details": [
                    {
                        "index": i,
                        "status": "pass" if history == 1 else "fail",
                        "history_index": i,
                        "id": "",
                        "buildTag": "Build-1",
                        "date": "2025-01-01",
                        "failureReason": "Error" if history == 0 else "",
                        "testStatus": "PASSED" if history == 1 else "FAILED",
                        "padded": False,
                    }
                    for i, history in enumerate([1, 0, 1, 0, 0, 1, 0, 0, 0, 1])
                ],
                "failure_pattern": "Intermittently failing due to same reason",
            }
        ]
        html_content, _ = gen.generate_html_report(
            summary,
            [],
            "TestReport-1",
            report_dir=self._REPORT_DIR,
            recurring_failures=recurring_failures,
        )
        assert "All Flaky Tests" in html_content
        assert "1 tests" in html_content or "(1 tests)" in html_content or "1 test" in html_content

    def test_generate_html_report_includes_no_flaky_message_when_empty(self):
        gen = ReportGenerator()
        summary = _minimal_summary()
        html_content, _ = gen.generate_html_report(
            summary,
            [],
            "TestReport-1",
            report_dir=self._REPORT_DIR,
            recurring_failures=None,
        )
        assert "flaky" in html_content.lower()
        assert "No flaky tests detected" in html_content or "no flaky" in html_content.lower()
