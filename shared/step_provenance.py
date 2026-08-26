"""Reconstruct what the test actually did before it failed.

The framework already narrates itself. `Log.step` and `Log.action` print
`STEP:` and `ACTION:` lines with timestamps, and every one of them ends up in the
execution log that reaches the healing agent — where it has only ever been passed
to the model as an undifferentiated blob of text.

Read as a sequence instead, it answers two questions no selector can:

  * how far did the flow get?  A locator that breaks on step nine of a journey is
    a different animal from one that breaks before anything has happened.
  * how long did the last wait take?  A wait that burned its entire configured
    budget was never going to succeed; one that failed early hit something else.

Timestamps in these logs are elapsed wall-clock (`HH:MM:SS`) from the start of
the run, so only differences between them are meaningful.
"""

import re
from typing import Dict, List, Optional

_LINE = re.compile(r"^\[(?P<ts>\d{2}:\d{2}:\d{2})\]\s*(?P<body>.*)$")
_KINDS = (("STEP:", "step"), ("ACTION:", "action"), ("API:", "api"))

# The framework's own wording when a wait gives up. Used to locate the failure
# moment inside the log, not to classify it.
_GIVE_UP = ("failed to load element", "element not visible after timeout",
            "------------end of execution------------")


def _seconds(timestamp: str) -> Optional[int]:
    try:
        hours, minutes, seconds = (int(part) for part in timestamp.split(":"))
        return hours * 3600 + minutes * 60 + seconds
    except (ValueError, AttributeError):
        return None


def parse_log(execution_log: str) -> List[Dict]:
    """Every timestamped line, tagged by kind, in order."""
    events: List[Dict] = []
    for raw in (execution_log or "").splitlines():
        match = _LINE.match(raw.strip())
        if not match:
            continue
        body = match.group("body").strip()
        kind = "log"
        text = body
        for prefix, name in _KINDS:
            if body.upper().startswith(prefix):
                kind, text = name, body[len(prefix):].strip()
                break
        events.append({"ts": match.group("ts"), "at": _seconds(match.group("ts")),
                       "kind": kind, "text": text[:300]})
    return events


def summarize(execution_log: str, budget_s: Optional[int] = None) -> Dict:
    """How far the flow got, and how the final wait behaved.

    `available` is False when the log carried no timestamped narration at all, so
    a caller can tell "the test did nothing" apart from "we cannot see what it did".
    """
    result: Dict = {
        "available": False, "steps": 0, "actions": 0,
        "last_step": "", "last_action": "", "sequence": [],
        "gap_before_failure_s": None, "burned_full_budget": None,
    }
    events = parse_log(execution_log)
    if not events:
        return result

    result["available"] = True
    result["steps"] = sum(1 for e in events if e["kind"] == "step")
    result["actions"] = sum(1 for e in events if e["kind"] == "action")
    for event in events:
        if event["kind"] == "step":
            result["last_step"] = event["text"]
        elif event["kind"] == "action":
            result["last_action"] = event["text"]
    result["sequence"] = [f"{e['ts']} {e['kind'].upper()}: {e['text'][:120]}"
                          for e in events if e["kind"] in ("step", "action", "api")][-12:]

    failure_at = next((e["at"] for e in events
                       if any(marker in e["text"].lower() for marker in _GIVE_UP)), None)
    if failure_at is not None:
        prior = [e["at"] for e in events
                 if e["at"] is not None and e["at"] < failure_at
                 and e["kind"] in ("step", "action", "api")]
        if prior:
            result["gap_before_failure_s"] = failure_at - max(prior)

    gap = result["gap_before_failure_s"]
    if gap is not None and budget_s:
        # Polling loops overshoot slightly; treat "within a second of the budget"
        # as having run the clock out.
        result["burned_full_budget"] = gap >= budget_s - 1
    return result


def describe(summary: Dict) -> str:
    """A short block for a prompt or a log line. Empty when there is nothing."""
    if not summary.get("available"):
        return ""
    lines = [f"{summary['steps']} step(s), {summary['actions']} action(s) completed"]
    if summary.get("last_action"):
        lines.append(f"last action: {summary['last_action'][:120]}")
    gap = summary.get("gap_before_failure_s")
    if gap is not None:
        note = " (the full wait budget)" if summary.get("burned_full_budget") else ""
        lines.append(f"{gap}s elapsed between that and the failure{note}")
    return "\n".join(lines)
