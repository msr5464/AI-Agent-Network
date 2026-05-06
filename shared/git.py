"""Shared git subprocess wrapper for all QA-Agent-Network steps."""

import subprocess
from pathlib import Path
from typing import Tuple


def run_git(args: list, cwd: Path, timeout: int = 60) -> Tuple[bool, str, str]:
    """
    Run a git command in cwd.
    Returns (success, stdout, stderr).
    success = True when returncode == 0.
    """
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
