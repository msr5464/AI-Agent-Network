#!/usr/bin/env python3
"""
learning-agent / 01 Collect

Bring the feedback store up to date: what reviewers did on the agents' PRs
(scripts/collect_feedback.py) and what people replied to the agents' Slack
messages (shared/slack_feedback.py). Everything else in the store was recorded as
it happened, by the agents, the server and the `make` commands.

Never fatal. A repo that is not configured, a `gh` that is not signed in, Slack
without the history scope: each is reported, and the run goes on with what the
store already holds.

Writes: 01-collect.json, 01-collect.md
"""

import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from shared import feedback, slack_feedback  # noqa: E402
from shared.log import log as _log  # noqa: E402


def log(msg): _log("collect", msg)


AUDIT_DIR = Path(os.environ["AUDIT_DIR"])


def _collector():
    spec = importlib.util.spec_from_file_location(
        "collect_feedback", REPO_ROOT / "scripts" / "collect_feedback.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def from_github() -> dict:
    org = (os.environ.get("GITHUB_ORG") or "").strip()
    name = (os.environ.get("GITHUB_REPO_AUTOMATION") or "").strip()
    if not (org and name):
        return {"skipped": "GITHUB_ORG / GITHUB_REPO_AUTOMATION not set"}
    collector = _collector()
    try:
        result = collector.collect(collector.GitHub(f"{org}/{name}"), 1000, 14, log=log)
    except Exception as exc:          # gh missing, not signed in, network
        return {"skipped": f"GitHub not read: {exc}"}
    new = feedback.append(result["events"])
    flagged = collector.record_diagnosis_feedback(new, result["prs"])
    return {"repo": f"{org}/{name}", "prs": len(result["prs"]), "new": new,
            "errors": result.get("errors", []), "not_followed": result.get("not_followed", []),
            "wrong_diagnoses": flagged}


def main():
    github = from_github()
    if github.get("skipped"):
        log(f"GitHub: {github['skipped']}")
    slack = slack_feedback.collect(REPO_ROOT / "agents", log=log)
    new = (github.get("new") or []) + (slack.get("new") or [])
    store = feedback.read()
    kinds = Counter(e["kind"] for e in new)
    log(f"{len(new)} new event(s)"
        + (f": {', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}" if kinds else "")
        + f"; the store holds {len(store)}")
    result = {
        "github": {k: v for k, v in github.items() if k != "new"},
        "slack": {k: v for k, v in slack.items() if k != "new"},
        "new_events": len(new),
        "new_by_kind": dict(kinds),
        "store_events": len(store),
    }
    (AUDIT_DIR / "01-collect.json").write_text(json.dumps(result, indent=2))
    lines = ["# Collect", "",
             f"- GitHub: {github.get('skipped') or str(github.get('prs', 0)) + ' agent PR(s) read in ' + github['repo']}",
             f"- Slack: {slack.get('skipped') or str(slack.get('threads', 0)) + ' thread(s) read'}",
             f"- New events: {len(new)}" + (f" ({', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))})" if kinds else ""),
             f"- Events in the store: {len(store)}"]
    for problem in github.get("errors") or []:
        lines.append(f"- Not read: {problem}")
    (AUDIT_DIR / "01-collect.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
