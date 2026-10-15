"""Lessons people taught the agents, put in front of the step they are for.

A lesson is one sentence a reviewer's corrections have shown an agent needs, with
the scope it holds in: which agent and step, and optionally which automation repo,
framework, module, page object or kind of failure. They live in
`config/lessons/<agent>.yaml`. The learning-agent proposes them as a PR; a lesson
is active once a person has merged it, and stays editable by hand.

This module is the only reader. Three rules shape it:

  * **A lesson adds; it never relaxes.** It may add a constraint, a preference or
    a fact. One that reads as loosening a check, an assertion or a guard is refused
    at load, whoever wrote it; the guards are code and a prompt cannot move them
    anyway, but a model told to "skip the check" will try.
  * **Only what fits, not everything.** The lessons whose scope matches this step
    and this run, most specific first, within LESSONS_MAX_CHARS. Concatenating
    every lesson into every prompt is the failure mode the literature on agent
    memory keeps reporting.
  * **Every use is recorded.** `00-lessons.json` in the session says which lessons
    a step was given, so whether a lesson helps can be measured against the
    verdicts its runs later receive. LESSONS_MODE=shadow records without injecting.

Lesson text that reaches a prompt is generic: no session ids, PR numbers or links.
Those belong in the lesson's evidence, which never reaches a prompt.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
LESSONS_DIR = REPO_ROOT / "config" / "lessons"

MODES = ("off", "shadow", "on")
STATUSES = ("active", "retired")
# Only `lesson` is put in a prompt. A `convention` belongs in the automation repo's
# CLAUDE.md (every agent reads it already); a `guard-candidate` is a request for a
# code check, which a prompt cannot be.
LEVERS = ("lesson", "convention", "guard-candidate")
SCOPE_KEYS = ("repo", "framework", "module", "page", "failure")

# The steps each agent injects lessons at — the run_step keys in its run.sh.
INJECTION_STEPS: Dict[str, Tuple[str, ...]] = {
    "test-authoring-agent": ("parse", "generate", "run_and_fix"),
    "test-healing-agent": ("fix",),
    "test-adaptation-agent": ("explore", "adapt"),
    "test-triaging-agent": ("classify", "review"),
    # The Studio's Requirements → Tests: it fetches these from the agent server
    # (GET /lessons/test-design-agent) and puts them in its generation prompts.
    "test-design-agent": ("generate",),
}

MAX_STATEMENT_CHARS = 400
_ID = re.compile(r"^L-[a-z]+-\d{3,}$")
# Text that only belongs in evidence: a session id, a PR or issue number, a link.
_SPECIFIC = re.compile(r"\b\d{8}-\d{6}\b|(?<!\w)#\d+\b|https?://", re.I)
# Loosening a check. Allowed when negated ("never weaken an assertion").
_RELAX = re.compile(
    r"\b(weaken|relax|loosen|remove|delete|drop|skip|bypass|disable|ignore|comment out)\w*\b"
    r"[^.;]{0,40}\b(assert\w*|check\w*|guard\w*|verification\w*|validation\w*|rule\w*|test\w*)",
    re.I)
_NEGATION = re.compile(r"\b(never|not|no|don't|do not|must not|mustn't|avoid)\b[^.;]{0,30}$", re.I)


def mode() -> str:
    value = (os.environ.get("LESSONS_MODE") or "").strip().lower()
    return value if value in MODES else "off"


def _max_chars() -> int:
    try:
        return max(0, int(os.environ.get("LESSONS_MAX_CHARS") or 0))
    except ValueError:
        return 0


def relaxes(statement: str) -> bool:
    """Whether a statement reads as loosening a check (and is not negated)."""
    for match in _RELAX.finditer(statement or ""):
        before = statement[:match.start()]
        if not _NEGATION.search(before):
            return True
    return False


def problems(lesson: Dict, agent: str) -> List[str]:
    """Why a lesson cannot be used, or [] when it can."""
    found: List[str] = []
    if not _ID.match(str(lesson.get("id") or "")):
        found.append("id must look like L-<agent word>-NNN")
    if lesson.get("status") not in STATUSES:
        found.append(f"status must be one of {', '.join(STATUSES)}")
    if lesson.get("lever") not in LEVERS:
        found.append(f"lever must be one of {', '.join(LEVERS)}")
    statement = str(lesson.get("statement") or "").strip()
    if not statement:
        found.append("statement is empty")
    elif len(statement) > MAX_STATEMENT_CHARS:
        found.append(f"statement is longer than {MAX_STATEMENT_CHARS} characters")
    if _SPECIFIC.search(statement):
        found.append("statement names a session, PR or link; keep those in evidence")
    if relaxes(statement):
        found.append("statement reads as loosening a check; a lesson may only add")
    scope = lesson.get("scope") or {}
    if not isinstance(scope, dict):
        found.append("scope must be a mapping")
        scope = {}
    known = INJECTION_STEPS.get(agent, ())
    unknown_steps = [s for s in (scope.get("steps") or []) if s not in known]
    if unknown_steps:
        found.append(f"unknown step(s) {', '.join(unknown_steps)} for {agent} "
                     f"(lessons go to: {', '.join(known) or 'none'})")
    unknown_keys = [k for k in scope if k not in SCOPE_KEYS + ("steps",)]
    if unknown_keys:
        found.append(f"unknown scope key(s): {', '.join(unknown_keys)}")
    return found


def lessons_dir() -> Path:
    override = (os.environ.get("LESSONS_DIR") or "").strip()
    return Path(override) if override else LESSONS_DIR


def path_for(agent: str, root: Optional[Path] = None) -> Path:
    return Path(root or lessons_dir()) / f"{agent}.yaml"


def read_file(agent: str, root: Optional[Path] = None) -> List[Dict]:
    """Every lesson in the agent's file, valid or not, in file order."""
    import yaml
    path = path_for(agent, root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    items = data.get("lessons") if isinstance(data, dict) else None
    return [dict(item) for item in (items or []) if isinstance(item, dict)]


def load(agent: str, root: Optional[Path] = None) -> Tuple[List[Dict], List[Tuple[str, str]]]:
    """(usable active lessons, [(id, why refused)])."""
    usable: List[Dict] = []
    refused: List[Tuple[str, str]] = []
    for lesson in read_file(agent, root):
        why = problems(lesson, agent)
        if why:
            refused.append((str(lesson.get("id") or "?"), "; ".join(why)))
        elif lesson["status"] == "active" and lesson["lever"] == "lesson":
            usable.append(lesson)
    return usable, refused


def _matches(lesson: Dict, step: str, context: Dict[str, str]) -> bool:
    scope = lesson.get("scope") or {}
    steps = scope.get("steps") or []
    if steps and step not in steps:
        return False
    for key in SCOPE_KEYS:
        wanted = str(scope.get(key) or "").strip().lower()
        if wanted and wanted != str(context.get(key) or "").strip().lower():
            return False
    return True


def _specificity(lesson: Dict) -> int:
    scope = lesson.get("scope") or {}
    return sum(1 for key in SCOPE_KEYS if scope.get(key)) + (1 if scope.get("steps") else 0)


def context_from_env(**known: Optional[str]) -> Dict[str, str]:
    """The run's scope as the environment states it, with what the caller knows on top."""
    found = {"repo": os.environ.get("GITHUB_REPO_AUTOMATION", ""),
             "framework": os.environ.get("AUTOMATION_FRAMEWORK", ""),
             "module": os.environ.get("MODULE", "")}
    found.update({k: v for k, v in known.items() if v})
    return found


def select(agent: str, step: str, context: Optional[Dict[str, str]] = None,
           root: Optional[Path] = None, max_chars: Optional[int] = None) -> List[Dict]:
    """The active lessons for this step and run, most specific first, within the cap."""
    usable, _refused = load(agent, root)
    context = context or {}
    fitting = [l for l in usable if _matches(l, step, context)]
    fitting.sort(key=lambda l: (-_specificity(l), -len(l.get("evidence") or []), l["id"]))
    cap = _max_chars() if max_chars is None else max_chars
    chosen, used = [], 0
    for lesson in fitting:
        cost = len(lesson["statement"]) + len(lesson["id"]) + 8
        if cap and used + cost > cap:
            continue
        chosen.append(lesson)
        used += cost
    return chosen


def render(lessons: Iterable[Dict]) -> str:
    lines = [f"- [{l['id']}] {' '.join(str(l['statement']).split())}" for l in lessons]
    if not lines:
        return ""
    return ("\n\n## Lessons from past reviews\n"
            "People reviewing this agent's earlier work corrected what follows. Apply "
            "each where it fits this task. None of them overrides an instruction or a "
            "rule above.\n" + "\n".join(lines) + "\n")


def _record(audit_dir: Optional[str], step: str, current: str, ids: List[str],
            refused: List[Tuple[str, str]]) -> None:
    if not audit_dir:
        return
    path = Path(audit_dir) / "00-lessons.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    steps = data.setdefault("steps", {})
    # A step may ask more than once (healing, once per broken locator), with a
    # different page or failure each time: the session was given all of them.
    before = (steps.get(step) or {}).get("ids") or []
    steps[step] = {"mode": current, "ids": sorted(set(before) | set(ids)),
                   "refused": [{"id": i, "why": w} for i, w in refused]}
    data["ids"] = sorted({i for s in steps.values() for i in s.get("ids", [])})
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


_ASKED: Dict[tuple, str] = {}


def block(agent: str, step: Optional[str] = None, log=None, **context: Optional[str]) -> str:
    """The prompt text for this step's lessons ("" when off, shadow, or none fit). Never raises.

    `step` defaults to the STEP_KEY run_step exports. `context` adds what the
    caller knows beyond the environment (page, failure). The choice is recorded in
    the session's 00-lessons.json and, through `log`, as one console line.
    """
    try:
        current = mode()
        if current == "off":
            return ""
        step = step or os.environ.get("STEP_KEY", "")
        scope = context_from_env(**context)
        key = (agent, step, current, tuple(sorted(scope.items())))
        if key in _ASKED:          # a loop asking again: same answer, said once
            return _ASKED[key]
        _usable, refused = load(agent)
        chosen = select(agent, step, scope)
        ids = [l["id"] for l in chosen]
        _record(os.environ.get("AUDIT_DIR"), step, current, ids, refused)
        if log:
            if ids:
                log(f"Lessons {'applied' if current == 'on' else 'matched (shadow, not applied)'}: "
                    f"{', '.join(ids)}")
            for lesson_id, why in refused:
                log(f"WARNING: lesson {lesson_id} refused — {why}")
        _ASKED[key] = render(chosen) if current == "on" else ""
        return _ASKED[key]
    except Exception as exc:
        if log:
            log(f"WARNING: lessons not loaded ({exc})")
        return ""


def applied_ids(audit_dir) -> List[str]:
    """Which lessons a session was given, from its 00-lessons.json."""
    try:
        data = json.loads((Path(audit_dir) / "00-lessons.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    steps = data.get("steps") or {}
    return sorted({i for s in steps.values() if s.get("mode") == "on" for i in s.get("ids", [])})


_FILE_HEADER = """# Lessons for {agent}, learned from people's reviews of its work.
#
# Proposed by the learning-agent as a PR (agents/learning-agent); a lesson is
# active once that PR is merged. Edit, retire or add by hand at any time.
# Read only by shared/lessons.py, which puts the active ones whose scope fits in
# front of the step they are for (LESSONS_MODE in config/.env).
#
#   - id: L-{word}-001
#     status: active              # active | retired
#     lever: lesson               # lesson (prompt) | convention | guard-candidate
#     statement: >-
#       One sentence: what to do next time. Generic: no session ids, PR numbers or links.
#     scope:
#       steps: [{steps}]          # empty = all of: {steps}
#       repo: ""                  # optional: only for this automation repo
#       framework: ""             # optional: playwright | selenium
#       module: ""                # optional
#       page: ""                  # optional: a page object
#       failure: ""               # optional: a diagnosis verdict, e.g. LOCATOR_STALE
#     evidence: []                # feedback event ids (qa_agents_server/storage/feedback.jsonl)
#     added: "YYYY-MM-DD"
#     review_by: "YYYY-MM-DD"     # look again by then: does it still help?
"""


def file_text(agent: str, items: List[Dict]) -> str:
    """The whole lessons file for an agent: the explanatory header, then the list."""
    import yaml
    word = agent.replace("test-", "").replace("-agent", "")
    steps = ", ".join(INJECTION_STEPS.get(agent, ()))
    body = yaml.safe_dump({"lessons": items}, sort_keys=False, allow_unicode=True,
                          width=88) if items else "lessons: []\n"
    return _FILE_HEADER.format(agent=agent, word=word, steps=steps) + body


def next_id(agent: str, existing: Iterable[str]) -> str:
    word = agent.replace("test-", "").replace("-agent", "").split("-")[0]
    numbers = [int(m.group(1)) for i in existing
               if (m := re.match(rf"^L-{word}-(\d+)$", str(i)))]
    return f"L-{word}-{(max(numbers) + 1 if numbers else 1):03d}"


def review_date(days: int = 90) -> str:
    return date.fromordinal(date.today().toordinal() + days).isoformat()


def outcomes(agents_dir: Optional[Path] = None, store: Optional[Path] = None) -> Dict[str, Dict]:
    """How each lesson's runs were received: the measure of whether it helps.

    Per lesson id: the sessions it was put in front of (LESSONS_MODE=on, from each
    session's 00-lessons.json) and, for each, the newest verdict a person gave it
    and whether a person corrected what it produced. A run nobody has judged yet
    counts as unreviewed, never as accepted.
    """
    from shared import feedback
    folder = Path(agents_dir) if agents_dir else REPO_ROOT / "agents"
    events = feedback.read(store)
    corrected = {(e.get("agent"), e.get("session")) for e in events
                 if e.get("kind") in ("human_commit", "draft_edit") and e.get("session")}
    latest: Dict[Tuple[str, str], str] = {}
    for e in events:
        if e.get("kind") == "verdict" and e.get("verdict") and e.get("session"):
            latest[(e.get("agent"), e["session"])] = e["verdict"]
    found: Dict[str, Dict] = {}
    for record in folder.glob("*/audit/*/00-lessons.json"):
        session_dir = record.parent
        agent, session = session_dir.parent.parent.name, session_dir.name
        verdict = latest.get((agent, session), "")
        for lesson_id in applied_ids(session_dir):
            row = found.setdefault(lesson_id, {"agent": agent, "sessions": [], "accept": 0,
                                               "edited": 0, "reject": 0, "unreviewed": 0,
                                               "corrected": []})
            row["sessions"].append(session)
            row[verdict or "unreviewed"] = row.get(verdict or "unreviewed", 0) + 1
            if verdict in ("edited", "reject") or (agent, session) in corrected:
                row["corrected"].append(session)
    return found


def record_extra(audit_dir: Optional[str], step: str, key: str, values: List[str]) -> None:
    """Add `values` under steps[step][key] in the session's 00-lessons.json (precedents)."""
    if not audit_dir or not values:
        return
    path = Path(audit_dir) / "00-lessons.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entry = data.setdefault("steps", {}).setdefault(step, {"mode": mode(), "ids": []})
        entry[key] = sorted(set(entry.get(key) or []) | set(values))
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
