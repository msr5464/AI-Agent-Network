"""Per-repo metadata from config/repo-map.json.

Only the `framework` field is used, by shared/frameworks/detect.py, as a
fallback when a repo's build files do not settle which framework it uses.

Usage:
    from shared.repo_config import load_repo_config
    framework = load_repo_config("Playwright-Automation-Framework").get("framework")
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
    """The repo's entry in config/repo-map.json, or {} when it has none.

    repo_name defaults to GITHUB_REPO_AUTOMATION.
    """
    if repo_name is None:
        repo_name = os.environ.get("GITHUB_REPO_AUTOMATION", "")
    entry = _load_map().get(repo_name) if repo_name else None
    return dict(entry) if isinstance(entry, dict) else {}
