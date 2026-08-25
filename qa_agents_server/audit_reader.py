"""Read-only utilities for parsing test-authoring-agent audit folders.

The agent writes a fixed set of files to `agents/test-authoring-agent/audit/<session>/`:

    00-session-init.md
    01-parse.json + .md
    02-validate-api.json + .md        (or "skipped" — runs alongside validate-web)
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
import os
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qa_agents_server.paths import AUTHORING_AUDIT_DIR
from qa_agents_server.agents import DEFAULT_AGENT, get_agent

# Ordered list of (step_key, json_filename, display_name) for the UI progress bar.
# display_name for "validate_web" is just "Validate" (not "Validate Web") because
# this slot's badge in the frontend covers BOTH validate_web AND validate_api —
# they run under the same numbered step-2 slot (see run.sh) but only validate_web
# is tracked here for live progress/SSE events; validate_api is exposed separately,
# History-modal-only, via get_session()'s "steps" dict below. Keep this display
# name in sync with AI-Test-Studio/frontend/customer/index.html's STEP_LABELS.
# The authoring step model, kept here for backwards compatibility. Per-agent
# steps live in qa_agents_server/agents.py — read them via get_agent(name).steps.
STEPS: List[Tuple[str, str, str]] = get_agent(DEFAULT_AGENT).steps

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
    if "error" in data or data.get("status") == "failed":
        return True
    # 02-validate-web.json's own status vocabulary (ok/timeout/error/empty/
    # skipped) never uses the literal string "failed" — without this, a
    # validation that crashed, timed out, or came back completely empty
    # showed up identically to a normal successful step everywhere this
    # helper is used (session status, per-step status in replay_events).
    if data.get("status") in ("timeout", "error", "empty"):
        return True
    # 04-run-and-fix.json has no "status"/"error" key at all — its own
    # vocabulary is "passed"/"skipped"/"stuck". A test that's still failing
    # (passed=False) and wasn't deliberately skipped for an infra reason is a
    # genuine failure for this step — "stuck" included, since that's still a
    # test that never passed. Without this, the Run & Fix badge showed
    # "done" (green) for a test that never actually passed.
    if data.get("passed") is False and not data.get("skipped"):
        return True
    # 05-ship.json also has no "status"/"error" key — its vocabulary is
    # "verdict"/"ship_status". A push/PR failure is a genuine hard failure
    # for THIS step even when the underlying test passed fine (verdict alone
    # can't distinguish that from a plain test failure — see 05_ship.py).
    # Without this, the Ship badge showed "done" (green) for a run that had
    # just failed to ship at all — the exact bug this fixes.
    if data.get("ship_status") in ("push_failed", "pr_failed"):
        return True
    return False


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
    # The server stopped this run when it shut down or reaped it on boot.
    if (session_dir / ".interrupted").exists():
        return "interrupted"

    # No ship.json — check if any step JSON carries an error flag.
    for _, fname, _ in STEPS:
        data = _safe_load_json(session_dir / fname)
        if data is not None and _step_has_error(data):
            return "failed"

    # Session init file exists → run started, just hasn't finished step 1 yet.
    # But a session nothing has written to in a long time is not still running —
    # a killed server or a crashed agent leaves one behind, and reporting it as
    # "running" forever is wrong (sessions from months earlier still showed as
    # live). The healing path already made this distinction; authoring did not.
    if (session_dir / "00-session-init.md").exists():
        return "interrupted" if _looks_abandoned(session_dir) else "running"
    parse_json = session_dir / "01-parse.json"
    if not parse_json.exists():
        return "unknown"
    return "interrupted" if _looks_abandoned(session_dir) else "in_progress"


def list_sessions(limit: int = 50, offset: int = 0,
                  agent: str = DEFAULT_AGENT) -> List[Dict]:
    """Return session summaries for one agent, newest first."""
    spec = get_agent(agent)
    if spec.name != DEFAULT_AGENT:
        return _list_healing_sessions(spec, limit, offset)

    if not spec.audit_dir.exists():
        return []

    sessions: List[Dict] = []
    for entry in spec.audit_dir.iterdir():
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


def get_session(session_id: str, agent: str = DEFAULT_AGENT) -> Optional[Dict]:
    """Return the full contents of an audit session, or None if missing."""
    spec = get_agent(agent)
    if spec.name != DEFAULT_AGENT:
        return _get_healing_session(spec, session_id)
    session_dir = spec.audit_dir / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return None

    parsed = _parse_session_id(session_id)
    init_md = _read_text(session_dir / "00-session-init.md")
    parse = _safe_load_json(session_dir / "01-parse.json")
    validate = _safe_load_json(session_dir / "02-validate-web.json")
    validate_api = _safe_load_json(session_dir / "02-validate-api.json")
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
            # Runs alongside validate_web in the SAME numbered step-2 slot (see
            # run.sh) — not in STEPS (progress-bar polling stays on the 5-step
            # model), but exposed here so the History detail view can show it.
            "validate_api": validate_api,
            "generate": generate,
            "run_and_fix": run_and_fix,
            "ship": ship,
        },
    }


def replay_events(session_id: str,
                  agent: str = DEFAULT_AGENT) -> Optional[List[Dict]]:
    """Reconstruct an SSE-shape event stream from a completed session.

    Returns a list of events in the same shape as runner.Event.to_dict() so the
    frontend can use the same rendering path for live and historical runs.
    Returns None if the session doesn't exist.

    Agent-aware: the audit directory, the step list and the terminal event all
    come from the agent's own spec. Without that, replay only ever found
    authoring sessions, so a finished healing run's Logs link 404ed and its
    output could not be shown in the live card at all.
    """
    spec = get_agent(agent)
    session_dir = spec.audit_dir / session_id
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
    for key, fname, display in spec.steps:
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

    if spec.name != DEFAULT_AGENT:
        # Healing: reuse the same summary the history table and the live card's
        # own result panel read, so a replayed run reports exactly what a live
        # one did rather than a second, drifting derivation of "what happened".
        summary = _healing_summary(spec, session_dir)
        emit("done", {
            "status": summary.get("status"),
            "verdict": summary.get("fix_gate"),
            "pr_url": summary.get("pr_url"),
            "test_passed": None,
            "files_count": None,
            "ship_status": None,
            "ship_detail": summary.get("failure_headline"),
            "duration_s": _compute_duration_s(
                session_id,
                _safe_load_json(session_dir / "02-ship.json")
                or _safe_load_json(session_dir / "01-fix.json")
                or _safe_load_json(session_dir / "00-reproduce.json")),
        })
        return events

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
            "ship_status": ship.get("ship_status"),
            "ship_detail": ship.get("ship_detail"),
            "duration_s": duration_s,
        })
    else:
        emit("done", {
            "status": _derive_status(session_dir, ship),
            "verdict": verdict,
            "pr_url": None,
            "test_passed": None,
            "files_count": None,
            "ship_status": None,
            "ship_detail": None,
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
            "status": data.get("status"),
            "attempts": data.get("attempts"),
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
            "ship_status": data.get("ship_status"),
            "ship_detail": data.get("ship_detail"),
        }
    return {}


# ── Healing sessions ──────────────────────────────────────────────────────────
#
# Deliberately reads the same fields scripts/audit_viewer.py::_get_healing_session
# reads — succeeded / unverified / failed / distinct_fixes — so the two dashboards
# cannot drift apart and disagree about the same run.

def _healing_counts(fix_data: Dict) -> Dict[str, int]:
    return {
        "distinct_fixes": fix_data.get("distinct_fixes", len(fix_data.get("fixes", []))),
        "tests_fixed": fix_data.get("succeeded", 0),
        "tests_unverified": fix_data.get("unverified", 0),
        "tests_failed": fix_data.get("failed", 0),
    }


# A session whose process died leaves no gate file, so it reads as "running"
# forever and the history table fills with phantom rows. Nothing here can see the
# process table, so fall back to staleness: untouched for this long with no
# verdict means it is not running, it is abandoned.
# How long a session can go untouched before it is treated as abandoned
# rather than running. Applies to both agents (the old healing-specific
# name is still honoured so existing deployments keep working).
_DEFAULT_STALE_AFTER_SECONDS = 900


def _stale_after() -> int:
    """Read per call so the admin Agent Settings page can change it without a restart."""
    try:
        return int(
            os.environ.get("QA_AGENT_STALE_AFTER_SECONDS")
            or os.environ.get("QA_HEALING_STALE_AFTER_SECONDS")
            or _DEFAULT_STALE_AFTER_SECONDS
        )
    except ValueError:
        return _DEFAULT_STALE_AFTER_SECONDS


def _looks_abandoned(session_dir: Path) -> bool:
    try:
        newest = max((f.stat().st_mtime for f in session_dir.iterdir()), default=0.0)
    except OSError:
        return False
    return bool(newest) and (time.time() - newest) > _stale_after()


# What the gate file says is an internal value; "skipped" collapses three very
# different outcomes into one word. Split them by what the reproduce step found.
_SHAPE_STATUS = {
    "passing": "nothing_to_fix",
    "ASSERTION": "not_a_locator",
    "UNKNOWN": "not_a_locator",
}


def _skipped_status(shape: str) -> str:
    if not shape:
        return "skipped"                      # pipeline run with nothing eligible
    if shape.startswith("INFRA"):
        return "blocked"                      # could not even run the test
    return _SHAPE_STATUS.get(shape, "not_a_locator")


def _healing_status(session_dir: Path, fix_gate: Optional[str],
                    ship_data: Optional[Dict], counts: Dict[str, int],
                    shape: str = "") -> str:
    if (ship_data or {}).get("pr_url"):
        return "pr_created"
    if (session_dir / ".crashed").exists():
        return "crashed"
    if (session_dir / ".cancelled").exists():
        return "cancelled"
    if (session_dir / ".interrupted").exists():
        return "interrupted"
    gate = (fix_gate or "").strip()
    if gate == "true":
        # A run where nothing could be verified is not a success, even though
        # the gate passed — mirror how the PR body reports it.
        return "unverified" if counts["tests_unverified"] and not counts["tests_fixed"] else "fixed"
    if gate == "false":
        return "needs_review"
    if gate == "skipped":
        return _skipped_status(shape)
    if gate:
        return "unknown"
    return "interrupted" if _looks_abandoned(session_dir) else "running"


def _healing_summary(spec, session_dir: Path) -> Dict:
    reproduce = _safe_load_json(session_dir / "00-reproduce.json")
    fix = _safe_load_json(session_dir / "01-fix.json") or {}
    ship = _safe_load_json(session_dir / "02-ship.json")
    fix_gate = (_read_text(session_dir / ".fix-passed") or "").strip() or None
    counts = _healing_counts(fix)
    parsed = _parse_session_id(session_dir.name)
    shape = (reproduce or {}).get("status") or ""

    # The failure shape is what decides whether the UI offers "Try anyway", so it
    # belongs in the summary rather than only inside the reproduce step blob.
    return {
        "session_id": session_dir.name,
        "agent": spec.name,
        "failure_shape": shape,
        "failure_headline": (reproduce or {}).get("headline", ""),
        "forced": bool((reproduce or {}).get("forced")),
        # A run the gate stopped: not a locator problem, nothing was attempted.
        "gate_stopped": bool(shape) and shape not in ("queued", "passing"),
        # `module` keeps the shape the frontend already renders for authoring.
        "module": fix.get("build_tag") or (reproduce or {}).get("test_name") or parsed["module"],
        "build_tag": fix.get("build_tag"),
        "test_name": (reproduce or {}).get("test_name"),
        "mode": "standalone" if reproduce else "pipeline",
        "started_at": parsed["timestamp"],
        "status": _healing_status(session_dir, fix_gate, ship, counts, shape),
        "fix_gate": fix_gate,
        "pr_url": (ship or {}).get("pr_url"),
        "timestamp": (ship or fix or {}).get("timestamp"),
        **counts,
    }


def _list_healing_sessions(spec, limit: int, offset: int) -> List[Dict]:
    if not spec.audit_dir.exists():
        return []
    sessions = [_healing_summary(spec, entry)
                for entry in spec.audit_dir.iterdir() if entry.is_dir()]
    sessions.sort(key=lambda s: s.get("session_id") or "", reverse=True)
    return sessions[offset: offset + limit]


def _get_healing_session(spec, session_id: str) -> Optional[Dict]:
    session_dir = spec.audit_dir / session_id
    if not session_dir.is_dir():
        return None

    summary = _healing_summary(spec, session_dir)
    summary["init_md"] = _read_text(session_dir / "00-session-init.md")
    summary["steps"] = {
        "reproduce": _safe_load_json(session_dir / "00-reproduce.json"),
        "fix": _safe_load_json(session_dir / "01-fix.json"),
        "ship": _safe_load_json(session_dir / "02-ship.json"),
    }
    # Markdown reports are what a human actually wants to read in the detail view.
    # The raw console is deliberately NOT included: it is served incrementally by
    # /run/<sid>/stream into the Live Run card, so shipping a second copy here
    # made every row click pay for the whole log — and a real run's maven output
    # is the largest thing in the session by far.
    summary["reports"] = {
        "reproduce_md": _read_text(session_dir / "00-reproduce.md"),
        "fix_md": _read_text(session_dir / "01-fix.md"),
        "ship_md": _read_text(session_dir / "02-ship.md"),
    }
    return summary
