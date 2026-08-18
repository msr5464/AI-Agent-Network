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
from qa_agents_server.audit_reader import (
    STEPS,
    get_session as _get_session,
    _safe_load_json,
    _step_has_error,
)
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
# Whole-pipeline (run.sh, all 5 steps) kill timeout — not just one step's budget.
# Worst case with current per-step defaults: step02 alone can now take up to
# VALIDATE_WEB_TIMEOUT_S x (1+VALIDATE_WEB_RETRY_ATTEMPTS) = 1800x2 = 3600s;
# step04's fix loop can take MAX_FIX_ATTEMPTS x ~600s = ~1800s; steps 01/03/05
# add roughly another 1500s combined — so a 1800s default was already shorter
# than step02 alone could legitimately take even before its retry loop existed.
DEFAULT_RUN_TIMEOUT = int(os.getenv("QA_AGENT_RUN_TIMEOUT_SECONDS", "7200"))  # 2h
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
    module: str
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
    start_from_step: int = 1  # >1 means this run resumed an existing session

    def snapshot(self) -> Dict:
        """Persistable snapshot (no Popen, no threading primitives)."""
        return {
            "session_id": self.session_id,
            "agent": AGENT,
            "module": self.module,
            "auto_push": self.auto_push,
            "audit_dir": str(self.audit_dir),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "start_from_step": self.start_from_step,
        }


# ── Module state ──────────────────────────────────────────────────────────────
_runs: Dict[str, RunState] = {}
_active_session_id: Optional[str] = None
_registry_lock = threading.Lock()

# ── Pending queue ─────────────────────────────────────────────────────────────
# Each entry: {"module": str, "auto_push": bool}
_pending_queue: List[Dict] = []
_queue_lock = threading.Lock()


# ── Errors ────────────────────────────────────────────────────────────────────
class RunnerError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _QueuedNotification(Exception):
    """Raised (not an error) to signal that a run was enqueued, not started."""
    def __init__(self, position: int, session_id: str):
        self.position = position
        self.session_id = session_id


def _start_next_from_queue() -> None:
    """Pick the first pending item and start it. Runs in the reap thread."""
    with _queue_lock:
        if not _pending_queue:
            return
        next_item = _pending_queue.pop(0)
    try:
        start_run(next_item["module"], next_item["auto_push"],
                  session_id=next_item.get("session_id"),
                  start_from_step=next_item.get("start_from_step", 1))
    except Exception as e:
        print(f"[runner] failed to start queued run for {next_item['module']!r}: {e}")


def get_queue() -> List[Dict]:
    """Return a snapshot of pending queue items."""
    with _queue_lock:
        return [{"module": item["module"], "auto_push": item["auto_push"],
                 "session_id": item.get("session_id"),
                 "start_from_step": item.get("start_from_step", 1),
                 "position": i + 1}
                for i, item in enumerate(_pending_queue)]


def remove_from_queue(index: int) -> bool:
    """Remove item at 0-based index. Returns True if removed."""
    with _queue_lock:
        if 0 <= index < len(_pending_queue):
            _pending_queue.pop(index)
            return True
        return False


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


def start_run(module: Optional[str], auto_push: bool, session_id: Optional[str] = None,
              start_from_step: int = 1) -> RunState:
    """Spawn run.sh for the given module. Raises RunnerError on validation.

    start_from_step > 1 resumes an EXISTING session (session_id required —
    it must already have valid output for every step before start_from_step)
    instead of starting a fresh one. module can be omitted when resuming; it's
    recovered from that session's own audit trail, since the original queue
    .txt file may already have been moved to processed/ by the run being
    resumed — feature_exists() would wrongly reject a legitimate resume.
    """
    global _active_session_id

    resuming = start_from_step > 1

    if resuming:
        if not session_id:
            raise RunnerError("session_id is required to resume from a step > 1")
        if not (AUTHORING_AUDIT_DIR / session_id).exists():
            raise RunnerError(f"session not found: {session_id}", status=404)
        if not module:
            existing = _get_session(session_id)
            module = existing.get("module") if existing else None
            if not module:
                raise RunnerError(
                    f"could not determine module for session {session_id}", status=500
                )
    else:
        if not module:
            raise RunnerError("module is required")
        # Module file must exist in the queue (writable via /queue endpoint).
        if feature_exists(module) is None:
            raise RunnerError(
                f"module file not found: {module}.txt — create it first via "
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
                # Queue instead of rejecting — generate session_id now so it's stable
                # (resuming reuses the session_id it was given rather than minting one).
                with _queue_lock:
                    queued_session_id = session_id if resuming else _make_session_id(module)
                    _pending_queue.append({
                        "module": module, "auto_push": auto_push,
                        "session_id": queued_session_id, "start_from_step": start_from_step,
                    })
                    position = len(_pending_queue)
                raise _QueuedNotification(position, queued_session_id)

        session_id = session_id or _make_session_id(module)
        audit_dir = AUTHORING_AUDIT_DIR / session_id
        audit_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["MODULE"] = module
        env["AUTO_PUSH"] = "true" if auto_push else "false"
        env["SESSION_ID"] = session_id
        env["AUDIT_DIR"] = str(audit_dir)
        if start_from_step > 1:
            env["START_FROM_STEP"] = str(start_from_step)

        # Captured BEFORE Popen() (not after) — _audit_watcher uses this as the
        # cutoff for "did THIS run's own subprocess actually write this file,
        # or is it a stale leftover from a previous attempt" (see there for
        # why that distinction matters for a resumed/retried session).
        started_at = time.time()
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
            module=module,
            auto_push=auto_push,
            audit_dir=audit_dir,
            started_at=started_at,
            proc=proc,
            pid=proc.pid,
            start_from_step=start_from_step,
        )
        _runs[session_id] = run
        _active_session_id = session_id

    # Emit initial status event
    _append_event(run, "status", {
        "session_id": session_id,
        "module": module,
        "auto_push": auto_push,
        "status": "running",
        "started_at": run.started_at,
        "start_from_step": start_from_step,
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
def _make_session_id(module: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", module)
    return f"{ts}-create-{safe}"


def _append_event(run: RunState, kind: str, data: dict) -> Event:
    with run.cond:
        run.seq_counter += 1
        event = Event(seq=run.seq_counter, kind=kind, data=data, ts=time.time())
        run.events.append(event)
        run.cond.notify_all()
    return event


def _stdout_reader(run: RunState) -> None:
    """Read the subprocess's stdout line-by-line into the ring buffer and persist to disk."""
    proc = run.proc
    if proc is None or proc.stdout is None:
        return
    log_path = run.audit_dir / "stdout.log"
    try:
        with open(log_path, "w", buffering=1) as log_fh:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                _append_event(run, "stdout", {"line": line})
                log_fh.write(line + "\n")
    except Exception as e:
        _append_event(run, "error", {"source": "stdout_reader", "message": str(e)})


def _step_file_is_fresh(run: RunState, idx: int, file_path: Path) -> bool:
    """True if file_path is safe to treat as this run's own output for the
    step at STEPS[idx].

    For a resumed/retried run (start_from_step > 1), steps BEFORE
    start_from_step are deliberately reused as-is — their existing file, of
    whatever age, is exactly what we want. But for the step run.sh is
    actually RESUMING FROM (and everything after it), the OLD file from the
    attempt being retried still sits on disk the instant this run's
    subprocess spawns — run.sh only deletes it moments later, as its own
    first real action (see run.sh's stale-artifact cleanup). If this thread
    scans in that window, it would read the PREVIOUS attempt's stale result
    (e.g. a push failure) and permanently mark the step from it, since
    step_progress then blocks ever looking at that key again — even after
    run.sh finishes rewriting the file with the real, current outcome. A
    file with an mtime from before this run started is exactly that stale
    leftover, not this run's own output.
    """
    step_num = idx + 1
    if step_num < run.start_from_step:
        return True  # reused step — existing file is valid regardless of age
    try:
        return file_path.stat().st_mtime >= run.started_at
    except OSError:
        return False


def _audit_watcher(run: RunState) -> None:
    """Poll the audit dir and emit step events as JSON files appear.

    run.step_progress (not a local dict) is the single source of truth for
    "have I already emitted an event for this key" — it's shared with
    _wait_and_reap, which does its own authoritative catch-up sweep right
    before the terminal "done" event (see there for why: this thread's poll
    interval can lose the race against a fast-finishing run). Consulting the
    same shared dict here means neither thread re-emits for a step the other
    one already handled, and this thread does its OWN final sweep once the
    process exits — _wait_and_reap's later sweep covers anything left over.
    """
    # Mark the actual first-to-run step as "running" up front so the UI
    # lights up immediately — for a resumed run (start_from_step > 1),
    # that's NOT "parse": steps before start_from_step are reused as-is,
    # not re-executed, so marking "parse" here was actively wrong (a
    # spurious "running" flicker on a step that isn't running at all).
    if STEPS:
        bootstrap_idx = min(max(run.start_from_step - 1, 0), len(STEPS) - 1)
        first_key, _, first_display = STEPS[bootstrap_idx]
        if run.step_progress.get(first_key) is None:
            run.step_progress[first_key] = "running"
            _append_event(run, "step", {
                "key": first_key, "display": first_display, "status": "running",
            })

    while True:
        if run.proc is None:
            return
        # Scan for new step files
        for idx, (key, fname, display) in enumerate(STEPS):
            if run.step_progress.get(key) in ("done", "failed"):
                continue
            file_path = run.audit_dir / fname
            if file_path.exists() and _step_file_is_fresh(run, idx, file_path):
                # File existing only means the step's process finished — it
                # says nothing about whether the step's OWN outcome was good
                # (e.g. 04-run-and-fix.json exists whether the test passed or
                # never did; 05-ship.json exists whether the push succeeded
                # or failed). _step_has_error() reads each step's own result
                # vocabulary to tell the two apart.
                step_status = "failed" if _step_has_error(_safe_load_json(file_path)) else "done"
                run.step_progress[key] = step_status
                _append_event(run, "step", {
                    "key": key, "display": display, "status": step_status,
                })
                # Mark the next step as running (if any, and regardless of
                # whether THIS step failed — the pipeline still runs the next
                # step either way), but only if nobody (including
                # _wait_and_reap's sweep) has touched it yet.
                if idx + 1 < len(STEPS):
                    next_key, _, next_display = STEPS[idx + 1]
                    if run.step_progress.get(next_key) is None:
                        run.step_progress[next_key] = "running"
                        _append_event(run, "step", {
                            "key": next_key, "display": next_display, "status": "running",
                        })
        if run.proc.poll() is not None:
            # Nothing left to do here — _wait_and_reap runs its own
            # authoritative sweep for any step this loop didn't catch before
            # it emits the terminal "done" event, guaranteeing every step's
            # "done" lands ahead of "done" itself regardless of which thread
            # gets there first.
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
        # Persist cancellation so audit_reader survives server restarts
        try:
            (run.audit_dir / ".cancelled").write_text("true\n")
        except OSError:
            pass
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

    # Authoritative final sweep — _audit_watcher polls on its own schedule
    # (every AUDIT_POLL_INTERVAL) and can lose the race against THIS thread for
    # a fast-finishing run: proc.wait() above returns the instant the process
    # exits, but _audit_watcher might not wake up again for up to
    # AUDIT_POLL_INTERVAL seconds — long enough for the terminal "done" event
    # below to already be in the buffer and the SSE stream to close before
    # _audit_watcher ever gets to emit that last step's "done"/"failed" event.
    # Doing the same file-existence + outcome check here, BEFORE the terminal
    # event, guarantees every step's final status is in the buffer ahead of
    # "done" itself — a harmless duplicate if _audit_watcher's own sweep also
    # catches it first. The process has already exited by this point, so the
    # file is definitely this run's own final output, not the stale-leftover
    # race _step_file_is_fresh guards against in _audit_watcher — but this
    # calls it too anyway for defense-in-depth/consistency between the two.
    for _idx, (_key, _fname, _display) in enumerate(STEPS):
        if run.step_progress.get(_key) in ("done", "failed"):
            continue
        _file_path = run.audit_dir / _fname
        if _file_path.exists() and _step_file_is_fresh(run, _idx, _file_path):
            _step_status = "failed" if _step_has_error(_safe_load_json(_file_path)) else "done"
            run.step_progress[_key] = _step_status
            _append_event(run, "step", {
                "key": _key, "display": _display, "status": _step_status,
            })

    # Load ship data for the terminal event payload
    ship_path = run.audit_dir / "05-ship.json"
    pr_url = None
    verdict = None
    test_passed = None
    files_count = None
    ship_status = None
    ship_detail = None
    if ship_path.exists():
        import json as _json
        try:
            ship = _json.loads(ship_path.read_text())
            pr_url = ship.get("pr_url")
            verdict = ship.get("verdict")
            test_passed = ship.get("test_passed")
            files_count = ship.get("files_count")
            ship_status = ship.get("ship_status")
            ship_detail = ship.get("ship_detail")
        except (OSError, _json.JSONDecodeError):
            pass

    _append_event(run, "done", {
        "status": final_status,
        "exit_code": exit_code,
        "verdict": verdict,
        "pr_url": pr_url,
        "test_passed": test_passed,
        "files_count": files_count,
        "ship_status": ship_status,
        "ship_detail": ship_detail,
        "ended_at": run.ended_at,
        "duration": run.ended_at - run.started_at,
    })

    # Persist final snapshot
    storage.upsert(run.snapshot())

    # Clear active run marker
    with _registry_lock:
        if _active_session_id == run.session_id:
            _active_session_id = None

    # Kick off the next queued run, if any
    _start_next_from_queue()
