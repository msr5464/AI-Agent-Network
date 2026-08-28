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
from qa_agents_server import metrics_reader
from qa_agents_server.agents import (
    AgentConfigError,
    AgentSpec,
    DEFAULT_AGENT,
    get_agent,
)

# Retained for backwards compatibility: callers that predate multi-agent support
# assume the authoring agent.
AGENT = DEFAULT_AGENT
MAX_BUFFERED_EVENTS = 10_000
AUDIT_POLL_INTERVAL = 0.5  # seconds
CANCEL_GRACE_SECONDS = 5
# Whole-pipeline (run.sh, all 5 steps) kill timeout — not just one step's budget.
# Worst case with current per-step defaults: step02 alone can now take up to
# VALIDATE_WEB_TIMEOUT_S x (1+VALIDATE_WEB_RETRY_ATTEMPTS) = 1800x2 = 3600s;
# step04's fix loop can take MAX_FIX_ATTEMPTS x ~600s = ~1800s; steps 01/03/05
# add roughly another 1500s combined — so a 1800s default was already shorter
# than step02 alone could legitimately take even before its retry loop existed.
DEFAULT_RUN_TIMEOUT = 7200  # 2h


def run_timeout() -> int:
    """Read the timeout per call, not once at import.

    The admin Agent Settings page can change QA_AGENT_RUN_TIMEOUT_SECONDS at
    runtime; a module-level constant would silently ignore every save until the
    server was restarted.
    """
    try:
        return int(os.environ.get("QA_AGENT_RUN_TIMEOUT_SECONDS") or DEFAULT_RUN_TIMEOUT)
    except ValueError:
        return DEFAULT_RUN_TIMEOUT
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
    module: str          # the run's headline label: module, test name, or build tag
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
    # Parallel to step_progress rather than folded into it: step_progress is the
    # state machine (six string comparisons across two threads depend on its
    # shape), and timing has no business inside that.
    step_metrics: Dict[str, dict] = field(default_factory=dict)  # key -> {started_at,...}
    proc: Optional[subprocess.Popen] = None
    cond: threading.Condition = field(default_factory=threading.Condition)
    start_from_step: int = 1  # >1 means this run resumed an existing session
    agent: str = DEFAULT_AGENT
    payload: Dict = field(default_factory=dict)

    def snapshot(self) -> Dict:
        """Persistable snapshot (no Popen, no threading primitives)."""
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "module": self.module,
            "auto_push": self.auto_push,
            "audit_dir": str(self.audit_dir),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "start_from_step": self.start_from_step,
            # Totals only — the per-stage detail stays in the session's
            # metrics.json rather than bloating every registry entry.
            "metrics": self.metrics_totals,
        }

    @property
    def metrics_totals(self) -> Dict:
        try:
            from qa_agents_server import metrics_reader
            return metrics_reader.summary_fields(
                metrics_reader.read_session_metrics(self.audit_dir))
        except Exception:
            return {}


# ── Module state ──────────────────────────────────────────────────────────────
_runs: Dict[str, RunState] = {}
_active_session_id: Optional[str] = None
_registry_lock = threading.Lock()

# ── Pending queue ─────────────────────────────────────────────────────────────
# Each entry: {"agent": str, "payload": dict, "module": str, "auto_push": bool}
_pending_queue: List[Dict] = []
# Set once shutdown begins. Killing the active run makes its reaper try to start
# the next queued item, which would spawn fresh work while the server is on its
# way out — and that run would then be orphaned, since shutdown has already
# passed the point where it stops things.
_shutting_down = threading.Event()
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
        if _shutting_down.is_set() or not _pending_queue:
            return
        next_item = _pending_queue.pop(0)
    try:
        start_run(next_item.get("payload", {}),
                  agent=next_item.get("agent", DEFAULT_AGENT),
                  session_id=next_item.get("session_id"),
                  start_from_step=next_item.get("start_from_step", 1))
    except Exception as e:
        print(f"[runner] failed to start queued run for {next_item.get('module')!r}: {e}")


def _unique_session_id(spec, payload: dict) -> str:
    """A session id no other run is using.

    Session ids are timestamped to the second, so two runs submitted within the
    same second produce the SAME id — and queueing makes that easy to hit, since
    firing several runs back to back is exactly what the queue is for. They would
    then share one audit directory and overwrite each other's step output, and the
    runner's registry (keyed by session id) would hand the second run's live
    stream to the first.

    Callers must already hold BOTH _registry_lock and _queue_lock — this reads
    _runs and _pending_queue without taking either. Re-acquiring _registry_lock
    here would self-deadlock, since it is a plain Lock and both call sites are
    already inside it; taking it in the other order would risk an ABBA deadlock
    against the existing registry-then-queue ordering.
    """
    base = spec.make_session_id(payload)
    taken = {item.get("session_id") for item in _pending_queue} | set(_runs.keys())

    candidate = base
    suffix = 2
    while candidate in taken or (spec.audit_dir / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def get_queue(agent: Optional[str] = None) -> List[Dict]:
    """
    Return a snapshot of pending queue items.

    The queue itself is global — the run slot is shared, because every agent
    drives the same automation-repo checkout. But a caller asking on behalf of
    one agent wants only its own rows, so `agent` filters them. Each row keeps
    `index`, its position in the GLOBAL queue, which is what remove_from_queue
    takes; `position` is only the 1-based rank within the filtered view and is
    not a valid index once filtering is in play.
    """
    with _queue_lock:
        rows = [{"agent": item.get("agent", DEFAULT_AGENT),
                 "module": item.get("module"), "auto_push": item.get("auto_push"),
                 "session_id": item.get("session_id"),
                 "start_from_step": item.get("start_from_step", 1),
                 "index": i}
                for i, item in enumerate(_pending_queue)]
    if agent:
        rows = [r for r in rows if r["agent"] == agent]
    for rank, row in enumerate(rows):
        row["position"] = rank + 1
    return rows


def remove_from_queue(index: int, agent: Optional[str] = None) -> bool:
    """
    Remove the item at 0-based GLOBAL index. Returns True if removed.

    `agent`, when given, must match the item's own agent — one panel must not
    be able to delete another agent's queued run by index collision.
    """
    with _queue_lock:
        if not (0 <= index < len(_pending_queue)):
            return False
        if agent and _pending_queue[index].get("agent", DEFAULT_AGENT) != agent:
            return False
        _pending_queue.pop(index)
        return True


# ── Lifecycle ─────────────────────────────────────────────────────────────────
# An agent run is spawned with start_new_session=True, so the child is its own
# process-group leader and pgid == pid. Signalling the GROUP is what actually
# stops the work: run.sh itself does little, while its children (claude, mvn and
# the JVM surefire forks) are what burn time and tokens. Signalling only the
# direct child leaves those running.
_KILL_GRACE_SECONDS = 5


def _describe_pid(pid: int) -> str:
    """The command line of a live pid, or '' if it is gone/unreadable."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _is_our_agent_process(pid: int) -> bool:
    """Guard against PID reuse before signalling a pid we only know from disk.

    A pid persisted by an earlier server process may since have been recycled by
    something unrelated, and killing that would be considerably worse than
    leaving an orphan. Only proceed when the command line still looks like one
    of our agents' run.sh.
    """
    cmd = _describe_pid(pid)
    return bool(cmd) and "run.sh" in cmd and "-agent/" in cmd


def _kill_group(pid: int, label: str = "") -> bool:
    """SIGTERM a process group, then SIGKILL whatever is left. True if signalled."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False

    deadline = time.time() + _KILL_GRACE_SECONDS
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)          # still alive?
        except (ProcessLookupError, PermissionError):
            print(f"[runner] stopped {label or pid} (SIGTERM)")
            return True
        time.sleep(0.2)

    try:
        os.killpg(pgid, signal.SIGKILL)
        print(f"[runner] stopped {label or pid} (SIGKILL after "
              f"{_KILL_GRACE_SECONDS}s grace)")
    except (ProcessLookupError, PermissionError):
        pass
    return True


def _mark_interrupted(audit_dir: Path) -> None:
    """Leave a marker the status derivation can read.

    Without it a killed run keeps reporting "running" until the staleness
    window (15 min) elapses, so the history lies about what is happening right
    after a restart — which is exactly when someone is looking.
    """
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / ".interrupted").write_text("true\n")
    except OSError:
        pass


def reconcile_on_boot() -> None:
    """Mark stranded runs interrupted AND kill any that are still alive.

    A clean shutdown stops its own children, but SIGKILL of the server (crash,
    `kill -9`, a container OOM) cannot run any handler — so the previous
    process's run.sh and its claude/maven children survive, orphaned, still
    working against the automation repo and still spending tokens. Nothing
    tracked them any more: the registry is in memory and died with the process.
    Their pids are on disk, so boot is the one place they can be reaped.
    """
    stranded = [e for e in storage.load_all() if e.get("status") == "running"]

    killed = []
    for entry in stranded:
        pid = entry.get("pid")
        sid = entry.get("session_id") or "?"
        if not pid:
            continue
        if not _is_our_agent_process(int(pid)):
            continue        # already gone, or the pid now belongs to something else
        if _kill_group(int(pid), f"orphaned run {sid} (pid {pid})"):
            killed.append(sid)
            if entry.get("audit_dir"):
                _mark_interrupted(Path(entry["audit_dir"]))

    interrupted = storage.mark_all_running_as_interrupted()
    if killed:
        print(f"[runner] killed {len(killed)} orphaned run(s) left by a previous "
              f"server process: {', '.join(killed)}")
    if interrupted:
        print(
            f"[runner] marked {len(interrupted)} stranded run(s) as interrupted: "
            f"{', '.join(interrupted)}"
        )


def shutdown_all() -> None:
    """Stop every tracked run and its children. Signal handler / atexit."""
    _shutting_down.set()
    with _registry_lock:
        runs = list(_runs.values())

    stopped = []
    for run in runs:
        proc = run.proc
        if proc is None or proc.poll() is not None:
            continue
        # The whole group, not just run.sh — see _KILL_GRACE_SECONDS above.
        if _kill_group(proc.pid, f"run {run.session_id} (pid {proc.pid})"):
            stopped.append(run.session_id)
        _mark_interrupted(run.audit_dir)
        run.status = "interrupted"
        run.ended_at = time.time()
        try:
            storage.upsert(run.snapshot())
        except Exception:
            pass

    # Anything still waiting will never start; drop it so a restart does not
    # silently resurrect work the operator thought they had stopped.
    with _queue_lock:
        dropped = len(_pending_queue)
        _pending_queue.clear()

    if stopped or dropped:
        print(f"[runner] shutdown: stopped {len(stopped)} run(s), "
              f"dropped {dropped} queued")


# ── Public API ────────────────────────────────────────────────────────────────
def get_active_session_id() -> Optional[str]:
    return _active_session_id


def get_run(session_id: str) -> Optional[RunState]:
    return _runs.get(session_id)


def start_run(payload: Optional[Dict] = None, agent: str = DEFAULT_AGENT,
              session_id: Optional[str] = None, start_from_step: int = 1,
              **legacy) -> RunState:
    """Spawn an agent's run.sh. Raises RunnerError on validation.

    payload is the request body; each AgentSpec turns it into environment
    variables, which is the only place the agents genuinely differ. Authoring
    sends {module, auto_push}; healing sends {test, repair, force} for a
    standalone run or {build_tag} for a queued handoff.

    start_from_step > 1 resumes an EXISTING session (session_id required — it
    must already have valid output for every step before start_from_step)
    instead of starting a fresh one. Only the authoring agent supports this;
    the healing agent has its own internal retry loop. On resume the label can
    be omitted and is recovered from the session's own audit trail, since the
    queue file may already have been moved to processed/.

    **legacy accepts the pre-multi-agent keyword form start_run(module=..., auto_push=...).
    """
    global _active_session_id

    payload = dict(payload or {})
    if "module" in legacy or "auto_push" in legacy:
        payload.setdefault("module", legacy.get("module"))
        payload.setdefault("auto_push", legacy.get("auto_push", False))

    try:
        spec = get_agent(agent)
    except AgentConfigError as e:
        raise RunnerError(e.message, status=e.status)

    resuming = start_from_step > 1
    if resuming and not spec.supports_resume:
        raise RunnerError(f"{spec.name} does not support resuming from a step")

    if resuming:
        if not session_id:
            raise RunnerError("session_id is required to resume from a step > 1")
        if not (spec.audit_dir / session_id).exists():
            raise RunnerError(f"session not found: {session_id}", status=404)
        if not payload.get("module"):
            existing = _get_session(session_id)
            recovered = existing.get("module") if existing else None
            if not recovered:
                raise RunnerError(
                    f"could not determine module for session {session_id}", status=500
                )
            payload["module"] = recovered
        payload["start_from_step"] = start_from_step
    elif spec.queue_kind == "txt":
        # Any agent fed by a human-authored queue file: it must already exist.
        # Keyed on queue_kind rather than the agent name so a second .txt-queue
        # agent gets the same 404 instead of dying inside run.sh with a bare
        # non-zero exit and no API-level error.
        module = payload.get("module")
        if not module:
            raise RunnerError("module is required")
        if feature_exists(module, spec.name) is None:
            raise RunnerError(
                f"queue file not found: {module}.txt — create it first via "
                f"POST /agents/{spec.name}/queue",
                status=404,
            )

    if _shutting_down.is_set():
        raise RunnerError("server is shutting down", status=503)

    try:
        agent_env = spec.build_env(payload)
    except AgentConfigError as e:
        raise RunnerError(e.message, status=e.status)

    label = spec.describe_run(payload)

    if not spec.run_sh.exists():
        raise RunnerError(f"agent run.sh not found at {spec.run_sh}", status=500)

    with _registry_lock:
        if _active_session_id is not None:
            active = _runs.get(_active_session_id)
            if active and active.status == "running":
                # Queue instead of rejecting. The slot is deliberately global
                # across agents: they all mutate the same automation-repo
                # checkout, so overlapping runs would corrupt the working tree.
                with _queue_lock:
                    queued_session_id = (session_id if resuming
                                         else _unique_session_id(spec, payload))
                    _pending_queue.append({
                        "agent": spec.name, "payload": payload,
                        "module": label, "auto_push": bool(payload.get("auto_push")),
                        "session_id": queued_session_id,
                        "start_from_step": start_from_step,
                    })
                    position = len(_pending_queue)
                raise _QueuedNotification(position, queued_session_id)

        if not session_id:
            with _queue_lock:
                session_id = _unique_session_id(spec, payload)
        audit_dir = spec.audit_dir / session_id
        audit_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env.update(agent_env)
        env["SESSION_ID"] = session_id
        env["AUDIT_DIR"] = str(audit_dir)

        # Captured BEFORE Popen() (not after) — _audit_watcher uses this as the
        # cutoff for "did THIS run's own subprocess actually write this file,
        # or is it a stale leftover from a previous attempt" (see there for
        # why that distinction matters for a resumed/retried session).
        started_at = time.time()
        try:
            proc = subprocess.Popen(
                ["bash", str(spec.run_sh)],
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
            module=label,
            auto_push=bool(payload.get("auto_push")),
            agent=spec.name,
            payload=payload,
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
        "agent": run.agent,
        "module": run.module,
        "auto_push": run.auto_push,
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
    step at the agent's steps[idx].

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


def _mark_step_running(run: RunState, key: str) -> dict:
    """Stamp a step's start and return the payload extras for its event."""
    slot = run.step_metrics.setdefault(key, {})
    slot.setdefault("started_at", time.time())
    return {"started_at": slot["started_at"]}


def _mark_step_done(run: RunState, key: str) -> dict:
    """Close a step's timing and fold in whatever the agent recorded for it.

    The poller only knows "the moment I noticed the file", which is up to one
    poll interval late and meaningless for the first step. Where the agent wrote
    an exact duration into stages.jsonl, that wins.
    """
    slot = run.step_metrics.setdefault(key, {})
    slot["ended_at"] = time.time()
    if slot.get("started_at"):
        slot["duration_s"] = round(slot["ended_at"] - slot["started_at"], 3)

    try:
        session = metrics_reader.read_session_metrics(run.audit_dir)
        exact = metrics_reader.step_metrics(session, key)
        if exact:
            slot.update(exact)          # agent-side duration_s overrides the estimate
    except Exception:
        pass
    return dict(slot)


def _run_metrics(run: RunState) -> dict:
    """Run-level totals plus the stage breakdown, for the terminal event.

    Rebuilds from the JSONL streams when the agent never wrote metrics.json,
    so a killed run still reports what it spent.
    """
    try:
        session = metrics_reader.read_session_metrics(run.audit_dir)
        if not session:
            return {}
        data = metrics_reader.totals(session)
        data["stages"] = metrics_reader.stage_list(session)
        return data
    except Exception:
        return {}


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
    steps = get_agent(run.agent).steps
    if steps:
        bootstrap_idx = min(max(run.start_from_step - 1, 0), len(steps) - 1)
        first_key, _, first_display = steps[bootstrap_idx]
        if run.step_progress.get(first_key) is None:
            run.step_progress[first_key] = "running"
            _append_event(run, "step", {
                "key": first_key, "display": first_display, "status": "running",
                **_mark_step_running(run, first_key),
            })

    while True:
        if run.proc is None:
            return
        # Scan for new step files
        for idx, (key, fname, display) in enumerate(steps):
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
                    **_mark_step_done(run, key),
                })
                # Mark the next step as running (if any, and regardless of
                # whether THIS step failed — the pipeline still runs the next
                # step either way), but only if nobody (including
                # _wait_and_reap's sweep) has touched it yet.
                if idx + 1 < len(steps):
                    next_key, _, next_display = steps[idx + 1]
                    if run.step_progress.get(next_key) is None:
                        run.step_progress[next_key] = "running"
                        _append_event(run, "step", {
                            "key": next_key, "display": next_display, "status": "running",
                            **_mark_step_running(run, next_key),
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
        timeout_s = run_timeout()
        exit_code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _append_event(run, "error", {
            "source": "timeout",
            "message": f"run exceeded {timeout_s}s — killing",
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
    for _idx, (_key, _fname, _display) in enumerate(get_agent(run.agent).steps):
        if run.step_progress.get(_key) in ("done", "failed"):
            continue
        _file_path = run.audit_dir / _fname
        if _file_path.exists() and _step_file_is_fresh(run, _idx, _file_path):
            _step_status = "failed" if _step_has_error(_safe_load_json(_file_path)) else "done"
            run.step_progress[_key] = _step_status
            _append_event(run, "step", {
                "key": _key, "display": _display, "status": _step_status,
                **_mark_step_done(run, _key),
            })
        elif run.step_progress.get(_key) == "running":
            # _audit_watcher marks the NEXT step "running" as soon as the previous
            # one lands, which is right while a run is in flight and wrong once it
            # has ended: a run that stops early — EXPLORE_ONLY, an escalation, a
            # skip — leaves that optimistic chip spinning forever in the live UI.
            # The replayed stream never had this problem because it only reports
            # steps that actually produced a file, so the two views disagreed about
            # the same run. The process has already exited here, so a step with no
            # output did not run.
            run.step_progress[_key] = "skipped"
            _append_event(run, "step", {
                "key": _key, "display": _display, "status": "skipped",
            })

    # Load ship data for the terminal event payload. The filename comes from the
    # agent's own step model: this was hardcoded to authoring's "05-ship.json",
    # so a healing run (whose ship step is 02-ship.json) never carried pr_url on
    # the live stream — only the replay path through audit_reader compensated.
    ship_path = run.audit_dir / get_agent(run.agent).steps[-1][1]
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
        "metrics": _run_metrics(run),
    })

    # The agent writes its own analytics row at the end of run.sh, which is what
    # covers plain `make run` invocations the server never sees. This second call
    # is the fallback for a run whose run.sh was SIGKILLed and never got there.
    # Duplicate session ids are resolved newest-wins at read time.
    try:
        from qa_agents_server import analytics
        analytics.append_from_session(run.audit_dir, agent=run.agent,
                                      status=final_status, exit_code=exit_code,
                                      module=run.module, started_at=run.started_at,
                                      ended_at=run.ended_at, auto_push=run.auto_push)
    except Exception:
        pass

    # Persist final snapshot
    storage.upsert(run.snapshot())

    # Clear active run marker
    with _registry_lock:
        if _active_session_id == run.session_id:
            _active_session_id = None

    # Kick off the next queued run, if any
    _start_next_from_queue()
