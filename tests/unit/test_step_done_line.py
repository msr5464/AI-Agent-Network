"""The "✓ <step>" line: when it is timed, and where it lands in the log.

Both halves of one bug seen in the Live Run console:

    ✓ Reproduce — 0s                         <- the step took 3m 20s
    [12:42:28] ✓ [01/04] Reproduce — 3m 20s
    [12:42:28] Reproduced — handoff: ...     <- reproduce's own epilogue

The 0s came from the server taking a rollup's placeholder duration; the order
came from run.sh logging the ✓ before the epilogue that still belongs to it.
"""

import json
import subprocess
import time
from pathlib import Path

from qa_agents_server import runner
from qa_agents_server.paths import REPO_ROOT


def _run(tmp_path):
    audit = tmp_path / "20260923-123904-fix-SauceDemoWebTest"
    (audit / "metrics").mkdir(parents=True)
    return runner.RunState(session_id=audit.name, module="SauceDemoWebTest",
                           auto_push=False, audit_dir=audit, started_at=time.time())


def test_placeholder_duration_never_beats_the_measured_one(tmp_path):
    """A step whose stages.jsonl row has not landed yet keeps its real time.

    run.sh appends that row only after the step's JSON file exists, so a rollup
    rebuilt in between carries a stage built from the step's tool rows alone —
    duration_s 0.0 — and taking it reported a three-minute step as "0s".
    """
    run = _run(tmp_path)
    (run.audit_dir / "metrics" / "tools.jsonl").write_text(
        json.dumps({"stage": "reproduce", "duration_s": 12.0}) + "\n")

    run.step_metrics["reproduce"] = {"started_at": time.time() - 200}
    payload = runner._mark_step_done(run, "reproduce")

    assert payload["duration_s"] >= 199
    assert payload["tool_duration_s"] == 12.0


def test_recorded_duration_still_wins(tmp_path):
    """Once the agent's own row is there, it overrides the poller's estimate."""
    run = _run(tmp_path)
    (run.audit_dir / "metrics" / "stages.jsonl").write_text(json.dumps(
        {"index": 1, "key": "reproduce", "label": "[01/04] Reproduce", "attempt": 1,
         "started_at": 1000.0, "ended_at": 1200.0, "duration_s": 200.0,
         "exit_code": 0, "skipped": False}) + "\n")

    run.step_metrics["reproduce"] = {"started_at": time.time() - 5}
    assert runner._mark_step_done(run, "reproduce")["duration_s"] == 200.0


def test_step_done_line_closes_the_block(tmp_path):
    """run_step holds its ✓ back until the step's epilogue has been logged."""
    script = tmp_path / "fake-run.sh"
    script.write_text(
        f'source "{REPO_ROOT}/shared/session.sh"\n'
        'run_step "[01/04] Reproduce" "echo inside-the-step"\n'
        'log "Reproduced — handoff: 00-handoff.json"\n'
        'flush_step_done\n'
    )
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "AUDIT_DIR": ""}).stdout
    lines = [ln for ln in out.splitlines() if ln.strip()]

    assert "▶ [01/04] Reproduce" in lines[0]
    assert lines[1] == "inside-the-step"
    assert "Reproduced — handoff" in lines[2]
    assert "✓ [01/04] Reproduce" in lines[3]


def test_a_direct_done_marker_flushes_the_pending_one(tmp_path):
    """A run.sh that logs its own ✓ for a skipped step stays in order."""
    script = tmp_path / "fake-run.sh"
    script.write_text(
        f'source "{REPO_ROOT}/shared/session.sh"\n'
        'run_step "[01/05] Parse" "true"\n'
        'log "✓ [02/05] Validate Web — skipped (TESTING_MODE cache hit)"\n'
    )
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "AUDIT_DIR": ""}).stdout
    done = [ln for ln in out.splitlines() if "✓ " in ln]

    assert "[01/05] Parse" in done[0]
    assert "[02/05] Validate Web" in done[1]
