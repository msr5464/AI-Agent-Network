"""Fixes people accepted, shown again when a similar failure comes up.

A lesson is a rule distilled from many corrections; a precedent is one accepted
piece of work, shown as an example. Both are levers of the learning loop
(docs/ARCHITECTURE.md, "Human feedback"), and both reach a prompt only under
LESSONS_MODE=on.

Only work a person explicitly accepted counts: a run whose newest verdict
(shared/feedback.py) is "accept", or "edited" — accepted after their changes, in
which case the changes they made are the example, since that is what they wanted.
A merged PR alone, or a run nobody judged, is never a precedent.

  * healing: the verified locator fix (its diff), for the same page object, else
    the same diagnosis verdict;
  * authoring: what the passing fix attempt found wrong, for the same module.

Nothing here is shown for the run it came from.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from shared import feedback, lessons

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
MAX_EXAMPLE_CHARS = 1200
LIMIT = 2


def _accepted(store=None) -> Dict[tuple, str]:
    found: Dict[tuple, str] = {}
    for event in feedback.read(store):
        if event.get("kind") == "verdict" and event.get("verdict") and event.get("session"):
            found[(event.get("agent"), event["session"])] = event["verdict"]
    return {k: v for k, v in found.items() if v in ("accept", "edited")}


def _corrections(store=None) -> Dict[tuple, str]:
    """A person's own diff on each run they corrected, joined."""
    found: Dict[tuple, List[str]] = {}
    for event in feedback.read(store):
        if event.get("kind") in ("human_commit", "draft_edit") and event.get("diff"):
            found.setdefault((event.get("agent"), event.get("session")), []).append(event["diff"])
    return {k: "\n".join(v) for k, v in found.items()}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build(agent: str, agents_dir: Optional[Path] = None, store=None) -> List[Dict]:
    """Every precedent for `agent`, newest first."""
    folder = Path(agents_dir) if agents_dir else AGENTS_DIR
    accepted = _accepted(store)
    corrected = _corrections(store)
    found: List[Dict] = []
    for (who, session), verdict in sorted(accepted.items(), key=lambda kv: kv[0][1], reverse=True):
        if who != agent:
            continue
        audit = folder / agent / "audit" / session
        changed = corrected.get((agent, session), "")[:MAX_EXAMPLE_CHARS] if verdict == "edited" else ""
        if agent == "test-healing-agent":
            for fix in (_load(audit / "01-fix.json") or {}).get("fixes") or []:
                if fix.get("status") != "success":
                    continue
                found.append({
                    "session": session, "verdict": verdict,
                    "page": Path(str(fix.get("target_file") or "")).stem,
                    "failure": ((fix.get("diagnosis") or {}).get("verdict") or ""),
                    "about": f"{fix.get('failed_selector') or ''} — {fix.get('root_cause') or ''}".strip(" —"),
                    "example": (fix.get("fix_diff") or "")[:MAX_EXAMPLE_CHARS],
                    "people_changed": changed})
        elif agent == "test-authoring-agent":
            history = _load(audit / ".fix-history.json") or []
            passed = [h for h in history if isinstance(h, dict) and h.get("outcome") == "passed"]
            if not passed:
                continue
            module = session.split("-create-", 1)[1] if "-create-" in session else ""
            last = passed[-1]
            found.append({
                "session": session, "verdict": verdict, "module": module,
                "about": f"{last.get('targeted') or ''}".strip(),
                "example": str(last.get("root_cause") or "")[:MAX_EXAMPLE_CHARS],
                "people_changed": changed})
    return found


def select(agent: str, current_session: str = "", page: str = "", failure: str = "",
           module: str = "", agents_dir: Optional[Path] = None, store=None) -> List[Dict]:
    """The precedents most like this failure: same page (or module), then same verdict."""
    ranked = []
    for item in build(agent, agents_dir, store):
        if item["session"] == current_session:
            continue
        score = 0
        if page and item.get("page") == page:
            score += 2
        if failure and item.get("failure") == failure:
            score += 1
        if module and item.get("module") == module:
            score += 2
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: -pair[0])
    return [item for _score, item in ranked[:LIMIT]]


def render(items: List[Dict]) -> str:
    if not items:
        return ""
    parts = ["\n\n## Accepted fixes for similar failures\n"
             "People accepted these fixes for failures like this one. Use them as examples "
             "of what was wanted; they never override an instruction above.\n"]
    for item in items:
        where = item.get("page") or item.get("module") or "this area"
        parts.append(f"\n### In {where}" + (f" ({item['failure']})" if item.get("failure") else "")
                     + (f": {item['about']}" if item.get("about") else "") + "\n")
        if item.get("people_changed"):
            parts.append("The fix as people corrected it before accepting it:\n```diff\n"
                         + item["people_changed"] + "\n```\n")
        elif item.get("example"):
            fence = "```diff\n" if item["example"].lstrip().startswith(("---", "@@", "diff")) else "```\n"
            parts.append(fence + item["example"] + "\n```\n")
    return "".join(parts)


def block(agent: str, log: Optional[Callable[[str], None]] = None, **context: str) -> str:
    """Prompt text for this failure's precedents ("" unless LESSONS_MODE=on). Never raises."""
    try:
        current = lessons.mode()
        if current == "off":
            return ""
        session = os.environ.get("SESSION_ID", "")
        chosen = select(agent, session, page=context.get("page", ""),
                        failure=context.get("failure", ""),
                        module=context.get("module") or os.environ.get("MODULE", ""))
        sessions = [item["session"] for item in chosen]
        lessons.record_extra(os.environ.get("AUDIT_DIR"), os.environ.get("STEP_KEY", ""),
                             "precedents", sessions)
        if log and sessions:
            log(f"Precedents {'applied' if current == 'on' else 'matched (shadow, not applied)'}: "
                f"accepted fixes from {', '.join(sessions)}")
        return render(chosen) if current == "on" else ""
    except Exception as exc:
        if log:
            log(f"WARNING: precedents not loaded ({exc})")
        return ""
