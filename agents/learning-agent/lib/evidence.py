"""Which feedback events can teach a lesson, and what each one says.

Deterministic first: everything here reads the events themselves (the diff a person
made, the reasons they ticked, the classification they corrected) with no model.
The model in step 04 only groups what this has already tagged.

The line it holds is the one the store was built around (shared/feedback.py):

  * **evidence** — a person said something (a verdict with reasons or a note, a
    `/lesson`, a comment, a corrected classification, a FORCE override), or a
    person changed what an agent wrote (a commit on its branch that touched its
    files, an edit to its draft, an edited test case). Only these count towards a
    lesson.
  * **context** — a rewritten input, a revert. Kept beside the evidence for the
    model to read, never counted: a revert in this repo has been made on purpose
    to stage a failure, and a rewritten input may be a new request.

Everything else (a closed PR, a cancel, a retry, what a run generated) is not
learnable at all and never reaches the model.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Set

EVIDENCE_KINDS = {"verdict", "lesson", "comment", "review", "reclassification", "override",
                  "human_commit", "draft_edit", "test_push"}
CONTEXT_KINDS = {"input_edit", "revert"}

# What a changed line is about. Matched against the + and - lines of a diff.
_TAG_PATTERNS = (
    ("locator", re.compile(r"(locator\(|getBy\w+\(|@FindBy|By\.(id|css|xpath|name)|frameLocator|"
                           r"\[data-[\w-]+|xpath=|css=)")),
    ("assertion", re.compile(r"\b(assert\w*\(|AssertHelper\.\w+|verify\w*\()")),
    ("wait", re.compile(r"(WaitHelper\.\w+|waitFor\w*\(|Thread\.sleep|setTimeout)")),
    ("logstep", re.compile(r"logStep\(")),
    ("structure", re.compile(r"^\s*(public|private|protected)\s+[\w<>\[\], ?]+\s+\w+\s*\(")),
)
# Folder names that say what kind of file sits below them, not which part of the
# product: the module is the nearest folder above a file that is none of these.
_GENERIC_DIRS = {"src", "main", "test", "tests", "java", "kotlin", "resources", "web", "api",
                 "mobile", "page", "pages", "pageobjects", "helpers", "data", "csvfiles",
                 "jsonfiles", "fixtures", "automation", "modules", "baselines", "parameters"}


def is_evidence(event: Dict) -> bool:
    """Whether this event may count towards a lesson."""
    kind = event.get("kind")
    if kind not in EVIDENCE_KINDS:
        return False
    if kind == "verdict":
        # An unqualified accept says the run was fine, which teaches nothing new.
        return event.get("verdict") in ("edited", "reject") or bool(event.get("text")) \
            or bool(event.get("reasons"))
    if kind == "human_commit":
        return bool((event.get("artifact") or {}).get("touches_agent_files"))
    if kind == "test_push":
        return event.get("verdict") == "edited"
    if kind == "reclassification":
        return not (event.get("artifact") or {}).get("agrees")
    return True


def is_context(event: Dict) -> bool:
    return event.get("kind") in CONTEXT_KINDS


def _changed_lines(diff: str) -> List[str]:
    return [line[1:] for line in (diff or "").splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def added_lines(diff: str) -> List[str]:
    """The lines a person added, stripped, worth matching (not braces or blanks)."""
    out = []
    for line in (diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if len(text) >= 8 and text not in ("{", "}", "};", "*/", "/**"):
                out.append(text)
    return out


def files_of(event: Dict) -> List[str]:
    artifact = event.get("artifact") or {}
    return list(artifact.get("touches_agent_files") or artifact.get("files") or [])


def tags(event: Dict) -> List[str]:
    """What this event is about, from its own content."""
    found: Set[str] = set(f"reason:{r}" for r in event.get("reasons") or [])
    for line in _changed_lines(event.get("diff") or ""):
        for name, pattern in _TAG_PATTERNS:
            if pattern.search(line):
                found.add(name)
    for path in files_of(event):
        if path.endswith((".csv", ".properties")):
            found.add("test-data")
    artifact = event.get("artifact") or {}
    if event.get("kind") == "test_push":
        found.update(f"field:{f}" for f in artifact.get("edited_fields") or [])
    if event.get("kind") == "reclassification":
        said = (artifact.get("agent_said") or {}).get("classification") or "?"
        found.add(f"classification:{said}->{(artifact.get('person_said') or {}).get('classification')}")
    if event.get("kind") == "override":
        found.add(f"override:{artifact.get('overridden')}:{artifact.get('outcome')}")
    return sorted(found)


# The step a kind of event is about, when it says nothing more specific.
_STEP_BY_AGENT = {
    "test-healing-agent": "fix",
    "test-adaptation-agent": "adapt",
    "test-triaging-agent": "classify",
}


def scope_guess(event: Dict) -> Dict[str, str]:
    """Where a lesson from this event would apply: step, module, page object."""
    agent = event.get("agent") or ""
    kind = event.get("kind")
    step = ""
    if agent == "test-authoring-agent":
        if kind == "input_edit":
            step = "parse"
        elif kind in ("human_commit", "draft_edit"):
            step = "generate"
    elif agent in _STEP_BY_AGENT and kind != "input_edit":
        step = _STEP_BY_AGENT[agent]
    if kind == "draft_edit":       # healing drafts change notes in its fix step
        step = "fix"
    modules, pages = set(), set()
    for path in files_of(event):
        module = next((part for part in reversed(PurePosixPath(path).parts[:-1])
                       if part.lower() not in _GENERIC_DIRS and "." not in part), "")
        if module:
            modules.add(module)
        name = PurePosixPath(path).stem
        if name.endswith("Page"):
            pages.add(name)
    return {"step": step,
            "module": modules.pop() if len(modules) == 1 else "",
            "page": pages.pop() if len(pages) == 1 else ""}


def independent(events: Iterable[Dict]) -> int:
    """How many separate pieces of work these events are about (a PR, else a session)."""
    keys = set()
    for event in events:
        artifact = event.get("artifact") or {}
        key = artifact.get("pr") or event.get("session") or event.get("id")
        keys.add((event.get("agent"), str(key)))
    return len(keys)


def enough(events: List[Dict]) -> Optional[str]:
    """Why these events cannot carry a new lesson, or None when they can.

    One explicit `/lesson` is enough: a person wrote the lesson. Otherwise at
    least two independent pieces of work must point the same way.
    """
    if not events:
        return "no evidence"
    if not all(is_evidence(e) for e in events):
        return "cites events that are context, not evidence"
    if any(e.get("kind") == "lesson" for e in events):
        return None
    if independent(events) >= 2:
        return None
    return "needs a /lesson or at least two independent pieces of work"
