"""Tests for the multi-user parallel-execution guarantees.

These pin the four things that were actually broken rather than the feature as a
whole: the two locks must be taken in one consistent order, the runner and
shared/workspace must agree on the name of the worktree flag, an attacker-shaped
X-User-ID must never become a path segment, and a missing identity header must
deny rather than grant.

Every case here corresponds to a defect that shipped, so a regression is a
returning bug, not a new one.
"""

import ast
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from qa_agents_server import feature_files, runner


# ── Lock ordering ─────────────────────────────────────────────────────────────
def _lock_sequence(func_name: str) -> list:
    """The order _registry_lock / _queue_lock are entered inside one function.

    Read from the AST rather than by running it: the deadlock needs two threads
    arriving at the wrong moment, so a timing test would pass by luck most runs.
    The ordering is a static property and is checked as one.
    """
    tree = ast.parse((REPO_ROOT / "qa_agents_server" / "runner.py").read_text())
    target = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == func_name)
    seen = []
    for node in ast.walk(target):
        if isinstance(node, ast.With):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Name) and expr.id in ("_registry_lock", "_queue_lock"):
                    seen.append((node.lineno, expr.id))
    return [name for _, name in sorted(seen)]


def test_queue_drain_does_not_invert_lock_order():
    """_start_next_from_queue must not hold _queue_lock while taking _registry_lock.

    It used to do exactly that while start_run took them registry-then-queue —
    an ABBA deadlock that wedged the whole run subsystem the moment a run
    finished at the same time as one starting.
    """
    order = _lock_sequence("_start_next_from_queue")
    for earlier, later in zip(order, order[1:]):
        assert not (earlier == "_queue_lock" and later == "_registry_lock"), (
            f"queue-then-registry in _start_next_from_queue: {order}")


def test_start_run_keeps_registry_before_queue():
    order = _lock_sequence("start_run")
    assert order, "expected start_run to take at least one lock"
    assert order[0] == "_registry_lock", order


def test_locks_are_never_held_across_worktree_creation():
    """Preparing a worktree can take minutes; holding the registry lock across it
    blocked every reader, GET /run/active included."""
    source = (REPO_ROOT / "qa_agents_server" / "runner.py").read_text()
    tree = ast.parse(source)
    start_run = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "start_run")
    for node in ast.walk(start_run):
        if not isinstance(node, ast.With):
            continue
        if not any(isinstance(i.context_expr, ast.Name)
                   and i.context_expr.id == "_registry_lock" for i in node.items):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        assert "prepare_worktree" not in body, "worktree prep inside _registry_lock"
        assert "'ensure'" not in body and '"ensure"' not in body, "clone inside _registry_lock"


# ── Worktree flag agreement ───────────────────────────────────────────────────
def test_runner_and_workspace_agree_on_the_isolation_flag():
    """The detach guard only fires if both sides spell the variable the same.

    They did not: the runner exported QA_ISOLATED_WORKTREE_READY while
    workspace.checkout_base read QA_ISOLATED_WORKTREE, so every parallel run
    took the `checkout -B <branch>` path and collided with
    "'<branch>' is already used by worktree at ...".
    """
    runner_src = (REPO_ROOT / "qa_agents_server" / "runner.py").read_text()
    workspace_src = (REPO_ROOT / "shared" / "workspace.py").read_text()

    exported = set(re.findall(r'env\["(QA_ISOLATED_WORKTREE\w*)"\]', runner_src))
    assert exported == {"QA_ISOLATED_WORKTREE_READY"}, exported

    read_in_checkout = re.search(
        r'def checkout_base.*?os\.environ\.get\("(QA_ISOLATED_WORKTREE\w*)"\)',
        workspace_src, re.S)
    assert read_in_checkout, "checkout_base no longer consults the isolation flag"
    assert read_in_checkout.group(1) in exported, (
        f"checkout_base reads {read_in_checkout.group(1)}, runner exports {exported}")


def test_detached_worktree_flag_selects_detach(tmp_path, monkeypatch):
    """With the flag set, checkout_base must detach rather than move a branch."""
    from shared import workspace

    origin = tmp_path / "origin"
    origin.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=origin, capture_output=True)
    (origin / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=origin, capture_output=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], capture_output=True)

    monkeypatch.setenv("QA_ISOLATED_WORKTREE_READY", "1")
    assert workspace.checkout_base(clone, "main")["ok"]

    head = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"],
                          cwd=clone, capture_output=True, text=True)
    assert head.returncode != 0, "expected a detached HEAD under the isolation flag"


# ── Identity is never a path ──────────────────────────────────────────────────
@pytest.mark.parametrize("hostile", [
    "../../../../tmp/pwn",      # traversal
    "/tmp/pwn",                 # absolute segment wins outright in pathlib
    "..",
    ".",
    "",
    "has space",
    "semi;colon",
    "a" * 200,
])
def test_hostile_user_id_cannot_escape_the_queue_directory(hostile):
    """X-User-ID reached _queue_dir as a raw path segment.

    Path("agents/x/queue") / "/tmp/pwn" == Path("/tmp/pwn"), so this was
    arbitrary directory creation and .txt write as the server user.
    """
    resolved = feature_files._safe_user_id(hostile)
    assert resolved == "default"

    queue_dir = feature_files._queue_dir("test-authoring-agent", hostile).resolve()
    agent_queue = feature_files.get_agent("test-authoring-agent").queue_dir.resolve()
    assert agent_queue == queue_dir or agent_queue in queue_dir.parents


def test_legitimate_user_id_is_preserved():
    """AI-Test-Studio derives ids as md5(username)[:12]."""
    assert feature_files._safe_user_id("21232f297a57") == "21232f297a57"


def test_readable_user_id_is_a_safe_path_segment():
    """`user-admin` replaced the hash as the identity, so it is now what gets
    joined onto the queue path and must survive _safe_user_id unchanged."""
    assert feature_files._safe_user_id("user-admin") == "user-admin"
    assert feature_files._safe_user_id("user-tester_1788776099") == "user-tester_1788776099"


# ── Readable identity ─────────────────────────────────────────────────────────
def _identity(headers: dict) -> str:
    """current_user_id() under a request carrying `headers`."""
    from flask import Flask
    from qa_agents_server import routes

    app = Flask(__name__)
    with app.test_request_context(headers=headers):
        return routes.current_user_id()


def test_username_that_matches_its_hash_becomes_the_identity():
    """The whole point: a person recognises queue/user-admin as theirs."""
    assert _identity({"X-User-ID": "21232f297a57",
                      "X-User-Name": "admin"}) == "user-admin"


def test_username_that_does_not_hash_to_the_id_is_ignored():
    """X-User-Name is otherwise a second, independent way to name a directory —
    a proxy bug or a stale session could point it at someone else's queue. The
    id is authoritative; a name that does not derive from it is discarded."""
    assert _identity({"X-User-ID": "21232f297a57",
                      "X-User-Name": "someone-else"}) == "21232f297a57"


def test_unauthenticated_placeholder_stays_anonymous():
    """The proxy sends "Unknown" when no one is logged in. That must not mint a
    user-unknown queue — it has to keep falling through to the shared one."""
    assert _identity({"X-User-Name": "Unknown"}) == routes_anonymous()
    assert _identity({"X-User-ID": "not-a-hash",
                      "X-User-Name": "Unknown"}) == routes_anonymous()


def test_hostile_username_cannot_escape_the_queue_directory():
    """A name is path content the moment it is trusted, so it is regex-gated
    before it can become one — and never reaches _safe_user_id as `../`."""
    for hostile in ("../../etc", "/tmp/pwn", "a/b", "..", "with space"):
        assert _identity({"X-User-ID": "21232f297a57",
                          "X-User-Name": hostile}) == "21232f297a57"


def routes_anonymous() -> str:
    from qa_agents_server import routes
    return routes.ANONYMOUS_USER_ID


# ── The server and run.sh must look in the same directory ─────────────────────
def test_server_and_run_sh_agree_on_the_queue_directory():
    """A spec written where run.sh does not look can be listed but never run.

    _queue_dir appended the id unconditionally, so the anonymous/CLI identity
    put the server in queue/default while run.sh read queue/ — and nothing the
    boot seeder wrote was visible to anyone.
    """
    from qa_agents_server.agents import AGENTS

    for name, spec in AGENTS.items():
        if spec.queue_kind != "txt":
            continue
        shared = set(re.findall(r'"\$USER_ID" == "(\w+)"', spec.run_sh.read_text()))
        assert shared == set(feature_files._SHARED_QUEUE_IDS), (
            f"{name}/run.sh shares the queue root with {sorted(shared)}, "
            f"the server with {sorted(feature_files._SHARED_QUEUE_IDS)}")

        for anon in shared:
            assert feature_files._queue_dir(name, anon) == spec.queue_dir
        assert (feature_files._queue_dir(name, "21232f297a57")
                == spec.queue_dir / "21232f297a57")


def test_a_new_users_queue_gets_the_shipped_examples(tmp_path, monkeypatch):
    """Boot-time seeding fills the queue ROOT, which no logged-in user reads —
    so every picker in the UI was empty and said so."""
    from dataclasses import replace

    from qa_agents_server import seed_examples
    from qa_agents_server.agents import AGENTS

    examples = tmp_path / "examples" / "test-authoring-agent"
    examples.mkdir(parents=True)
    (examples / "demo.txt").write_text("Module: demo\nType: web\n\nSteps:\n1. Go\n")
    monkeypatch.setattr(seed_examples, "EXAMPLES_DIR", tmp_path / "examples")
    monkeypatch.delenv("QA_SEED_EXAMPLES", raising=False)
    spec = replace(AGENTS["test-authoring-agent"], queue_dir=tmp_path / "queue")
    monkeypatch.setattr(feature_files, "get_agent", lambda _name: spec)

    listed = feature_files.list_features("test-authoring-agent", user_id="21232f297a57")
    assert [item["name"] for item in listed] == ["demo"]

    # Once per user: an example they delete stays deleted.
    (tmp_path / "queue" / "21232f297a57" / "demo.txt").unlink()
    assert feature_files.list_features(
        "test-authoring-agent", user_id="21232f297a57") == []


def test_per_user_seeding_respects_the_off_switch(tmp_path, monkeypatch):
    from dataclasses import replace

    from qa_agents_server import seed_examples
    from qa_agents_server.agents import AGENTS

    examples = tmp_path / "examples" / "test-authoring-agent"
    examples.mkdir(parents=True)
    (examples / "demo.txt").write_text("Module: demo\n")
    monkeypatch.setattr(seed_examples, "EXAMPLES_DIR", tmp_path / "examples")
    monkeypatch.setenv("QA_SEED_EXAMPLES", "false")
    spec = replace(AGENTS["test-authoring-agent"], queue_dir=tmp_path / "queue")
    monkeypatch.setattr(feature_files, "get_agent", lambda _name: spec)

    assert feature_files.list_features(
        "test-authoring-agent", user_id="21232f297a57") == []


# ── Ownership defaults to deny ────────────────────────────────────────────────
def test_missing_identity_header_does_not_grant_access():
    """The old guard read `if run_record and user_id and ...`, so omitting the
    header short-circuited the comparison and granted access to any session."""
    from qa_agents_server import routes

    # Strip comments and docstrings first: the module explains the old bug in
    # prose, and matching that prose would fail for the wrong reason.
    source = (REPO_ROOT / "qa_agents_server" / "routes.py").read_text()
    code_only = "\n".join(line.split("#")[0] for line in source.splitlines())
    assert "and user_id and" not in code_only, (
        "the missing-header bypass is back in an ownership check")

    tree = ast.parse(source)
    guarded = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Name) and d.id == "owns_session"
                for d in node.decorator_list)
    }
    for endpoint in ("run_stream", "sessions_get", "session_events",
                     "session_metrics", "run_cancel", "session_retry"):
        matches = [n for n in guarded if endpoint in n or n in endpoint]
        assert matches, f"{endpoint} is not guarded by @owns_session (guarded: {sorted(guarded)})"


def test_queue_removal_is_scoped_to_the_owner():
    """An index is a position, not a capability."""
    import inspect
    signature = inspect.signature(runner.remove_from_queue)
    assert "user_id" in signature.parameters

    runner._pending_queue.clear()
    runner._pending_queue.append(
        {"agent": "test-authoring-agent", "module": "m", "user_id": "aaaaaaaaaaaa"})
    try:
        assert not runner.remove_from_queue(0, "test-authoring-agent",
                                            user_id="bbbbbbbbbbbb")
        assert runner.remove_from_queue(0, "test-authoring-agent",
                                        user_id="aaaaaaaaaaaa")
    finally:
        runner._pending_queue.clear()


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_agent_settings_are_admin_only(method, monkeypatch):
    """/settings reads GITHUB_TOKEN and writes config/.env. Studio's proxy once let a
    member reach it with ../../settings; this server must refuse that by itself."""
    from flask import Flask
    from qa_agents_server import agent_settings, routes

    writes = []
    monkeypatch.setattr(agent_settings, "get_all_for_api", lambda: {"fields": []})
    monkeypatch.setattr(agent_settings, "set_many", writes.append)
    monkeypatch.delenv("QA_AGENT_PROXY_SECRET", raising=False)
    app = Flask(__name__)
    app.register_blueprint(routes.qa_bp)
    client = app.test_client()

    def status(**headers):
        return client.open("/settings", method=method, json={}, headers=headers).status_code

    assert status() == 403
    assert status(**{"X-User-Role": "member"}) == 403
    assert status(**{"X-User-Role": "admin"}) == 200
    monkeypatch.setenv("QA_AGENT_PROXY_SECRET", "s3cret")
    assert status(**{"X-User-Role": "admin"}) == 403      # a typed header is not the proxy
    assert status(**{"X-User-Role": "admin", "X-Proxy-Secret": "s3cret"}) == 200
    assert len(writes) == (2 if method == "PUT" else 0)


# ── Worktree root validation ──────────────────────────────────────────────────
@pytest.mark.parametrize("unsafe", ["/", "/Users", "/tmp", "~", ""])
def test_unsafe_worktree_roots_fall_back_to_the_default(unsafe, monkeypatch):
    """reconcile_on_boot deletes directories under this root, so a typo in the
    admin settings box would otherwise recursively delete whatever was typed."""
    monkeypatch.setenv("QA_WORKTREE_TEMP_DIR", unsafe)
    assert runner._worktree_temp_dir() == runner.DEFAULT_WORKTREE_TEMP_DIR


def test_reasonable_worktree_root_is_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("QA_WORKTREE_TEMP_DIR", str(tmp_path / "runs"))
    assert runner._worktree_temp_dir() == str(tmp_path / "runs")


# ── Multi-run visibility ──────────────────────────────────────────────────────
def _fake_run(session_id, agent, module, started, user_id):
    from qa_agents_server.runner import RunState
    return RunState(session_id=session_id, module=module, auto_push=False,
                    audit_dir=Path("/tmp/does-not-matter"), started_at=started,
                    agent=agent, user_id=user_id)


@pytest.fixture
def registry():
    """Populate the run registry, and always leave it empty again."""
    runner._runs.clear()
    runner._active_runs.clear()
    runner._pending_queue.clear()

    def add(session_id, agent, module, started, user_id):
        run = _fake_run(session_id, agent, module, started, user_id)
        runner._runs[session_id] = run
        runner._active_runs[session_id] = run
        return run

    yield add
    runner._runs.clear()
    runner._active_runs.clear()
    runner._pending_queue.clear()


def test_every_run_a_user_has_in_flight_is_reported(registry):
    """A user's second concurrent run was invisible.

    get_active_session_id returns the FIRST match, which is dict-ordering
    dependent — so with two runs the UI showed an arbitrary one and offered no
    route to the other.
    """
    registry("s-b", "test-authoring-agent", "checkout", 200, "aaaaaaaaaaaa")
    registry("s-a", "test-authoring-agent", "payments", 100, "aaaaaaaaaaaa")
    registry("s-c", "test-healing-agent", "LoginTest", 300, "aaaaaaaaaaaa")
    registry("s-x", "test-authoring-agent", "theirs", 50, "bbbbbbbbbbbb")

    mine = runner.get_active_runs("aaaaaaaaaaaa")
    assert [r.session_id for r in mine] == ["s-a", "s-b", "s-c"], "expected oldest-first"

    scoped = runner.get_active_runs("aaaaaaaaaaaa", agent="test-authoring-agent")
    assert [r.session_id for r in scoped] == ["s-a", "s-b"]

    # Another user's run is never listed.
    assert all(r.user_id == "aaaaaaaaaaaa" for r in mine)


def test_active_runs_are_ordered_stably(registry):
    """A UI rendering one tab per run must not have them reorder underneath
    the person using it, so ordering is by start time, not dict order."""
    for i, sid in enumerate(["z", "m", "a"]):
        registry(sid, "test-authoring-agent", sid, 300 - i * 100, "aaaaaaaaaaaa")
    first = [r.session_id for r in runner.get_active_runs("aaaaaaaaaaaa")]
    assert first == sorted(first, key=lambda s: {"z": 300, "m": 200, "a": 100}[s])
    assert first == [r.session_id for r in runner.get_active_runs("aaaaaaaaaaaa")]


def test_capacity_reports_numerator_and_denominator(registry, monkeypatch):
    """The server sent only a boolean `busy`, which cannot render "2 of 4
    workers busy" — the numbers were missing on both sides."""
    monkeypatch.setenv("QA_MAX_CONCURRENT_RUNS", "4")
    registry("s-1", "test-authoring-agent", "a", 100, "aaaaaaaaaaaa")
    registry("s-2", "test-authoring-agent", "b", 200, "bbbbbbbbbbbb")

    cap = runner.capacity("aaaaaaaaaaaa")
    assert cap["active"] == 2 and cap["max"] == 4
    assert cap["busy"] is False
    assert cap["mine_active"] == 1

    registry("s-3", "test-authoring-agent", "c", 300, "bbbbbbbbbbbb")
    registry("s-4", "test-authoring-agent", "d", 400, "bbbbbbbbbbbb")
    assert runner.capacity("aaaaaaaaaaaa")["busy"] is True


def test_queued_rows_are_marked_with_ownership(registry):
    """A person queued behind two of someone else's runs needs to see that the
    wait is real; `mine` marks the rows they may cancel."""
    runner._pending_queue.extend([
        {"agent": "test-authoring-agent", "module": "theirs", "user_id": "bbbbbbbbbbbb"},
        {"agent": "test-authoring-agent", "module": "mine", "user_id": "aaaaaaaaaaaa"},
    ])
    rows = runner.get_queue("test-authoring-agent")
    assert [r["mine"] for r in rows] == [False, False], "no user_id means no claim"

    rows = runner.get_queue("test-authoring-agent", user_id="aaaaaaaaaaaa")
    assert len(rows) == 1 and rows[0]["module"] == "mine" and rows[0]["mine"] is True


def test_run_active_keeps_its_historic_shape(registry):
    """`runs` and `capacity` are additive: existing panels read the top-level
    fields and must keep working untouched."""
    from qa_agents_server.app import create_app

    registry("s-a", "test-authoring-agent", "payments", 100, "aaaaaaaaaaaa")
    registry("s-b", "test-authoring-agent", "checkout", 200, "aaaaaaaaaaaa")

    client = create_app().test_client()
    body = client.get("/agents/test-authoring-agent/run/active",
                      headers={"X-User-ID": "aaaaaaaaaaaa"}).get_json()

    for legacy in ("active", "busy", "session_id", "module", "status",
                   "started_at", "step_progress", "start_from_step"):
        assert legacy in body, f"dropped the historic field {legacy!r}"
    assert body["active"] is True
    assert body["session_id"] == "s-a", "the primary run should be the oldest"
    assert [r["session_id"] for r in body["runs"]] == ["s-a", "s-b"]
    assert body["capacity"]["active"] == 2


def test_run_active_separates_other_agents(registry):
    """A healing session rendered under authoring's step labels is the exact
    confusion the per-agent scoping exists to prevent, so cross-agent runs are
    reported separately rather than mixed into `runs`."""
    from qa_agents_server.app import create_app

    registry("s-a", "test-authoring-agent", "payments", 100, "aaaaaaaaaaaa")
    registry("s-h", "test-healing-agent", "LoginTest", 200, "aaaaaaaaaaaa")

    client = create_app().test_client()
    body = client.get("/agents/test-authoring-agent/run/active",
                      headers={"X-User-ID": "aaaaaaaaaaaa"}).get_json()
    assert [r["session_id"] for r in body["runs"]] == ["s-a"]
    assert [r["agent"] for r in body["other_agent_runs"]] == ["test-healing-agent"]


# ── Cancellation ──────────────────────────────────────────────────────────────
# Cancel had three separate defects and no test at all: run.status was never set
# to "cancelled" (so a cancelled run reported as failed and the .cancelled marker
# was never written), the SIGKILL escalation was deleted (so a child ignoring
# SIGTERM held its slot for the full 2h timeout), and worktree teardown ran
# microseconds after SIGTERM — rm -rf-ing the directory out from under a still
# running mvn/JVM that had it as cwd.
def _spawn_stubborn_child(tmp_path):
    """A process group that ignores SIGTERM, like a JVM surefire fork can."""
    # Two things are needed for this child to actually resist SIGTERM, and
    # missing either makes the escalation test pass for the wrong reason:
    #
    #   * `trap '' TERM` alone is not enough — killpg also signals the `sleep`
    #     child, which dies and lets bash fall through. Looping over short
    #     sleeps means one dying does not end the process.
    #   * The signal must not arrive before bash has installed the trap. Without
    #     the readiness handshake below the child dies with rc=-15 and the test
    #     never reaches the SIGKILL path at all.
    ready = tmp_path / "ready"
    script = tmp_path / "stubborn.sh"
    script.write_text("#!/bin/bash\n"
                      "trap '' TERM\n"
                      f"touch {ready}\n"
                      "for _ in $(seq 240); do sleep 0.5; done\n")
    script.chmod(0o755)
    proc = subprocess.Popen(["bash", str(script)], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline and not ready.exists():
        time.sleep(0.05)
    assert ready.exists(), "the stubborn child never started"
    return proc


def _register(run):
    runner._runs[run.session_id] = run
    runner._active_runs[run.session_id] = run


def test_cancel_marks_the_run_cancelled_not_failed(tmp_path):
    """_wait_and_reap derives the final status from run.status. It was never set,
    so SIGTERM's non-zero exit was indistinguishable from a real failure."""
    proc = _spawn_stubborn_child(tmp_path)
    audit = tmp_path / "audit"
    audit.mkdir()
    run = _fake_run("cancel-status", "test-authoring-agent", "m", time.time(), "aaaaaaaaaaaa")
    run.audit_dir = audit
    run.proc = proc
    run.pid = proc.pid
    _register(run)
    try:
        assert runner.cancel_run("cancel-status") is True
        assert run.status == "cancelled", (
            f"expected 'cancelled', got {run.status!r} — a cancelled run will "
            f"report as failed and write no .cancelled marker")
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait(timeout=10)
        runner._runs.clear()
        runner._active_runs.clear()


def test_cancel_escalates_to_sigkill(tmp_path):
    """A child that ignores SIGTERM must still die, or it holds its worker slot
    until the 2h run timeout."""
    proc = _spawn_stubborn_child(tmp_path)
    audit = tmp_path / "audit"
    audit.mkdir()
    run = _fake_run("cancel-kill", "test-authoring-agent", "m", time.time(), "aaaaaaaaaaaa")
    run.audit_dir = audit
    run.proc = proc
    run.pid = proc.pid
    _register(run)
    try:
        assert runner.cancel_run("cancel-kill") is True
        # cancel_run escalates in a thread so the HTTP request does not block for
        # the grace period; allow for it plus a margin.
        deadline = time.time() + runner._KILL_GRACE_SECONDS + 15
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.25)
        assert proc.poll() is not None, (
            "the SIGTERM-ignoring child survived — SIGKILL escalation is gone")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        runner._runs.clear()
        runner._active_runs.clear()


def test_cancel_does_not_delete_the_worktree_under_a_live_process(tmp_path, monkeypatch):
    """Teardown belongs to _wait_and_reap, after proc.wait() returns.

    Removing it inside cancel_run raced the dying process: the directory was
    rm -rf'd while mvn/JVM/claude still had it as their cwd.
    """
    removed = []
    monkeypatch.setattr("shared.workspace.cleanup_worktree",
                        lambda *a, **k: removed.append(a) or {"ok": True})

    proc = _spawn_stubborn_child(tmp_path)
    audit = tmp_path / "audit"
    audit.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run = _fake_run("cancel-wt", "test-authoring-agent", "m", time.time(), "aaaaaaaaaaaa")
    run.audit_dir = audit
    run.proc = proc
    run.pid = proc.pid
    run.worktree_path = str(worktree)
    _register(run)
    try:
        runner.cancel_run("cancel-wt")
        assert not removed, "cancel_run tore down the worktree while the process was alive"
        assert worktree.exists()
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait(timeout=10)
        runner._runs.clear()
        runner._active_runs.clear()


def test_cancelling_an_already_finished_run_is_a_noop():
    run = _fake_run("cancel-done", "test-authoring-agent", "m", time.time(), "aaaaaaaaaaaa")
    run.proc = None
    _register(run)
    try:
        assert runner.cancel_run("cancel-done") is False
        assert runner.cancel_run("no-such-session") is False
    finally:
        runner._runs.clear()
        runner._active_runs.clear()


# ── Baseline directory follows the worktree ───────────────────────────────────
def test_out_of_tree_baseline_override_hides_a_new_baseline(tmp_path, monkeypatch):
    """HEALING_BASELINE_DIR pinned to the main checkout loses every new baseline.

    config/.env builds it as an absolute ${WORKSPACE_DIR}/${GITHUB_REPO_AUTOMATION}
    path. Inherited into a worktree run, the Java framework writes fingerprints
    into the developer's main checkout while ship reads repo_directory(worktree) —
    which rejects the out-of-tree override and falls back inside the worktree. Ship
    then sees only the baselines the checkout came with, logs "none changed", and
    the PR carries a new page object with no baseline for it.
    """
    from shared import baseline

    worktree = tmp_path / "worktree"
    (worktree / baseline.REPO_SUBPATH).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=worktree, capture_output=True)

    fingerprint = '{"pageObject": "NaukriProfilePage", "locators": {}}'
    (worktree / baseline.REPO_SUBPATH / "NaukriProfilePage.json").write_text(fingerprint)

    # The override points at a different checkout entirely — exactly what a
    # worktree run inherits today.
    outside = tmp_path / "main-checkout" / "src" / "main" / "resources" / "baselines"
    outside.mkdir(parents=True)
    monkeypatch.setenv(baseline._DIR_ENV, str(outside))

    # It must not win: an out-of-tree directory is not committable from here.
    assert baseline.repo_directory(worktree) == worktree / baseline.REPO_SUBPATH

    # And with it dropped — what runner.py now does — the new baseline is found
    # and reported as differing from HEAD, so ship commits it.
    monkeypatch.delenv(baseline._DIR_ENV)
    assert "NaukriProfilePage.json" in " ".join(baseline.changed(worktree))


def test_runner_drops_the_baseline_override_for_worktree_runs():
    """The pop must sit with the FRAMEWORK_DIR redirect it belongs to."""
    src = (REPO_ROOT / "qa_agents_server" / "runner.py").read_text()
    block = re.search(r'env\["FRAMEWORK_DIR"\].*?\n\n', src, re.S)
    assert block, "FRAMEWORK_DIR is no longer redirected for worktree runs"
    assert 'env.pop("HEALING_BASELINE_DIR", None)' in block.group(0), (
        "HEALING_BASELINE_DIR survives into the worktree run — baselines the run "
        "records will be written outside the tree ship commits from")
