#!/usr/bin/env python3
"""Record that a person disagrees with (or confirms) how triaging classified a failure.

Triaging's verdicts end in an HTML report and a Slack message, neither of which can
answer back, so a wrong PRODUCT_BUG or AUTOMATION_ISSUE left no trace anywhere. This
records the correction next to what the agent said, read from the triaging session
for that build when it is on this machine (shared/feedback.py).

    python3 scripts/reclassify.py --build-tag ProdSanity-All-Tests-901 \\
        --test CartTest.checkout --as PRODUCT_BUG --category ASSERTION_FAILURE

It records and nothing else: a handoff already sent to healing stays where it is.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from shared import feedback  # noqa: E402

AUDIT = REPO_ROOT / "agents" / "test-triaging-agent" / "audit"
CLASSES = ("PRODUCT_BUG", "AUTOMATION_ISSUE", "UNKNOWN")


def _same_test(recorded: str, asked: str) -> bool:
    """`Class#method`, `Class.method` or a package-qualified name all name one test."""
    a = recorded.replace("#", ".").lower()
    b = asked.replace("#", ".").lower()
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def find(build_tag: str, test: str, audit: Path = AUDIT) -> Tuple[str, Optional[Dict]]:
    """The newest triaging session for the build, and its classification of the test."""
    try:
        sessions = sorted(d for d in audit.iterdir()
                          if d.is_dir() and d.name.endswith(f"-{build_tag}"))
    except OSError:
        return "", None
    for session in reversed(sessions):
        try:
            data = json.loads((session / "03-classify.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in data.get("classifications") or []:
            if _same_test(entry.get("test_name", ""), test):
                return session.name, entry
        return session.name, None
    return "", None


def main(argv: Optional[List[str]] = None, audit: Path = AUDIT) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-tag", required=True)
    ap.add_argument("--test", required=True, help="Class#method, as the report names it")
    ap.add_argument("--as", dest="to", required=True, choices=CLASSES,
                    help="what the failure really is")
    ap.add_argument("--category", default="",
                    help="optional root-cause category, e.g. ASSERTION_FAILURE or TIMEOUT")
    ap.add_argument("--note", default="", help="why")
    args = ap.parse_args(argv)
    category = args.category.strip().upper()

    session, entry = find(args.build_tag, args.test, audit)
    said = {"classification": (entry or {}).get("classification", ""),
            "category": (entry or {}).get("root_cause_category", ""),
            "confidence": (entry or {}).get("confidence", "")}
    if not session:
        print(f"Note: no triaging session for {args.build_tag} on this machine; "
              f"recording without what the agent said.")
    elif entry is None:
        print(f"Note: session {session} has no classification for {args.test}; "
              f"recording without what the agent said.")
    agrees = bool(entry) and said["classification"] == args.to and \
        (not category or category == said["category"])

    written = feedback.append([feedback.event(
        "reclassification", source="cli", explicit=True,
        key=(args.build_tag, args.test, args.to, category, feedback.now()),
        agent="test-triaging-agent", session=session,
        artifact={"build_tag": args.build_tag, "test": args.test,
                  "agent_said": said, "person_said": {"classification": args.to,
                                                      "category": category},
                  "agrees": agrees},
        text=args.note.strip(), who=getpass.getuser(), at=feedback.now())])
    if written:
        verb = "Confirmed" if agrees else "Recorded"
        was = f" (agent said {said['classification']}" + \
              (f", {said['category']}" if said["category"] else "") + ")" if entry else ""
        print(f"{verb} {args.test} in {args.build_tag} as {args.to}"
              + (f", {category}" if category else "") + was + f" → {feedback.store_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
