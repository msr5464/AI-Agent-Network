"""Read-only utilities for parsing test-authoring-agent audit folders.

The agent writes a fixed set of files to `agents/test-authoring-agent/audit/<session>/`:

    00-session-init.md
    01-parse.json + .md
    02-validate-web.json + .md        (or "skipped")
    02-validate-web.js                (Playwright script, optional)
    03-generate.json + .md
    04-run-and-fix.json + .md
    .fix-passed                        (gate: true | false | skipped)
    05-ship.json + .md
    .verdict                           (APPROVED | NEEDS-REVIEW)

This module is purely read-only — it does not mutate session state in any way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qa_agents_server.paths import AUTHORING_AUDIT_DIR

# Ordered list of (step_key, json_filename, display_name) for the UI progress bar.
STEPS: List[Tuple[str, str, str]] = [
    ("parse", "01-parse.json", "Parse"),
    ("validate_web", "02-validate-web.json", "Validate Web"),
    ("generate", "03-generate.json", "Generate"),
    ("run_and_fix", "04-run-and-fix.json", "Run & Fix"),
    ("ship", "05-ship.json", "Ship"),
]

_SESSION_RE = re.compile(r"^(\d{8}-\d{6})-(create-)?(.+)$")


def _safe_load_json(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def _parse_session_id(session_id: str) -> Dict[str, Optional[str]]:
    """Split a session_id like '20260410-142203-create-payments' into parts."""
    m = _SESSION_RE.match(session_id)
    if not m:
        return {"timestamp": None, "feature": None}
    ts_raw, _, feature = m.groups()
    # ts_raw format: YYYYMMDD-HHMMSS
    try:
        iso = (
            f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]}T"
            f"{ts_raw[9:11]}:{ts_raw[11:13]}:{ts_raw[13:15]}"
        )
    except IndexError:
        iso = None
    return {"timestamp": iso, "feature": feature}


def _derive_status(session_dir: Path, ship_data: Optional[Dict]) -> str:
    """Compute a UI status: running / completed / failed / unknown."""
    if ship_data is not None:
        verdict = ship_data.get("verdict")
        if verdict == "APPROVED":
            return "completed"
        if verdict == "NEEDS-REVIEW":
            return "failed"
    # 05-ship.json missing. Did any step land? If step 1 hasn't appeared,
    # the session likely never got past init.
    parse_json = session_dir / "01-parse.json"
    if not parse_json.exists():
        return "unknown"
    return "in_progress"


def list_sessions(limit: int = 50, offset: int = 0) -> List[Dict]:
    """Return session summaries, newest first."""
    if not AUTHORING_AUDIT_DIR.exists():
        return []

    sessions: List[Dict] = []
    for entry in AUTHORING_AUDIT_DIR.iterdir():
        if not entry.is_dir():
            continue
        ship = _safe_load_json(entry / "05-ship.json")
        parsed = _parse_session_id(entry.name)
        verdict = _read_text(entry / ".verdict")
        fix_gate = _read_text(entry / ".fix-passed")
        status = _derive_status(entry, ship)
        sessions.append({
            "session_id": entry.name,
            "feature": (ship or {}).get("feature") or parsed["feature"],
            "feature_class": (ship or {}).get("feature_class"),
            "started_at": parsed["timestamp"],
            "status": status,
            "verdict": (verdict or "").strip() or None,
            "fix_gate": (fix_gate or "").strip() or None,
            "test_passed": (ship or {}).get("test_passed"),
            "pr_url": (ship or {}).get("pr_url"),
            "files_count": (ship or {}).get("files_count"),
            "timestamp": (ship or {}).get("timestamp"),
        })

    sessions.sort(key=lambda s: s.get("session_id") or "", reverse=True)
    return sessions[offset : offset + limit]


def get_session(session_id: str) -> Optional[Dict]:
    """Return the full contents of an audit session, or None if missing."""
    session_dir = AUTHORING_AUDIT_DIR / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return None

    parsed = _parse_session_id(session_id)
    init_md = _read_text(session_dir / "00-session-init.md")
    parse = _safe_load_json(session_dir / "01-parse.json")
    validate = _safe_load_json(session_dir / "02-validate-web.json")
    generate = _safe_load_json(session_dir / "03-generate.json")
    run_and_fix = _safe_load_json(session_dir / "04-run-and-fix.json")
    ship = _safe_load_json(session_dir / "05-ship.json")
    verdict = (_read_text(session_dir / ".verdict") or "").strip() or None
    fix_gate = (_read_text(session_dir / ".fix-passed") or "").strip() or None

    return {
        "session_id": session_id,
        "feature": (ship or {}).get("feature") or parsed["feature"],
        "started_at": parsed["timestamp"],
        "status": _derive_status(session_dir, ship),
        "verdict": verdict,
        "fix_gate": fix_gate,
        "init_md": init_md,
        "steps": {
            "parse": parse,
            "validate_web": validate,
            "generate": generate,
            "run_and_fix": run_and_fix,
            "ship": ship,
        },
    }


def replay_events(session_id: str) -> Optional[List[Dict]]:
    """Reconstruct an SSE-shape event stream from a completed session.

    Returns a list of events in the same shape as runner.Event.to_dict() so the
    frontend can use the same rendering path for live and historical runs.
    Returns None if the session doesn't exist.
    """
    session_dir = AUTHORING_AUDIT_DIR / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return None

    events: List[Dict] = []
    seq = 0

    def emit(kind: str, data: Dict):
        nonlocal seq
        seq += 1
        events.append({"seq": seq, "kind": kind, "data": data})

    # Initial status event
    parsed = _parse_session_id(session_id)
    emit("status", {
        "session_id": session_id,
        "feature": parsed["feature"],
        "status": "running",
        "started_at": parsed["timestamp"],
    })

    # Each step's presence becomes a 'step' event with status=done
    for key, fname, display in STEPS:
        data = _safe_load_json(session_dir / fname)
        if data is None:
            continue
        emit("step", {
            "key": key,
            "display": display,
            "status": "done",
            "summary": _summarise_step(key, data),
        })

    # Terminal event from ship + verdict
    ship = _safe_load_json(session_dir / "05-ship.json")
    verdict = (_read_text(session_dir / ".verdict") or "").strip() or None
    if ship is not None:
        emit("done", {
            "status": _derive_status(session_dir, ship),
            "verdict": verdict,
            "pr_url": ship.get("pr_url"),
            "test_passed": ship.get("test_passed"),
            "files_count": ship.get("files_count"),
        })
    else:
        emit("done", {
            "status": _derive_status(session_dir, ship),
            "verdict": verdict,
            "pr_url": None,
            "test_passed": None,
            "files_count": None,
        })

    return events


def _summarise_step(key: str, data: Dict) -> Dict:
    """Extract a small, UI-friendly summary of a step's JSON output."""
    if not isinstance(data, dict):
        return {}
    if key == "parse":
        return {
            "test_type": data.get("test_type"),
            "feature": data.get("feature"),
            "existing_module": data.get("existing_module"),
        }
    if key == "validate_web":
        return {
            "skipped": data.get("skipped", False),
            "reason": data.get("reason"),
            "steps_passed": len(data.get("steps_passed") or []),
            "steps_failed": len(data.get("steps_failed") or []),
        }
    if key == "generate":
        return {
            "files_written": len(data.get("files_written") or []),
        }
    if key == "run_and_fix":
        return {
            "test_passed": data.get("test_passed"),
            "attempts": data.get("attempts"),
        }
    if key == "ship":
        return {
            "verdict": data.get("verdict"),
            "pr_url": data.get("pr_url"),
            "branch": data.get("branch"),
            "files_count": data.get("files_count"),
        }
    return {}
