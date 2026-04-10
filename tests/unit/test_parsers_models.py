"""
Unit tests for src/parsers/models.py.
Test TestResult, TestSummary, TestStatus, FailureSummary and their properties.
"""

import sys
from pathlib import Path

import pytest

_agent_dir = Path(__file__).resolve().parent.parent.parent / 'agents' / 'test-triaging-agent'
sys.path.insert(0, str(_agent_dir))

from lib.parsers.models import (
    TestResult as ResultModel,
    TestStatus as StatusEnum,
    TestSummary as SummaryModel,
    FailureSummary as FailureSummaryModel,
)


class TestTestStatus:
    """TestStatus enum."""

    def test_values(self):
        assert StatusEnum.PASS.value == "PASS"
        assert StatusEnum.FAIL.value == "FAIL"
        assert StatusEnum.SKIP.value == "SKIP"
        assert StatusEnum.ERROR.value == "ERROR"


class TestTestResult:
    """TestResult dataclass and properties."""

    def test_full_name_simple(self):
        r = ResultModel(
            class_name="pkg.TestClass",
            method_name="testMethod",
            status=StatusEnum.PASS,
            duration_seconds=1.0,
        )
        assert r.full_name == "pkg.TestClass.testMethod"

    def test_full_name_dedupes_class(self):
        """full_name uses remove_duplicate_class_name."""
        r = ResultModel(
            class_name="pkg.TestClass.TestClass",
            method_name="testMethod",
            status=StatusEnum.PASS,
            duration_seconds=1.0,
        )
        assert r.full_name == "pkg.TestClass.testMethod"

    def test_is_failure_fail(self):
        r = ResultModel("C", "m", StatusEnum.FAIL, 0.0)
        assert r.is_failure is True

    def test_is_failure_error(self):
        r = ResultModel("C", "m", StatusEnum.ERROR, 0.0)
        assert r.is_failure is True

    def test_is_failure_pass(self):
        r = ResultModel("C", "m", StatusEnum.PASS, 0.0)
        assert r.is_failure is False

    def test_is_failure_skip(self):
        r = ResultModel("C", "m", StatusEnum.SKIP, 0.0)
        assert r.is_failure is False

    def test_repr_contains_status(self):
        r = ResultModel("C", "m", StatusEnum.PASS, 1.0)
        assert "PASS" in repr(r)
        r2 = ResultModel("C", "m", StatusEnum.FAIL, 1.0)
        assert "FAIL" in repr(r2)


class TestTestSummary:
    """TestSummary and pass_rate."""

    def test_pass_rate_full(self):
        s = SummaryModel(total=10, passed=10, failed=0, skipped=0, errors=0, duration_seconds=10.0)
        assert s.pass_rate == 100.0

    def test_pass_rate_half(self):
        s = SummaryModel(total=10, passed=5, failed=5, skipped=0, errors=0, duration_seconds=10.0)
        assert s.pass_rate == 50.0

    def test_pass_rate_zero_total(self):
        s = SummaryModel(total=0, passed=0, failed=0, skipped=0, errors=0, duration_seconds=0.0)
        assert s.pass_rate == 0.0

    def test_repr(self):
        s = SummaryModel(total=5, passed=4, failed=1, skipped=0, errors=0, duration_seconds=5.0)
        assert "total=5" in repr(s)
        assert "pass_rate" in repr(s) or "80" in repr(s)


class TestFailureSummary:
    """FailureSummary from CSV."""

    def test_full_name(self):
        f = FailureSummaryModel(
            testrail_id="T1",
            platform="WEB",
            class_name="pkg.TestClass",
            test_name="testOne",
            failure_reason="Assert failed",
            maintained_by="dev",
            status="Failed",
        )
        assert f.full_name == "pkg.TestClass.testOne"
