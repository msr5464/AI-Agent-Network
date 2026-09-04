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
# Step order comes from this list, not from the numeric filename prefix. Locate
# sits between reproduce and fix and keeps its own 01- prefix deliberately:
# renumbering 01-fix.json would break audit_reader's hardcoded reads and every
# session already archived, which is a migration bought for nothing.
HEALING_STEPS: List[Tuple[str, str, str]] = [
    ("reproduce", "00-reproduce.json", "Reproduce"),
    ("locate", "01-locate.json", "Locate"),
    ("fix", "01-fix.json", "Fix"),
    ("ship", "02-ship.json", "Ship"),
]


# The adaptation pipeline. Step 03 is keyed on the COMBINED summary file, not on
# either half: keying a shared slot on one half (as authoring does with
# 02-validate-web.json) means a run that only exercised the other half never
# completes the step, and the UI's chip stays on "running" until the process exits.
#
# The keys below are also `data-step` attributes on #adaptProgressSteps in
# AI-Test-Studio/frontend/customer/index.html, and the display names are mirrored
# in that file's STEP_LABELS — so a rename here is a two-repo change.
ADAPTATION_STEPS: List[Tuple[str, str, str]] = [
    ("parse_change", "01-parse-change.json", "Parse Change"),
    ("scope", "02-scope.json", "Scope"),
    ("explore", "03-explore.json", "Explore"),
    ("adapt", "04-adapt.json", "Adapt"),
    ("ship", "05-ship.json", "Ship"),
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
    # Which family of step files audit_reader should parse this agent's sessions
    # with. Deliberately required and deliberately not a callable: audit_reader
    # imports from this module, so it owns the functions and dispatches on this
    # discriminator. It used to infer the family as "authoring if this is the
    # default agent, else healing", which meant a third agent silently got
    # healing's parser and reported empty sessions. Having no default is the
    # point — a new agent cannot forget to answer this.
    summary_kind: str
    supports_resume: bool = False
    # Which request field names the run's headline label, for the UI.
    label_field: str = "module"
    # "txt" — a human-authored queue file the UI may create and edit
    # (test-authoring-agent's feature specs). "json" — a handoff written by
    # another agent, read-only over HTTP.
    queue_kind: str = "json"
    # Whether GET /agents/<a>/tests should enumerate the automation repo.
    uses_test_catalog: bool = False

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


def adapt_apply_default() -> bool:
    """Same story as AUTO_PUSH, for the adaptation agent's apply/propose switch.

    `_adaptation_env` exports ADAPT_APPLY whenever the field is present, and a
    caller-exported var beats config/.env, so a checkbox that always renders
    unticked silently turns an admin's ADAPT_APPLY=true into propose-only.
    """
    return os.environ.get("ADAPT_APPLY", "false").strip().lower() == "true"


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
        summary_kind="authoring",
        queue_kind="txt",
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
        summary_kind="healing",
        uses_test_catalog=True,
        session_prefix="fix",
        build_env=_healing_env,
        describe_run=_healing_label,
        supports_resume=False,
        label_field="test",
    ),
}

def _adaptation_env(payload: dict) -> Dict[str, str]:
    module = (payload.get("module") or "").strip()
    if not module:
        raise AgentConfigError("module is required")
    env = {"MODULE": module}
    env.update(_auto_push_env(payload))
    if payload.get("start_from_step", 1) > 1:
        env["START_FROM_STEP"] = str(payload["start_from_step"])
    # Exploration is the expensive half, so "look but do not touch" is a
    # first-class request rather than a debug flag.
    if payload.get("explore_only"):
        env["EXPLORE_ONLY"] = "true"
    if payload.get("apply") is not None:
        env["ADAPT_APPLY"] = "true" if payload["apply"] else "false"
    return env


AGENTS["test-adaptation-agent"] = AgentSpec(
    name="test-adaptation-agent",
    run_sh=AGENTS_DIR / "test-adaptation-agent" / "run.sh",
    audit_dir=AGENTS_DIR / "test-adaptation-agent" / "audit",
    queue_dir=AGENTS_DIR / "test-adaptation-agent" / "queue",
    steps=ADAPTATION_STEPS,
    summary_kind="adaptation",
    session_prefix="adapt",
    build_env=_adaptation_env,
    describe_run=lambda p: (p.get("module") or "run"),
    supports_resume=True,
    label_field="module",
    queue_kind="txt",
    uses_test_catalog=True,
)

DEFAULT_AGENT = "test-authoring-agent"


def get_agent(name: Optional[str]) -> AgentSpec:
    spec = AGENTS.get(name or DEFAULT_AGENT)
    if spec is None:
        raise AgentConfigError(
            f"unknown agent: {name!r} (known: {', '.join(sorted(AGENTS))})", status=404
        )
    return spec
