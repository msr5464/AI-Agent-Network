"""Seed each agent's queue with the documented examples on first boot.

The queue directories are git-ignored runtime state, so a fresh clone starts with
empty inboxes and the GUI's queue view has nothing in it. This copies
`docs/examples/queue/<agent>/` into each agent's queue once, so there is
something to read and run without hand-writing an input first.

Once, deliberately. A restart must never resurrect work a user has already dealt
with, so three rules apply per file:

  - a file already in the queue is left alone — it may be an edit in progress
  - a file whose name is already in `processed/` is not re-created — that run
    has already happened
  - after an agent is seeded it gets a marker file and is skipped from then on,
    so an example the user deletes stays deleted

Set QA_SEED_EXAMPLES=false to skip seeding entirely.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from qa_agents_server.agents import AGENTS, AgentSpec
from qa_agents_server.paths import REPO_ROOT

EXAMPLES_DIR: Path = REPO_ROOT / "docs" / "examples" / "queue"

# Lives inside the queue directory, which is git-ignored, so it is per-checkout.
# Clearing the queue directory also clears this, which is the intended escape
# hatch: an empty inbox gets the examples back.
MARKER_NAME = ".examples-seeded"


def enabled() -> bool:
    return os.getenv("QA_SEED_EXAMPLES", "true").strip().lower() not in (
        "false", "0", "no", "off")


def seed_agent(spec: AgentSpec) -> List[str]:
    """Copy this agent's examples into its queue. Returns the filenames copied."""
    source = EXAMPLES_DIR / spec.name
    if not source.is_dir():
        return []

    queue = spec.queue_dir
    if (queue / MARKER_NAME).exists():
        return []

    # A txt queue holds human-authored specs; a json queue holds handoffs written
    # by another agent. Both are listed by the UI, and run.sh globs one extension
    # per agent — copying the other kind in would be invisible at best.
    suffix = ".txt" if spec.queue_kind == "txt" else ".json"
    processed = queue / "processed"

    queue.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for path in sorted(source.glob(f"*{suffix}")):
        if (queue / path.name).exists() or (processed / path.name).exists():
            continue
        # Write to a dot-prefixed temp name and rename, for the same reason
        # feature_files.py does: run.sh globs this directory to pick its next
        # item and must never see a half-written file.
        tmp = queue / f".{path.name}.seeding"
        tmp.write_bytes(path.read_bytes())
        tmp.replace(queue / path.name)
        copied.append(path.name)

    (queue / MARKER_NAME).write_text(
        f"Seeded from {EXAMPLES_DIR / spec.name} at "
        f"{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}.\n"
        f"Delete this file to let the server seed the examples again.\n")
    return copied


def seed_all(log=print) -> Dict[str, List[str]]:
    """Seed every registered agent. Never raises — an empty inbox is cosmetic,
    and must not be the reason the server fails to boot."""
    if not enabled():
        return {}

    seeded: Dict[str, List[str]] = {}
    for spec in AGENTS.values():
        try:
            copied = seed_agent(spec)
        except OSError as exc:
            log(f"  could not seed {spec.name}: {exc}")
            continue
        if copied:
            seeded[spec.name] = copied
            log(f"  {spec.name}: {len(copied)} example(s) — {', '.join(copied)}")
    return seeded
