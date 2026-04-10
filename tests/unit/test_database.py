"""
Unit tests for Database (agents/test-triaging-agent/lib/database.py).
Tests table name mapping from report names; no MySQL connection required for these tests.
"""

import sys
from pathlib import Path

import pytest

_agent_dir = Path(__file__).resolve().parent.parent.parent / 'agents' / 'test-triaging-agent'
sys.path.insert(0, str(_agent_dir))

from lib.database import Database


class TestDatabaseTableNameMapping:
    """Test Database.get_table_name_from_report_name static mapping."""

    def test_regression_account_opening(self):
        assert Database.get_table_name_from_report_name("Regression-AccountOpening-Tests-420") == "results_accountopening"

    def test_regression_suite_with_word(self):
        assert Database.get_table_name_from_report_name("Regression-MySuite-Tests-1") == "results_mysuite"

    def test_prodsanity_all_tests(self):
        assert Database.get_table_name_from_report_name("ProdSanity-All-Tests-524") == "results_prodsanity"

    def test_prodsanity_any_name(self):
        assert Database.get_table_name_from_report_name("ProdSanity-Smoke-100") == "results_prodsanity"

    def test_fallback_regression_word_only(self):
        assert Database.get_table_name_from_report_name("Regression-SomeProject") == "results_someproject"

    def test_empty_report_name_returns_none(self):
        assert Database.get_table_name_from_report_name("") is None

    def test_none_like_blank_returns_none(self):
        # Empty string is the only falsy we support; None would raise if passed
        assert Database.get_table_name_from_report_name("") is None

    def test_unmatched_pattern_returns_none(self):
        # Name that doesn't match Regression- or ProdSanity-
        result = Database.get_table_name_from_report_name("RandomReport-123")
        assert result is None
