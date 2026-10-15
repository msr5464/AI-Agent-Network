"""What people did with the agents' work, kept in one store.

Feedback reached the agents one way: someone noticed a problem, and a prompt, a
guard or the automation repo's CLAUDE.md was changed by hand. The places it could
have been read from automatically held nothing usable. No agent PR carried a
review or a comment. A closed PR more often meant a discarded trial run than a
rejection. And a person's commit on an agent's branch could not be told from the
agent's own, because the agents push with the same token, as the same GitHub user.

This module is the capture half of the learning loop
(docs/ARCHITECTURE.md, "Human feedback"):

  * the reviewer verdict block every agent PR carries, and how to read it back;
  * `/lesson` comments: a reviewer teaching the agent in one line;
  * which commits an agent wrote (the `Agent:` trailer; older subjects by prefix);
  * an append-only store, one event per line, deduplicated by id;
  * the signals a run itself can see: a person overriding its refusal with FORCE,
    and a person's edit to its input, or to a draft an agent wrote for them;
  * a person's verdict on a run, from the CLI or the Studio (`person_verdict`),
    and what test design generated and what a person then pushed to TestRail.

It records and nothing more. No event changes how an agent behaves: lessons are
distilled from these events later, and reach an agent only once a person has
approved them. Two rules keep the record honest:

  * **Not reviewed is not rejected.** An unticked verdict block records nothing,
    and a closed PR is an inferred outcome, never a verdict.
  * **Inferred is not explicit.** A revert, a closed PR, or a commit added after
    the agent's are kept with `explicit: False`, carrying what they say about
    themselves. On their own they are never evidence that the agent was wrong:
    the one revert of an agent's fix on record undid it on purpose, to stage a
    failure for testing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
_STORE = REPO_ROOT / "qa_agents_server" / "storage" / "feedback.jsonl"

AGENTS = ("test-authoring-agent", "test-triaging-agent",
          "test-healing-agent", "test-adaptation-agent")
# The Studio's Requirements → Tests feature, under the name Analytics gives it.
DESIGN_AGENT = "test-design-agent"

# A correction is evidence only with its diff, and a diff is the bulk of an event;
# past this it is cut, and the cut is marked.
MAX_DIFF_CHARS = 8000

# What a reviewer can say about a whole PR. "edited" is an acceptance that needed
# a person's changes first, and those changes are the most useful signal there is.
VERDICTS: Dict[str, str] = {
    "accept": "Accept as is",
    "edited": "Accept after my edits (push them to this branch)",
    "reject": "Reject",
}

# Why. A fixed list, so that reasons can be counted across agents and over time.
REASONS: Dict[str, str] = {
    "wrong-locator": "Wrong locator",
    "wrong-flow": "Wrong flow or page",
    "weak-assertion": "Weak or missing assertion",
    "convention": "Convention or style",
    "no-reuse": "Didn't reuse existing code",
    "test-data": "Test data",
    "flaky": "Flaky or timing",
    "product-bug": "Product bug misread",
    "wrong-scope": "Wrong scope",
    "other": "Other",
}

BLOCK_BEGIN = "<!-- qa-agent-network:verdict -->"
BLOCK_END = "<!-- /qa-agent-network:verdict -->"

AGENT_TRAILER = "Agent"
SESSION_TRAILER = "Session"

# Not an agent, but not a person either: scripts/commit_baselines.py refreshes
# fingerprints after a green suite, and those commits land on files agent PRs
# touched. Signed like an agent's, so they are never read as a reviewer's edit.
BASELINE_JOB = "commit-baselines"

# Subjects the agents wrote before their commits carried an `Agent:` trailer.
# Consulted only for a commit that has none.
_SUBJECT_AGENTS = (
    (re.compile(r"^authoring: "), "test-authoring-agent"),
    (re.compile(r"^healing: "), "test-healing-agent"),
    (re.compile(r"^adaptation: "), "test-adaptation-agent"),
    (re.compile(r"^\[Authoring Agent\]: "), "test-authoring-agent"),
    (re.compile(r"^feat\(automation\): "), "test-authoring-agent"),
    (re.compile(r"^fix\(automation\): "), "test-healing-agent"),
    (re.compile(r"^chore\(baselines\): "), BASELINE_JOB),
)

_TASK = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")
_LESSON = re.compile(r"^\s*/lesson\b[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE | re.DOTALL)
_TRAILER = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.+?)\s*$")


# ── The verdict block ─────────────────────────────────────────────────────────

def verdict_block() -> str:
    """The markdown every agent PR ends with. A reviewer ticks boxes; nothing else."""
    verdicts = "\n".join(f"- [ ] {label}" for label in VERDICTS.values())
    reasons = "\n".join(f"- [ ] {label}" for label in REASONS.values())
    return (
        "---\n\n"
        "### Reviewer verdict\n\n"
        f"{BLOCK_BEGIN}\n"
        "Tick one once you have reviewed this PR: it is how the agents learn what was "
        "accepted. Unticked means not reviewed.\n\n"
        f"{verdicts}\n\n"
        "<details>\n<summary>Why? Optional, tick any</summary>\n\n"
        f"{reasons}\n\n"
        "</details>\n\n"
        "To teach the agent something for next time, comment "
        "`/lesson <what it should do>`.\n"
        f"{BLOCK_END}\n"
    )


def _key(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().lower()


_VERDICT_BY_LABEL = {_key(label): code for code, label in VERDICTS.items()}
_REASON_BY_LABEL = {_key(label): code for code, label in REASONS.items()}


def parse_verdict(body: str) -> Optional[Dict]:
    """What the reviewer ticked, or None when the body has no verdict block.

    `verdict` is None while nothing is ticked, and also when more than one verdict
    is (`ambiguous`): two answers average into one that means nothing.
    """
    text = body or ""
    start = text.find(BLOCK_BEGIN)
    if start < 0:
        return None
    end = text.find(BLOCK_END, start)
    block = text[start:end if end >= 0 else len(text)]
    ticked_verdicts: List[str] = []
    reasons: List[str] = []
    for line in block.splitlines():
        match = _TASK.match(line)
        if not match or match.group(1) == " ":
            continue
        label = _key(match.group(2))
        if label in _VERDICT_BY_LABEL:
            ticked_verdicts.append(_VERDICT_BY_LABEL[label])
        elif label in _REASON_BY_LABEL:
            reasons.append(_REASON_BY_LABEL[label])
    ambiguous = len(ticked_verdicts) > 1
    return {
        "verdict": ticked_verdicts[0] if len(ticked_verdicts) == 1 else None,
        "reasons": reasons,
        "ambiguous": ambiguous,
    }


def parse_lesson(comment: str) -> Optional[str]:
    """The lesson a `/lesson ...` comment teaches, or None for any other comment."""
    match = _LESSON.search(comment or "")
    if not match:
        return None
    lesson = match.group(1).strip()
    return lesson or None


# ── Commits ───────────────────────────────────────────────────────────────────

def trailers(message: str) -> Dict[str, str]:
    """`Token: value` lines of the message's last paragraph (git's trailer block)."""
    paragraphs = [p for p in re.split(r"\n\s*\n", (message or "").strip()) if p.strip()]
    if len(paragraphs) < 2:
        return {}
    found: Dict[str, str] = {}
    for line in paragraphs[-1].splitlines():
        match = _TRAILER.match(line)
        if not match:
            return {}
        found.setdefault(match.group(1), match.group(2))
    return found


def agent_of_commit(message: str) -> Optional[str]:
    """Which agent (or automated job) wrote this commit, or None when a person did.

    The `Agent:` trailer decides. A commit without one is read by its subject,
    because the agents wrote fixed subjects long before they wrote the trailer.
    """
    agent = trailers(message).get(AGENT_TRAILER)
    if agent:
        return agent
    subject = (message or "").lstrip().split("\n", 1)[0]
    for pattern, name in _SUBJECT_AGENTS:
        if pattern.match(subject):
            return name
    return None


def session_of_commit(message: str) -> str:
    return trailers(message).get(SESSION_TRAILER, "")


# ── The store ─────────────────────────────────────────────────────────────────

def store_path() -> Path:
    override = (os.getenv("FEEDBACK_FILE") or "").strip()
    if override:
        return Path(override)
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    return _STORE


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def event(kind: str, *, source: str, key: Iterable, explicit: bool,
          agent: str = "", session: str = "", artifact: Optional[Dict] = None,
          verdict: Optional[str] = None, reasons: Iterable[str] = (),
          text: str = "", diff: str = "", who: str = "", at: str = "") -> Dict:
    """One feedback event. `key` names the record it came from, which makes its id.

    The same record read twice gets the same id, so a collector can run as often
    as it likes; a changed record (a verdict ticked differently) gets a new one,
    and readers take the newest per artifact.
    """
    digest = hashlib.sha1(json.dumps([source, kind, *key], default=str)
                          .encode("utf-8")).hexdigest()[:16]
    return {
        "id": digest,
        "kind": kind,
        "source": source,
        "explicit": explicit,
        "agent": agent,
        "session": session,
        "artifact": artifact or {},
        "verdict": verdict,
        "reasons": list(reasons),
        "text": text,
        "diff": diff,
        "who": who,
        "at": at,
    }


def read(path: Optional[Path] = None) -> List[Dict]:
    """Every event, oldest first. Unreadable lines are skipped, never fatal."""
    target = Path(path) if path else store_path()
    events: List[Dict] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    events.append(row)
    except OSError:
        return []
    return events


def append(events: Iterable[Dict], path: Optional[Path] = None) -> List[Dict]:
    """Write the events not already stored; return the ones written.

    Raises on a write failure. The callers are scripts a person runs, and a
    collector that silently drops what it read reports a loop with nothing in it.
    """
    target = Path(path) if path else store_path()
    known = {row.get("id") for row in read(target)}
    fresh: List[Dict] = []
    for item in events:
        if item["id"] in known:
            continue
        known.add(item["id"])
        fresh.append({**item, "recorded_at": now()})
    if not fresh:
        return []
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in fresh)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return fresh


def record_quietly(events: Iterable[Dict]) -> int:
    """append() for code inside an agent run. Never raises; returns how many were new.

    A run must not fail, or even stop, over a feedback event: losing one costs a
    data point, failing the run costs the run.
    """
    try:
        return len(append(events))
    except Exception:
        return 0


# ── Signals a run sees for itself ─────────────────────────────────────────────

def diff(old: str, new: str, label: str = "input") -> str:
    """A unified diff of two texts, cut at MAX_DIFF_CHARS."""
    text = "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
        fromfile=f"{label} (before)", tofile=f"{label} (after)"))
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n… (cut at {MAX_DIFF_CHARS} characters)"
    return text


def _digest(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def override(agent: str, session: str, overridden: str, outcome: str,
             test: str = "", detail: str = "") -> Dict:
    """A person told the agent to go past its own refusal (FORCE=true).

    Explicit: someone read why the agent stopped and decided it was wrong to. The
    outcome says whether they were right ("verified"), half right ("advanced": the
    test got further) or not ("failed"); "proceeded" and "applied" mean the run
    went on and its own result says how that ended.
    """
    return event("override", source="run", explicit=True,
                 key=(agent, session, test, overridden, outcome),
                 agent=agent, session=session,
                 artifact={"test": test, "overridden": overridden, "outcome": outcome},
                 text=detail)


def draft(queue_name: str, text: str, session: str, test: str = "") -> Dict:
    """A change note healing drafted for a person to finish (shared/adaptation_handoff).

    Not feedback itself: it is kept so that what the person changed in it can be
    measured when adaptation runs the note (`note_input`).
    """
    return event("draft", source="run", explicit=False,
                 key=("test-healing-agent", queue_name, _digest(text)),
                 agent="test-healing-agent", session=session,
                 artifact={"queue_name": queue_name, "test": test}, text=text)


def _previous_session(audit_root: Path, name: str, current: str) -> Dict:
    """The newest earlier session of this input, and how it ended, from its markers."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")
    try:
        sessions = sorted((d for d in Path(audit_root).iterdir()
                           if d.is_dir() and d.name.endswith(f"-{slug}") and d.name != current),
                          key=lambda d: d.name)
    except OSError:
        return {}
    if not sessions:
        return {}
    return {"session": sessions[-1].name, "ended": ended(sessions[-1])}


def ended(audit_dir) -> Dict[str, str]:
    """How a run ended, from the marker files it left (docs/ARCHITECTURE.md)."""
    found: Dict[str, str] = {}
    for marker in (".fix-passed", ".verdict", ".skip-reason", ".cancelled", ".crashed"):
        try:
            found[marker.lstrip(".")] = (Path(audit_dir) / marker).read_text(
                encoding="utf-8").strip()
        except OSError:
            continue
    return found


def note_input(agent: str, session: str, input_file, audit_root,
               store: Optional[Path] = None) -> Optional[Dict]:
    """Record how this run's input differs from what came before it. Never raises.

    Two comparisons, the first that applies:

      * **a draft an agent wrote** (`draft`): the person's edits to it, attributed
        to the agent that drafted it — a correction of that agent's output;
      * **the previous run's input** (`processed/<name>`, where each run leaves
        it): a person rewrote the input after seeing a run, which says the earlier
        input was unclear or wrong, or that the request changed. Which of the two
        is not knowable here, so it is inferred and carries how that run ended.

    Returns the event recorded, or None when nothing changed.
    """
    try:
        path = Path(input_file)
        text = path.read_text(encoding="utf-8", errors="ignore")
        drafts = [e for e in read(store) if e.get("kind") == "draft"
                  and (e.get("artifact") or {}).get("queue_name") == path.name]
        if drafts:
            base = drafts[-1]
            if base.get("text", "") == text:
                return None
            found = event("draft_edit", source="run", explicit=False,
                          key=(path.name, _digest(base.get("text", "")), _digest(text)),
                          agent=base.get("agent", ""), session=base.get("session", ""),
                          artifact={"queue_name": path.name, "used_by": agent,
                                    "used_in": session},
                          diff=diff(base.get("text", ""), text, path.name))
        else:
            earlier = path.parent / "processed" / path.name
            if not earlier.is_file():
                return None
            before = earlier.read_text(encoding="utf-8", errors="ignore")
            if before == text:
                return None
            previous = _previous_session(Path(audit_root), path.stem, session)
            found = event("input_edit", source="run", explicit=False,
                          key=(agent, str(path), _digest(before), _digest(text)),
                          agent=agent, session=session,
                          artifact={"queue_name": path.name,
                                    "previous_session": previous.get("session", ""),
                                    "previous_ended": previous.get("ended", {})},
                          diff=diff(before, text, path.name))
        written = append([found], store)
        return written[0] if written else None
    except Exception:
        return None


# ── A person's verdict on one run ─────────────────────────────────────────────

def person_verdict(agent: str, session: str, verdict: str, reasons: Iterable[str] = (),
                   *, item: Optional[int] = None, note: str = "", who: str = "",
                   source: str = "cli", agents_dir: Optional[Path] = None,
                   test: str = "") -> Dict:
    """Record a verdict given outside a PR, and pass it on where an agent already looks.

    The same for `make verdict` and the Studio: an explicit `verdict` event; for
    adaptation, its proposals judged; for healing, a reject as the wrong flow or
    page also recorded as a false LOCATOR_STALE. Raises ValueError on an unknown
    agent, verdict or reason, and on a failed write of the event itself.
    """
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r} (choose from {', '.join(VERDICTS)})")
    reasons = [r for r in reasons if r]
    unknown = [r for r in reasons if r not in REASONS]
    if unknown:
        raise ValueError(f"unknown reason(s): {', '.join(unknown)} "
                         f"(choose from {', '.join(REASONS)})")
    at = now()
    written = append([event(
        "verdict", source=source, explicit=True,
        key=(agent, session, item, verdict, sorted(reasons), at),
        agent=agent, session=session,
        artifact={"item": item} if item is not None else {},
        verdict=verdict, reasons=reasons, text=(note or "").strip(), who=who, at=at)])
    judged, wrong_diagnosis = verdict_effects(agent, session, verdict, reasons, item=item,
                                              test=test, source=source, agents_dir=agents_dir)
    return {"event": written[0] if written else None, "judged_proposals": judged,
            "wrong_diagnosis": wrong_diagnosis}


def verdict_effects(agent: str, session: str, verdict: str, reasons: Iterable[str] = (),
                    *, item: Optional[int] = None, test: str = "", source: str = "",
                    agents_dir: Optional[Path] = None):
    """Pass a verdict on to where an agent already looks: (proposals judged, wrong diagnosis).

    For a verdict however it arrived (CLI, Studio, a Slack reply). Callers apply it
    once per verdict, when its event is first written.
    """
    from shared import verdict_feedback
    folder = Path(agents_dir) if agents_dir else REPO_ROOT / "agents"
    judged = 0
    if agent == "test-adaptation-agent" and verdict in VERDICTS:
        judged = verdict_feedback.judge_proposals(
            folder / agent / "feedback" / "proposals.json", session, verdict, item)
    wrong_diagnosis = verdict_feedback.from_review(
        folder / "test-healing-agent" / "feedback" / "known-issues.json",
        agent, verdict, list(reasons), test, session=session,
        detail=f"rejected by its reviewer ({source}) as the wrong flow or page")
    return judged, wrong_diagnosis


def latest_verdict(agent: str, session: str, store: Optional[Path] = None) -> Optional[Dict]:
    """The newest verdict on a run, from wherever it was given (PR, CLI, Studio)."""
    found = None
    for row in read(store):
        if row.get("kind") == "verdict" and row.get("agent") == agent \
                and row.get("session") == session and row.get("verdict"):
            found = row
    if not found:
        return None
    return {key: found.get(key) for key in ("verdict", "reasons", "text", "who", "source", "at")}


# ── Test design (the Studio's Requirements → Tests) ───────────────────────────

CASE_FIELDS = ("title", "priority", "preconditions", "steps", "expected_result")


def _case(fields: Optional[Dict]) -> Dict[str, str]:
    """A test case's text fields, normalised enough that only real edits differ."""
    fields = fields or {}
    out = {}
    for name in CASE_FIELDS:
        value = str(fields.get(name) or "").replace("\r\n", "\n").strip()
        out[name] = value.upper() if name == "priority" else value
    return out


def case_text(fields: Optional[Dict]) -> str:
    case = _case(fields)
    return "".join(f"## {name}\n{case[name]}\n\n" for name in CASE_FIELDS)


def design_generation(session: str, tests: Iterable[Dict], updates: Iterable[Dict],
                      who: str = "", lessons: Iterable[str] = ()) -> Dict:
    """What one Requirements → Tests run offered: new tests, and updates to existing ones.

    Not feedback by itself. Matched against the `test_push` events of the same
    session, by target, it says which tests a person never pushed.
    """
    tests = [{"target": t.get("target"), "title": str(t.get("title") or "")[:300],
              "priority": str(t.get("priority") or "")[:8]} for t in tests][:500]
    updates = [{"req_id": str(u.get("req_id") or "")[:80],
                "testrail_id": str(u.get("testrail_id") or "")[:40],
                "title": str(u.get("title") or "")[:300]} for u in updates][:500]
    return event("generation", source="studio", explicit=False, key=(DESIGN_AGENT, session),
                 agent=DESIGN_AGENT, session=session,
                 artifact={"tests": tests, "updates": updates,
                           "lessons": [str(l)[:40] for l in lessons][:50]}, who=who)


def design_push(session: str, action: str, testrail_id: str, source: str,
                original: Optional[Dict], final: Optional[Dict], target=None,
                who: str = "", at: str = "") -> Dict:
    """A person pushed AI-written test text to TestRail: as written, or after editing it.

    Explicit: pushing is the person accepting it. `source` is what the AI wrote it
    as: a generated test, a suggested update to an existing case, or a case it
    improved for automation.
    """
    before, after = _case(original), _case(final)
    edited = before != after
    return event("test_push", source="studio", explicit=True,
                 key=(DESIGN_AGENT, session, action, str(testrail_id), _digest(case_text(final)),
                      at),
                 agent=DESIGN_AGENT, session=session,
                 artifact={"action": action, "testrail_id": str(testrail_id)[:40],
                           "source": source, "target": target,
                           "edited_fields": [n for n in CASE_FIELDS if before[n] != after[n]]},
                 verdict="edited" if edited else "accept",
                 diff=diff(case_text(original), case_text(final), "test case") if edited else "",
                 who=who, at=at)

