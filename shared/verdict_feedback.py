"""Record where a diagnosis turned out to be wrong.

Rule thresholds cannot be tuned against intuition. The two mistakes that matter
point in opposite directions and are invisible unless someone writes them down:

  * a **false stop** — the agent refused to attempt a fix that would have worked.
    Visible when a human passes FORCE=true and the forced fix then verifies.
  * a **false LOCATOR_STALE** — the agent edited a selector on a page the test
    never reached. Visible when a shipped fix is later reverted, or when the same
    test fails again on the same element straight after a "successful" fix.

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
