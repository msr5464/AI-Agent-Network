#!/usr/bin/env python3
"""Record a person's verdict on one agent run, for runs reviewed outside GitHub.

A PR's verdict block is the usual way (shared/feedback.py). This is the way for a
run with no PR to tick: a CI or dry run, and adaptation's proposals, which until
now nothing could mark as accepted or rejected.

    python3 scripts/record_verdict.py --agent test-adaptation-agent \\
        --session 20261012-101500-adapt-checkout --verdict edited --reason convention

Each call adds one event; when a run is judged twice, the newest verdict stands.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from shared import feedback  # noqa: E402

AGENTS_DIR = REPO_ROOT / "agents"


def _reasons(raw: List[str]) -> List[str]:
    reasons = [r.strip() for value in raw for r in value.split(",") if r.strip()]
    unknown = [r for r in reasons if r not in feedback.REASONS]
    if unknown:
        raise SystemExit(f"unknown reason(s): {', '.join(unknown)} "
                         f"(choose from {', '.join(feedback.REASONS)})")
    return reasons


def main(argv: Optional[List[str]] = None, agents_dir: Path = AGENTS_DIR) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, choices=feedback.AGENTS)
    ap.add_argument("--session", required=True, help="the run's session id")
    ap.add_argument("--verdict", required=True, choices=list(feedback.VERDICTS))
    ap.add_argument("--reason", action="append", default=[],
                    help=f"repeatable or comma-separated: {', '.join(feedback.REASONS)}")
    ap.add_argument("--item", type=int, default=None,
                    help="adaptation only: the change item judged (default: all of them)")
    ap.add_argument("--note", default="", help="what the reviewer wants remembered")
    args = ap.parse_args(argv)
    reasons = _reasons(args.reason)

    audit = agents_dir / args.agent / "audit" / args.session
    if not audit.is_dir():
        print(f"Note: no audit folder at {audit}; recording the verdict anyway "
              f"(audit folders are local and may have been cleared).")

    result = feedback.person_verdict(args.agent, args.session, args.verdict, reasons,
                                     item=args.item, note=args.note, who=getpass.getuser(),
                                     source="cli", agents_dir=agents_dir)
    print(f"Recorded {args.verdict} for {args.agent} session {args.session}"
          + (f" (item {args.item})" if args.item is not None else "")
          + (f", reasons: {', '.join(reasons)}" if reasons else "")
          + f" → {feedback.store_path()}")
    if args.agent == "test-adaptation-agent":
        judged = result["judged_proposals"]
        print(f"Judged {judged} proposal(s) of that session." if judged else
              "No proposals were recorded for that session (it ran with "
              "ADAPTATION_APPLY=true, or proposed nothing).")
    if result["wrong_diagnosis"]:
        print("Also recorded as a wrong diagnosis (false LOCATOR_STALE) in "
              "test-healing-agent/feedback/known-issues.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
