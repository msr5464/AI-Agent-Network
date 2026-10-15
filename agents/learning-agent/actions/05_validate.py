#!/usr/bin/env python3
"""
learning-agent / 05 Validate

Two checks on the lessons step 04 accepted, and neither blocks one: a person
reviews the PR, and these are what they review with.

  * **Conflicts** (no model). A new or changed lesson that says nearly the same
    words as another active lesson for the same step, one of them negated, may
    contradict it. Flagged.
  * **Replay** (model; LEARNING_REPLAY_LIMIT lessons, two calls each, 0 = off).
    The call the lesson is for is in an evidence run's claude-*.log, word for
    word. It is sent again twice, as it was and with the lesson added, and each
    answer is measured against what the person actually changed: the share of
    the lines they added that the answer contains. A lesson that moves the answer
    toward the person's version is doing its job.

The replay is honest about its limits. It runs with no tools, from this repo
rather than the run's long-gone worktree, so a call that relied on reading files
answers from less than it had; both arms share that handicap, which is why the
comparison is between them and not against the original answer.

Reads:  04-propose.json, 03-classify.json, agents/<agent>/audit/<session>/claude-*.log
Writes: 05-validate.json
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import lessons  # noqa: E402
from shared.claude import call_claude_ex  # noqa: E402
from shared.log import log as _log  # noqa: E402
from lib import evidence  # noqa: E402


def log(msg): _log("validate", msg)


AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENTS_DIR = REPO_ROOT / "agents"
_NEGATED = re.compile(r"\b(never|not|no|don't|do not|avoid|instead of)\b", re.I)
_WORD = re.compile(r"[a-z][a-z0-9_-]{2,}")


def replay_limit() -> int:
    try:
        return max(0, int(os.environ.get("LEARNING_REPLAY_LIMIT") or 0))
    except ValueError:
        return 0


# ── Conflicts ─────────────────────────────────────────────────────────────────

def _words(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def conflicts(lesson: dict, others: list) -> list:
    """Active lessons for the same step that may say the opposite."""
    mine = set((lesson.get("scope") or {}).get("steps") or [])
    found = []
    for other in others:
        if other.get("id") == lesson.get("id") or other.get("status") != "active":
            continue
        theirs = set((other.get("scope") or {}).get("steps") or [])
        if mine and theirs and not mine & theirs:
            continue
        a, b = _words(lesson["statement"]), _words(other["statement"])
        overlap = len(a & b) / max(1, len(a | b))
        if overlap >= 0.4 and bool(_NEGATED.search(lesson["statement"])) != \
                bool(_NEGATED.search(other["statement"])):
            found.append(other["id"])
    return found


# ── Replay ────────────────────────────────────────────────────────────────────

def read_call(log_path: Path):
    """(step, argv) of the claude call a claude-*.log recorded, or None."""
    step, argv = "", None
    try:
        with log_path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("step: "):
                    step = line[6:].strip()
                elif line.startswith("command: "):
                    argv = shlex.split(line[9:].rstrip("\n"))
                    break
    except (OSError, ValueError):
        return None
    return (step, argv) if argv else None


def _option(argv: list, flag: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else ""


def pick_call(session_dir: Path, step: str, files: list):
    """The recorded call a lesson for `step` would have changed, in one evidence run.

    A log that names its step decides by that. An older one, written before logs
    named their step, is chosen by the files the person corrected: the call whose
    prompt names most of them. Browser-driving calls are never replayed.
    """
    best, best_score = None, 0
    names = [Path(f).name for f in files]
    for log_path in sorted(session_dir.glob("claude-*.log")):
        found = read_call(log_path)
        if not found:
            continue
        logged_step, argv = found
        if "-p" not in argv or "--mcp-config" in argv:
            continue
        if logged_step:
            if step and logged_step != step:
                continue
            score = 100
        else:
            prompt = _option(argv, "-p")
            score = sum(1 for n in names if n and n in prompt)
        if score > best_score:
            best, best_score = (log_path, argv), score
    return best


def recall(person_lines: list, answer: str) -> float:
    """Share of the lines a person added that an answer contains (whitespace-blind)."""
    if not person_lines:
        return 0.0
    flat = " ".join((answer or "").split())
    hits = sum(1 for line in person_lines if " ".join(line.split()) in flat)
    return round(hits / len(person_lines), 3)


def replay(lesson: dict, agent: str, events: dict):
    """Replay one lesson against the first evidence run that can be replayed."""
    step = ((lesson.get("scope") or {}).get("steps") or [""])[0]
    for event_id in lesson.get("evidence") or []:
        event = events.get(event_id) or {}
        person = evidence.added_lines(event.get("diff") or "")
        session = event.get("session")
        if not person or not session:
            continue
        session_dir = AGENTS_DIR / agent / "audit" / session
        chosen = pick_call(session_dir, step, evidence.files_of(event))
        if not chosen:
            continue
        log_path, argv = chosen
        prompt, model = _option(argv, "-p"), _option(argv, "--model")
        system = _option(argv, "--system-prompt-file")
        if system and not Path(system).is_file():
            system = ""
        effort = _option(argv, "--effort") or None
        answers = {}
        for arm, text in (("without", prompt), ("with", prompt + lessons.render([lesson]))):
            call = call_claude_ex(prompt=text, model=model, effort=effort, cwd=str(REPO_ROOT),
                                  system_prompt_file=system or None, tools="",
                                  timeout=900, log_dir=str(AUDIT_DIR / "replay"))
            if call.status != "ok":
                return {"lesson": lesson["id"], "session": session,
                        "result": f"replay call ended {call.status}"}
            answers[arm] = recall(person, call.stdout)
        moved = answers["with"] - answers["without"]
        return {"lesson": lesson["id"], "session": session, "log": log_path.name,
                "person_lines": len(person), "without": answers["without"],
                "with": answers["with"],
                "result": "moved toward the person's version" if moved > 0
                else "moved away from the person's version" if moved < 0 else "no change"}
    return {"lesson": lesson["id"], "result": "no evidence run with a replayable call "
                                              "and a person's added lines"}


def main():
    proposed = json.loads((AUDIT_DIR / "04-propose.json").read_text())
    events = {e["id"]: e for e in json.loads((AUDIT_DIR / "03-classify.json").read_text())["events"]}
    current = {agent: lessons.read_file(agent) for agent in lessons.INJECTION_STEPS}
    checked, budget = [], replay_limit()
    for decision in proposed.get("decisions") or []:
        lesson = decision.get("lesson") or {}
        if decision["status"] != "accepted" or decision["op"] == "retire" or not lesson:
            continue
        others = current.get(decision["agent"], [])
        row = {"lesson": lesson["id"], "agent": decision["agent"],
               "may_contradict": conflicts(lesson, others)}
        if row["may_contradict"]:
            log(f"  {lesson['id']} may contradict {', '.join(row['may_contradict'])}")
        if budget and lesson.get("lever") == "lesson":
            budget -= 1
            row["replay"] = replay(lesson, decision["agent"], events)
            log(f"  {lesson['id']} replay: {row['replay']['result']}")
        checked.append(row)
    if not checked:
        log("No accepted lesson to check")
    (AUDIT_DIR / "05-validate.json").write_text(json.dumps({"checked": checked}, indent=2))


if __name__ == "__main__":
    main()
