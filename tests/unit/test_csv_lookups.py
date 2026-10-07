"""Tests for step 03's check of each generated CSV read against the sheet it reads.

The test and its sheet are written by one call, and nothing compiled checks that
they agree. A test copied the environment-aware read from a reference Helper whose
sheets carry an environment column, and pointed it at a new sheet with none, so
no row could ever match and step 04 spent a run and a fix finding that out.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import entry_path  # noqa: E402

SHEET = "src/test/resources/payments/csvFiles/payments.csv"
TEST = "src/test/java/automation/payments/PaymentsWebTest.java"

SCOPED_READ = """
    private PaymentsData buildPayment()
    {
        Map<String, String> data = TestDataReader.loadCsvRowByColumnValue(
            "payments", "payments", "scenario", "credit_card_promo", Config.environment);
        return new PaymentsBuilder().withAmount(data.get("amount")).build();
    }
"""
PLAIN_READ = SCOPED_READ.replace('"credit_card_promo", Config.environment)',
                                 '"credit_card_promo")')
NO_ENV_SHEET = "scenario,amount,card_number\ncredit_card_promo,50000,4111 1111 1111 1111\n"
ENV_SHEET = "scenario,environment,amount\ncredit_card_promo,staging,50000\n"


@pytest.fixture
def step03(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("REPO_ROOT", str(ROOT))
    monkeypatch.setenv("AGENT_DIR", str(ROOT / "agents" / "test-authoring-agent"))
    path = ROOT / "agents" / "test-authoring-agent" / "actions" / "03_generate.py"
    spec = importlib.util.spec_from_file_location("authoring_03", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fw = tmp_path / "fw"
    fw.mkdir()
    monkeypatch.setattr(module, "AUTOMATION_FRAMEWORK_DIR", fw)
    module.fw = fw
    return module


class TestCsvReads:
    def test_the_four_argument_read_is_not_environment_scoped(self):
        [read] = entry_path.csv_reads(PLAIN_READ)
        assert (read["module"], read["file"], read["column"], read["value"]) == \
            ("payments", "payments", "scenario", "credit_card_promo")
        assert read["environment_scoped"] is False

    def test_an_argument_after_the_key_value_is_the_environment_filter(self):
        [read] = entry_path.csv_reads(SCOPED_READ)
        assert read["environment_scoped"] is True
        start, end = read["filter_span"]
        assert SCOPED_READ[start:end] == ", Config.environment"

    def test_a_variable_key_value_is_not_reported_as_a_literal(self):
        source = 'return TestDataReader.loadCsvRowByColumnValue("saucedemo", "users", ' \
                 '"user_key", userKey, Config.environment);'
        [read] = entry_path.csv_reads(source)
        assert read["value"] == "" and read["environment_scoped"] is True

    def test_a_chained_call_is_not_read_as_another_argument(self):
        source = 'String a = TestDataReader.loadCsvRowByColumnValue("m", "f", "k", "v").get("amount");'
        [read] = entry_path.csv_reads(source)
        assert read["value"] == "v" and read["environment_scoped"] is False

    def test_an_example_in_a_comment_is_not_a_read(self):
        source = """
        /**
         * TestDataReader.loadCsvRowByColumnValue("module", "file", "scenario", "checkout", Config.environment);
         */
        // TestDataReader.loadCsvRowByColumnValue("module", "file", "scenario", "checkout");
        """
        assert entry_path.csv_reads(source) == []

    def test_the_helper_data_source_reports_a_four_argument_read_unscoped(self):
        helper = """
        public Map<String, String> getUser(String userKey)
        {
            return TestDataReader.loadCsvRowByColumnValue("saucedemo", "users", "user_key", userKey);
        }
        """
        source = entry_path._data_source(helper, "getUser")
        assert source["file"] == "users" and source["environment_scoped"] is False


class TestCheckCsvLookups:
    def test_a_filter_the_sheet_cannot_answer_is_dropped(self, step03):
        files, problems = step03._check_csv_lookups({TEST: SCOPED_READ, SHEET: NO_ENV_SHEET})
        assert files[TEST] == PLAIN_READ
        assert problems == {}

    def test_a_sheet_with_an_environment_column_keeps_the_filter(self, step03):
        files, problems = step03._check_csv_lookups({TEST: SCOPED_READ, SHEET: ENV_SHEET})
        assert files[TEST] == SCOPED_READ
        assert problems == {}

    def test_the_sheet_on_disk_is_read_when_this_run_did_not_write_one(self, step03):
        disk = step03.fw / SHEET
        disk.parent.mkdir(parents=True)
        disk.write_text(ENV_SHEET)
        files, problems = step03._check_csv_lookups({TEST: SCOPED_READ})
        assert files[TEST] == SCOPED_READ
        assert problems == {}

    def test_a_missing_sheet_is_recorded(self, step03):
        files, problems = step03._check_csv_lookups({TEST: SCOPED_READ})
        assert files[TEST] == SCOPED_READ
        assert "neither this run nor the repository has" in problems[TEST][0]

    def test_a_key_column_the_header_lacks_is_recorded(self, step03):
        sheet = NO_ENV_SHEET.replace("scenario", "case")
        _, problems = step03._check_csv_lookups({TEST: PLAIN_READ, SHEET: sheet})
        assert "'scenario'" in problems[TEST][0]

    def test_a_literal_key_no_row_has_is_recorded(self, step03):
        sheet = NO_ENV_SHEET.replace("credit_card_promo", "gopay")
        _, problems = step03._check_csv_lookups({TEST: PLAIN_READ, SHEET: sheet})
        assert "scenario='credit_card_promo'" in problems[TEST][0]
