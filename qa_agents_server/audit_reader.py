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
from qa_agents_server import metrics_reader
from qa_agents_server.agents import (AGENTS, AgentConfigError, DEFAULT_AGENT,
                                     get_agent)

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

# A session id is '<stamp>-<verb>-<slug>', where <verb> is the AgentSpec's
# session_prefix. This used to strip only 'create-', so an adaptation session
# parsed as feature='adapt-checkout' and a healing one as 'fix-LoginTest' —
# invisible while the verb was only ever a fallback, and wrong the moment
# anything displayed it. The alternation is built from the specs so a fourth
# agent cannot forget to add itself; the group stays optional so an id that
# does not carry a known verb still yields its timestamp rather than nothing.
_SESSION_VERBS = "|".join(sorted(
    (re.escape(spec.session_prefix) for spec in AGENTS.values()),
    key=len, reverse=True))
_SESSION_RE = re.compile(r"^(\d{8}-\d{6})-(?:(" + _SESSION_VERBS + r")-)?(.+)$")


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
    # 03-explore.json's vocabulary again differs: exploration that could not use
    # a session, performed something it should have refused, or reached nothing
    # is a hard failure for that step even though it produced a valid file.
    # "partial" deliberately is NOT here — a flow map that recorded most of a
    # journey is a usable result, not a failed step.
    if data.get("status") in ("unsafe", "no_session", "unreachable"):
        return True
    # 01-fix.json's counts can't tell a failed fix from a diagnosis-only run
    # (both have failed > 0), so read the gate the step wrote. Without this, a
    # fix that repaired nothing showed "done" (green).
    if data.get("fix_gate") == "false":
        return True
    # 04-adapt.json has no "status" key at all — its vocabulary is per item.
    # A step that wrote a valid file in which every item failed showed "done"
    # (green) in the UI, the same way Run & Fix used to. `escalated` and
    # `declined` are deliberate outcomes and deliberately not counted here.
    if any(isinstance(item, dict) and item.get("status") == "failed"
           for item in (data.get("items") or [])):
        return True
    return False


def _step_unfinished(data: Optional[Dict]) -> bool:
    """Whether the step wrote this file mid-way and is still working.

    A step that run.sh retries rewrites its own file on every attempt, and a
    mid-run attempt legitimately ends with a failing gate — some tests repaired,
    others still being worked on. Judging the file the moment it changed painted
    the step red while the next attempt was starting. `final_attempt` is the
    step's own statement that run.sh is done with it; a step that does not write
    the key never retries, so its absence means finished.
    """
    return isinstance(data, dict) and data.get("final_attempt") is False


def _step_status(data: Optional[Dict], finished: bool = False) -> str:
    """Chip state for one step's file: running, failed, skipped, or done.

    `finished` means the process that writes the file has exited. A snapshot
    that promised another attempt is then the last word after all — a run
    cancelled or crashed between attempts otherwise leaves the chip spinning.
    """
    if not finished and _step_unfinished(data):
        return "running"
    if _step_has_error(data):
        return "failed"
    return "skipped" if isinstance(data, dict) and data.get("status") == "skipped" else "done"


def _derive_status(session_dir: Path, ship_data: Optional[Dict],
                   steps: Optional[List[Tuple[str, str, str]]] = None) -> str:
    """Compute a UI status: running / completed / diagnosed / failed / cancelled / unknown.

    `steps` defaults to the authoring model for backwards compatibility; callers
    that know their agent should pass `spec.steps` so a third agent's step files
    are the ones actually checked for an error flag.
    """
    # A run that identified why a test fails, and correctly declined to edit
    # anything, has succeeded at its job. Reporting it red trains people to
    # ignore exactly the runs worth reading.
    if (_read_text(session_dir / ".skip-reason") or "").strip() in _DIAGNOSED_SKIP_REASONS:
        return "diagnosed"

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

    # No ship.json — check if any step JSON carries an error flag. A step that
    # says it is being retried is excluded: its file is a mid-run snapshot, and
    # reading a failing gate off it reports the whole session failed while the
    # next attempt is still running.
    for _, fname, _ in (steps or STEPS):
        data = _safe_load_json(session_dir / fname)
        if data is not None and not _step_unfinished(data) and _step_has_error(data):
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
                  agent: str = DEFAULT_AGENT, user_id: Optional[str] = None) -> List[Dict]:
    """Return session summaries for one agent, newest first."""
    spec = get_agent(agent)
    all_sessions = _dispatch("list", spec)(spec, 10000, 0)
    
    if user_id:
        # Ownership resolved the same way analytics resolves it. These two
        # disagreed: history defaulted a missing owner to "default" while
        # analytics defaulted it to the admin id, so every pre-attribution run
        # vanished from EVERYONE's history (no real user has id "default") while
        # simultaneously appearing in the admin's analytics.
        from qa_agents_server import storage
        from qa_agents_server.analytics import _owner_of, canonical_user_id
        runs = {r.get("session_id"): _owner_of(r) for r in storage.load_all()}
        user_id = canonical_user_id(user_id)
        all_sessions = [s for s in all_sessions
                        if runs.get(s["session_id"]) == user_id]

    return all_sessions[offset : offset + limit]


def _authoring_summary(spec, session_dir: Path) -> Dict:
    """One session row for the authoring agent.

    Extracted so the detail view can build on the same derivation the history
    rows use — the shape healing and adaptation already have. It used to be a
    literal inlined in the list function plus a second, shorter literal in the
    detail function, which is why a detail payload carried no `pr_url`,
    `duration_s`, `timestamp` or cost fields even though the row beside it did.
    """
    ship = _safe_load_json(session_dir / "05-ship.json")
    parsed = _parse_session_id(session_dir.name)
    verdict = _read_text(session_dir / ".verdict")
    fix_gate = _read_text(session_dir / ".fix-passed")
    return {
        "session_id": session_dir.name,
        "module": (ship or {}).get("feature") or parsed["module"],
        "feature_class": (ship or {}).get("feature_class"),
        "started_at": parsed["timestamp"],
        "status": _derive_status(session_dir, ship, spec.steps),
        "verdict": (verdict or "").strip() or None,
        "fix_gate": (fix_gate or "").strip() or None,
        "diagnosis": _diagnosis_outcome(session_dir, fix_gate),
        "test_passed": (ship or {}).get("test_passed"),
        "pr_url": (ship or {}).get("pr_url"),
        "files_count": (ship or {}).get("files_count"),
        "timestamp": (ship or {}).get("timestamp"),
        "duration_s": _duration_with_fallback(session_dir, session_dir.name, ship),
        # Flat cost/token fields so the history table renders without an
        # N+1 fetch per row.
        **metrics_reader.summary_fields(
            metrics_reader.read_session_metrics(session_dir)),
    }


def _list_authoring_sessions(spec, limit: int, offset: int) -> List[Dict]:
    if not spec.audit_dir.exists():
        return []

    sessions = [_authoring_summary(spec, entry)
                for entry in spec.audit_dir.iterdir() if entry.is_dir()]
    sessions.sort(key=lambda s: s.get("session_id") or "", reverse=True)
    return sessions[offset : offset + limit]


def get_session(session_id: str, agent: str = DEFAULT_AGENT) -> Optional[Dict]:
    """Return the full contents of an audit session, or None if missing."""
    spec = get_agent(agent)
    session = _dispatch("get", spec)(spec, session_id)
    if session is not None:
        # Attached here rather than in each per-agent getter, so all three
        # agents' detail views carry the same metrics block.
        rollup = metrics_reader.read_session_metrics(spec.audit_dir / session_id)
        if rollup:
            session["metrics"] = _replay_metrics(rollup)
    return session


def _steps_and_reports(spec, session_dir: Path) -> Dict[str, Dict]:
    """The `steps` (parsed JSON) and `reports` (markdown) dicts, by step key.

    Derived from the spec rather than hand-written per agent, which is what the
    three copies of this used to be. Those copies drifted exactly as you would
    expect: authoring never grew a `reports` dict at all even though its agent
    writes the markdown; healing's `steps` omitted `locate` while its `reports`
    included it; and the two keyed reports differently (`<name>_md` vs the step
    key), so one payload could not be rendered by the other's view.

    Both dicts always share a key set here, so a renderer can walk one and index
    the other. A missing file is None, not an error — any run that died
    mid-pipeline lacks its later artefacts, and so does authoring's
    02-validate-web.md on an API-only run.
    """
    artifacts = spec.detail_artifacts()
    return {
        "steps": {key: _safe_load_json(session_dir / filename)
                  for key, filename, _label in artifacts},
        "reports": {key: _read_text(session_dir / filename.replace(".json", ".md"))
                    for key, filename, _label in artifacts},
    }


def _get_authoring_session(spec, session_id: str) -> Optional[Dict]:
    session_dir = spec.audit_dir / session_id
    if not session_dir.is_dir():
        return None

    summary = _authoring_summary(spec, session_dir)
    summary["init_md"] = _read_text(session_dir / "00-session-init.md")
    summary.update(_steps_and_reports(spec, session_dir))
    return summary


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

    # The live path (runner.py) puts timing and cost on the step and done events;
    # the replayed stream must carry identical fields or the same run reads
    # differently before and after a page reload.
    session_metrics = metrics_reader.read_session_metrics(session_dir)

    # Each step's presence becomes a 'step' event — status reflects error if present
    for key, fname, display in spec.steps:
        data = _safe_load_json(session_dir / fname)
        if data is None:
            continue
        step_status = _step_status(data, finished=True)
        emit("step", {
            "key": key,
            "display": display,
            "status": step_status,
            "summary": _summarise_step(key, data),
            **metrics_reader.step_metrics(session_metrics, key),
        })

    if spec.summary_kind != "authoring":
        # Reuse the same summary the history table and the live card's own
        # result panel read, so a replayed run reports exactly what a live one
        # did rather than a second, drifting derivation of "what happened".
        summary = _healing_summary(spec, session_dir)
        emit("done", {
            "status": summary.get("status"),
            "verdict": summary.get("fix_gate"),
            "pr_url": summary.get("pr_url"),
            "test_passed": None,
            "files_count": None,
            "ship_status": None,
            "ship_detail": summary.get("failure_headline"),
            "duration_s": _duration_with_fallback(
                session_dir, session_id,
                _safe_load_json(session_dir / "02-ship.json")
                or _safe_load_json(session_dir / "01-fix.json")
                or _safe_load_json(session_dir / "00-reproduce.json")),
            "metrics": _replay_metrics(session_metrics),
        })
        return events

    # Terminal event from ship + verdict
    ship = _safe_load_json(session_dir / spec.steps[-1][1])
    verdict = (_read_text(session_dir / ".verdict") or "").strip() or None
    duration_s = _duration_with_fallback(session_dir, session_id, ship)
    if ship is not None:
        emit("done", {
            "status": _derive_status(session_dir, ship, spec.steps),
            "verdict": verdict,
            "pr_url": ship.get("pr_url"),
            "test_passed": ship.get("test_passed"),
            "files_count": ship.get("files_count"),
            "ship_status": ship.get("ship_status"),
            "ship_detail": ship.get("ship_detail"),
            "duration_s": duration_s,
            "metrics": _replay_metrics(session_metrics),
        })
    else:
        emit("done", {
            "status": _derive_status(session_dir, ship, spec.steps),
            "verdict": verdict,
            "pr_url": None,
            "test_passed": None,
            "files_count": None,
            "ship_status": None,
            "ship_detail": None,
            "duration_s": duration_s,
            "metrics": _replay_metrics(session_metrics),
        })

    return events


def _replay_metrics(session_metrics: Optional[Dict]) -> Dict:
    """Run totals + stage breakdown, matching runner._run_metrics exactly."""
    if not session_metrics:
        return {}
    data = metrics_reader.totals(session_metrics)
    data["stages"] = metrics_reader.stage_list(session_metrics)
    return data


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


def _duration_with_fallback(session_dir: Path, session_id: str,
                            ship_data: Optional[Dict]) -> Optional[float]:
    """The metrics rollup's active time, falling back to the ship timestamp.

    Rollup first: the ship-based span runs from the session id's timestamp, so a
    resumed run counts the idle gap between attempts. The ship span remains for
    old sessions with no metrics.
    """
    rollup = metrics_reader.read_session_metrics(session_dir)
    value = (rollup or {}).get("duration_s")
    if value is not None:
        return value
    return _compute_duration_s(session_id, ship_data)


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
            # Neither passed nor failed: the step ran, but what it claims was
            # never observed. A run reading 15/15 passed used to hide one of these.
            "steps_unverified": len(data.get("steps_unverified") or []),
        }
    if key == "generate":
        return {
            "files_written": len(data.get("files_written") or []),
        }
    if key == "run_and_fix":
        # The writer emits "passed"/"attempt" (04_run_and_fix.py), not
        # "test_passed"/"attempts" — so this branch returned two nulls for every
        # run until now. The old key names are still read as a fallback in case
        # an older session on disk used them.
        return {
            "test_passed": data.get("passed", data.get("test_passed")),
            "attempts": data.get("attempt", data.get("attempts")),
            "fixes_applied": data.get("fixes_applied"),
            "root_cause": data.get("root_cause"),
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

# A run that produced a diagnosis instead of a fix is a result, not a failure.
# Without this it renders as "0 fixed / could not fix" — the same unhelpful
# outcome as before any of this existed, with the actual answer buried in an
# audit file nobody opens.
_DIAGNOSED_SKIP_REASONS = ("diagnosed",)

# Verdicts the diagnosis engine reaches from measured evidence. Offering "try a
# locator fix anyway" on one of these re-enables precisely the behaviour the gate
# exists to prevent, so the override is re-labelled rather than presented as the
# obvious next step.
#
# Imported rather than restated: this file already carries three "keep in sync"
# comments against copies elsewhere, and nothing enforces any of them.
try:
    from shared.diagnosis import STOP as _STOP_VERDICTS
except ImportError:  # server started without the repo root on the path
    _STOP_VERDICTS = ()


def _diagnosis_outcome(session_dir: Path, fix_gate: Optional[str]) -> Optional[Dict]:
    """The diagnosis a skipped run reached, when that is why it skipped."""
    if (fix_gate or "").strip() != "skipped":
        return None
    reason = (_read_text(session_dir / ".skip-reason") or "").strip()
    if reason not in _DIAGNOSED_SKIP_REASONS:
        return None

    verdict, remediation, reasons = "", "", []
    for name in ("00-reproduce.json", "01-fix.json"):
        data = _safe_load_json(session_dir / name) or {}
        recorded = data.get("diagnosis") or {}
        verdict = verdict or recorded.get("verdict") or ""
        remediation = remediation or recorded.get("remediation") or ""
        reasons = reasons or list(recorded.get("reasons") or [])
        # Standalone records the verdict as the run's status; the fix step records
        # it per cluster.
        if data.get("status") and data["status"] not in ("queued", "passing"):
            verdict = verdict or data["status"]
        for entry in (data.get("failed_fixes") or []):
            diagnosis = entry.get("diagnosis") or {}
            verdict = verdict or diagnosis.get("verdict") or ""
            remediation = remediation or diagnosis.get("remediation") or ""
            reasons = reasons or list(diagnosis.get("reasons") or [])
        headline = data.get("headline")
        if headline and not remediation:
            remediation = headline
    return {"verdict": verdict or "DIAGNOSED", "remediation": remediation,
            "reasons": reasons[:4]}


def _locate_counts(locate_data: Optional[Dict]) -> Dict:
    """What the Locate step concluded, flat enough for a history row.

    `located_deterministically` is the number that matters: every one of those is
    a locator fixed without a model call, which is the whole point of the step.
    """
    if not locate_data:
        return {"healing_locate_mode": "", "located_deterministically": 0,
                "locate_attempted": 0, "locate_refused": 0, "locate_verdicts": {}}
    return {
        "healing_locate_mode": locate_data.get("mode", ""),
        "located_deterministically": locate_data.get("located", 0),
        "locate_attempted": locate_data.get("attempted", 0),
        "locate_refused": locate_data.get("refused", 0),
        "locate_verdicts": locate_data.get("verdicts") or {},
    }


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
    if shape in _STOP_VERDICTS:
        # The run reached a measured conclusion. "not a locator" says what it was
        # not; "diagnosed" says it found out what it was.
        return "diagnosed"
    return _SHAPE_STATUS.get(shape, "not_a_locator")


def _healing_status(session_dir: Path, fix_gate: Optional[str],
                    ship_data: Optional[Dict], counts: Dict[str, int],
                    shape: str = "") -> str:
    if (ship_data or {}).get("pr_url"):
        return "pr_created"
    # Cancelled before crashed: cancelling kills the step, which trips run.sh's
    # ERR trap into writing .crashed too. .cancelled only exists if the cancel
    # landed while the run was alive, so the crash is the cancel's own echo.
    if (session_dir / ".cancelled").exists():
        return "cancelled"
    if (session_dir / ".crashed").exists():
        return "crashed"
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
        # A handoff gated in the fix step has no reproduce output to read a shape
        # from — pipeline runs skip that step entirely — so the skip reason is the
        # only place the conclusion is recorded.
        if (_read_text(session_dir / ".skip-reason") or "").strip() in _DIAGNOSED_SKIP_REASONS:
            return "diagnosed"
        return _skipped_status(shape)
    if gate:
        return "unknown"
    return "interrupted" if _looks_abandoned(session_dir) else "running"


def _healing_summary(spec, session_dir: Path) -> Dict:
    reproduce = _safe_load_json(session_dir / "00-reproduce.json")
    locate = _safe_load_json(session_dir / "01-locate.json")
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
        # What the diagnosis concluded, so the panel can say why rather than only
        # that nothing happened — and so it can tell a measured cause apart from
        # an unrecognised one when deciding whether to offer the override.
        "diagnosis": _diagnosis_outcome(session_dir, fix_gate),
        # Pipeline runs never produce a reproduce shape, so the verdict recorded
        # by the fix step is the only signal that the conclusion was measured.
        "diagnosis_confident": (
            shape in _STOP_VERDICTS
            or (_diagnosis_outcome(session_dir, fix_gate) or {}).get("verdict", "")
            in _STOP_VERDICTS),
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
        "duration_s": _duration_with_fallback(session_dir, session_dir.name,
                                              ship or fix or reproduce),
        **counts,
        **_locate_counts(locate),
        **metrics_reader.summary_fields(
            metrics_reader.read_session_metrics(session_dir)),
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
    # Markdown reports are what a human actually wants to read in the detail view.
    # The raw console is deliberately NOT included: it is served incrementally by
    # /run/<sid>/stream into the Live Run card, so shipping a second copy here
    # made every row click pay for the whole log — and a real run's maven output
    # is the largest thing in the session by far.
    summary.update(_steps_and_reports(spec, session_dir))
    return summary




# ── test-adaptation-agent ─────────────────────────────────────────────────────

def _flow_line(step: Dict) -> str:
    """One explored step as 'Page · verb · target', plus its outcome if not ok."""
    page = step.get("page")
    page = page.get("id", "") if isinstance(page, dict) else (page or "")
    action = step.get("action") or {}
    target = action.get("target") or {}
    outcome = (step.get("result") or {}).get("outcome") or "ok"
    line = " · ".join(str(x) for x in (page, action.get("verb"),
                                        target.get("accessible_name") or target.get("name"))
                      if x)
    return line if outcome == "ok" else f"{line} — {outcome}"


def _adaptation_summary(spec, session_dir: Path) -> Dict:
    """One session row for the adaptation agent.

    Reads the same files the history table and the live result panel read, so a
    replayed run reports exactly what a live one did rather than a second,
    drifting derivation of "what happened".
    """
    parse = _safe_load_json(session_dir / "01-parse-change.json") or {}
    scope = _safe_load_json(session_dir / "02-scope.json") or {}
    explore = _safe_load_json(session_dir / "03-explore.json") or {}
    adapt = _safe_load_json(session_dir / "04-adapt.json") or {}
    ship = _safe_load_json(session_dir / "05-ship.json") or {}

    gate = (_read_text(session_dir / ".fix-passed") or "").strip() or None
    skip = (_read_text(session_dir / ".skip-reason") or "").strip() or None
    verdict = (_read_text(session_dir / ".verdict") or "").strip() or None
    parsed = _parse_session_id(session_dir.name)

    items = adapt.get("items") or []
    counts = {
        "items": len(items),
        "applied": sum(1 for i in items if i.get("status") in ("applied", "partial")),
        "proposed": sum(1 for i in items if i.get("status") == "proposed"),
        "rejected": sum(1 for i in items if i.get("status") in ("rejected", "rolled_back")),
        "escalated": sum(1 for i in items if i.get("status") in ("escalated", "declined")),
        # Done by an earlier item's verified edit — no escalation, so count it here
        # or it disappears from the UI entirely.
        "covered": sum(1 for i in items if i.get("status") == "covered"),
        # Checks removed or changed by applied or proposed items — the number a
        # reviewer most needs, so the badge shows it rather than burying it.
        "checks_changed": sum(
            1 for i in items if i.get("status") in ("applied", "partial", "proposed")
            for r in i.get("check_changes") or []
            if r.get("action") in ("remove", "change")),
        "verified": len(adapt.get("verified") or []),
        "failed": len(adapt.get("failed") or []),
    }

    # Cancelled before crashed — see _healing_status.
    if (session_dir / ".cancelled").exists():
        status = "cancelled"
    elif (session_dir / ".crashed").exists():
        status = "failed"
    elif (session_dir / ".interrupted").exists():
        status = "interrupted"
    elif skip == "infra":
        # The model call never happened (a usage cap, an API error). Falling
        # through to the ship check below reported that as "completed".
        status = "blocked"
    elif skip in ("escalate", "unsafe", "no-session", "unreachable"):
        # The agent stopped rather than guessing. That is the design working, so
        # it must not be painted red — reporting a correct refusal as a failure
        # trains people to ignore exactly the runs worth reading.
        status = "diagnosed"
    elif skip == "explore-only":
        status = "completed"
    elif ship:
        status = "failed" if ship.get("ship_status") in ("push_failed", "pr_failed") \
            else "completed"
    elif (session_dir / "00-session-init.md").exists():
        status = "interrupted" if _looks_abandoned(session_dir) else "running"
    else:
        status = "unknown"

    escalations = (adapt.get("escalations") or []) + (ship.get("escalations") or [])
    headline = ""
    if escalations:
        headline = escalations[0].get("why", "")[:200]
    elif explore.get("unexplained_failures"):
        headline = (f"{len(explore['unexplained_failures'])} failure(s) the change "
                    f"note does not account for")

    return {
        "session_id": session_dir.name,
        # Two different things, and the UI shows both. `module` is the product
        # area the note's `Module:` header names; `note` is the queue file the
        # run was started from (MODULE, i.e. queue/<note>.txt). Reporting only
        # the first left the history unable to say which note produced a run.
        "module": parse.get("module") or parsed["module"],
        "note": parsed["module"],
        "started_at": parsed["timestamp"],
        "status": status,
        "verdict": verdict,
        "fix_gate": gate,
        "skip_reason": skip,
        "pr_url": ship.get("pr_url"),
        "timestamp": ship.get("timestamp") or adapt.get("timestamp"),
        "change_items": len(parse.get("items") or []),
        "tests_in_scope": len(scope.get("verify") or []),
        "explore_status": explore.get("status"),
        "explore_steps": explore.get("steps"),
        "counts": counts,
        "escalations": escalations,
        "failure_headline": headline,
        # The Result panel's digest. The .md reports stay the full audit trail;
        # read raw, three of them buried the answer under excluded-test lists
        # and per-guard checkmarks.
        "items": [{**{k: i.get(k) for k in ("index", "kind", "text", "status", "summary",
                                          "reason", "covered_by", "attempt")},
                   "check_changes": [{k: r.get(k) for k in ("action", "message",
                                                           "evidence", "why")}
                                     for r in i.get("check_changes") or []
                                     if r.get("action") in ("remove", "change")]}
                  for i in items],
        "tests": {"passed": adapt.get("verified") or [], "failed": adapt.get("failed") or []},
        "flow": [_flow_line(step) for step in (explore.get("flow") or {}).get("steps") or []],
        "duration_s": _duration_with_fallback(session_dir, session_dir.name, ship),
        **metrics_reader.summary_fields(
            metrics_reader.read_session_metrics(session_dir)),
    }


def _list_adaptation_sessions(spec, limit: int, offset: int) -> List[Dict]:
    if not spec.audit_dir.exists():
        return []
    sessions = [_adaptation_summary(spec, entry)
                for entry in spec.audit_dir.iterdir() if entry.is_dir()]
    sessions.sort(key=lambda s: s.get("session_id") or "", reverse=True)
    return sessions[offset: offset + limit]


def _get_adaptation_session(spec, session_id: str) -> Optional[Dict]:
    session_dir = spec.audit_dir / session_id
    if not session_dir.is_dir():
        return None
    summary = _adaptation_summary(spec, session_dir)
    summary["init_md"] = _read_text(session_dir / "00-session-init.md")
    summary.update(_steps_and_reports(spec, session_dir))
    return summary


# ── Per-agent session parsers ─────────────────────────────────────────────────
#
# This used to be `if spec.name != DEFAULT_AGENT: <healing>`, i.e. "anything that
# is not authoring is healing". That is invisible with two agents and wrong with
# three: a newly registered agent got healing's parser and reported sessions that
# were structurally empty rather than failing loudly.
#
# Dispatch is on AgentSpec.summary_kind, which has no default, so a new agent has
# to say which family it belongs to — and an unregistered one raises here instead
# of silently returning another agent's shape.
_SESSION_PARSERS = {
    "authoring": {"list": _list_authoring_sessions, "get": _get_authoring_session},
    "healing": {"list": _list_healing_sessions, "get": _get_healing_session},
    "adaptation": {"list": _list_adaptation_sessions, "get": _get_adaptation_session},
}


def _dispatch(operation: str, spec):
    parsers = _SESSION_PARSERS.get(spec.summary_kind)
    if parsers is None:
        raise AgentConfigError(
            f"no session parser registered for summary_kind="
            f"{spec.summary_kind!r} (agent {spec.name}); add one to "
            f"_SESSION_PARSERS in qa_agents_server/audit_reader.py", status=500)
    return parsers[operation]
