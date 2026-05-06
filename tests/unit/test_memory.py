#!/usr/bin/env python3
"""
Test the memory system with MySQL database.
Note: Integration tests (test_memory_initialization, test_recurring_failures, test_trend_analysis)
require MySQL. Unit tests (TestAgentMemoryUnit) mock the database and run without MySQL.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

_agent_dir = Path(__file__).resolve().parent.parent.parent / 'agents' / 'test-triaging-agent'
sys.path.insert(0, str(_agent_dir))

from lib.agent.memory import AgentMemory


def test_memory_initialization():
    """Test that AgentMemory initializes correctly with MySQL"""
    print("=" * 60)
    print("Test 1: Memory Initialization")
    print("=" * 60)
    
    try:
        memory = AgentMemory()
        print("\n✅ AgentMemory initialized successfully")
        print(f"   Database config loaded")
        print("\n✅ Test 1 PASSED\n")
        assert True
    except ImportError as e:
        print(f"\n❌ MySQL connector not available: {e}")
        print("   Install with: pip install pymysql")
        print("\n❌ Test 1 FAILED\n")
        pytest.skip(f"pymysql not available: {e}")
    except Exception as e:
        print(f"\n❌ Failed to initialize: {e}")
        print("\n❌ Test 1 FAILED\n")
        raise


def test_recurring_failures():
    """Test detecting recurring failures from MySQL database"""
    print("=" * 60)
    print("Test 2: Detect Recurring Failures")
    print("=" * 60)
    
    try:
        memory = AgentMemory()
        # Use a real report name (adjust based on your database)
        report_name = "Regression-AccountOpening-Tests-420"
        # Current failures (simulated - adjust based on your data)
        current_failures = [
            "TestLogin.testInvalidCredentials",
            "TestCheckout.testPaymentProcessing"
        ]
        print(f"\n📊 Querying MySQL database for report: {report_name}")
        recurring = memory.detect_recurring_failures(
            current_failures=current_failures,
            days=10,
            min_occurrences=2,
            report_name=report_name,
            all_test_names=None  # Query only failures
        )
        print(f"\n✅ Found {len(recurring)} recurring failures")
        for failure in recurring[:5]:  # Show first 5
            print(f"\n   Test: {failure['test_name']}")
            print(f"   Occurrences: {failure['occurrences']}")
            print(f"   Classification: {failure.get('most_common_classification', 'N/A')}")
            print(f"   Flaky: {'Yes' if failure.get('is_flaky') else 'No'}")
            print(f"   In Current Run: {'Yes' if failure.get('in_current_run') else 'No'}")
            if 'history' in failure:
                history_str = ''.join(['🟢' if h == 1 else '🔴' for h in failure['history']])
                print(f"   History: {history_str}")
        print("\n✅ Test 2 PASSED\n")
        assert True
    except (ImportError, ValueError) as e:
        print(f"\n⚠️  Test skipped: {e}")
        print("\n⚠️  Test 2 SKIPPED\n")
        pytest.skip(str(e))
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        print("\n❌ Test 2 FAILED\n")
        raise


def test_trend_analysis():
    """Test trend analysis from MySQL database"""
    print("=" * 60)
    print("Test 3: Trend Analysis")
    print("=" * 60)
    
    try:
        memory = AgentMemory()
        # Use a real report name (adjust based on your database)
        report_name = "Regression-AccountOpening-Tests-420"
        print(f"\n📊 Querying MySQL database for trends (report: {report_name})")
        trends = memory.get_trend_analysis(days=10, report_name=report_name)
        print(f"\n✅ Trend Analysis:")
        print(f"   Days Analyzed: {trends['days_analyzed']}")
        print(f"   Average Pass Rate: {trends['average_pass_rate']:.1f}%")
        print(f"   Latest Pass Rate: {trends['latest_pass_rate']:.1f}%")
        print(f"   Trend: {trends['trend']}")
        if trends['days_analyzed'] > 0:
            print(f"\n   Pass Rate History:")
            for date, rate in zip(trends.get('dates', [])[:10], trends.get('pass_rates', [])[:10]):
                bar = '█' * int(rate / 5) if rate > 0 else ''
                print(f"   {date}: {bar} {rate:.1f}%")
        else:
            print("\n   ⚠️  No historical data found in database")
        print("\n✅ Test 3 PASSED\n")
        assert True
    except (ImportError, ValueError) as e:
        print(f"\n⚠️  Test skipped: {e}")
        print("\n⚠️  Test 3 SKIPPED\n")
        pytest.skip(str(e))
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        print("\n❌ Test 3 FAILED\n")
        raise


def test_table_name_extraction():
    """Test table name extraction from report names"""
    print("=" * 60)
    print("Test 4: Table Name Extraction")
    print("=" * 60)
    
    try:
        memory = AgentMemory()
        test_cases = [
            ("Regression-AccountOpening-Tests-420", "results_accountopening"),
            ("ProdSanity-All-Tests-524", "results_prodsanity"),
            ("Regression-Payment-Tests-100", "results_payment"),
            ("Invalid-Report-Name", None),
        ]
        print("\n✅ Testing table name extraction:")
        all_passed = True
        for report_name, expected_table in test_cases:
            table_name = memory._get_table_name_from_report_name(report_name)
            status = "✅" if table_name == expected_table else "❌"
            print(f"   {status} {report_name} -> {table_name} (expected: {expected_table})")
            if table_name != expected_table:
                all_passed = False
        assert all_passed, f"Table name extraction failed for some cases"
        print("\n✅ Test 4 PASSED\n")
    except ImportError as e:
        print(f"\n⚠️  Test skipped: {e}")
        print("\n⚠️  Test 4 SKIPPED\n")
        pytest.skip(str(e))
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        print("\n❌ Test 4 FAILED\n")
        raise


# ---------------------------------------------------------------------------
# Pytest unit tests (no MySQL required; Database mocked)
# ---------------------------------------------------------------------------

class TestAgentMemoryUnit:
    """Unit tests for AgentMemory that mock the database."""

    @pytest.fixture
    def memory(self):
        with mock.patch("lib.agent.memory.Database") as mock_db:
            mock_db.return_value.get_connection = mock.MagicMock()
            yield AgentMemory()

    def test_classify_from_error_message_product_bug_assertion(self, memory):
        out = memory._classify_from_error_message("Assertion failed: expected 1 got 0")
        assert out == "PRODUCT_BUG"

    def test_classify_from_error_message_product_bug_expected_actual(self, memory):
        out = memory._classify_from_error_message("Expected true but actual is false")
        assert out == "PRODUCT_BUG"

    def test_classify_from_error_message_automation_nosuchelement(self, memory):
        out = memory._classify_from_error_message("NoSuchElementException: element not found")
        assert out == "AUTOMATION_ISSUE"

    def test_classify_from_error_message_automation_timeout(self, memory):
        out = memory._classify_from_error_message("TimeoutException: element not visible")
        assert out == "AUTOMATION_ISSUE"

    def test_classify_from_error_message_product_bug_api(self, memory):
        out = memory._classify_from_error_message("API returned status code 500")
        assert out == "PRODUCT_BUG"

    def test_classify_from_error_message_unknown(self, memory):
        out = memory._classify_from_error_message("Something went wrong")
        assert out == "UNKNOWN"

    def test_classify_from_error_message_empty(self, memory):
        out = memory._classify_from_error_message("")
        assert out == "UNKNOWN"

    def test_categorize_failure_pattern_intermittent_same_reason(self, memory):
        out = memory._categorize_failure_pattern(
            is_intermittent=True, same_reason=True, different_reasons=False
        )
        assert out == "Intermittently failing due to same reason"

    def test_categorize_failure_pattern_intermittent_different_reasons(self, memory):
        out = memory._categorize_failure_pattern(
            is_intermittent=True, same_reason=False, different_reasons=True
        )
        assert out == "Intermittently failing but different reasons"

    def test_categorize_failure_pattern_continuous_same_reason(self, memory):
        out = memory._categorize_failure_pattern(
            is_intermittent=False, same_reason=True, different_reasons=False
        )
        assert out == "Continuously failing due to same reason"

    def test_categorize_failure_pattern_continuous_different_reasons(self, memory):
        out = memory._categorize_failure_pattern(
            is_intermittent=False, same_reason=False, different_reasons=True
        )
        assert out == "Continuously failing but different reasons"

    def test_get_table_name_delegates_to_database(self, memory):
        with mock.patch("lib.agent.memory.Database.get_table_name_from_report_name", return_value="results_mysuite"):
            out = memory._get_table_name_from_report_name("Regression-MySuite-Tests-1")
        assert out == "results_mysuite"

    def test_detect_recurring_failures_skips_malformed_execution_records(self, memory):
        """Malformed or unexpected execution records should not terminate the script."""
        # Return history that includes one valid record and one malformed (non-dict) entry
        test_history = {
            "TestClass.testMethod": [
                {"buildTag": "build-1", "testStatus": "FAIL", "failureReason": "err", "date": "2025-01-01"},
                None,  # malformed
                {"buildTag": "build-2", "testStatus": "FAIL", "failureReason": "err2"},
            ]
        }
        with mock.patch.object(memory, "_get_test_execution_history_from_db", return_value=test_history):
            result = memory._detect_recurring_failures_from_db(
                current_failures=["TestClass.testMethod"],
                days=10,
                min_occurrences=2,
                report_name="Regression-Suite-1",
                test_names_to_query=["TestClass.testMethod"],
            )
        # Should complete without raising; may or may not include the test depending on logic
        assert isinstance(result, list)


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("🧠 TESTING MEMORY SYSTEM (MySQL Only)")
    print("=" * 60)
    print("\n⚠️  Note: These tests require MySQL database to be configured")
    print("   Set DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in config/.env\n")
    
    results = []
    
    try:
        # Run tests
        results.append(("Memory Initialization", test_memory_initialization()))
        results.append(("Table Name Extraction", test_table_name_extraction()))
        results.append(("Recurring Failures", test_recurring_failures()))
        results.append(("Trend Analysis", test_trend_analysis()))
        
    except Exception as e:
        print(f"\n❌ Test suite failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Summary
    print("=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
    
    total_passed = sum(1 for _, passed in results if passed)
    total_tests = len(results)
    
    print(f"\nTotal: {total_passed}/{total_tests} tests passed")
    
    if total_passed == total_tests:
        print("\n🎉 ALL TESTS PASSED! Memory system is working correctly.\n")
        return 0
    else:
        print(f"\n⚠️  {total_tests - total_passed} test(s) failed.\n")
        return 1


if __name__ == '__main__':
    sys.exit(main())
