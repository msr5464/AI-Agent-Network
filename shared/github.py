"""Shared GitHub PR creation helper for all QA-Agent-Network steps."""

import subprocess
from pathlib import Path
from typing import Optional


def create_pr(
    workspace: Path,
    full_repo: str,
    title: str,
    body: str,
    branch: str,
    base: str,
    reviewers: Optional[list] = None,
) -> tuple:
    """
    Create a GitHub PR using the `gh` CLI.
    workspace — repo directory to run gh from (must be checked out)
    full_repo — "org/repo" string
    Returns (pr_url, error): pr_url is the PR URL string on success, None on
    failure; error carries gh's stderr on failure (empty string on success)
    so the caller can surface WHY, not just that it failed.
    """
    cmd = [
        "gh", "pr", "create",
        "--repo", full_repo,
        "--title", title,
        "--body", body,
        "--base", base,
        "--head", branch,
    ]
    for reviewer in (reviewers or []):
        cmd += ["--reviewer", reviewer]

    result = subprocess.run(
        cmd, cwd=str(workspace), capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return (result.stdout.strip() or None), ""
