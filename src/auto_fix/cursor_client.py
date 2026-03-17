"""
Cursor patch application adapter using the headless Cursor CLI (`agent`).
Reference: https://cursor.com/docs/cli/headless
"""

import logging
import os
import shutil
import subprocess
from typing import List, Tuple

from .models import FileChange

logger = logging.getLogger(__name__)


class CursorClient:
    """
    Adapter for applying patches via Cursor headless CLI.
    Requires `agent` to be installed and `CURSOR_API_KEY` set.
    """

    def __init__(self):
        self.api_token = os.getenv("CURSOR_API_KEY")
        self.agent_path = shutil.which("agent")

    def _is_configured(self) -> bool:
        return bool(self.agent_path and self.api_token)

    def _run_agent(self, prompt: str, cwd: str) -> Tuple[bool, str]:
        env = os.environ.copy()
        env["CURSOR_API_KEY"] = self.api_token
        try:
            result = subprocess.run(
                [self.agent_path, "-p", "--force", prompt],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                msg = f"Cursor CLI failed (code {result.returncode}): {result.stderr}"
                logger.error(msg)
                return False, msg
            return True, result.stdout
        except Exception as e:
            logger.error(f"Cursor CLI invocation error: {e}")
            return False, str(e)

    def apply_changes(self, repo_path: str, file_changes: List[FileChange]) -> Tuple[bool, str]:
        """
        Apply file changes via Cursor CLI by instructing it to write the exact content.
        One invocation per file for determinism.
        """
        if not self._is_configured():
            msg = "Cursor CLI not configured (missing agent binary or CURSOR_API_KEY)"
            logger.error(msg)
            return False, msg

        for change in file_changes:
            prompt = (
                f"Write exactly the following content to {change.file_path}:\n"
                "```\n"
                f"{change.new_content}\n"
                "```"
            )
            ok, err = self._run_agent(prompt, cwd=repo_path)
            if not ok:
                return False, err
        return True, ""

    def restore_files(self, repo_path: str, originals: dict) -> None:
        """
        Restore files to their original contents using Cursor CLI.
        """
        if not self._is_configured():
            logger.error("Cursor CLI not configured; cannot restore files")
            return

        for rel_path, content in originals.items():
            prompt = (
                f"Write exactly the following content to {rel_path}:\n"
                "```\n"
                f"{content}\n"
                "```"
            )
            ok, err = self._run_agent(prompt, cwd=repo_path)
            if not ok:
                logger.error("Restore failed for %s: %s", rel_path, err)
