"""Agent descriptors — what the server needs to know to drive any agent.

The server was originally written for test-authoring-agent alone, with the agent
name, its run.sh, its audit directory and its step model all hardcoded. Adding a
second agent by copying that machinery would have duplicated the genuinely hard
parts (SSE streaming, the run registry, process-group cancellation, boot
reconciliation) — all of which are already agent-agnostic.

So the differences are collected here instead: where the agent lives, what its
steps are called, and how a request body becomes environment variables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from qa_agents_server.paths import AGENTS_DIR

# (key, step output filename, display name). Display names are mirrored in
# AI-Test-Studio/frontend/customer/index.html's STEP_LABELS — keep them in sync.
AUTHORING_STEPS: List[Tuple[str, str, str]] = [
    ("parse", "01-parse.json", "Parse"),
    ("validate_web", "02-validate-web.json", "Validate"),
    ("generate", "03-generate.json", "Generate"),
    ("run_and_fix", "04-run-and-fix.json", "Run & Fix"),
    ("ship", "05-ship.json", "Ship"),
]

# Reproduce only runs in standalone mode; in pipeline mode it stays pending and
# the UI shows it as skipped rather than stuck.
HEALING_STEPS: List[Tuple[str, str, str]] = [
    ("reproduce", "00-reproduce.json", "Reproduce"),
    ("fix", "01-fix.json", "Fix"),
    ("ship", "02-ship.json", "Ship"),
]


class AgentConfigError(Exception):
    """A request that cannot be turned into a valid run."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _slug(value: str, fallback: str = "run") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip()).strip("-")
    return cleaned or fallback


@dataclass(frozen=True)
class AgentSpec:
    name: str
    run_sh: Path
    audit_dir: Path
    queue_dir: Path
    steps: List[Tuple[str, str, str]]
    session_prefix: str
    build_env: Callable[[dict], Dict[str, str]]
    describe_run: Callable[[dict], str]
    supports_resume: bool = False
    # Which request field names the run's headline label, for the UI.
    label_field: str = "module"

    def make_session_id(self, payload: dict) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{self.session_prefix}-{_slug(self.describe_run(payload))}"

    def step_keys(self) -> List[str]:
        return [key for key, _file, _label in self.steps]


# ── test-authoring-agent ──────────────────────────────────────────────────────

def _auto_push_env(payload: dict) -> Dict[str, str]:
    """AUTO_PUSH, but only when the caller actually expressed a preference.

    Exporting it unconditionally makes an absent field indistinguishable from an
    explicit "no" — and because shared/load_env.sh lets caller-exported vars beat
    the .env files, that silently overrode AUTO_PUSH=true in config/.env and
    turned real runs into dry runs.
    """
    if "auto_push" not in payload or payload["auto_push"] is None:
        return {}
    return {"AUTO_PUSH": "true" if payload["auto_push"] else "false"}


def auto_push_default() -> bool:
    """What config/.env says, for a UI that wants to show the real default."""
    return os.environ.get("AUTO_PUSH", "false").strip().lower() == "true"


def _authoring_env(payload: dict) -> Dict[str, str]:
    module = (payload.get("module") or "").strip()
    if not module:
        raise AgentConfigError("module is required")
    env = {"MODULE": module}
    env.update(_auto_push_env(payload))
    if payload.get("start_from_step", 1) > 1:
        env["START_FROM_STEP"] = str(payload["start_from_step"])
    return env


# ── test-healing-agent ────────────────────────────────────────────────────────

def _healing_env(payload: dict) -> Dict[str, str]:
    test = (payload.get("test") or payload.get("test_name") or "").strip()
    build_tag = (payload.get("build_tag") or "").strip()
    if not test and not build_tag:
        raise AgentConfigError(
            "either 'test' (standalone: run and fix one test) or 'build_tag' "
            "(pipeline: a handoff already in the queue) is required"
        )
    if test and build_tag:
        raise AgentConfigError("pass 'test' or 'build_tag', not both")

    env: Dict[str, str] = dict(_auto_push_env(payload))
    if test:
        env["TEST_NAME"] = test
        if payload.get("repair"):
            env["REPAIR"] = "true"
        if payload.get("force"):
            env["FORCE"] = "true"
    else:
        env["BUILD_TAG"] = build_tag
    return env


def _healing_label(payload: dict) -> str:
    return (payload.get("test") or payload.get("test_name")
            or payload.get("build_tag") or "queue")


AGENTS: Dict[str, AgentSpec] = {
    "test-authoring-agent": AgentSpec(
        name="test-authoring-agent",
        run_sh=AGENTS_DIR / "test-authoring-agent" / "run.sh",
        audit_dir=AGENTS_DIR / "test-authoring-agent" / "audit",
        queue_dir=AGENTS_DIR / "test-authoring-agent" / "queue",
        steps=AUTHORING_STEPS,
        session_prefix="create",
        build_env=_authoring_env,
        describe_run=lambda p: (p.get("module") or "run"),
        supports_resume=True,
        label_field="module",
    ),
    "test-healing-agent": AgentSpec(
        name="test-healing-agent",
        run_sh=AGENTS_DIR / "test-healing-agent" / "run.sh",
        audit_dir=AGENTS_DIR / "test-healing-agent" / "audit",
        queue_dir=AGENTS_DIR / "test-healing-agent" / "queue",
        steps=HEALING_STEPS,
        session_prefix="fix",
        build_env=_healing_env,
        describe_run=_healing_label,
        supports_resume=False,
        label_field="test",
    ),
}

DEFAULT_AGENT = "test-authoring-agent"


def get_agent(name: Optional[str]) -> AgentSpec:
    spec = AGENTS.get(name or DEFAULT_AGENT)
    if spec is None:
        raise AgentConfigError(
            f"unknown agent: {name!r} (known: {', '.join(sorted(AGENTS))})", status=404
        )
    return spec
