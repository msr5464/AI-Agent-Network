#!/usr/bin/env python3
"""
learning-agent / 04 Propose

The one model step. The model reads the classified evidence, the current lessons
and how each lesson's runs were received, and proposes changes one at a time:
add, update or retire (config/prompts/learn.md). Python then decides each one, and
the model's say-so counts for nothing:

  * every cited event must exist, be evidence (not context) and be about that agent;
  * a new lesson needs a `/lesson` or two independent pieces of work
    (lib/evidence.enough);
  * the statement must pass shared/lessons.problems: generic, one sentence, and
    never loosening a check;
  * steps must be ones that agent takes lessons at;
  * a statement nearly identical to an active lesson is a duplicate.

Guard candidates are found here too, without the model: an active lesson whose
runs people keep correcting for the same thing is not being held by the prompt,
and needs a code check instead (the guards in shared/edit_guards.py are where
those live).

No evidence and no lesson to judge: no model call at all.

Reads:  03-classify.json, config/lessons/*.yaml, the feedback store, audit 00-lessons.json files
Writes: 04-propose.json, proposed/config/lessons/<agent>.yaml for each agent that changes
"""

import difflib
import json
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import feedback, lessons  # noqa: E402
from shared.claude import call_claude_ex  # noqa: E402
from shared.json_extract import extract_json  # noqa: E402
from shared.log import log as _log  # noqa: E402
from lib import evidence  # noqa: E402


def log(msg): _log("propose", msg)


AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
MODEL = os.environ.get("LEARNING_MODEL", "")
EFFORT = os.environ.get("LEARNING_EFFORT", "") or None
PROMPT_FILE = REPO_ROOT / "config" / "prompts" / "learn.md"
DUPLICATE_RATIO = 0.85
MAX_TEXT, MAX_DIFF = 600, 1500


def guard_after() -> int:
    try:
        return max(1, int(os.environ.get("LEARNING_GUARD_AFTER") or 2))
    except ValueError:
        return 2


# ── Guard candidates ──────────────────────────────────────────────────────────

def guard_candidates(current: dict, outcome: dict, store: list) -> list:
    """Active lessons whose runs are still corrected for what the lesson is about."""
    by_id = {e["id"]: e for e in store}
    found = []
    for agent, items in current.items():
        for lesson in items:
            row = outcome.get(lesson.get("id"))
            if lesson.get("status") != "active" or not row:
                continue
            about = set()
            for event_id in lesson.get("evidence") or []:
                if event_id in by_id:
                    about.update(evidence.tags(by_id[event_id]))
            repeats = []
            for session in sorted(set(row["corrected"])):
                said = [e for e in store if e.get("session") == session
                        and e.get("agent") == agent and evidence.is_evidence(e)]
                if about & {t for e in said for t in evidence.tags(e)}:
                    repeats.append(session)
            if len(repeats) >= guard_after():
                found.append({"agent": agent, "id": lesson["id"], "statement": lesson["statement"],
                              "sessions": repeats, "about": sorted(about),
                              "why": f"given this lesson, {len(repeats)} run(s) were still "
                                     f"corrected for the same thing"})
    return found


# ── The model's proposals, decided ───────────────────────────────────────────

def _scope(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    scope = {k: str(raw.get(k) or "").strip() for k in lessons.SCOPE_KEYS if raw.get(k)}
    steps = [str(s) for s in raw.get("steps") or [] if s]
    if steps:
        scope["steps"] = steps
    return scope


def _duplicate(statement: str, items: list, skip_id: str = "") -> str:
    for other in items:
        if other.get("status") != "active" or other.get("id") == skip_id:
            continue
        ratio = difflib.SequenceMatcher(None, statement.lower(),
                                        str(other.get("statement", "")).lower()).ratio()
        if ratio >= DUPLICATE_RATIO:
            return other["id"]
    return ""


def _cited(ids, agent: str, by_id: dict):
    """(events, why not) for evidence ids the model cited for `agent`."""
    events = []
    for event_id in ids or []:
        event = by_id.get(str(event_id))
        if event is None:
            return [], f"cites {event_id}, which is not in the evidence"
        if event.get("role") != "evidence":
            return [], f"cites {event_id}, which is context, not evidence"
        if event.get("agent") != agent:
            return [], f"cites {event_id}, which is about {event.get('agent')}, not {agent}"
        events.append(event)
    return events, ""


def decide(operations: list, current: dict, by_id: dict, today: str = ""):
    """Apply the model's operations to copies of the lesson lists. Returns (lists, decisions)."""
    today = today or date.today().isoformat()
    lists = {agent: [dict(l) for l in items] for agent, items in current.items()}
    decisions = []
    for op in operations if isinstance(operations, list) else []:
        if not isinstance(op, dict):
            continue
        kind, agent = op.get("op"), op.get("agent")
        record = {"op": kind, "agent": agent, "why": op.get("why", ""), "status": "refused"}
        decisions.append(record)
        if agent not in lists:
            record["because"] = f"unknown agent {agent!r}"
            continue
        items = lists[agent]
        if kind == "add":
            cited, problem = _cited(op.get("evidence"), agent, by_id)
            problem = problem or evidence.enough(cited)
            lesson = {"id": lessons.next_id(agent, [l.get("id") for l in items]),
                      "status": "active", "lever": op.get("lever") or "lesson",
                      "statement": " ".join(str(op.get("statement") or "").split()),
                      "scope": _scope(op.get("scope")),
                      "evidence": [e["id"] for e in cited],
                      "added": today, "review_by": lessons.review_date()}
            counter = [str(i) for i in op.get("counter_evidence") or [] if str(i) in by_id]
            if counter:
                lesson["counter_evidence"] = counter
            if lesson["lever"] not in ("lesson", "convention"):
                problem = problem or "a new lesson's lever is lesson or convention"
            problem = problem or "; ".join(lessons.problems(lesson, agent))
            twin = _duplicate(lesson["statement"], items)
            problem = problem or (f"duplicate of {twin}" if twin else "")
            record.update(lesson=lesson)
            if problem:
                record["because"] = problem
                continue
            items.append(lesson)
            record["status"] = "accepted"
        elif kind in ("update", "retire"):
            target = next((l for l in items if l.get("id") == op.get("id")), None)
            if target is None:
                record["because"] = f"no lesson {op.get('id')!r} for {agent}"
                continue
            record["id"] = target["id"]
            if kind == "retire":
                if target.get("status") != "active" or not op.get("why"):
                    record["because"] = "only an active lesson, and only with a reason"
                    continue
                target.update(status="retired", retired=today, retired_because=op["why"])
                record.update(status="accepted", lesson=dict(target))
                continue
            changed = dict(target)
            if op.get("statement"):
                changed["statement"] = " ".join(str(op["statement"]).split())
            if isinstance(op.get("scope"), dict):
                changed["scope"] = _scope(op["scope"])
            added, problem = _cited(op.get("add_evidence"), agent, by_id)
            changed["evidence"] = list(dict.fromkeys(
                list(target.get("evidence") or []) + [e["id"] for e in added]))
            problem = problem or "; ".join(lessons.problems(changed, agent))
            twin = _duplicate(changed["statement"], items, skip_id=target["id"])
            problem = problem or (f"duplicate of {twin}" if twin else "")
            record.update(lesson=changed, before=dict(target))
            if problem:
                record["because"] = problem
                continue
            target.clear()
            target.update(changed)
            record["status"] = "accepted"
        else:
            record["because"] = f"unknown op {kind!r}"
    return lists, decisions


# ── The model call ────────────────────────────────────────────────────────────

def _condensed(event: dict) -> dict:
    artifact = event.get("artifact") or {}
    out = {k: event.get(k) for k in ("id", "kind", "agent", "role", "verdict", "reasons",
                                     "tags", "scope_guess", "at")}
    out["text"] = (event.get("text") or "")[:MAX_TEXT]
    if event.get("diff"):
        out["diff"] = event["diff"][:MAX_DIFF]
    out["where"] = {k: artifact.get(k) for k in ("pr", "files", "touches_agent_files", "test",
                                                 "overridden", "outcome", "edited_fields",
                                                 "agent_said", "person_said") if artifact.get(k)}
    out["lessons_applied"] = (event.get("context") or {}).get("lessons_applied") or []
    return out


def build_prompt(classified: dict, current: dict, outcome: dict) -> str:
    events = classified["events"]
    data = {
        "steps": {agent: list(steps) for agent, steps in lessons.INJECTION_STEPS.items()},
        "lessons": current,
        "evidence": [_condensed(e) for e in events if e["role"] == "evidence"],
        "context": [_condensed(e) for e in events if e["role"] == "context"],
        "groups": classified["groups"],
        "outcomes": {lid: {k: v for k, v in row.items() if k != "sessions"}
                     for lid, row in outcome.items()},
    }
    return PROMPT_FILE.read_text(encoding="utf-8") + "\n\n```json\n" + \
        json.dumps(data, indent=1, default=str) + "\n```\n"


def main():
    classified = json.loads((AUDIT_DIR / "03-classify.json").read_text())
    by_id = {e["id"]: e for e in classified["events"]}
    current = {agent: lessons.read_file(agent) for agent in lessons.INJECTION_STEPS}
    outcome = lessons.outcomes()
    store = feedback.read()
    guards = guard_candidates(current, outcome, store)
    for g in guards:
        log(f"Guard candidate: {g['id']} — {g['why']}")

    has_evidence = any(e["role"] == "evidence" for e in classified["events"])
    has_active = any(l.get("status") == "active" for items in current.values() for l in items)
    result = {"model_called": False, "decisions": [], "not_learned": [],
              "guard_candidates": guards, "outcomes": outcome, "changed_agents": []}
    if not has_evidence and not has_active:
        log("Nothing to learn from yet: no evidence and no active lesson to judge — "
            "no model call")
        (AUDIT_DIR / "04-propose.json").write_text(json.dumps(result, indent=2))
        return

    call = call_claude_ex(prompt=build_prompt(classified, current, outcome), model=MODEL,
                          effort=EFFORT, cwd=str(REPO_ROOT), timeout=900,
                          log_dir=str(AUDIT_DIR), tools="", setting_sources="project,local")
    result["model_called"] = True
    if call.status != "ok":
        log(f"ERROR: the model call ended {call.status}: {(call.stderr or '')[:200]}")
        (AUDIT_DIR / "04-propose.json").write_text(json.dumps(result, indent=2))
        sys.exit(1)
    answer = extract_json(call.stdout) or {}
    lists, decisions = decide(answer.get("operations") or [], current, by_id)
    result.update(decisions=decisions, not_learned=answer.get("not_learned") or [])
    for agent, items in lists.items():
        if items != current[agent]:
            path = AUDIT_DIR / "proposed" / "config" / "lessons" / f"{agent}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(lessons.file_text(agent, items))
            result["changed_agents"].append(agent)
    accepted = [d for d in decisions if d["status"] == "accepted"]
    for d in decisions:
        mark = "✓" if d["status"] == "accepted" else "✗"
        what = (d.get("lesson") or {}).get("statement", d.get("id", ""))
        log(f"  {mark} {d['op']} {d.get('agent')}: {what[:110]}"
            + ("" if d["status"] == "accepted" else f" — refused: {d.get('because')}"))
    log(f"{len(accepted)} of {len(decisions)} proposal(s) accepted")
    (AUDIT_DIR / "04-propose.json").write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
