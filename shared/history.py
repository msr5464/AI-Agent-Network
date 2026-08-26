"""What this test has done recently, and whether its failures are intermittent.

Every other channel looks at one run. This one looks across runs, which is the
only way to answer the question that separates "broken" from "flaky": has this
test recovered before without anyone changing the code?

Two sources, both already produced and neither previously reaching the diagnosis:

  * triaging's flaky detection, which computes `{test_name, failure_count,
    last_days, in_current_run}` for the whole build and today stops at the HTML
    report;
  * the healing agent's own audit sessions, for standalone runs with no triaging
    upstream — the same test's earlier verdicts and outcomes.

Nothing here is decisive on its own, and it must never outrank a structural
finding. "It works sometimes" is what you conclude when nothing explains the
failure, not before you have looked.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

# Below this many recorded failures there is no pattern, only an incident.
_MIN_OBSERVATIONS = 2


def _simple_name(test_name: str) -> str:
    """`pkg.Class.method` → `Class.method`, so the two sources can be compared."""
    parts = (test_name or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (test_name or "")


def from_flaky_records(test_name: str, flaky_tests: List[Dict]) -> Dict:
    """Read triaging's flaky detection for one test."""
    result = {"available": False, "intermittent": False, "failure_count": 0,
              "window_days": 0, "source": "triage-flaky"}
    if not flaky_tests or not test_name:
        return result
    wanted = _simple_name(test_name)
    for record in flaky_tests:
        if _simple_name(record.get("test_name", "")) != wanted:
            continue
        result["available"] = True
        result["failure_count"] = record.get("failure_count", 0) or 0
        result["window_days"] = record.get("last_days", 0) or 0
        # A test that fails every single run in the window is broken, not flaky.
        # Triaging only records tests that failed *some* of the time, so presence
        # here plus more than one observation is the signal.
        result["intermittent"] = result["failure_count"] >= _MIN_OBSERVATIONS
        return result
    return result


def from_audit_sessions(test_name: str, audit_dir, limit: int = 25) -> Dict:
    """Earlier verdicts for this test in the healing agent's own sessions.

    Used when a run has no triaging upstream. A test that has been diagnosed
    differently on different days, or that stopped failing without a fix being
    shipped, is behaving intermittently.
    """
    result = {"available": False, "intermittent": False, "runs": 0,
              "verdicts": [], "source": "audit-history"}
    if not test_name or not audit_dir or not Path(audit_dir).exists():
        return result

    wanted = _simple_name(test_name)
    sessions = sorted((d for d in Path(audit_dir).iterdir() if d.is_dir()),
                      key=lambda d: d.name, reverse=True)[:limit]
    verdicts: List[str] = []
    for session in sessions:
        record = session / "00-reproduce.json"
        if not record.exists():
            continue
        try:
            data = json.loads(record.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        if _simple_name(data.get("test_name", "")) != wanted:
            continue
        result["runs"] += 1
        status = data.get("status", "")
        if status:
            verdicts.append(status)

    result["available"] = result["runs"] > 0
    result["verdicts"] = verdicts[:10]
    # "passing" among the recent history means it has recovered on its own before.
    # Differing verdicts across runs mean the failure is not reproducing the same
    # way twice, which is the same story told differently.
    distinct = {v for v in verdicts if v not in ("queued",)}
    result["intermittent"] = ("passing" in distinct and len(distinct) > 1) or len(distinct) > 2
    return result


def load(test_name: str, flaky_tests: Optional[List[Dict]] = None,
         audit_dir=None) -> Dict:
    """Combine both sources. Triaging's record wins where it exists."""
    triage = from_flaky_records(test_name, flaky_tests or [])
    if triage["available"]:
        return triage
    return from_audit_sessions(test_name, audit_dir)


def describe(history: Dict) -> str:
    """One line for a log or prompt. Empty when there is no history to speak of."""
    if not history.get("available"):
        return ""
    if history.get("source") == "triage-flaky":
        return (f"failed {history['failure_count']} time(s) in the last "
                f"{history['window_days']} days"
                + (" — recorded as intermittent" if history["intermittent"] else ""))
    return (f"{history['runs']} earlier run(s) of this test"
            + (f", verdicts: {', '.join(history['verdicts'][:4])}"
               if history.get("verdicts") else "")
            + (" — behaving intermittently" if history["intermittent"] else ""))
