"""Shared GitHub PR creation helper for all QA-Agent-Network steps."""

import subprocess
from pathlib import Path
from typing import Optional

from shared.feedback import verdict_block

# GitHub refuses a PR body longer than this, and `gh pr create` fails outright.
MAX_BODY_CHARS = 65536


def with_verdict_block(body: str) -> str:
    """The PR body with the reviewer verdict block at the end.

    The block is what turns a review into something the agents can learn from
    (shared/feedback.py). It always survives: an over-long body is cut before it,
    never through it.
    """
    block = verdict_block()
    room = MAX_BODY_CHARS - len(block) - 2
    text = (body or "").rstrip()
    if len(text) > room:
        note = "\n\n_(PR description truncated to fit GitHub's limit.)_"
        text = text[:room - len(note)].rstrip() + note
    return f"{text}\n\n{block}"


def create_pr(
    workspace: Path,
    full_repo: str,
    title: str,
    body: str,
    branch: str,
    base: str,
    reviewers: Optional[list] = None,
    draft: bool = False,
) -> tuple:
    """
    Create a GitHub PR using the `gh` CLI.
    workspace — repo directory to run gh from (must be checked out)
    full_repo — "org/repo" string
    draft     — open it as a draft: work a reviewer should see, not merge
    Returns (pr_url, error): pr_url is the PR URL string on success, None on
    failure; error carries gh's stderr on failure (empty string on success)
    so the caller can surface WHY, not just that it failed.
    """
    cmd = [
        "gh", "pr", "create",
        "--repo", full_repo,
        "--title", title,
        "--body", with_verdict_block(body),
        "--base", base,
        "--head", branch,
    ]
    for reviewer in (reviewers or []):
        cmd += ["--reviewer", reviewer]
    if draft:
        cmd.append("--draft")

    result = subprocess.run(
        cmd, cwd=str(workspace), capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return (result.stdout.strip() or None), ""
