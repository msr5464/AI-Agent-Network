"""Subprocess orchestration + live event streaming for qa_agents_server.

Responsibilities
----------------
1. Spawn `agents/test-authoring-agent/run.sh` with a pre-computed SESSION_ID
   and AUDIT_DIR so the server knows where to watch before the agent writes
   its first file.
2. Read the subprocess's stdout line-by-line on a background thread and append
   each line as a `stdout` Event to an in-memory ring buffer.
3. Poll the audit directory on a second background thread and emit `step`
   Events when `NN-*.json` files land.
4. Wait for the process to exit, emit a terminal `done` Event, and flip the
   run's status to completed / failed / cancelled.
5. Expose `subscribe_stream(session_id, offset)` — a generator that replays
   buffered events from `offset` then blocks for new ones until terminal.

Concurrency policy for v1: one active run at a time. A second trigger while
a run is in flight returns 409.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Generator, List, Optional

from qa_agents_server import storage
from qa_agents_server.audit_reader import STEPS
from qa_agents_server.feature_files import feature_exists
from qa_agents_server.paths import (
    AUTHORING_AUDIT_DIR,
    AUTHORING_RUN_SH,
    REPO_ROOT,
)

# ── Constants ─────────────────────────────────────────────────────────────────
AGENT = "test-authoring-agent"
MAX_BUFFERED_EVENTS = 10_000
AUDIT_POLL_INTERVAL = 0.5  # seconds
CANCEL_GRACE_SECONDS = 5
DEFAULT_RUN_TIMEOUT = int(os.getenv("QA_AGENT_RUN_TIMEOUT_SECONDS", "1800"))  # 30m
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


# ── Event model ───────────────────────────────────────────────────────────────
@dataclass
class Event:
    seq: int
    kind: str  # 'stdout' | 'step' | 'status' | 'done' | 'error' | 'heartbeat'
    data: dict
    ts: float

    def to_dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "data": self.data, "ts": self.ts}


# ── RunState ──────────────────────────────────────────────────────────────────
@dataclass
class RunState:
    session_id: str
    feature: str
    auto_push: bool
    audit_dir: Path
    started_at: float
    status: str = "running"  # running | completed | failed | cancelled | interrupted
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    events: Deque[Event] = field(default_factory=lambda: deque(maxlen=MAX_BUFFERED_EVENTS))
    seq_counter: int = 0
    step_progress: Dict[str, str] = field(default_factory=dict)  # key -> 'running'|'done'
    proc: Optional[subprocess.Popen] = None
    cond: threading.Condition = field(default_factory=threading.Condition)

    def snapshot(self) -> Dict:
        """Persistable snapshot (no Popen, no threading primitives)."""
        return {
            "session_id": self.session_id,
            "agent": AGENT,
            "feature": self.feature,
            "auto_push": self.auto_push,
            "audit_dir": str(self.audit_dir),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": self.pid,
        }


# ── Module state ──────────────────────────────────────────────────────────────
_runs: Dict[str, RunState] = {}
_active_session_id: Optional[str] = None
_registry_lock = threading.Lock()


# ── Errors ────────────────────────────────────────────────────────────────────
class RunnerError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Lifecycle ─────────────────────────────────────────────────────────────────
def reconcile_on_boot() -> None:
    """On server boot, mark any persisted 'running' runs as 'interrupted'."""
    interrupted = storage.mark_all_running_as_interrupted()
    if interrupted:
        print(
            f"[runner] marked {len(interrupted)} stranded run(s) as interrupted: "
            f"{', '.join(interrupted)}"
        )


def shutdown_all() -> None:
    """SIGTERM every tracked subprocess. Called from signal handler / atexit."""
    with _registry_lock:
        runs = list(_runs.values())
    for run in runs:
        proc = run.proc
        if proc is None:
            continue
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


# ── Public API ────────────────────────────────────────────────────────────────
def get_active_session_id() -> Optional[str]:
    return _active_session_id


def get_run(session_id: str) -> Optional[RunState]:
    return _runs.get(session_id)


def start_run(feature: str, auto_push: bool) -> RunState:
    """Spawn run.sh for the given feature. Raises RunnerError on validation."""
    global _active_session_id

    if not feature:
        raise RunnerError("feature is required")

    # Feature file must exist in the queue (writable via /queue endpoint).
    if feature_exists(feature) is None:
        raise RunnerError(
            f"feature file not found: {feature}.txt — create it first via "
            f"POST /agents/{AGENT}/queue",
            status=404,
        )

    if not AUTHORING_RUN_SH.exists():
        raise RunnerError(
            f"agent run.sh not found at {AUTHORING_RUN_SH}", status=500
        )

    with _registry_lock:
        if _active_session_id is not None:
            active = _runs.get(_active_session_id)
            if active and active.status == "running":
                raise RunnerError(
                    f"another run is already in progress: {_active_session_id}",
                    status=409,
                )

        session_id = _make_session_id(feature)
        audit_dir = AUTHORING_AUDIT_DIR / session_id
        audit_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["FEATURE"] = feature
        env["AUTO_PUSH"] = "true" if auto_push else "false"
        env["SESSION_ID"] = session_id
        env["AUDIT_DIR"] = str(audit_dir)

        try:
            proc = subprocess.Popen(
                ["bash", str(AUTHORING_RUN_SH)],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                start_new_session=True,  # new process group so SIGTERM reaches children
            )
        except OSError as e:
            raise RunnerError(f"failed to spawn agent: {e}", status=500)

        run = RunState(
            session_id=session_id,
            feature=feature,
            auto_push=auto_push,
            audit_dir=audit_dir,
            started_at=time.time(),
            proc=proc,
            pid=proc.pid,
        )
        _runs[session_id] = run
        _active_session_id = session_id

    # Emit initial status event
    _append_event(run, "status", {
        "session_id": session_id,
        "feature": feature,
        "auto_push": auto_push,
        "status": "running",
        "started_at": run.started_at,
    })

    storage.upsert(run.snapshot())

    # Spawn background workers
    threading.Thread(
        target=_stdout_reader, args=(run,), daemon=True,
        name=f"stdout-{session_id}",
    ).start()
    threading.Thread(
        target=_audit_watcher, args=(run,), daemon=True,
        name=f"audit-{session_id}",
    ).start()
    threading.Thread(
        target=_wait_and_reap, args=(run,), daemon=True,
        name=f"reap-{session_id}",
    ).start()

    return run


def cancel_run(session_id: str) -> bool:
    run = _runs.get(session_id)
    if run is None:
        return False
    proc = run.proc
    if proc is None or proc.poll() is not None:
        return False

    _append_event(run, "status", {"status": "cancelling"})

    # Send SIGTERM to the entire process group (run.sh spawns python3 / mvn / claude).
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass

    # Give it a grace period, then SIGKILL if still alive.
    def _escalate():
        time.sleep(CANCEL_GRACE_SECONDS)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    threading.Thread(target=_escalate, daemon=True).start()
    run.status = "cancelled"  # _wait_and_reap will confirm once proc exits
    return True


def subscribe_stream(session_id: str, offset: int) -> Generator[Event, None, None]:
    """Generator yielding events from `offset` onward until the run terminates.

    Safe to call from a Flask SSE endpoint — does not hold any lock while
    yielding to the client.
    """
    run = _runs.get(session_id)
    if run is None:
        return
    last_seen = max(offset - 1, 0)

    while True:
        to_yield: List[Event] = []
        with run.cond:
            # Drain any events with seq > last_seen from the buffer.
            to_yield = [e for e in run.events if e.seq > last_seen]
            if not to_yield and run.status in TERMINAL_STATUSES:
                # No new events and we're done → exit the generator
                return
            if not to_yield:
                # Wait for new events or terminal state (with timeout so the
                # endpoint can send heartbeats to keep the connection alive).
                run.cond.wait(timeout=15)
                continue

        # Yield outside the lock so slow clients don't block writers.
        for e in to_yield:
            yield e
            last_seen = e.seq


# ── Internal helpers ──────────────────────────────────────────────────────────
def _make_session_id(feature: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", feature)
    return f"{ts}-create-{safe}"


def _append_event(run: RunState, kind: str, data: dict) -> Event:
    with run.cond:
        run.seq_counter += 1
        event = Event(seq=run.seq_counter, kind=kind, data=data, ts=time.time())
        run.events.append(event)
        run.cond.notify_all()
    return event


def _stdout_reader(run: RunState) -> None:
    """Read the subprocess's stdout line-by-line into the ring buffer."""
    proc = run.proc
    if proc is None or proc.stdout is None:
        return
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            _append_event(run, "stdout", {"line": line})
    except Exception as e:
        _append_event(run, "error", {"source": "stdout_reader", "message": str(e)})


def _audit_watcher(run: RunState) -> None:
    """Poll the audit dir and emit step events as JSON files appear."""
    seen: Dict[str, bool] = {key: False for key, _, _ in STEPS}
    # Mark the first unseen step as "running" up front so the UI lights up.
    if STEPS:
        first_key, _, first_display = STEPS[0]
        run.step_progress[first_key] = "running"
        _append_event(run, "step", {
            "key": first_key, "display": first_display, "status": "running",
        })

    while True:
        if run.proc is None:
            return
        # Scan for new step files
        for key, fname, display in STEPS:
            if seen[key]:
                continue
            if (run.audit_dir / fname).exists():
                seen[key] = True
                run.step_progress[key] = "done"
                _append_event(run, "step", {
                    "key": key, "display": display, "status": "done",
                })
                # Mark the next step as running (if any)
                idx = [k for k, _, _ in STEPS].index(key)
                if idx + 1 < len(STEPS):
                    next_key, _, next_display = STEPS[idx + 1]
                    if not seen.get(next_key) and run.step_progress.get(next_key) != "running":
                        run.step_progress[next_key] = "running"
                        _append_event(run, "step", {
                            "key": next_key, "display": next_display, "status": "running",
                        })
        # Exit when the process is gone AND all steps checked (let the reap thread handle terminal state)
        if run.proc.poll() is not None:
            # One final sweep after the process exits
            for key, fname, display in STEPS:
                if not seen[key] and (run.audit_dir / fname).exists():
                    seen[key] = True
                    run.step_progress[key] = "done"
                    _append_event(run, "step", {
                        "key": key, "display": display, "status": "done",
                    })
            return
        time.sleep(AUDIT_POLL_INTERVAL)


def _wait_and_reap(run: RunState) -> None:
    """Wait for the subprocess to exit, determine final status, emit done."""
    global _active_session_id

    proc = run.proc
    if proc is None:
        return

    try:
        exit_code = proc.wait(timeout=DEFAULT_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _append_event(run, "error", {
            "source": "timeout",
            "message": f"run exceeded {DEFAULT_RUN_TIMEOUT}s — killing",
        })
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        exit_code = proc.wait()

    run.exit_code = exit_code
    run.ended_at = time.time()

    # Derive final status
    if run.status == "cancelled":
        final_status = "cancelled"
    elif exit_code == 0:
        # Check .verdict if present to distinguish APPROVED vs NEEDS-REVIEW
        verdict_path = run.audit_dir / ".verdict"
        if verdict_path.exists():
            verdict = verdict_path.read_text().strip()
            final_status = "completed" if verdict == "APPROVED" else "failed"
        else:
            final_status = "completed"
    else:
        final_status = "failed"
    run.status = final_status

    # Load ship data for the terminal event payload
    ship_path = run.audit_dir / "05-ship.json"
    pr_url = None
    verdict = None
    test_passed = None
    files_count = None
    if ship_path.exists():
        import json as _json
        try:
            ship = _json.loads(ship_path.read_text())
            pr_url = ship.get("pr_url")
            verdict = ship.get("verdict")
            test_passed = ship.get("test_passed")
            files_count = ship.get("files_count")
        except (OSError, _json.JSONDecodeError):
            pass

    _append_event(run, "done", {
        "status": final_status,
        "exit_code": exit_code,
        "verdict": verdict,
        "pr_url": pr_url,
        "test_passed": test_passed,
        "files_count": files_count,
        "ended_at": run.ended_at,
        "duration": run.ended_at - run.started_at,
    })

    # Persist final snapshot
    storage.upsert(run.snapshot())

    # Clear active run marker
    with _registry_lock:
        if _active_session_id == run.session_id:
            _active_session_id = None
