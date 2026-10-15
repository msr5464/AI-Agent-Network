"""Booting a server must not kill another server's live runs.

`reconcile_on_boot` exists to reap run.sh processes orphaned by a server that
died without running its handlers. It identified them as "status == running in
storage, and the pid still looks like one of our agents" — which is equally true
of a run a *healthy* server is in the middle of. Starting a second server (which
then exits on the port conflict) killed the first one's work: an observed
adaptation run died 47 seconds into its Adapt step, with the original server
still up and serving.

The record now says which server owns the run, and reconciliation reaps only
what a dead owner left behind.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa_agents_server import runner, storage                    # noqa: E402

LIVE_OWNER = 1          # launchd: always running, never us
DEAD_OWNER = 999_999    # high enough to be unused on a normal machine


def _entry(server_pid, session="20260916-124138-adapt-x", pid=4242):
    return {"session_id": session, "status": "running", "pid": pid,
            "server_pid": server_pid, "audit_dir": "/tmp/does-not-matter"}


def _arrange(monkeypatch, entries):
    """Reconciliation with its dangerous edges replaced by recorders."""
    killed, marked = [], []
    monkeypatch.setattr(storage, "load_all", lambda: entries)
    monkeypatch.setattr(runner, "_is_our_agent_process", lambda pid: True)
    monkeypatch.setattr(runner, "_kill_group",
                        lambda pid, label="": killed.append(pid) or True)
    monkeypatch.setattr(runner, "_mark_interrupted", lambda d: marked.append(str(d)))
    return killed, marked


def test_a_run_owned_by_a_live_server_is_left_alone(monkeypatch):
    killed, marked = _arrange(monkeypatch, [_entry(LIVE_OWNER)])
    runner.reconcile_on_boot()
    assert killed == [], "that run belongs to a server that is still working"
    assert marked == []


def test_a_run_whose_owner_is_gone_is_reaped(monkeypatch):
    killed, marked = _arrange(monkeypatch, [_entry(DEAD_OWNER)])
    runner.reconcile_on_boot()
    assert killed == [4242], "nothing is tracking this one any more"
    assert marked, "and the history must stop claiming it is still running"


def test_a_record_without_an_owner_still_gets_reaped(monkeypatch):
    # Written by a server from before this field existed. The old behaviour is
    # the safe default there: an untracked run.sh is what this function is for.
    killed, _ = _arrange(monkeypatch, [_entry(None)])
    runner.reconcile_on_boot()
    assert killed == [4242]


def test_our_own_leftover_record_is_reaped(monkeypatch):
    # Same pid as this process: a crash-restart under a process manager that
    # reuses the pid, or a stale record we wrote ourselves.
    killed, _ = _arrange(monkeypatch, [_entry(os.getpid())])
    runner.reconcile_on_boot()
    assert killed == [4242]


def test_a_finished_run_is_never_touched(monkeypatch):
    done = dict(_entry(DEAD_OWNER), status="completed")
    killed, marked = _arrange(monkeypatch, [done])
    runner.reconcile_on_boot()
    assert (killed, marked) == ([], [])
