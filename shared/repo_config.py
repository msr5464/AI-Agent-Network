"""Shared repo metadata loader for all QA-Agent-Network agents.

Reads config/repo-map.json and returns per-repo config (language, test runner,
PR checklist, etc.). Falls back to environment variables if the repo key is not found.

Usage:
    from shared.repo_config import load_repo_config
    cfg = load_repo_config("Jarvis")
    test_cmd = cfg["test_runner"]["cmd"]
    pr_checklist = cfg["pr_checklist"]
"""

import json
import os
from pathlib import Path

_REPO_MAP_PATH = Path(__file__).resolve().parents[1] / "config" / "repo-map.json"


def _load_map() -> dict:
    if _REPO_MAP_PATH.exists():
        try:
            return json.loads(_REPO_MAP_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_repo_config(repo_name: str | None = None) -> dict:
    """Return repo config for repo_name from config/repo-map.json.

    Falls back gracefully:
    - If repo_name is None, uses GITHUB_REPO_AUTOMATION env var
    - If repo not in map, returns a minimal config built from env vars
    """
    if repo_name is None:
        repo_name = os.environ.get("GITHUB_REPO_AUTOMATION", "")

    repo_map = _load_map()
    if repo_name and repo_name in repo_map:
        cfg = dict(repo_map[repo_name])
        # Resolve any {method}/{class} placeholders using env overrides
        if "TEST_RUNNER_CMD" in os.environ:
            cfg["test_runner"] = {"cmd": os.environ["TEST_RUNNER_CMD"].split()}
        return cfg

    # Fallback: minimal config from env vars
    return {
        "language": os.environ.get("REPO_LANGUAGE", "java"),
        "framework": os.environ.get("REPO_FRAMEWORK", ""),
        "default_branch": os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
        "test_runner": {
            "cmd": os.environ.get("TEST_RUNNER_CMD", "mvn test -Dtest={class}#{method}").split(),
            "test_dirs": ["src/test"],
        },
        "pr_checklist": [],
        "conventions_file": os.environ.get("REPO_CONTEXT_FILE", "CONVENTIONS.md"),
    }
