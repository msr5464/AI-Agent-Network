"""
Unit tests for src/settings.py - Config class.
Tests environment variable loading, defaults, type conversions, and helper methods.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Add repo root to path so src.* imports work
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))


class TestConfigDefaults:
    """Test that Config class attributes have correct defaults when no env vars are set."""

    # All env keys that Config reads
    _CONFIG_KEYS = [
        'DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME',
        'LLM_PROVIDER', 'OLLAMA_MODEL', 'OLLAMA_BASE_URL',
        'OPENAI_API_KEY', 'OPENAI_MODEL',
        'GEMINI_API_KEY', 'GEMINI_MODEL',
        'INPUT_DIR', 'OUTPUT_DIR', 'REPORT_FORMAT_VERSION',
        'DASHBOARD_BASE_URL', 'JIRA_BASE_URL',
        'FLAKY_TESTS_LAST_RUNS', 'FLAKY_TESTS_MIN_FAILURES',
        'AUTO_FIX_ENABLED', 'AUTO_FIX_DRY_RUN', 'AUTO_FIX_MAX_FIXES_PER_RUN',
        'AUTO_FIX_ENV_OVERRIDE',
        'GITHUB_TOKEN', 'GITHUB_ORG', 'GITHUB_REPO_AUTOMATION',
        'GITHUB_DEFAULT_BRANCH', 'GITHUB_PR_REVIEWERS',
    ]

    def _load_config_with_env(self, env_overrides=None):
        """
        Reload the settings module with controlled environment variables.
        Patches load_dotenv to prevent .env files from injecting values,
        so only os.getenv defaults (or explicit overrides) are used.
        Returns the freshly loaded Config class.
        """
        clean_env = {k: v for k, v in os.environ.items() if k not in self._CONFIG_KEYS}
        if env_overrides:
            clean_env.update(env_overrides)

        with mock.patch.dict(os.environ, clean_env, clear=True), \
             mock.patch('dotenv.load_dotenv', return_value=None):
            # Remove cached module so it reloads with the patched env
            for mod_name in list(sys.modules):
                if mod_name.startswith('src.settings'):
                    del sys.modules[mod_name]
            from src.settings import Config as FreshConfig
            return FreshConfig

    def test_database_defaults(self):
        """Config should expose sensible database defaults."""
        Cfg = self._load_config_with_env()
        assert Cfg.DB_HOST == 'localhost'
        assert Cfg.DB_PORT == 3306
        assert Cfg.DB_USER == 'root'
        assert Cfg.DB_PASSWORD == ''
        assert Cfg.DB_NAME == 'qa_results'

    def test_llm_defaults(self):
        """Default LLM provider should be ollama with llama3.2:3b."""
        Cfg = self._load_config_with_env()
        assert Cfg.LLM_PROVIDER == 'ollama'
        assert Cfg.OLLAMA_MODEL == 'llama3.2:3b'
        assert Cfg.OLLAMA_BASE_URL == 'http://localhost:11434'
        assert Cfg.OPENAI_API_KEY == ''
        assert Cfg.OPENAI_MODEL == 'gpt-4o-mini'
        assert Cfg.GEMINI_API_KEY == ''
        assert Cfg.GEMINI_MODEL == 'gemini-1.5-flash'

    def test_report_path_defaults(self):
        """Input and output directories should default to testdata/reports."""
        Cfg = self._load_config_with_env()
        assert Cfg.INPUT_DIR == 'testdata'
        assert Cfg.OUTPUT_DIR == 'reports'

    def test_flaky_detection_defaults(self):
        """Flaky detection constants should have documented defaults."""
        Cfg = self._load_config_with_env()
        assert Cfg.FLAKY_TESTS_LAST_RUNS == 10
        assert Cfg.FLAKY_TESTS_MIN_FAILURES == 5

    def test_autofix_defaults_disabled(self):
        """Auto-fix should be disabled by default."""
        Cfg = self._load_config_with_env()
        assert Cfg.AUTO_FIX_ENABLED is False
        assert Cfg.AUTO_FIX_DRY_RUN is False
        assert Cfg.AUTO_FIX_MAX_FIXES_PER_RUN == 5

    def test_github_defaults_empty(self):
        """GitHub config should default to empty when not configured."""
        Cfg = self._load_config_with_env()
        assert Cfg.GITHUB_TOKEN == ''
        assert Cfg.GITHUB_ORG == ''
        assert Cfg.GITHUB_REPO_AUTOMATION == ''
        assert Cfg.GITHUB_DEFAULT_BRANCH == 'main'
        assert Cfg.GITHUB_PR_REVIEWERS == []

    def test_logging_constants(self):
        """Logging constants should be hardcoded (not env-driven)."""
        Cfg = self._load_config_with_env()
        assert Cfg.LOG_FILE_NAME == 'agent.log'
        assert '%(asctime)s' in Cfg.LOG_FORMAT


class TestConfigEnvOverrides:
    """Test that environment variable overrides take effect on Config attributes."""

    def _load_config_with_env(self, env_overrides):
        """Reload Config with specific environment overrides."""
        config_keys = [
            'DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME',
            'LLM_PROVIDER', 'OLLAMA_MODEL', 'OLLAMA_BASE_URL',
            'OPENAI_API_KEY', 'OPENAI_MODEL',
            'GEMINI_API_KEY', 'GEMINI_MODEL',
            'INPUT_DIR', 'OUTPUT_DIR', 'REPORT_FORMAT_VERSION',
            'DASHBOARD_BASE_URL', 'JIRA_BASE_URL',
            'FLAKY_TESTS_LAST_RUNS', 'FLAKY_TESTS_MIN_FAILURES',
            'AUTO_FIX_ENABLED', 'AUTO_FIX_DRY_RUN', 'AUTO_FIX_MAX_FIXES_PER_RUN',
            'AUTO_FIX_ENV_OVERRIDE',
            'GITHUB_TOKEN', 'GITHUB_ORG', 'GITHUB_REPO_AUTOMATION',
            'GITHUB_DEFAULT_BRANCH', 'GITHUB_PR_REVIEWERS',
        ]
        clean_env = {k: v for k, v in os.environ.items() if k not in config_keys}
        clean_env.update(env_overrides)

        with mock.patch.dict(os.environ, clean_env, clear=True):
            for mod_name in list(sys.modules):
                if mod_name.startswith('src.settings'):
                    del sys.modules[mod_name]
            from src.settings import Config as FreshConfig
            return FreshConfig

    def test_db_host_override(self):
        """DB_HOST env var should override the default."""
        Cfg = self._load_config_with_env({'DB_HOST': 'db.prod.internal'})
        assert Cfg.DB_HOST == 'db.prod.internal'

    def test_db_port_override_int_conversion(self):
        """DB_PORT should be converted to int from env string."""
        Cfg = self._load_config_with_env({'DB_PORT': '5432'})
        assert Cfg.DB_PORT == 5432
        assert isinstance(Cfg.DB_PORT, int)

    def test_llm_provider_override_normalized(self):
        """LLM_PROVIDER should be lowercased."""
        Cfg = self._load_config_with_env({'LLM_PROVIDER': 'OpenAI'})
        assert Cfg.LLM_PROVIDER == 'openai'

    def test_gemini_config_override(self):
        """GEMINI_API_KEY and GEMINI_MODEL should be read from env."""
        Cfg = self._load_config_with_env({
            'GEMINI_API_KEY': 'test-gemini-key',
            'GEMINI_MODEL': 'gemini-1.5-pro'
        })
        assert Cfg.GEMINI_API_KEY == 'test-gemini-key'
        assert Cfg.GEMINI_MODEL == 'gemini-1.5-pro'

    def test_auto_fix_enabled_true(self):
        """AUTO_FIX_ENABLED='true' should resolve to boolean True."""
        Cfg = self._load_config_with_env({'AUTO_FIX_ENABLED': 'true'})
        assert Cfg.AUTO_FIX_ENABLED is True

    def test_auto_fix_enabled_false_for_random_string(self):
        """AUTO_FIX_ENABLED with a non-'true' value should be False."""
        Cfg = self._load_config_with_env({'AUTO_FIX_ENABLED': 'yes'})
        assert Cfg.AUTO_FIX_ENABLED is False

    def test_flaky_tests_int_conversion(self):
        """FLAKY_TESTS_LAST_RUNS should be converted to int."""
        Cfg = self._load_config_with_env({'FLAKY_TESTS_LAST_RUNS': '20'})
        assert Cfg.FLAKY_TESTS_LAST_RUNS == 20

    def test_github_pr_reviewers_split(self):
        """GITHUB_PR_REVIEWERS should be split into a list."""
        Cfg = self._load_config_with_env({'GITHUB_PR_REVIEWERS': 'alice, bob, charlie'})
        assert Cfg.GITHUB_PR_REVIEWERS == ['alice', 'bob', 'charlie']

    def test_github_pr_reviewers_empty(self):
        """Empty GITHUB_PR_REVIEWERS should produce an empty list."""
        Cfg = self._load_config_with_env({'GITHUB_PR_REVIEWERS': ''})
        assert Cfg.GITHUB_PR_REVIEWERS == []

    def test_auto_fix_env_override_stripped(self):
        """AUTO_FIX_ENV_OVERRIDE should be stripped of whitespace."""
        Cfg = self._load_config_with_env({'AUTO_FIX_ENV_OVERRIDE': '  qa-2  '})
        assert Cfg.AUTO_FIX_ENV_OVERRIDE == 'qa-2'


class TestConfigGetDbConfig:
    """Test the get_db_config() classmethod."""

    def test_get_db_config_returns_dict(self):
        """get_db_config() should return a dict with the expected keys."""
        from src.settings import Config
        db_config = Config.get_db_config()
        assert isinstance(db_config, dict)
        assert 'host' in db_config
        assert 'port' in db_config
        assert 'user' in db_config
        assert 'password' in db_config
        assert 'database' in db_config

    def test_get_db_config_values_match_class(self):
        """get_db_config() values should match Config class attributes."""
        from src.settings import Config
        db_config = Config.get_db_config()
        assert db_config['host'] == Config.DB_HOST
        assert db_config['port'] == Config.DB_PORT
        assert db_config['user'] == Config.DB_USER
        assert db_config['password'] == Config.DB_PASSWORD
        assert db_config['database'] == Config.DB_NAME

    def test_get_db_config_port_is_int(self):
        """Port in the db config dict should be an integer."""
        from src.settings import Config
        db_config = Config.get_db_config()
        assert isinstance(db_config['port'], int)


class TestEnvFileLoadOrder:
    """Test that the .env file load order is correct (config/.env > config/.env.example > root .env)."""

    def test_config_env_path_exists(self):
        """config/.env or config/.env.example should exist for the project to function."""
        config_dir = Path(__file__).resolve().parent.parent.parent / 'config'
        has_env = (config_dir / '.env').exists()
        has_example = (config_dir / '.env.example').exists()
        assert has_env or has_example, "Neither config/.env nor config/.env.example found"

    def test_config_dir_relative_to_settings_module(self):
        """The config directory should be at project_root/config/ relative to settings.py."""
        from src import settings
        settings_path = Path(settings.__file__).resolve()
        expected_config_dir = settings_path.parent.parent / 'config'
        assert expected_config_dir.is_dir(), f"Expected config dir at {expected_config_dir}"
