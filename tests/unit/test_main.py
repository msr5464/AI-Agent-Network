"""
Unit tests for main orchestration helpers (environment detection, argparse, prompts config).
Does not test src/main.py (deleted) — tests standalone helper logic only.
"""

import sys
from pathlib import Path

import pytest
import yaml

_repo_root = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helper: extract _guess_environment from main.main (it's a nested function)
# We replicate its logic here for isolated testing to avoid DB / import side-effects.
# ---------------------------------------------------------------------------

def _guess_environment(report_path: str, env_override: str = "") -> str:
    """
    Replica of main._guess_environment for unit testing.
    Determines the target environment from a report file path.
    """
    name = Path(report_path).name.lower() if report_path else ""
    if env_override:
        return env_override
    for token, env in [
        ("prod", "production"),
        ("production", "production"),
        ("qa-2", "qa-2"),
        ("qa2", "qa-2"),
        ("qa-1", "qa-1"),
        ("qa1", "qa-1"),
        ("staging", "staging"),
    ]:
        if token in name:
            return env
    return ""


class TestGuessEnvironment:
    """Test the _guess_environment helper used in main()."""

    def test_prod_sanity_report(self):
        """ProdSanity report path should map to 'production'."""
        assert _guess_environment("/data/ProdSanity-All-Tests-541") == "production"

    def test_production_keyword(self):
        """Report with 'production' in name should map correctly."""
        assert _guess_environment("/data/Production-Regression-Tests-100") == "production"

    def test_qa1_report(self):
        """Report with 'qa-1' should map to 'qa-1'."""
        assert _guess_environment("/data/Regression-QA-1-Tests-200") == "qa-1"

    def test_qa2_report(self):
        """Report with 'qa2' (no dash) should map to 'qa-2'."""
        assert _guess_environment("/data/Regression-QA2-Tests-300") == "qa-2"

    def test_staging_report(self):
        """Report with 'staging' should map to 'staging'."""
        assert _guess_environment("/data/Staging-Tests-400") == "staging"

    def test_unknown_report_returns_empty(self):
        """Unknown report name should return empty string."""
        assert _guess_environment("/data/Regression-AccountOpening-Tests-420") == ""

    def test_empty_path(self):
        """Empty path should return empty string."""
        assert _guess_environment("") == ""

    def test_none_path(self):
        """None path should return empty string."""
        assert _guess_environment(None) == ""

    def test_env_override_takes_precedence(self):
        """Explicit env override should take precedence over name detection."""
        result = _guess_environment("/data/ProdSanity-All-Tests-541", env_override="qa-2")
        assert result == "qa-2"

    def test_case_insensitive_matching(self):
        """Environment detection should be case-insensitive (Path.name.lower())."""
        assert _guess_environment("/data/PRODSANITY-ALL-TESTS-100") == "production"

    def test_prod_token_matches_before_production(self):
        """The 'prod' token appears before 'production' in the list, so 'prod' substring wins first."""
        result = _guess_environment("/data/prod-smoke-tests-1")
        assert result == "production"


class TestArgparse:
    """Test that main module's argparse setup defines expected arguments."""

    def test_argparse_setup(self):
        """The main() function should support all documented CLI flags."""
        import argparse
        # Replicate the argparse setup from main.py
        parser = argparse.ArgumentParser(description="QA Agent Network")
        parser.add_argument("--input-dir")
        parser.add_argument("--output-dir")
        parser.add_argument("--table-name")
        parser.add_argument("--environment")
        parser.add_argument("--skip-report", action="store_true")
        parser.add_argument("--skip-autofix", action="store_true")
        parser.add_argument("--autofix-tests")
        parser.add_argument("--autofix-tests-file")

        args = parser.parse_args([
            '--input-dir', '/tmp/reports',
            '--output-dir', '/tmp/output',
            '--table-name', 'results_custom',
            '--environment', 'qa-1',
            '--skip-report',
            '--autofix-tests', 'TestA.testMethod1,TestB.testMethod2',
        ])

        assert args.input_dir == '/tmp/reports'
        assert args.output_dir == '/tmp/output'
        assert args.table_name == 'results_custom'
        assert args.environment == 'qa-1'
        assert args.skip_report is True
        assert args.skip_autofix is False
        assert args.autofix_tests == 'TestA.testMethod1,TestB.testMethod2'
        assert args.autofix_tests_file is None

    def test_defaults_without_args(self):
        """Parsing empty args should give None/False defaults."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--input-dir")
        parser.add_argument("--output-dir")
        parser.add_argument("--skip-report", action="store_true")
        parser.add_argument("--skip-autofix", action="store_true")

        args = parser.parse_args([])
        assert args.input_dir is None
        assert args.output_dir is None
        assert args.skip_report is False
        assert args.skip_autofix is False


class TestPromptsYaml:
    """Test that config/prompts.yaml is well-formed and contains expected prompt templates."""

    @pytest.fixture
    def prompts(self):
        """Load prompts.yaml once per test."""
        prompts_path = _repo_root / 'config' / 'prompts.yaml'
        assert prompts_path.exists(), f"prompts.yaml not found at {prompts_path}"
        with open(prompts_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def test_prompts_yaml_is_valid(self, prompts):
        """prompts.yaml should parse as a valid YAML dict."""
        assert isinstance(prompts, dict)

    def test_classification_prompt_exists(self, prompts):
        """classification_prompt key should exist."""
        assert 'classification_prompt' in prompts

    def test_summary_prompt_exists(self, prompts):
        """summary_prompt key should exist."""
        assert 'summary_prompt' in prompts

    def test_recurring_analysis_prompt_exists(self, prompts):
        """recurring_analysis_prompt key should exist."""
        assert 'recurring_analysis_prompt' in prompts

    def test_classification_prompt_has_placeholder(self, prompts):
        """classification_prompt should contain {failure_details} placeholder."""
        assert '{failure_details}' in prompts['classification_prompt']

    def test_summary_prompt_has_placeholders(self, prompts):
        """summary_prompt should contain key placeholders for test metrics."""
        prompt = prompts['summary_prompt']
        assert '{total_tests}' in prompt
        assert '{passed}' in prompt
        assert '{failed}' in prompt
        assert '{pass_rate}' in prompt

    def test_recurring_prompt_has_placeholders(self, prompts):
        """recurring_analysis_prompt should contain {failure_count} and {days}."""
        prompt = prompts['recurring_analysis_prompt']
        assert '{failure_count}' in prompt
        assert '{days}' in prompt

    def test_classification_prompt_mentions_all_categories(self, prompts):
        """classification_prompt should mention PRODUCT_BUG, AUTOMATION_ISSUE, and PRODUCT_CHANGE."""
        prompt = prompts['classification_prompt']
        assert 'PRODUCT_BUG' in prompt
        assert 'AUTOMATION_ISSUE' in prompt
        assert 'PRODUCT_CHANGE' in prompt

    def test_classification_prompt_requests_json(self, prompts):
        """classification_prompt should instruct the LLM to respond with JSON."""
        prompt = prompts['classification_prompt']
        assert 'JSON' in prompt or 'json' in prompt.lower()


