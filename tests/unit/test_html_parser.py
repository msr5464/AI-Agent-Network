"""
Unit tests for src/parsers/html_parser.py.
Test overview parsing, test result parsing, and summary stats with minimal HTML.
"""

import os
import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))

from src.parsers.html_parser import HTMLReportParser
from src.parsers.models import TestStatus


# Minimal overview.html fragment (ReportNG-style)
OVERVIEW_HTML = """<!DOCTYPE html>
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
<tr class="test">
  <td class="test"><a href="suite1_test2_results.html">SuiteTwo</a></td>
  <td class="duration">5.0s</td>
  <td class="zero number">0</td>
  <td class="zero number">0</td>
  <td class="failed number">1</td>
  <td class="passRate">0%</td>
</tr>
</table>
</body></html>
"""

# Minimal results HTML: one failed test with group + method row
RESULTS_HTML_FAILED = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<table class="resultsTable">
  <tr><th colspan="3" class="header failed">Failed Tests</th></tr>
  <tr><td colspan="3" class="group">Automation.Access.web.TestActivation</td></tr>
  <tr>
    <td class="method"><a href="#TestActivation.testSecondActivationMissionCards">testSecondActivationMissionCards</a></td>
    <td class="duration">0.410s</td>
    <td class="result">
      <div class="testOutput">
        <font style="font-size:110%">Execution started for testcase - [Home page] verify link</font>
        <font style="font-size:110%">EXECUTION OF TESTCASE ENDS HERE</font>
      </div>
      <a href="javascript:toggleElement('exception-0','block')"><b>java.lang.AssertionError: expected 1</b></a>
      <div class="stackTrace" id="exception-0">java.lang.AssertionError: expected 1<br/>at MyTest.method(MyTest.java:10)</div>
    </td>
  </tr>
</table>
</body></html>
"""

# Minimal results HTML: one passed test
RESULTS_HTML_PASSED = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<table class="resultsTable">
  <tr><th colspan="3" class="header passed">Passed Tests</th></tr>
  <tr><td colspan="3" class="group">Automation.Access.api.TestGetUserApis</td></tr>
  <tr>
    <td class="method"><a href="#TestGetUserApis.testGetUser">testGetUser</a></td>
    <td class="duration">1.234s</td>
    <td class="result"><div class="testOutput"><font style="font-size:110%">Execution started for testcase</font></div></td>
  </tr>
</table>
</body></html>
"""


class TestHTMLReportParserOverview:
    """parse_overview with minimal HTML."""

    def test_parse_overview_returns_suites(self, tmp_path):
        overview_file = tmp_path / "overview.html"
        overview_file.write_text(OVERVIEW_HTML, encoding="utf-8")
        parser = HTMLReportParser()
        suites = parser.parse_overview(str(overview_file))
        assert len(suites) == 2
        assert suites[0]["name"] == "SuiteOne"
        assert suites[0]["results_file"] == "suite1_test1_results.html"
        assert suites[0]["passed"] == 2
        assert suites[0]["failed"] == 0
        assert suites[1]["name"] == "SuiteTwo"
        assert suites[1]["failed"] == 1

    def test_parse_overview_file_not_found(self):
        parser = HTMLReportParser()
        with pytest.raises(FileNotFoundError, match="not found"):
            parser.parse_overview("/nonexistent/overview.html")


class TestHTMLReportParserTestResults:
    """parse_test_results with minimal HTML."""

    def test_parse_failed_section(self, tmp_path):
        results_file = tmp_path / "results.html"
        results_file.write_text(RESULTS_HTML_FAILED, encoding="utf-8")
        parser = HTMLReportParser()
        results = parser.parse_test_results(str(results_file))
        assert len(results) == 1
        r = results[0]
        assert r.status == TestStatus.FAIL
        assert "TestActivation" in r.class_name
        assert r.method_name == "testSecondActivationMissionCards"
        assert r.duration_seconds == 0.41
        assert r.platform == "WEB"
        assert r.execution_log is not None
        assert r.error_message is not None or r.error_type is not None

    def test_parse_passed_section(self, tmp_path):
        results_file = tmp_path / "results.html"
        results_file.write_text(RESULTS_HTML_PASSED, encoding="utf-8")
        parser = HTMLReportParser()
        results = parser.parse_test_results(str(results_file))
        assert len(results) == 1
        r = results[0]
        assert r.status == TestStatus.PASS
        assert "api" in r.class_name.lower()
        assert r.method_name == "testGetUser"
        assert r.platform == "API"

    def test_parse_test_results_file_not_found(self):
        parser = HTMLReportParser()
        with pytest.raises(FileNotFoundError, match="not found"):
            parser.parse_test_results("/nonexistent/results.html")


class TestHTMLReportParserSummary:
    """get_summary_stats."""

    def test_get_summary_stats(self):
        from src.parsers.models import TestResult as TR, TestSummary as TS

        parser = HTMLReportParser()
        results = [
            TR("C", "m1", TestStatus.PASS, 1.0),
            TR("C", "m2", TestStatus.PASS, 2.0),
            TR("C", "m3", TestStatus.FAIL, 0.5),
        ]
        summary = parser.get_summary_stats(results)
        assert isinstance(summary, TS)
        assert summary.total == 3
        assert summary.passed == 2
        assert summary.failed == 1
        assert summary.duration_seconds == 3.5
        assert summary.pass_rate == pytest.approx(200 / 3, rel=0.01)


@pytest.mark.skipif(
    not os.path.isfile(
        str(_repo_root / "testdata" / "Regression-AccountOpening-Tests-420" / "html" / "overview.html")
    ),
    reason="Test data not present (testdata/Regression-AccountOpening-Tests-420/html)",
)
class TestHTMLReportParserWithRealData:
    """Optional integration-style tests when testdata is available."""

    def test_parse_real_overview(self):
        overview_path = _repo_root / "testdata" / "Regression-AccountOpening-Tests-420" / "html" / "overview.html"
        parser = HTMLReportParser()
        suites = parser.parse_overview(str(overview_path))
        assert len(suites) >= 1
        for s in suites:
            assert "name" in s and "results_file" in s

    def test_parse_real_results_has_execution_log(self):
        html_dir = _repo_root / "testdata" / "Regression-AccountOpening-Tests-420" / "html"
        results_file = html_dir / "suite1_test50_results.html"
        if not results_file.exists():
            pytest.skip("suite1_test50_results.html not found")
        parser = HTMLReportParser()
        results = parser.parse_test_results(str(results_file))
        failed = [r for r in results if r.is_failure]
        if failed:
            assert failed[0].execution_log is not None or failed[0].error_message
