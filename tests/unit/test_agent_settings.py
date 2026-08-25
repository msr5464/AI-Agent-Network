"""
Unit tests for qa_agents_server/agent_settings.py.

Covers the two behaviours the admin Agent Settings page depends on:
  - config/.env is edited in place — comments and unrelated keys survive
  - a masked secret submitted unchanged does not overwrite the real value
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))

from qa_agents_server import agent_settings  # noqa: E402


_SAMPLE_ENV = """\
# ─────────────────────────────────────────────
# config/.env — shared configuration
# ─────────────────────────────────────────────

CLAUDE_CLI_PATH=claude

# ── GitHub ──
GITHUB_TOKEN=ghp_realsecrettoken_abc123
MAX_FIX_ATTEMPTS=3

# A key no schema entry covers
UNRELATED_KEY=keepme
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the module at a throwaway .env and isolate os.environ."""
    path = tmp_path / ".env"
    path.write_text(_SAMPLE_ENV)
    monkeypatch.setattr(agent_settings, "CONFIG_ENV_FILE", path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    return path


class TestSchema:
    """The frontend renders straight off this schema, so its shape is a contract."""

    def test_every_field_has_the_full_contract(self):
        required = {"key", "env_var", "label", "description",
                    "type", "category", "default", "sensitive"}
        for field in agent_settings.SETTINGS_SCHEMA:
            missing = required - set(field)
            assert not missing, f"{field.get('key')} is missing {missing}"

    def test_keys_and_env_vars_are_unique(self):
        keys = [f["key"] for f in agent_settings.SETTINGS_SCHEMA]
        envs = [f["env_var"] for f in agent_settings.SETTINGS_SCHEMA]
        assert len(keys) == len(set(keys))
        assert len(envs) == len(set(envs))

    def test_categories_are_all_renderable(self):
        for field in agent_settings.SETTINGS_SCHEMA:
            assert field["category"] in agent_settings.CATEGORIES

    def test_select_fields_declare_options(self):
        for field in agent_settings.SETTINGS_SCHEMA:
            if field["type"] == "select":
                assert field.get("options"), f"{field['key']} is a select with no options"

    def test_per_invocation_vars_are_not_exposed(self):
        """Writing these to config/.env would pin every run to one test."""
        forbidden = {"TEST_NAME", "FORCE", "REPAIR", "BUILD_TAG", "MODULE",
                     "SESSION_ID", "AUDIT_DIR", "AGENT_DIR", "REPO_ROOT",
                     "HANDOFF_FILE", "INPUT_FILE", "FIX_ATTEMPT", "START_FROM_STEP"}
        exposed = {f["env_var"] for f in agent_settings.SETTINGS_SCHEMA}
        assert not (exposed & forbidden)


class TestEnvFileWrites:

    def test_existing_key_is_updated_in_place(self, env_file):
        agent_settings.set_many({"max_fix_attempts": 4})
        assert "MAX_FIX_ATTEMPTS=4" in env_file.read_text()

    def test_comments_and_unrelated_keys_survive(self, env_file):
        agent_settings.set_many({"max_fix_attempts": 4})
        text = env_file.read_text()
        assert "# ── GitHub ──" in text
        assert "# config/.env — shared configuration" in text
        assert "UNRELATED_KEY=keepme" in text

    def test_new_key_is_appended(self, env_file):
        agent_settings.set_many({"slack_notify_channel": "#qa-new"})
        assert "SLACK_NOTIFY_CHANNEL=#qa-new" in env_file.read_text()

    def test_unknown_keys_are_ignored(self, env_file):
        before = env_file.read_text()
        agent_settings.set_many({"not_a_real_setting": "x"})
        assert env_file.read_text() == before

    def test_save_updates_os_environ_for_the_next_run(self, env_file):
        """runner.py builds each run's env from os.environ.copy(), so this is
        what makes a save apply without restarting the server."""
        agent_settings.set_many({"max_fix_attempts": 7})
        assert os.environ["MAX_FIX_ATTEMPTS"] == "7"

    def test_booleans_are_written_lowercase(self, env_file):
        """run.sh compares with [[ "$TESTING_MODE" == "true" ]] — no lowercasing,
        so "True" would silently read as off."""
        agent_settings.set_many({"auto_push": True, "testing_mode": False})
        text = env_file.read_text()
        assert "AUTO_PUSH=true" in text
        assert "TESTING_MODE=false" in text


class TestSecretMasking:

    def test_value_is_masked_in_api_output(self, env_file):
        os.environ["GITHUB_TOKEN"] = "ghp_realsecrettoken_abc123"
        values = agent_settings.get_all_for_api()["values"]
        assert values["github_token"] == "ghp**********123"
        assert "realsecret" not in values["github_token"]

    def test_short_secrets_are_masked_entirely(self):
        assert agent_settings._partial_mask("abc123") == "***"

    def test_resubmitting_the_mask_preserves_the_real_value(self, env_file):
        os.environ["GITHUB_TOKEN"] = "ghp_realsecrettoken_abc123"
        masked = agent_settings.get_all_for_api()["values"]["github_token"]
        agent_settings.set_many({"github_token": masked})
        assert os.environ["GITHUB_TOKEN"] == "ghp_realsecrettoken_abc123"
        assert "ghp_realsecrettoken_abc123" in env_file.read_text()

    def test_a_genuinely_new_secret_is_written(self, env_file):
        os.environ["GITHUB_TOKEN"] = "ghp_realsecrettoken_abc123"
        agent_settings.set_many({"github_token": "ghp_brandnewtoken_xyz789"})
        assert os.environ["GITHUB_TOKEN"] == "ghp_brandnewtoken_xyz789"


class TestCoercion:

    def test_numbers_are_clamped_to_schema_bounds(self, env_file):
        agent_settings.set_many({"max_fix_attempts": 999})
        assert os.environ["MAX_FIX_ATTEMPTS"] == "10"

    def test_garbage_numbers_fall_back_to_the_default(self, env_file):
        agent_settings.set_many({"max_fix_attempts": "not-a-number"})
        assert os.environ["MAX_FIX_ATTEMPTS"] == "3"

    def test_select_rejects_values_outside_its_options(self, env_file):
        agent_settings.set_many({"classifier_effort": "bogus"})
        assert os.environ["CLASSIFIER_EFFORT"] == "medium"


class TestWorkspaceValidation:
    """WORKSPACE_DIR is the one value that hard-fails every agent when wrong:
    run.sh exits outright on an empty one."""

    @pytest.mark.parametrize("bad", ["", "   ", "relative/path", "/nope/does/not/exist"])
    def test_unusable_paths_are_rejected(self, env_file, bad):
        with pytest.raises(agent_settings.SettingsValidationError) as exc:
            agent_settings.set_many({"workspace_dir": bad})
        assert "workspace_dir" in exc.value.errors

    def test_a_path_inside_the_repo_is_rejected(self, env_file):
        with pytest.raises(agent_settings.SettingsValidationError):
            agent_settings.set_many({"workspace_dir": str(_repo_root / "agents")})

    def test_a_parent_directory_is_accepted(self, env_file):
        """The normal setup: one folder holding this repo and the automation
        repo side by side."""
        agent_settings.set_many({"workspace_dir": str(_repo_root.parent)})
        assert os.environ["WORKSPACE_DIR"] == str(_repo_root.parent)

    def test_a_rejected_batch_writes_nothing(self, env_file):
        before = env_file.read_text()
        with pytest.raises(agent_settings.SettingsValidationError):
            agent_settings.set_many({
                "db_name": "should_not_land",
                "workspace_dir": "/nope/does/not/exist",
            })
        assert env_file.read_text() == before
        assert os.environ.get("DB_NAME") != "should_not_land"


class TestShadowDetection:
    """shared/load_env.sh sources agents/<agent>/.env last, so a key declared
    there silently beats anything saved from the UI."""

    @pytest.fixture
    def fake_root(self, tmp_path):
        """A repo root of its own, so the env_file fixture's config/.env (which
        lives at tmp_path) is not itself mistaken for a shadowing override."""
        root = tmp_path / "repo"
        (root / "agents").mkdir(parents=True)
        with mock.patch.object(agent_settings, "REPO_ROOT", root), \
             mock.patch.object(agent_settings, "AGENTS_DIR", root / "agents"):
            yield root

    def test_no_shadowing_reported_when_no_overrides_exist(self, env_file, fake_root):
        assert agent_settings._find_shadowed() == {}

    def test_an_agent_level_override_is_reported(self, env_file, fake_root):
        agent_dir = fake_root / "agents" / "test-healing-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / ".env").write_text("AUTOFIX_MODEL=claude-opus-5\n")
        shadowed = agent_settings._find_shadowed()
        assert shadowed["autofix_model"] == "agents/test-healing-agent/.env"

    def test_a_repo_root_override_is_reported(self, env_file, fake_root):
        (fake_root / ".env").write_text("AUTO_PUSH=false\n")
        assert agent_settings._find_shadowed()["auto_push"] == ".env"

    def test_commented_out_keys_do_not_count_as_shadowing(self, env_file, fake_root):
        agent_dir = fake_root / "agents" / "test-healing-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / ".env").write_text("# AUTOFIX_MODEL=claude-opus-5\n")
        assert agent_settings._find_shadowed() == {}
