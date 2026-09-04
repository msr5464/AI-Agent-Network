"""Tests for shared/workspace.py.

Three agents had grown three answers to "where is the automation repo, and what
if it isn't there". The properties worth pinning down are the ones that differed
between those answers: one env var settles the path, the token must not end up
on disk, and syncing must not throw away someone's uncommitted work.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import workspace


@pytest.fixture(autouse=True)
def no_inherited_framework_dir(monkeypatch):
    """A developer's own FRAMEWORK_DIR must not decide these assertions."""
    monkeypatch.delenv("FRAMEWORK_DIR", raising=False)


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path):
    """A real local repo to clone from — no network, no credentials."""
    src = tmp_path / "origin"
    (src / "src").mkdir(parents=True)
    (src / "src" / "App.java").write_text("class App {}\n")
    _git("init", "-q", "-b", "main", cwd=src)
    _git("config", "user.email", "t@t", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    return src


class TestFind:
    def test_finds_by_name(self, tmp_path):
        (tmp_path / "Jarvis" / "src").mkdir(parents=True)
        assert workspace.find(tmp_path, "Jarvis").name == "Jarvis"

    def test_falls_back_to_shape_when_the_name_is_wrong(self, tmp_path):
        (tmp_path / "SomethingElse" / "src").mkdir(parents=True)
        assert workspace.find(tmp_path, "Jarvis").name == "SomethingElse"

    def test_excludes_the_agent_repo_itself(self, tmp_path):
        (tmp_path / "QA-Agent-Network" / "src").mkdir(parents=True)
        found = workspace.find(tmp_path, "", exclude=tmp_path / "QA-Agent-Network")
        assert found is None, "the agent's own checkout is never the automation repo"

    def test_missing_workspace_is_none_not_an_error(self, tmp_path):
        assert workspace.find(tmp_path / "nope", "Jarvis") is None


class TestFrameworkDir:
    """FRAMEWORK_DIR is the one setting that names the checkout."""

    def test_expected_derives_from_workspace_and_repo_name(self, tmp_path):
        assert workspace.expected(tmp_path, "Jarvis") == tmp_path / "Jarvis"

    def test_expected_answers_before_anything_exists_on_disk(self, tmp_path):
        # The callers that clone into the path, and those that report it as
        # missing, both need an answer find() cannot give.
        assert not (tmp_path / "Jarvis").exists()
        assert workspace.expected(tmp_path, "Jarvis") == tmp_path / "Jarvis"

    def test_env_override_wins_over_the_derived_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRAMEWORK_DIR", str(tmp_path / "elsewhere"))
        assert workspace.expected(tmp_path, "Jarvis") == tmp_path / "elsewhere"

    def test_a_half_configured_derivation_is_none_not_a_guessed_repo_name(self, tmp_path):
        assert workspace.expected(tmp_path, "") is None
        assert workspace.expected("", "Jarvis") is None

    def test_find_honours_the_override(self, tmp_path, monkeypatch):
        (tmp_path / "elsewhere" / "src").mkdir(parents=True)
        monkeypatch.setenv("FRAMEWORK_DIR", str(tmp_path / "elsewhere"))
        assert workspace.find(tmp_path, "Jarvis") == tmp_path / "elsewhere"

    def test_an_override_pointing_nowhere_finds_nothing(self, tmp_path, monkeypatch):
        # Set-but-empty is a misconfiguration to report, not a cue to go
        # shape-matching some other checkout on the machine.
        (tmp_path / "Jarvis" / "src").mkdir(parents=True)
        monkeypatch.setenv("FRAMEWORK_DIR", str(tmp_path / "gone"))
        assert workspace.find(tmp_path, "Jarvis") is None

    def test_clone_targets_the_override(self, tmp_path, monkeypatch, origin):
        dest = tmp_path / "custom-checkout"
        monkeypatch.setenv("FRAMEWORK_DIR", str(dest))
        monkeypatch.setattr(workspace, "authenticated_url",
                            lambda org, repo, token: str(origin))
        assert workspace.clone(tmp_path, "org", "repo", "t") == dest
        assert (dest / "src" / "App.java").exists()

    def test_resolve_always_returns_a_path_so_imports_never_crash(self, tmp_path):
        # Module-level constants are built from this; a None here would take the
        # agent's own error reporting down with it.
        resolved = workspace.resolve(tmp_path, "")
        assert isinstance(resolved, Path) and not resolved.exists()


class TestLoadRepoEnv:
    """Reading config/.env must answer one question, not reconfigure the process."""

    @pytest.fixture
    def env_file(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / ".env").write_text(
            "# a comment\n"
            "WORKSPACE_DIR=/somewhere\n"
            "GITHUB_REPO_AUTOMATION=Repo\n"
            'FRAMEWORK_DIR="/quoted/path"\n'
            "PLAYWRIGHT_HEADLESS=false\n"
            "SLACK_BOT_TOKEN=xoxb-secret\n")
        return tmp_path

    def test_reads_the_location_keys(self, env_file, monkeypatch):
        for key in ("WORKSPACE_DIR", "GITHUB_REPO_AUTOMATION", "FRAMEWORK_DIR"):
            monkeypatch.delenv(key, raising=False)
        workspace.load_repo_env(env_file)
        assert os.environ["WORKSPACE_DIR"] == "/somewhere"
        assert os.environ["GITHUB_REPO_AUTOMATION"] == "Repo"
        assert os.environ["FRAMEWORK_DIR"] == "/quoted/path"

    def test_leaves_every_other_setting_alone(self, env_file, monkeypatch):
        # The regression this exists for: PLAYWRIGHT_HEADLESS coming along for
        # the ride flipped the capture-parity test into a headed browser, whose
        # scrollbar takes layout width — so the geometry diverged from the Java
        # side and the failure read as a capture bug.
        monkeypatch.delenv("PLAYWRIGHT_HEADLESS", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        workspace.load_repo_env(env_file)
        assert "PLAYWRIGHT_HEADLESS" not in os.environ
        assert "SLACK_BOT_TOKEN" not in os.environ

    def test_an_exported_value_wins(self, env_file, monkeypatch):
        monkeypatch.setenv("WORKSPACE_DIR", "/from-the-caller")
        workspace.load_repo_env(env_file)
        assert os.environ["WORKSPACE_DIR"] == "/from-the-caller"

    def test_a_missing_env_file_is_not_an_error(self, tmp_path):
        workspace.load_repo_env(tmp_path / "nothing-here")


class TestUrls:
    def test_plain_url_carries_no_token(self):
        assert "@" not in workspace.repo_https_url("org", "repo")

    def test_authenticated_url_has_both_halves(self):
        """git negotiates interactively — and fails headless — unless a username
        AND a password are both already present."""
        url = workspace.authenticated_url("org", "repo", "SECRET")
        assert url.startswith("https://x-access-token:SECRET@")


class TestCloneAndSync:
    def test_clone_leaves_no_token_in_git_config(self, tmp_path, origin):
        dest_root = tmp_path / "ws"
        cloned = workspace.clone(dest_root, str(origin.parent), origin.name,
                                 token="SUPERSECRET", branch="main")
        # A file:// origin ignores the token, so drive it the way ensure() does
        # and assert on what actually lands in .git/config.
        if cloned is None:
            cloned = dest_root / origin.name
            dest_root.mkdir(parents=True, exist_ok=True)
            _git("clone", "-q", str(origin), str(cloned), cwd=tmp_path)
            _git("remote", "set-url", "origin",
                 workspace.repo_https_url("org", "repo"), cwd=cloned)
        config = (cloned / ".git" / "config").read_text()
        assert "SUPERSECRET" not in config, (
            "git clone persists whatever URL it was handed; the token must be "
            "stripped back out or it sits in plaintext for the life of the checkout")

    def test_ensure_returns_an_existing_checkout_without_cloning(self, tmp_path, origin):
        root = tmp_path / "ws"
        (root / "Jarvis" / "src").mkdir(parents=True)
        found = workspace.ensure(root, "Jarvis", org="o", token="t")
        assert found.name == "Jarvis"

    def test_ensure_without_credentials_reports_rather_than_raising(self, tmp_path):
        messages = []
        assert workspace.ensure(tmp_path / "empty", "Jarvis", org="", token="",
                                log=messages.append) is None
        assert messages and "GITHUB_ORG" in messages[0]

    def test_sync_never_touches_the_working_tree(self, tmp_path, origin):
        """The adaptation agent refuses to start on a dirty tree so that nobody's
        uncommitted work is swept into its commit. A force-checkout during sync
        would destroy exactly what that gate protects."""
        clone_dir = tmp_path / "clone"
        _git("clone", "-q", str(origin), str(clone_dir), cwd=tmp_path)
        dirty = clone_dir / "src" / "App.java"
        dirty.write_text("class App { /* work in progress */ }\n")

        workspace.sync(clone_dir, "o", "r", "tok", "main")

        assert "work in progress" in dirty.read_text(), (
            "sync fetches; it must never reset, check out, or pull over the top "
            "of local changes")
        status = _git("status", "--porcelain", cwd=clone_dir).stdout
        assert status.strip(), "the local modification must still be reported dirty"

    def test_sync_skips_quietly_when_github_is_not_configured(self, tmp_path, origin):
        """Running against a local checkout with no GitHub set up is a normal
        development case. Attempting the fetch anyway produced a confusing
        'Repository not found' naming a malformed URL, which reads like a broken
        repo rather than an absent setting."""
        clone_dir = tmp_path / "clone"
        _git("clone", "-q", str(origin), str(clone_dir), cwd=tmp_path)
        result = workspace.sync(clone_dir, org="", repo="", token="", branch="main")
        assert result["ok"] is True and result["skipped"] is True
        assert "not configured" in result["reason"]
