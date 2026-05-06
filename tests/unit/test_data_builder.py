"""
Unit tests for src/parsers/data_builder.py.
Test DB row conversion, matching helpers, HTML extraction, and full report build (with mocks).
"""

import sys
from pathlib import Path

import pytest

_agent_dir = Path(__file__).resolve().parent.parent.parent / 'agents' / 'test-triaging-agent'
sys.path.insert(0, str(_agent_dir))

from lib.parsers.data_builder import (
    db_row_to_test_result,
    get_full_report_data_from_db,
    get_execution_logs_from_html,
    get_test_durations_from_html,
    find_latest_report,
)
from lib.parsers.models import TestStatus

# Minimal HTML for HTML extraction tests (report_dir/html/overview + results file)
_MINIMAL_OVERVIEW = """<!DOCTYPE html>
<html><body>
<table class="overviewTable">
<tr class="columnHeadings"><th>Suite</th><th>Duration</th><th>Passed</th><th>Skipped</th><th>Failed</th><th>Rate</th></tr>
<tr class="test">
  <td class="test"><a href="suite1_test1_results.html">SuiteOne</a></td>
  <td class="duration">10.5s</td>
  <td class="passed number">2</td>
  <td class="zero number">0</td>
  <td class="zero number">0</td>
  <td class="passRate">100%</td>
</tr>
</table>
</body></html>
"""

_MINIMAL_RESULTS = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<table class="resultsTable">
  <tr><th colspan="3" class="header passed">Passed Tests</th></tr>
  <tr><td colspan="3" class="group">pkg.TestClass</td></tr>
  <tr>
    <td class="method"><a href="#TestClass.testOne">testOne</a></td>
    <td class="duration">1.5s</td>
    <td class="result"><div class="testOutput"><font style="font-size:110%">Execution started for testcase</font></div></td>
  </tr>
</table>
</body></html>
"""


class TestDbRowToTestResult:
    """db_row_to_test_result mapping."""

    def test_minimal_pass(self):
        row = {
            "testcaseName": "pkg.TestClass.testMethod",
            "testStatus": "PASS",
        }
        r = db_row_to_test_result(row)
        assert r.class_name == "pkg.TestClass"
        assert r.method_name == "testMethod"
        assert r.status == TestStatus.PASS
        assert r.duration_seconds == 0.0
        assert r.execution_log is None

    def test_requires_testcase_name(self):
        with pytest.raises(ValueError, match="testcaseName"):
            db_row_to_test_result({})

    def test_fail_status(self):
        row = {
            "testcaseName": "A.B.testOne",
            "testStatus": "FAIL",
        }
        r = db_row_to_test_result(row)
        assert r.status == TestStatus.FAIL

    def test_skipped_and_error(self):
        row_skip = {"testcaseName": "C.M.m1", "testStatus": "SKIPPED"}
        assert db_row_to_test_result(row_skip).status == TestStatus.SKIP
        row_err = {"testcaseName": "C.M.m2", "testStatus": "ERROR"}
        assert db_row_to_test_result(row_err).status == TestStatus.ERROR

    def test_execution_log_and_duration_injected(self):
        row = {"testcaseName": "A.B.m", "testStatus": "PASS"}
        r = db_row_to_test_result(row, execution_log="log line", duration=12.5)
        assert r.execution_log == "log line"
        assert r.duration_seconds == 12.5

    def test_known_failure(self):
        row = {
            "testcaseName": "C.M.m",
            "testStatus": "PASS",
            "knownFailure": "PROJ-123",
        }
        r = db_row_to_test_result(row)
        assert r.known_failure == "PROJ-123"

    def test_failure_reason_parsed(self):
        row = {
            "testcaseName": "C.M.m",
            "testStatus": "FAIL",
            "failureReason": "AssertionError: expected 1 got 0",
        }
        r = db_row_to_test_result(row)
        assert r.error_type == "AssertionError"
        assert r.error_message is not None

    def test_failure_reason_cleans_results_url_and_testcase_name_lines(self):
        """failureReason has 'Results Url:' and 'Testcase Name:' lines stripped before parsing."""
        row = {
            "testcaseName": "C.M.m",
            "testStatus": "FAIL",
            "failureReason": "Results Url: http://example.com\nTestcase Name: C.M.m\nAssertionError: expected 1",
        }
        r = db_row_to_test_result(row)
        assert "Results Url:" not in (r.error_message or "")
        assert "Testcase Name:" not in (r.error_message or "")
        assert r.error_type == "AssertionError"

    def test_single_part_testcase_name(self):
        row = {"testcaseName": "OnlyMethod", "testStatus": "PASS"}
        r = db_row_to_test_result(row)
        assert r.class_name == ""
        assert r.method_name == "OnlyMethod"


class TestGetFullReportDataFromDb:
    """get_full_report_data_from_db dedup and merge."""

    def test_empty_db_returns_empty_results(self):
        out = get_full_report_data_from_db(
            report_dir="/tmp/report",
            db_results=[],
            execution_logs={},
            durations={},
        )
        assert out["test_results"] == []
        assert out["summary"].total == 0
        assert out["report_dir"] == "/tmp/report"
        assert out["html_links"] == {}

    def test_deduplicates_by_testcase_name(self):
        db_results = [
            {"testcaseName": "pkg.Class.m1", "testStatus": "PASS"},
            {"testcaseName": "pkg.Class.m1", "testStatus": "PASS"},
        ]
        out = get_full_report_data_from_db(
            report_dir="/tmp",
            db_results=db_results,
            execution_logs={},
            durations={},
        )
        assert len(out["test_results"]) == 1
        assert out["test_results"][0].full_name == "pkg.Class.m1"

    def test_merges_execution_log_by_full_name(self):
        db_results = [{"testcaseName": "pkg.Class.m1", "testStatus": "PASS"}]
        logs = {"pkg.Class.m1": "execution log here"}
        out = get_full_report_data_from_db(
            report_dir="/tmp",
            db_results=db_results,
            execution_logs=logs,
            durations={},
        )
        assert len(out["test_results"]) == 1
        assert out["test_results"][0].execution_log == "execution log here"

    def test_matching_execution_log_by_class_method_when_full_name_differs(self):
        """Execution log is matched by last two segments (Class.method) when full name differs slightly."""
        db_results = [{"testcaseName": "pkg.Class.m1", "testStatus": "PASS"}]
        # HTML parser may produce key like "Class.m1" (no package prefix)
        logs = {"Class.m1": "log from html"}
        out = get_full_report_data_from_db(
            report_dir="/tmp",
            db_results=db_results,
            execution_logs=logs,
            durations={},
        )
        assert len(out["test_results"]) == 1
        assert out["test_results"][0].execution_log == "log from html"

    def test_merges_duration_by_full_name(self):
        db_results = [{"testcaseName": "pkg.Class.m1", "testStatus": "PASS"}]
        durations = {"pkg.Class.m1": 3.5}
        out = get_full_report_data_from_db(
            report_dir="/tmp",
            db_results=db_results,
            execution_logs={},
            durations=durations,
        )
        assert len(out["test_results"]) == 1
        assert out["test_results"][0].duration_seconds == 3.5

    def test_summary_counts(self):
        db_results = [
            {"testcaseName": "C.M.p1", "testStatus": "PASS"},
            {"testcaseName": "C.M.p2", "testStatus": "FAIL"},
        ]
        out = get_full_report_data_from_db(
            report_dir="/tmp",
            db_results=db_results,
            execution_logs={},
            durations={},
        )
        assert out["summary"].total == 2
        assert out["summary"].passed == 1
        assert out["summary"].failed == 1


class TestFindLatestReport:
    """find_latest_report discovery."""

    def test_nonexistent_dir_returns_none(self, tmp_path):
        assert find_latest_report(str(tmp_path / "nonexistent")) is None

    def test_no_matching_dirs_returns_none(self, tmp_path):
        (tmp_path / "OtherFolder").mkdir()
        assert find_latest_report(str(tmp_path), pattern="Regression-*") is None

    def test_returns_latest_matching_dir(self, tmp_path):
        r1 = tmp_path / "Regression-A-1"
        r2 = tmp_path / "Regression-B-2"
        r1.mkdir()
        r2.mkdir()
        # Ensure r2 is "newer" by touching
        (r2 / "dummy").write_text("x")
        latest = find_latest_report(str(tmp_path), pattern="Regression-*")
        assert latest is not None
        assert "Regression-" in latest


class TestGetExecutionLogsFromHtml:
    """get_execution_logs_from_html parses report_dir/html/ and returns logs + html_links."""

    def test_missing_html_dir_returns_empty(self, tmp_path):
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        # No html/ subdir
        logs, links = get_execution_logs_from_html(str(report_dir))
        assert logs == {}
        assert links == {}

    def test_missing_overview_returns_empty(self, tmp_path):
        report_dir = tmp_path / "report"
        html_dir = report_dir / "html"
        html_dir.mkdir(parents=True)
        # overview.html not present
        logs, links = get_execution_logs_from_html(str(report_dir))
        assert logs == {}
        assert links == {}

    def test_parses_overview_and_results_returns_logs_and_links(self, tmp_path):
        report_dir = tmp_path / "report"
        html_dir = report_dir / "html"
        html_dir.mkdir(parents=True)
        (html_dir / "overview.html").write_text(_MINIMAL_OVERVIEW, encoding="utf-8")
        (html_dir / "suite1_test1_results.html").write_text(_MINIMAL_RESULTS, encoding="utf-8")
        logs, links = get_execution_logs_from_html(str(report_dir))
        # One test in minimal results: pkg.TestClass.testOne
        assert "pkg.TestClass.testOne" in logs or len(logs) >= 1
        assert len(links) >= 1
        # Each key in links should point to the results file
        for url in links.values():
            assert "suite1_test1_results.html" in url


class TestGetTestDurationsFromHtml:
    """get_test_durations_from_html extracts durations keyed by full_name."""

    def test_missing_html_dir_returns_empty(self, tmp_path):
        report_dir = tmp_path / "report"
        report_dir.mkdir()
        durations = get_test_durations_from_html(str(report_dir))
        assert durations == {}

    def test_parses_results_returns_durations(self, tmp_path):
        report_dir = tmp_path / "report"
        html_dir = report_dir / "html"
        html_dir.mkdir(parents=True)
        (html_dir / "overview.html").write_text(_MINIMAL_OVERVIEW, encoding="utf-8")
        (html_dir / "suite1_test1_results.html").write_text(_MINIMAL_RESULTS, encoding="utf-8")
        durations = get_test_durations_from_html(str(report_dir))
        assert len(durations) >= 1
        # Minimal results has one test with 1.5s
        assert any(d > 0 for d in durations.values())
