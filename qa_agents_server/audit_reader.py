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
from datetime import datetime, timezone
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
        return {"timestamp": None, "module": None}
    ts_raw, _, feature = m.groups()
    # ts_raw format: YYYYMMDD-HHMMSS
    try:
        iso = (
            f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]}T"
            f"{ts_raw[9:11]}:{ts_raw[11:13]}:{ts_raw[13:15]}"
        )
    except IndexError:
        iso = None
    return {"timestamp": iso, "module": feature}


def _step_has_error(data: Optional[Dict]) -> bool:
    """Return True if a step JSON signals a hard failure."""
    if not isinstance(data, dict):
        return False
    return "error" in data or data.get("status") == "failed"


def _derive_status(session_dir: Path, ship_data: Optional[Dict]) -> str:
    """Compute a UI status: running / completed / failed / cancelled / unknown."""
    if ship_data is not None:
        verdict = ship_data.get("verdict")
        if verdict == "APPROVED":
            return "completed"
        if verdict == "NEEDS-REVIEW":
            return "failed"

    # Explicit cancellation marker written by runner._wait_and_reap
    if (session_dir / ".cancelled").exists():
        return "cancelled"

    # No ship.json — check if any step JSON carries an error flag.
    for _, fname, _ in STEPS:
        data = _safe_load_json(session_dir / fname)
        if data is not None and _step_has_error(data):
            return "failed"

    # Session init file exists → run started, just hasn't finished step 1 yet
    if (session_dir / "00-session-init.md").exists():
        return "running"
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
            "module": (ship or {}).get("feature") or parsed["module"],
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
        "module": (ship or {}).get("feature") or parsed["module"],
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
        "module": parsed["module"],
        "status": "running",
        "started_at": parsed["timestamp"],
    })

    # Replay persisted stdout lines (written by runner._stdout_reader)
    stdout_log = session_dir / "stdout.log"
    if stdout_log.exists():
        try:
            for line in stdout_log.read_text(errors="replace").splitlines():
                emit("stdout", {"line": line})
        except OSError:
            pass

    # Each step's presence becomes a 'step' event — status reflects error if present
    for key, fname, display in STEPS:
        data = _safe_load_json(session_dir / fname)
        if data is None:
            continue
        step_status = "failed" if _step_has_error(data) else "done"
        emit("step", {
            "key": key,
            "display": display,
            "status": step_status,
            "summary": _summarise_step(key, data),
        })

    # Terminal event from ship + verdict
    ship = _safe_load_json(session_dir / "05-ship.json")
    verdict = (_read_text(session_dir / ".verdict") or "").strip() or None
    duration_s = _compute_duration_s(session_id, ship)
    if ship is not None:
        emit("done", {
            "status": _derive_status(session_dir, ship),
            "verdict": verdict,
            "pr_url": ship.get("pr_url"),
            "test_passed": ship.get("test_passed"),
            "files_count": ship.get("files_count"),
            "duration_s": duration_s,
        })
    else:
        emit("done", {
            "status": _derive_status(session_dir, ship),
            "verdict": verdict,
            "pr_url": None,
            "test_passed": None,
            "files_count": None,
            "duration_s": duration_s,
        })

    return events


def _compute_duration_s(session_id: str, ship_data: Optional[Dict]) -> Optional[float]:
    """Return wall-clock duration in seconds from session start to ship timestamp."""
    if not ship_data:
        return None
    end_ts = ship_data.get("timestamp")
    if not end_ts:
        return None
    m = re.match(r'^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})', session_id)
    if not m:
        return None
    try:
        yr, mo, dy, hh, mm, ss = (int(x) for x in m.groups())
        naive_local = datetime(yr, mo, dy, hh, mm, ss)
        start_utc = naive_local.astimezone(timezone.utc)
        end_utc = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
        secs = (end_utc - start_utc).total_seconds()
        return secs if secs >= 0 else None
    except Exception:
        return None


def _summarise_step(key: str, data: Dict) -> Dict:
    """Extract a small, UI-friendly summary of a step's JSON output."""
    if not isinstance(data, dict):
        return {}
    if key == "parse":
        return {
            "test_type": data.get("test_type"),
            "module": data.get("feature"),
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
