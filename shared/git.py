"""Shared git subprocess wrapper for all QA-Agent-Network steps."""

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from shared.feedback import AGENT_TRAILER, SESSION_TRAILER, trailers


def with_agent_trailer(message: str, agent: str) -> str:
    """The commit message, signed with an `Agent: <agent>` trailer.

    A commit on an agent's branch that carries no such trailer, made after the
    agent's last one, was made by a person: that is how the feedback collector
    tells a reviewer's correction from the agent's own work. Authorship cannot,
    because the agents push with the same token as the people reviewing them.

    Every agent message already ends with `Session: <id>`, which git reads as a
    trailer, so the agent line joins that paragraph. Idempotent.
    """
    text = (message or "").rstrip()
    if trailers(text).get(AGENT_TRAILER):
        return text
    lines = text.split("\n")
    if len(lines) > 2 and lines[-1].startswith(f"{SESSION_TRAILER}: ") and not lines[-2].strip():
        return "\n".join(lines[:-1] + [f"{AGENT_TRAILER}: {agent}", lines[-1]])
    return f"{text}\n\n{AGENT_TRAILER}: {agent}"


def run_git(args: list, cwd: Path, timeout: int = 60, push_url: Optional[str] = None) -> Tuple[bool, str, str]:
    """
    Run a git command in cwd.
    Returns (success, stdout, stderr).
    success = True when returncode == 0.

    push_url, if given, replaces any literal "origin" argument with this URL
    for THIS invocation only — never written to .git/config, so it can't
    persist on disk or get echoed by git's own error output on some LATER,
    unrelated command. Confirmed by direct testing against a real GitHub
    remote: a remote configured with no embedded credentials makes git try
    to interactively negotiate a username/password, which fails hard in a
    headless subprocess with no TTY — and NEITHER a bare token-only URL NOR
    `-c http.extraHeader` avoids this. Only a URL with a username AND
    password both already present (e.g.
    "https://x-access-token:<TOKEN>@github.com/OWNER/REPO.git" — the
    username is a fixed, non-secret placeholder; the token is the real
    secret, held only in this process's memory for one invocation) skips
    git's own credential negotiation entirely and goes straight to the
    server, which is what actually fixed the original failure.

    GIT_TERMINAL_PROMPT=0 is set unconditionally so that if credentials are
    ever genuinely missing for some other reason, git fails with a clear
    "terminal prompts disabled" message instead of an opaque OS-level
    "Device not configured".
    """
    cmd = ["git"]
    for a in args:
        cmd.append(push_url if (push_url and a == "origin") else a)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
