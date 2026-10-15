#!/usr/bin/env python3
"""
learning-agent / 03 Classify

Say what each event is about before any model reads it: tags from the diff a person
made and the reasons they gave (locator, assertion, wait, logStep, test data,
structure, reason:<code>, …) and where a lesson from it would apply (step, module,
page object). Then group the evidence by agent, step and tag, which is the shape
step 04 proposes lessons from.

No model, no network.

Reads:  02-attribute.json
Writes: 03-classify.json
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.log import log as _log  # noqa: E402
from lib import evidence  # noqa: E402


def log(msg): _log("classify", msg)


AUDIT_DIR = Path(os.environ["AUDIT_DIR"])


def main():
    events = json.loads((AUDIT_DIR / "02-attribute.json").read_text())["events"]
    groups = defaultdict(list)
    for event in events:
        event["tags"] = evidence.tags(event)
        event["scope_guess"] = evidence.scope_guess(event)
        if event["role"] != "evidence":
            continue
        for tag in event["tags"] or ["untagged"]:
            groups[(event.get("agent"), event["scope_guess"]["step"], tag)].append(event["id"])
    ordered = sorted(({"agent": a, "step": s, "tag": t, "events": ids,
                       "independent": evidence.independent(
                           [e for e in events if e["id"] in ids])}
                      for (a, s, t), ids in groups.items()),
                     key=lambda g: (-g["independent"], g["agent"] or "", g["tag"]))
    strong = [g for g in ordered if g["independent"] >= 2]
    log(f"{len(ordered)} group(s) of evidence, {len(strong)} backed by two or more "
        f"separate pieces of work")
    (AUDIT_DIR / "03-classify.json").write_text(json.dumps(
        {"events": events, "groups": ordered}, indent=2))


if __name__ == "__main__":
    main()
