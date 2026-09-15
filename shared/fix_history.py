"""Every fix attempt this session has already made, and whether the next one can differ.

Step 04's retry loop was, in effect, the same attempt run several times. Two things
made it that way, and both are fixed here.

**The window was one deep.** `04-run-and-fix.json` is overwritten on every attempt, so
attempt 3 could see attempt 2 and nothing before it — and was therefore free to
re-propose attempt 1's idea, which had already been tried and had already failed. The
per-attempt files existed on disk the whole time but were only ever read by the ship
step, to build git commits.

**Guard rejections never reached the model.** A fix rejected by `no_selector_broadening`
is recorded, logged, shipped in the audit trail — and absent from the next prompt. So the
model kept being told "try something different" without ever being told what it had done
wrong, and the run burned its budget triggering the same guard.

So this module keeps an append-only record and answers two questions from it:

  * `render()` — what should the next prompt know? Every prior attempt: the diagnosis, the
    files proposed, and for a rejected one the guard that stopped it and why.
  * `exhausted()` — is another attempt worth paying for at all? Only when the model can
    bring something new. Three ways it demonstrably cannot:

      1. It said so — `edits: []` with a root cause explaining it cannot fix this from the
         files it can see. That is an answer, not a failure to answer.
      2. The same guard rejected everything twice running. The first rejection earns a
         retry, because the model had not yet been told why. The second, made with that
         reason in the prompt, says no other shape is coming.
      3. The proposed edits repeat a set already proposed. Matched on exact content
         hashes, never fuzzily — "similar" is a judgement call and this must not make one.

A budget bounds the worst case; this decides whether the worst case is even worth
reaching. The two are independent on purpose: raising the budget should buy more real
attempts, not more identical ones.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Outcomes an attempt can end in. `no_edits` and `all_rejected` are deliberately
# distinct: nothing reached disk in either case, but only the first is the model
# telling us it has no fix to offer.
PASSED = "passed"
FAILED = "failed"
ALL_REJECTED = "all_rejected"
NO_EDITS = "no_edits"

HISTORY_FILE = ".fix-history.json"

_MAX_REASON_CHARS = 300


# ── Persistence ───────────────────────────────────────────────────────────────

def _path(audit_dir) -> Path:
    return Path(audit_dir) / HISTORY_FILE


def load(audit_dir) -> List[Dict[str, Any]]:
    """Every attempt recorded so far, oldest first. Never raises.

    A history that cannot be read must degrade the next prompt, never break the fix
    path — the same rule `gather_runtime_evidence` follows for its artefacts.
    """
    try:
        data = json.loads(_path(audit_dir).read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def append(audit_dir, record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Add one attempt's record and return the full history.

    Appends rather than overwrites — that distinction is the whole point of this file
    existing alongside `04-run-and-fix.json` rather than inside it.
    """
    history = load(audit_dir)
    history.append(record)
    try:
        _path(audit_dir).write_text(json.dumps(history, indent=2))
    except Exception:
        pass
    return history


def record(attempt: int, root_cause: str = "", confidence: str = "",
           proposed: Optional[List[Dict[str, str]]] = None,
           applied: Optional[List[str]] = None,
           rejections: Optional[List[Dict[str, str]]] = None,
           outcome: str = FAILED, failure_location: str = "") -> Dict[str, Any]:
    """Build one attempt's record. Pure — the caller decides when to persist it."""
    return {
        "attempt": attempt,
        "root_cause": (root_cause or "").strip(),
        "confidence": confidence or "",
        "proposed": proposed or [],
        "applied": applied or [],
        "rejections": rejections or [],
        "outcome": outcome,
        "failure_location": failure_location or "",
    }


# ── Identifying what was proposed ─────────────────────────────────────────────

def fingerprint(files_map: Optional[dict], edits_map: Optional[dict]) -> List[Dict[str, str]]:
    """Content hashes for everything this attempt proposed.

    Hashed, not stored verbatim, for two reasons: whole-file replacements would make the
    history enormous, and a hash forces the repeat check to be exact. `old` is empty for a
    whole-file replacement, which has no anchor text.
    """
    out: List[Dict[str, str]] = []
    for rel_path, edits in (edits_map or {}).items():
        for edit in edits or []:
            if not isinstance(edit, dict):
                continue
            out.append({
                "file": rel_path,
                "old": _sha(edit.get("old_string", "")),
                "new": _sha(edit.get("new_string", "")),
            })
    for rel_path, content in (files_map or {}).items():
        if rel_path in (edits_map or {}):
            continue  # already counted as an edit; apply_fix prefers edits too
        out.append({"file": rel_path, "old": "", "new": _sha(content or "")})
    return out


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _key(entry: Dict[str, str]) -> Tuple[str, str, str]:
    return (entry.get("file", ""), entry.get("old", ""), entry.get("new", ""))


def _guards(rejections: List[Dict[str, str]]) -> set:
    """The guard names in a set of rejections.

    `_run_guards` returns "<guard>: <detail>", and `apply_edits`/path checks return a bare
    reason with no guard prefix. Splitting on the first colon covers both: an unprefixed
    reason simply becomes its own key, which is still a stable identity to compare on.
    """
    names = set()
    for entry in rejections or []:
        reason = (entry.get("guard") or entry.get("reason") or "").strip()
        names.add(reason.split(":", 1)[0].strip() if reason else "unknown")
    return names


# ── The two questions ─────────────────────────────────────────────────────────

def exhausted(history: List[Dict[str, Any]],
              current: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Can the next attempt bring anything new? Returns (stop, reason).

    `current` is this attempt's record, not yet persisted. Passing it lets the caller
    decide before writing the gate. When omitted, the last entry in `history` is used.
    """
    attempts = list(history or [])
    if current is not None:
        attempts = attempts + [current]
    if not attempts:
        return False, ""

    last = attempts[-1]
    if last.get("outcome") == PASSED:
        return False, ""

    # 1. The model said it has no fix. Re-running proves nothing: nothing changed on
    #    disk since the run that just failed.
    if last.get("outcome") == NO_EDITS:
        return True, ("the model returned no edits — it reports this cannot be fixed from "
                      "the files it can see, which is an answer rather than a failed attempt")

    # 2. The same guard, twice running, with the first rejection's reason already in the
    #    prompt for the second.
    if last.get("outcome") == ALL_REJECTED and len(attempts) >= 2:
        prev = attempts[-2]
        if prev.get("outcome") == ALL_REJECTED:
            repeated = _guards(last.get("rejections", [])) & _guards(prev.get("rejections", []))
            if repeated:
                return True, (f"every proposed fix was rejected by {', '.join(sorted(repeated))} "
                              f"on two consecutive attempts — the guard's reason was already in "
                              f"the second prompt, so a different shape is not coming")

    # 3. Nothing proposed that was not proposed before.
    proposed = {_key(e) for e in last.get("proposed", [])}
    if proposed:
        seen = set()
        for earlier in attempts[:-1]:
            seen |= {_key(e) for e in earlier.get("proposed", [])}
        if seen and proposed <= seen:
            return True, ("this attempt proposed only edits an earlier attempt already made "
                          "— identical content, so the result would be identical too")

    return False, ""


def render(history: List[Dict[str, Any]]) -> str:
    """The prompt section describing every prior attempt. Empty string when there are none."""
    if not history:
        return ""

    lines = ["\n<previous_fix_attempts>",
             "Everything already tried for this failure, oldest first. Do not repeat any of "
             "it — an edit that was rejected will be rejected again, and one that was applied "
             "is already on disk in the files shown above.", ""]

    for entry in history:
        lines.append(f"### Attempt {entry.get('attempt', '?')} — {_describe(entry)}")
        if entry.get("root_cause"):
            lines.append(f"Diagnosed: {entry['root_cause']}")
        for rejection in entry.get("rejections", []):
            reason = (rejection.get("reason") or "")[:_MAX_REASON_CHARS]
            lines.append(f"REJECTED {rejection.get('file', '?')} — {reason}")
        if entry.get("applied"):
            lines.append("Applied, and the test still failed: "
                         + ", ".join(entry["applied"]))
        lines.append("")

    lines.append("</previous_fix_attempts>")
    return "\n".join(lines) + "\n"


def _describe(entry: Dict[str, Any]) -> str:
    outcome = entry.get("outcome")
    if outcome == NO_EDITS:
        return "the model proposed no edits"
    if outcome == ALL_REJECTED:
        return ("every proposed edit was rejected by a guard and NOTHING reached disk; "
                "the test was not re-run, so the failure below is unchanged")
    if outcome == PASSED:
        return "passed"
    return "the fix was applied and the test still failed"
