"""Record where a diagnosis turned out to be wrong.

Rule thresholds cannot be tuned against intuition. The two mistakes that matter
point in opposite directions and are invisible unless someone writes them down:

  * a **false stop** — the agent refused to attempt a fix that would have worked.
    Visible when a human passes FORCE=true and the forced fix then verifies.
  * a **false LOCATOR_STALE** — the agent edited a selector on a page the test
    never reached. Visible when a reviewer rejects the fix as the wrong flow or
    page (`from_review`), or when the same test fails again on the same element
    straight after a "successful" fix. Not when a fix is reverted: a revert looks
    like the same evidence and is not, since reverts also put a broken locator
    back on purpose, to stage a failure for testing.

Both are appended to the agent's existing `feedback/known-issues.json`, which has
sat empty since it was created. This only records; nothing reads it back
automatically, because a threshold changed by a machine on evidence a machine
gathered is how a system talks itself into a corner.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_MAX_ENTRIES = 500


def _load(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def record(feedback_file, kind: str, test_name: str, verdict: str,
           detail: str = "", session: str = "") -> bool:
    """Append one observation. Returns whether it was written.

    Never raises and never blocks a run: losing a feedback entry costs a data
    point, while failing a fix run to record one costs the fix.
    """
    if kind not in ("false_stop", "false_locator_fix", "confirmed"):
        return False
    try:
        path = Path(feedback_file)
        entries = _load(path)
        entries.append({
            "kind": kind,
            "test_name": test_name,
            "verdict": verdict,
            "detail": detail[:400],
            "session": session,
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries[-_MAX_ENTRIES:], indent=2))
        return True
    except Exception:
        return False


def summarize(feedback_file) -> Dict[str, int]:
    """Counts per kind, for a human deciding whether a threshold needs moving."""
    counts: Dict[str, int] = {}
    for entry in _load(Path(feedback_file)):
        kind = entry.get("kind")
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


# ── test-adaptation-agent: did a human accept what was proposed? ──────────────
#
# The promotion criterion from propose-only to apply is not a date and not a
# feeling — it is whether people were accepting the proposals verbatim. That is
# only knowable if somebody writes it down, so this records it in the same file
# and the same shape as the verdict mistakes above.
#
# As with everything else here: this records, nothing reads it back
# automatically. A threshold moved by a machine on evidence a machine gathered is
# how a system talks itself into a corner.

def record_proposal(path, session_id: str, module: str, item: int, kind: str,
                    accepted: Optional[bool], note: str = "") -> None:
    """Log one proposed change item and what a human did with it.

    `accepted` is None while nobody has judged it yet — deliberately distinct
    from False, because "not reviewed" and "rejected" would otherwise average
    together into a number that means nothing.
    """
    target = Path(path)
    entries = _load(target)
    entries.append({
        "kind": "adaptation_proposal",
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "module": module,
        "item": item,
        "change_kind": kind,
        "accepted": accepted,
        "note": note,
    })
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(entries[-_MAX_ENTRIES:], indent=2))


def judge_proposals(path, session_id: str, verdict: str,
                    item: Optional[int] = None) -> int:
    """Record a human's verdict on a session's proposals. Returns how many it judged.

    `verdict` is "accept", "edited" or "reject". Only "accept" sets `accepted` to
    True: the promotion criterion is proposals accepted *verbatim*, and one a person
    had to change first was not. The verdict itself is kept beside it.

    An item proposed on several attempts is judged once, on its latest proposal:
    that is the one a person read. With no `item`, every item of the session is.
    """
    if verdict not in ("accept", "edited", "reject"):
        raise ValueError(f"unknown verdict: {verdict!r}")
    target = Path(path)
    entries = _load(target)
    latest: Dict[int, int] = {}
    for index, entry in enumerate(entries):
        if entry.get("kind") != "adaptation_proposal" or entry.get("session_id") != session_id:
            continue
        if item is not None and entry.get("item") != item:
            continue
        latest[entry.get("item")] = index
    for index in latest.values():
        entries[index]["accepted"] = verdict == "accept"
        entries[index]["verdict"] = verdict
        entries[index]["judged_at"] = (datetime.now(timezone.utc).isoformat()
                                       .replace("+00:00", "Z"))
    if latest:
        target.write_text(json.dumps(entries[-_MAX_ENTRIES:], indent=2))
    return len(latest)


def proposal_acceptance(path) -> Dict:
    """How proposals have been received so far. Read by a human, not by a gate."""
    entries = [e for e in _load(Path(path))
               if e.get("kind") == "adaptation_proposal"]
    judged = [e for e in entries if e.get("accepted") is not None]
    accepted = [e for e in judged if e["accepted"]]
    by_kind: Dict[str, Dict[str, int]] = {}
    for entry in judged:
        row = by_kind.setdefault(entry.get("change_kind", "?"),
                                 {"accepted": 0, "total": 0})
        row["total"] += 1
        row["accepted"] += 1 if entry["accepted"] else 0
    return {
        "proposed": len(entries),
        "judged": len(judged),
        "accepted": len(accepted),
        "rate": (len(accepted) / len(judged)) if judged else None,
        "by_kind": by_kind,
    }


# ── A reviewer's verdict on a PR ──────────────────────────────────────────────

def from_review(feedback_file, agent: str, verdict: Optional[str], reasons,
                test_name: str, session: str = "", detail: str = "") -> bool:
    """Record what a reviewer's verdict says about a diagnosis, if anything.

    One verdict maps onto the kinds above: a healing PR rejected as the wrong flow
    or page is a false LOCATOR_STALE, an edit to a selector while the test was
    somewhere else. A wrong *locator* is a bad repair of a right diagnosis, so it
    stays in the feedback store (shared/feedback.py) for the learning loop.
    """
    if agent != "test-healing-agent" or verdict != "reject":
        return False
    if "wrong-flow" not in (reasons or ()):
        return False
    return record(feedback_file, "false_locator_fix", test_name, "LOCATOR_STALE",
                  detail=detail, session=session)
