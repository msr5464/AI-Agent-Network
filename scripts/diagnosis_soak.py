#!/usr/bin/env python3
"""Analyse a shadow-mode soak: what the diagnosis said, against what happened.

`DIAGNOSIS_MODE=shadow` makes the engine report a verdict and change nothing, so
its judgement can be checked against reality before it is allowed to refuse work.
Running that for a week produces a pile of audit sessions and no answer — this
turns them into the two numbers that decide whether to enforce:

  false stops        a verdict that would have blocked a fix that then worked
  false LOCATOR_STALE  a fix attempted on a page the test never reached

They are not symmetric. A false stop is visible and recoverable — someone passes
FORCE and moves on. A false LOCATOR_STALE edits a selector on the wrong page, and
the test suite cannot tell you it happened.

    python3 scripts/diagnosis_soak.py [--agent test-healing-agent] [--since 20260801]

Only the healing agent is walked. The table pairs a verdict with what became of
the fix, and triaging never attempts one — its sessions have a verdict column and
nothing to put beside it, which would pad the table without informing it.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared import verdict_feedback


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None


def read_session(session_dir: Path) -> dict | None:
    """One session's verdict and what actually became of it."""
    reproduce = _load(session_dir / "00-reproduce.json") or {}
    fix = _load(session_dir / "01-fix.json") or {}
    ship = _load(session_dir / "02-ship.json") or {}
    diagnosis = reproduce.get("diagnosis") or {}

    verdict = diagnosis.get("verdict") or ""
    if not verdict:
        for entry in (fix.get("failed_fixes") or []):
            verdict = (entry.get("diagnosis") or {}).get("verdict") or verdict
    if not verdict:
        for entry in (fix.get("fixes") or []):
            verdict = (entry.get("diagnosis") or {}).get("verdict") or verdict
    if not verdict:
        return None

    fixes = fix.get("fixes") or []
    failed = fix.get("failed_fixes") or []
    if fixes:
        outcome = "fix_verified"
    elif any(e.get("status") == "test_failed" for e in failed):
        outcome = "fix_reverted"
    elif fix.get("unverified"):
        outcome = "fix_unverified"
    else:
        outcome = "no_fix"

    return {
        "session": session_dir.name,
        "verdict": verdict,
        "confidence": diagnosis.get("confidence", ""),
        "outcome": outcome,
        "forced": bool(reproduce.get("forced")),
        "pr": bool(ship.get("pr_url")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", default="test-healing-agent")
    parser.add_argument("--since", default="",
                        help="only sessions whose id sorts at or after this (e.g. 20260801)")
    args = parser.parse_args()

    audit = REPO_ROOT / "agents" / args.agent / "audit"
    if not audit.exists():
        print(f"No audit directory at {audit}")
        return 1

    rows = []
    for session in sorted(d for d in audit.iterdir() if d.is_dir()):
        if args.since and session.name < args.since:
            continue
        row = read_session(session)
        if row:
            rows.append(row)

    if not rows:
        print("No sessions carrying a diagnosis yet — run with DIAGNOSIS_MODE=shadow "
              "and come back.")
        return 0

    print(f"{len(rows)} session(s) with a recorded verdict\n")

    matrix = defaultdict(Counter)
    for row in rows:
        matrix[row["verdict"]][row["outcome"]] += 1

    outcomes = ["fix_verified", "fix_reverted", "fix_unverified", "no_fix"]
    width = max(len(v) for v in matrix) + 2
    print(f"{'verdict':<{width}}" + "".join(f"{o:>16}" for o in outcomes))
    for verdict in sorted(matrix):
        counts = matrix[verdict]
        print(f"{verdict:<{width}}" + "".join(f"{counts[o]:>16}" for o in outcomes))

    # A stop verdict that was overridden and then produced a working fix was wrong.
    false_stops = [r for r in rows
                   if r["forced"] and r["outcome"] == "fix_verified"]
    # A locator fix that could not survive its own verification was aimed at the
    # wrong thing — most often a page the test never reached.
    false_locator = [r for r in rows
                     if r["verdict"] == "LOCATOR_STALE" and r["outcome"] == "fix_reverted"]

    print(f"\nfalse stops           : {len(false_stops)}")
    for row in false_stops[:5]:
        print(f"    {row['session']}  ({row['verdict']})")
    print(f"false LOCATOR_STALE   : {len(false_locator)}")
    for row in false_locator[:5]:
        print(f"    {row['session']}")

    # Sessions are inferred evidence. feedback/known-issues.json is confirmed
    # evidence: a human overrode a verdict and the fix then worked. Both are shown
    # because they disagree in useful ways — a confirmed false stop that the
    # session scan missed means the scan's heuristic needs widening.
    feedback_file = REPO_ROOT / "agents" / args.agent / "feedback" / "known-issues.json"
    confirmed = verdict_feedback.summarize(feedback_file)
    if confirmed:
        print("\nconfirmed by a human (feedback/known-issues.json):")
        for kind, count in sorted(confirmed.items()):
            print(f"    {kind:<22} {count}")
    else:
        print("\nconfirmed by a human   : none recorded yet")

    confident_stops = sum(1 for r in rows
                          if r["confidence"] == "HIGH" and r["outcome"] == "no_fix")
    print(f"\nHIGH-confidence stops : {confident_stops}")
    print("\nEnforce when false stops is 0 and false LOCATOR_STALE is not rising.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
