"""Stopping a run takes down what its steps started in sessions of their own.

shared/claude.py runs `claude -p` in its own session, out of reach of a signal to the
run's process group, and forwards SIGTERM to it. A step forked just after the
server's SIGTERM never gets one, and the SIGKILL that follows left its claude (and
browser) running. Real processes here: a step that ignores SIGTERM stands in for
that step, and `sleep` in its own session for its claude.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa_agents_server import runner  # noqa: E402

STEP = r'''
import signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
claude = subprocess.Popen(["sleep", "60"], start_new_session=True)
open(sys.argv[1], "w").write(str(claude.pid))
time.sleep(60)
'''


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(condition, seconds=10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_a_step_that_missed_the_sigterm_does_not_orphan_its_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_KILL_GRACE_SECONDS", 0.5)
    pid_file = tmp_path / "claude.pid"
    run = subprocess.Popen([sys.executable, "-c", STEP, str(pid_file)], start_new_session=True)
    claude = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text())
        claude = int(pid_file.read_text())

        assert runner._kill_group(run.pid, "test run")
        run.wait(timeout=5)
        assert _wait_until(lambda: not _alive(claude)), "the step's claude outlived the run"
    finally:
        for pid in (claude, run.pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        run.wait(timeout=5)


def test_descendant_groups_never_include_the_run_group_or_our_own(monkeypatch):
    table = "\n".join([
        " 100     1   100",     # run.sh, its own group
        " 101   100   100",     # a step in the run's group
        " 102   101   102",     # its claude, in a session of its own
        " 103   102   102",     # an MCP server under that claude
        " 104   102   104",     # a browser in yet another group
        " 200     1   200",     # something unrelated
    ])
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=table))
    assert runner._descendant_groups(100) == [102, 104]
