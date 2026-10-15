#!/usr/bin/env python3
"""
learning-agent / 02 Attribute

Pick the events that can teach something (lib/evidence.py) and join each to the
run it is about: the session's audit folder, how that run ended, and which lessons
it had already been given. Events already cited by a lesson, in any state, are
left out: they have taught what they can.

No model, no network.

Reads:  the feedback store, config/lessons/*.yaml, agents/<agent>/audit/<session>/
Writes: 02-attribute.json
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import feedback, lessons  # noqa: E402
from shared.log import log as _log  # noqa: E402
from lib import evidence  # noqa: E402


def log(msg): _log("attribute", msg)


AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENTS_DIR = REPO_ROOT / "agents"
# Newest first, at most this many: the prompt in step 04 has to hold them.
MAX_EVENTS = 150
MAX_DIFF = 2500


def cited() -> set:
    """Every event id any lesson already cites, active or retired."""
    ids = set()
    for agent in lessons.INJECTION_STEPS:
        for lesson in lessons.read_file(agent):
            ids.update(str(i) for i in lesson.get("evidence") or [])
    return ids


def context(event: dict) -> dict:
    """What the run behind an event left on disk, when it is still there."""
    session = event.get("session") or ""
    agent = event.get("agent") or ""
    audit = AGENTS_DIR / agent / "audit" / session if session else None
    if not audit or not audit.is_dir():
        return {"audit": None}
    return {"audit": str(audit.relative_to(REPO_ROOT)),
            "ended": feedback.ended(audit),
            "lessons_applied": lessons.applied_ids(audit)}


def main():
    store = feedback.read()
    already = cited()
    picked, skipped = [], Counter()
    for event in store:
        if event["id"] in already:
            skipped["already a lesson's evidence"] += 1
        elif evidence.is_evidence(event):
            picked.append({**event, "role": "evidence"})
        elif evidence.is_context(event):
            picked.append({**event, "role": "context"})
        else:
            skipped[f"not learnable ({event.get('kind')})"] += 1
    picked = sorted(picked, key=lambda e: e.get("recorded_at") or "", reverse=True)[:MAX_EVENTS]
    for event in picked:
        event["context"] = context(event)
        if len(event.get("diff") or "") > MAX_DIFF:
            event["diff"] = event["diff"][:MAX_DIFF] + "\n… (cut)"
    roles = Counter(e["role"] for e in picked)
    log(f"{roles['evidence']} evidence and {roles['context']} context event(s) of "
        f"{len(store)} in the store"
        + (f"; left out: {', '.join(f'{v} {k}' for k, v in sorted(skipped.items()))}" if skipped else ""))
    (AUDIT_DIR / "02-attribute.json").write_text(json.dumps({
        "events": picked,
        "by_agent": dict(Counter(e.get("agent") for e in picked)),
        "left_out": dict(skipped),
    }, indent=2))


if __name__ == "__main__":
    main()
