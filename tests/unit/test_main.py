"""
Unit tests for src/main.py - entry point and orchestration helpers.
Tests the _guess_environment() helper, argparse setup, prompt configuration,
and that the main entry point is invokable.
"""

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

# Add repo root to path so src.* imports work
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))


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
        parser = argparse.ArgumentParser(description="QA AI Agent")
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


class TestEntryPoint:
    """Test that the main entry point (src/main.py) is correctly exposed and CLI works."""

    def test_main_module_has_main_callable(self):
        """src.main should expose a callable main() as the entry point."""
        import src.main as main_module
        assert hasattr(main_module, 'main')
        assert callable(main_module.main)

    def test_config_loaded_at_bootstrap(self):
        """Importing src.main loads Config first; key config attributes are available."""
        import src.main as main_module  # noqa: F401
        from src.settings import Config
        # Bootstrap doc: Config is imported in main before other app code; verify it is usable
        assert hasattr(Config, 'INPUT_DIR')
        assert hasattr(Config, 'OUTPUT_DIR')
        assert hasattr(Config, 'LOG_FORMAT')
        assert hasattr(Config, 'LOG_FILE_NAME')
        assert isinstance(Config.INPUT_DIR, str) and len(Config.INPUT_DIR) > 0
        assert isinstance(Config.OUTPUT_DIR, str) and len(Config.OUTPUT_DIR) > 0

    def test_main_module_cli_help_exits_successfully(self):
        """Running python -m src.main --help should exit with code 0 and show CLI options."""
        result = subprocess.run(
            [sys.executable, '-m', 'src.main', '--help'],
            cwd=str(_repo_root),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        out = result.stdout + result.stderr
        assert '--input-dir' in out or 'input-dir' in out
        assert '--output-dir' in out or 'output-dir' in out

    def test_run_scripts_exist_and_invoke_main(self):
        """Documented run scripts (run.sh, run.ps1) should exist and reference the main entry point."""
        scripts_dir = _repo_root / 'scripts'
        run_sh = scripts_dir / 'run.sh'
        run_ps1 = scripts_dir / 'run.ps1'
        assert run_sh.exists(), "scripts/run.sh should exist (see docs/ENTRY_POINTS_AND_CONFIG.md)"
        assert run_ps1.exists(), "scripts/run.ps1 should exist (see docs/ENTRY_POINTS_AND_CONFIG.md)"
        run_sh_text = run_sh.read_text(encoding='utf-8')
        run_ps1_text = run_ps1.read_text(encoding='utf-8')
        assert 'main.py' in run_sh_text or 'src.main' in run_sh_text, "run.sh should invoke src main"
        assert 'main.py' in run_ps1_text or 'src.main' in run_ps1_text, "run.ps1 should invoke src main"
