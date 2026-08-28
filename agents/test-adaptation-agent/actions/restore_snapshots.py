#!/usr/bin/env python3
"""
Restore every file this run had edited, from the snapshots the adapt step wrote.

Called by run.sh's ERR trap. Healing can skip this because it edits one file and
restores it inline; this agent applies a whole change item across several files
before it verifies anything, so a crash midway leaves the automation repo in a
state that is neither the original nor a working change — and that checkout is
shared with every other agent run on this machine.

Reads:  $AUDIT_DIR/.snapshots.json  ({path: original_text})
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from shared.log import log as _log
def log(msg): _log("restore", msg)

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])


def main() -> int:
    path = AUDIT_DIR / ".snapshots.json"
    if not path.exists():
        return 0
    try:
        snapshots = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log(f"could not read snapshots: {exc}")
        return 1

    failures = 0
    for target, original in (snapshots or {}).items():
        try:
            Path(target).write_text(original, encoding="utf-8")
            log(f"restored {Path(target).name}")
        except OSError as exc:
            failures += 1
            log(f"FAILED to restore {target}: {exc}")
    path.unlink(missing_ok=True)
    if failures:
        log(f"{failures} file(s) could not be restored — inspect the repo by hand")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
