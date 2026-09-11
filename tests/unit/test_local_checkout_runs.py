"""Tests for AUTO_PUSH=false running in the developer's own checkout.

The isolated worktree silently voided a contract all three agents already
implemented: "no PR, edits left uncommitted for you to review". A run cut from
origin/<base> cannot see the locator you broke on purpose or the credentials you
refuse to commit, and the edits it leaves behind are deleted with the worktree
seconds later.

What is pinned here is the pair of properties that made that safe to change:
a local run never destroys the checkout it was given, and two of them never share
one working tree.
"""

import ast
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from qa_agents_server import runner
from qa_agents_server.agents import effective_auto_push
from shared import workspace


@pytest.fixture(autouse=True)
def clean_registry():
    runner._runs.clear()
    runner._active_runs.clear()
    runner._pending_queue.clear()
    runner._starting.clear()
    runner._starting_local.clear()
    yield
    runner._runs.clear()
    runner._active_runs.clear()
    runner._pending_queue.clear()
    runner._starting.clear()
    runner._starting_local.clear()


def _repo(path: Path) -> Path:
    """A real git checkout with one commit."""
    path.mkdir(parents=True)
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=path, capture_output=True)
    (path / "Page.java").write_text("class Page { String sel = \"#old\"; }\n")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, capture_output=True)
    return path


# ── The mode is read from the environment the run will actually get ───────────

def test_absent_auto_push_falls_back_to_config_not_to_false(monkeypatch):
    """payload.get("auto_push") is not the answer.

    _auto_push_env exports nothing when the caller expressed no preference, so a
    reader that checked the payload would call every such run a dry run — and now
    that the flag also picks WHERE the run executes, that would quietly move a
    real PR run into the developer's checkout.
    """
    monkeypatch.setenv("AUTO_PUSH", "true")
    assert effective_auto_push({}) is True
    assert effective_auto_push({"AUTO_PUSH": "false"}) is False
    monkeypatch.setenv("AUTO_PUSH", "false")
    assert effective_auto_push({}) is False
    assert effective_auto_push({"AUTO_PUSH": "true"}) is True


# ── A local run never destroys the checkout ───────────────────────────────────

def test_checkout_base_refuses_during_a_local_run(tmp_path, monkeypatch):
    """The one gate that matters, at the only place that performs the damage.

    Three agents call checkout_base with three different ideas of when it is
    safe; the refusal lives here so a call site added later inherits it.
    """
    clone = _repo(tmp_path / "clone")
    subprocess.run(["git", "checkout", "-q", "-b", "feature/local"],
                   cwd=clone, capture_output=True)
    (clone / "Page.java").write_text("class Page { String sel = \"#broken\"; }\n")

    monkeypatch.setenv(workspace.LOCAL_RUN_ENV, "1")
    result = workspace.checkout_base(clone, "main")

    assert result["ok"] is False
    assert "local" in result["reason"].lower()
    # The uncommitted edit and the branch both survive — that is the whole point.
    assert "#broken" in (clone / "Page.java").read_text()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=clone, capture_output=True, text=True)
    assert branch.stdout.strip() == "feature/local"


def test_checkout_base_still_works_without_the_flag(tmp_path, monkeypatch):
    """Regression: AUTO_PUSH=true keeps today's behaviour exactly."""
    monkeypatch.delenv(workspace.LOCAL_RUN_ENV, raising=False)
    monkeypatch.delenv("QA_ISOLATED_WORKTREE_READY", raising=False)
    clone = _repo(tmp_path / "clone")
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=clone, capture_output=True)

    assert workspace.checkout_base(clone, "main", "main")["ok"] is True
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=clone, capture_output=True, text=True)
    assert branch.stdout.strip() == "main"


def test_teardown_never_names_the_developers_checkout():
    """worktree_path is what teardown deletes, so a local run must leave it empty.

    _wait_and_reap force-removes the worktree and then shutil.rmtree's whatever
    is left. Handing it the main checkout would delete the developer's repo, so
    the assignment is pinned statically rather than left to a comment.
    """
    src = (REPO_ROOT / "qa_agents_server" / "runner.py").read_text()
    assignment = re.search(r'worktree_path=([^,\n]+)', src)
    assert assignment, "start_run no longer sets worktree_path on the run"
    assert 'local_mode' in assignment.group(1), (
        f"worktree_path is set to {assignment.group(1)!r} without a local-mode "
        f"guard — a local run would have its checkout deleted at teardown")


def test_local_run_keeps_its_baseline_dir():
    """The HEALING_BASELINE_DIR pop exists only because a worktree splits it.

    In a local run the variable already points inside the checkout being used, so
    dropping it would send the framework's fingerprints somewhere else.
    """
    tree = ast.parse((REPO_ROOT / "qa_agents_server" / "runner.py").read_text())
    pops = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "pop" and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "HEALING_BASELINE_DIR"]
    assert pops, "the HEALING_BASELINE_DIR pop is gone entirely"
    for pop in pops:
        branch = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.If)
                      and any(pop is c for c in ast.walk(n)))
        # It must sit under `if local_mode: ... else: <pop>`, never unguarded.
        assert any(isinstance(t, ast.Name) and t.id == "local_mode"
                   for t in ast.walk(branch.test)), \
            "HEALING_BASELINE_DIR is dropped without checking local_mode"
        assert any(pop is c for c in ast.walk(ast.Module(body=branch.orelse,
                                                         type_ignores=[]))), \
            "HEALING_BASELINE_DIR is dropped on the local-mode branch"


# ── Two local runs never share one working tree ───────────────────────────────

def _local_run(session_id):
    from qa_agents_server.runner import RunState
    run = RunState(session_id=session_id, module="m", auto_push=False,
                   audit_dir=Path("/tmp/does-not-matter"), started_at=time.time(),
                   local_mode=True)
    runner._runs[session_id] = run
    runner._active_runs[session_id] = run
    return run


def test_an_active_local_run_occupies_the_checkout():
    assert runner._local_slot_busy() is False
    _local_run("local-1")
    assert runner._local_slot_busy() is True


def test_a_starting_local_run_occupies_it_too():
    """_active_runs is populated after the subprocess spawns.

    Two dry runs fired together both looked at an empty registry and both started
    in the same working tree; the reservation has to cover the gap.
    """
    runner._starting_local.add("local-starting")
    assert runner._local_slot_busy() is True


def test_isolated_runs_do_not_occupy_the_checkout():
    """Worktree runs stay parallel — only local-vs-local contends."""
    from qa_agents_server.runner import RunState
    run = RunState(session_id="wt-1", module="m", auto_push=True,
                   audit_dir=Path("/tmp/does-not-matter"), started_at=time.time(),
                   worktree_path="/tmp/wt-1", work_dir="/tmp/wt-1")
    runner._runs["wt-1"] = run
    runner._active_runs["wt-1"] = run
    assert runner._local_slot_busy() is False


def test_queue_drain_skips_a_local_run_while_the_checkout_is_busy():
    """Skipped, not started-and-requeued.

    start_run would push it straight back and _start_next_from_queue returns
    after one attempt — so picking the blocked item stranded every isolated run
    queued behind it until something else finished.
    """
    _local_run("local-1")
    runner._pending_queue.extend([
        {"agent": "test-healing-agent", "payload": {}, "module": "dry",
         "local_mode": True, "user_id": "default"},
        {"agent": "test-healing-agent", "payload": {}, "module": "pr",
         "local_mode": False, "user_id": "default"},
    ])
    started = []
    original = runner.start_run
    try:
        runner.start_run = lambda payload, **kw: started.append(kw.get("session_id") or payload)
        runner._start_next_from_queue()
    finally:
        runner.start_run = original

    assert len(started) == 1
    assert [item["module"] for item in runner._pending_queue] == ["dry"], \
        "the blocked local run should still be queued"


def test_queue_drain_stops_when_only_blocked_local_runs_remain():
    _local_run("local-1")
    runner._pending_queue.append(
        {"agent": "test-healing-agent", "payload": {}, "module": "dry",
         "local_mode": True, "user_id": "default"})
    started = []
    original = runner.start_run
    try:
        runner.start_run = lambda payload, **kw: started.append(payload)
        runner._start_next_from_queue()
    finally:
        runner.start_run = original

    assert started == []
    assert len(runner._pending_queue) == 1


# ── Artefacts survive a local run ─────────────────────────────────────────────

def test_artefacts_are_preserved_from_the_local_checkout(tmp_path):
    """_preserve_worktree_artefacts read worktree_path, which a local run leaves
    empty — so every dry run would have lost its screenshots and traces."""
    from qa_agents_server.runner import RunState
    checkout = tmp_path / "checkout"
    (checkout / "test-output").mkdir(parents=True)
    (checkout / "test-output" / "shot.png").write_bytes(b"png")
    audit = tmp_path / "audit"
    audit.mkdir()

    run = RunState(session_id="local-1", module="m", auto_push=False,
                   audit_dir=audit, started_at=time.time(),
                   local_mode=True, work_dir=str(checkout))
    runner._preserve_worktree_artefacts(run)

    assert (audit / "test-output" / "shot.png").read_bytes() == b"png"


# ── What the subprocess is actually handed ────────────────────────────────────

def _start_and_capture_env(monkeypatch, tmp_path, checkout, auto_push):
    """Drive start_run far enough to see the environment, then stop it dead.

    Popen raises after recording, which start_run turns into a RunnerError and
    unwinds cleanly — no threads, no reaper, no spawned bash.
    """
    import dataclasses
    from qa_agents_server.agents import get_agent
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        raise OSError("stopped before spawning")

    # AgentSpec is frozen, so the audit root is redirected by swapping the
    # lookup rather than the field — a real run's audit tree stays untouched.
    spec = dataclasses.replace(get_agent("test-healing-agent"),
                               audit_dir=tmp_path / "audit")
    monkeypatch.setattr(runner, "get_agent", lambda name: spec)
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr("shared.workspace.ensure", lambda *a, **k: checkout)
    monkeypatch.setenv("AUTO_PUSH", "true" if auto_push else "false")

    with pytest.raises(runner.RunnerError):
        runner.start_run({"test": "NaukriProfileSummaryWebTest",
                          "auto_push": auto_push},
                         agent="test-healing-agent")
    return captured["env"]


def test_a_dry_run_executes_in_the_developers_checkout(tmp_path, monkeypatch):
    checkout = _repo(tmp_path / "automation")
    monkeypatch.setattr(
        "shared.workspace.prepare_worktree",
        lambda *a, **k: pytest.fail("a dry run must not create a worktree"))

    env = _start_and_capture_env(monkeypatch, tmp_path, checkout, auto_push=False)

    assert env["FRAMEWORK_DIR"] == str(checkout)
    assert env[workspace.LOCAL_RUN_ENV] == "1"
    assert "QA_ISOLATED_WORKTREE_READY" not in env, \
        "the isolation flag would make checkout_base detach the developer's HEAD"


def test_an_auto_push_run_still_gets_an_isolated_worktree(tmp_path, monkeypatch):
    """Regression: the whole point is that only the dry-run path changed."""
    checkout = _repo(tmp_path / "automation")
    prepared = []
    monkeypatch.setattr("shared.workspace.prepare_worktree",
                        lambda main, wt, branch, **k: prepared.append(wt) or {"ok": True})
    monkeypatch.setenv("QA_WORKTREE_TEMP_DIR", str(tmp_path / "runs"))

    env = _start_and_capture_env(monkeypatch, tmp_path, checkout, auto_push=True)

    assert prepared, "prepare_worktree was not called for an auto-push run"
    assert env["FRAMEWORK_DIR"] == prepared[0]
    assert env["FRAMEWORK_DIR"] != str(checkout)
    assert env["QA_ISOLATED_WORKTREE_READY"] == "1"
    assert workspace.LOCAL_RUN_ENV not in env
