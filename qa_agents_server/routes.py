"""REST + SSE endpoints for qa_agents_server.

Most routes are scoped under /agents/<agent>/*; /settings and /health are
top-level, because agent settings live in one config/.env shared by all agents.
<agent> is a key in qa_agents_server.agents.AGENTS — currently
test-authoring-agent and test-healing-agent. The authoring URLs are unchanged
from when this server served that agent alone, because the AI-Test-Studio
frontend hardcodes them.

The frontend proxies these paths through its own backend, so auth is enforced at
the proxy layer — this server does not implement its own auth (it is expected to
bind to localhost).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Generator

from flask import (Blueprint, Response, jsonify, request, send_file,
                   stream_with_context)

from qa_agents_server import agent_settings, audit_reader, feature_files, runner
from qa_agents_server import analytics, metrics_reader
from shared import assertion_graph, code_analyzer, intent, test_catalog
from shared import workspace as workspace_helper
from shared.git import run_git
from qa_agents_server.agents import (AgentConfigError, DEFAULT_AGENT,
                                     adapt_apply_default, auto_push_default,
                                     default_branch, get_agent)
from qa_agents_server.runner import TERMINAL_STATUSES, AGENT

logger = logging.getLogger(__name__)

qa_bp = Blueprint("qa_agents", __name__)

_BASE = "/agents/<agent>"


def _resolve(agent: str):
    """Return (spec, None) or (None, error_response)."""
    try:
        return get_agent(agent), None
    except AgentConfigError as e:
        return None, (jsonify({"error": e.message}), e.status)


# ── Identity ──────────────────────────────────────────────────────────────────
# Identity is resolved ONCE, here, and validated before it is used. It used to be
# re-read with request.headers.get("X-User-ID") at each call site, which went
# wrong in two different ways at once:
#
#   * The value reached feature_files as a raw path segment, so an absolute or
#     ../-laden id escaped the queue directory entirely — arbitrary directory
#     creation and file write as the server user.
#   * Ownership checks spelled `if run_record and user_id and ...` treated a
#     MISSING header as permission granted, so omitting it read any user's run.
#
# AI-Test-Studio derives ids as md5(username)[:12]; anything else is a client
# talking to this server directly, and gets the anonymous id rather than a path.
_USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
# The readable identity, built from X-User-Name. Deliberately as tight as the
# hash pattern it replaces, because this value is still joined onto a path: one
# leading letter/digit, then letters, digits, underscore or hyphen. Anything
# else (a dot, a slash, a space, 40 characters of unicode) fails to match and
# falls back to the hash, so a surprising username degrades to the old
# behaviour rather than escaping the queue directory.
_USER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
ANONYMOUS_USER_ID = "default"


def current_user_id() -> str:
    """The validated caller identity. Never attacker-controlled path content.

    Prefers a readable `user-<username>` over the md5 hash, so queue and cache
    directories say who they belong to. The hash was never a secret — it is
    md5(username)[:12] and AI-Test-Studio notes it is derivable — so spelling
    the name out loses no protection, and the pattern above keeps the value as
    path-safe as the hash was.
    """
    if not _from_trusted_proxy():
        return ANONYMOUS_USER_ID
    raw = (request.headers.get("X-User-ID") or "").strip()
    if not _USER_ID_RE.match(raw):
        return ANONYMOUS_USER_ID
    # The name must HASH TO the id it arrives with. X-User-Name is otherwise a
    # second, independent way to name a directory, and the two headers could
    # disagree — through a proxy bug, or a session whose username changed after
    # the id was minted. Checking md5(name)[:12] == id makes the readable form
    # provably the same identity rather than a parallel one, and it costs one
    # hash. It also disposes of the proxy's "Unknown" placeholder for free: it
    # does not hash to a real id, so an unauthenticated caller keeps falling
    # through to the anonymous shared queue exactly as before.
    name = (request.headers.get("X-User-Name") or "").strip()
    if (_USER_NAME_RE.match(name)
            and hashlib.md5(name.encode()).hexdigest()[:12] == raw):
        # ponytail: not lower-cased, so "Admin" and "admin" stay distinct ids
        # as their hashes are. They collide on a case-insensitive filesystem
        # (macOS default); if two such accounts ever exist, hash the name into
        # the directory suffix instead of rejecting the login.
        return f"user-{name}"
    return raw


def _from_trusted_proxy() -> bool:
    """Whether this request carries the shared secret proving it came from the
    AI-Test-Studio proxy rather than straight off the network.

    When QA_AGENT_PROXY_SECRET is unset the answer is True, preserving existing
    local setups — that case is covered by binding to localhost instead (see
    qa_agents_server.app). Setting it is what makes the identity headers
    trustworthy, and it is the only thing that does: X-User-Role is a header any
    client can type, and it was previously the sole admin assertion in the
    entire server.
    """
    expected = (os.environ.get("QA_AGENT_PROXY_SECRET") or "").strip()
    if not expected:
        return True
    import hmac
    presented = (request.headers.get("X-Proxy-Secret") or "").strip()
    return bool(presented) and hmac.compare_digest(presented, expected)


def _is_admin() -> bool:
    """Admin only when the proxy vouched for the request AND said so."""
    return (_from_trusted_proxy()
            and (request.headers.get("X-User-Role") or "").strip().lower() == "admin")


def _owns(session_id: str) -> bool:
    """Whether the caller may see/act on this session. Default-deny."""
    from qa_agents_server import storage
    if _is_admin():
        return True
    record = storage.get(session_id)
    if record is None:
        # Unknown session: fall back to the live registry, then refuse. An
        # evicted record must not become an access-control hole.
        run = runner.get_run(session_id)
        if run is None:
            return False
        return run.user_id == current_user_id()
    return record.get("user_id", ANONYMOUS_USER_ID) == current_user_id()


def owns_session(view):
    """Refuse a session-scoped route unless the caller owns the session.

    A decorator rather than four lines repeated per route: the previous
    per-endpoint approach was applied to two of the seven session-scoped
    endpoints, and both copies had the missing-header bypass. /events, /metrics,
    /cancel, /retry and /artifact had no check at all.
    """
    @functools.wraps(view)
    def guarded(*args, **kwargs):
        session_id = kwargs.get("session_id")
        if session_id and not _owns(session_id):
            return jsonify({"error": "forbidden"}), 403
        return view(*args, **kwargs)
    return guarded


# ── Module file CRUD ──────────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/queue", methods=["GET"])
def queue_list(agent: str):
    spec, err = _resolve(agent)
    if err:
        return err
    user_id = current_user_id()
    if spec.queue_kind == "txt":
        return jsonify({"items": feature_files.list_features(spec.name, user_id=user_id)})
    # A json queue holds handoffs written by another agent — read-only here;
    # nothing but that agent should be putting work in it.
    items = []
    # Using spec.queue_dir without user_id because healing handoff is global from CI?
    # Actually wait. If triaging output is global, then handoffs are global.
    # Healing agent doesn't use queue_kind="txt", so it uses spec.queue_dir directly.
    # Let's keep it that way for healing agent.
    if spec.queue_dir.exists():
        for path in sorted(spec.queue_dir.glob("*.json")):
            stat = path.stat()
            items.append({"name": path.stem, "size": stat.st_size,
                          "modified": stat.st_mtime})
    return jsonify({"items": items})


@qa_bp.route(f"{_BASE}/queue", methods=["POST"])
def queue_create(agent: str):
    spec, err = _resolve(agent)
    if err:
        return err
    user_id = current_user_id()
    if spec.queue_kind != "txt":
        return jsonify({"error": f"{spec.name}'s queue is written by "
                                 f"another agent, not through this API"}), 405
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    content = body.get("content")
    try:
        result = feature_files.write_feature(name, content, spec.name, user_id=user_id)
    except feature_files.FeatureFileError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify(result), 201


@qa_bp.route(f"{_BASE}/queue/<name>", methods=["GET"])
def queue_read(agent: str, name: str):
    spec, err = _resolve(agent)
    if err:
        return err
    user_id = current_user_id()
    if spec.queue_kind != "txt":
        path = spec.queue_dir / f"{name}.json"
        if not path.exists():
            return jsonify({"error": f"handoff not found: {name}"}), 404
        return jsonify({"name": name, "content": path.read_text()})
    try:
        return jsonify(feature_files.read_feature(name, spec.name, user_id=user_id))
    except feature_files.FeatureFileError as e:
        return jsonify({"error": str(e)}), e.status


# ── UI defaults ───────────────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/config", methods=["GET"])
def agent_config(agent: str):
    """Effective defaults for this agent, so the UI can reflect config/.env.

    A checkbox that always starts unticked is not a neutral default — the value
    it sends is exported into the run and beats config/.env, so an untouched box
    silently turned AUTO_PUSH=true into a dry run.
    """
    spec, err = _resolve(agent)
    if err:
        return err
    return jsonify({
        "agent": spec.name,
        "auto_push_default": auto_push_default(),
        "adapt_apply_default": adapt_apply_default(),
        # The base a run gets when its branch field is left alone. The UI seeds
        # the field from this rather than hardcoding "main", so a panel never
        # shows a default the server would not actually use.
        "default_branch": default_branch(),
    })


# `git ls-remote` is a network round trip, and three panels mounting at once
# would each pay for it. Cached briefly rather than for the process lifetime:
# a branch created a minute ago should turn up without a server restart.
_BRANCH_CACHE: dict = {"key": None, "at": 0.0, "branches": []}
_BRANCH_CACHE_TTL = 60.0
_BRANCH_CACHE_LOCK = threading.Lock()


@qa_bp.route(f"{_BASE}/branches", methods=["GET"])
def agent_branches(agent: str):
    """Branches on the automation repo, for the run panel's picker.

    Never a 500 and never an error body: autocomplete is a convenience, and a
    picker that cannot reach GitHub must still leave the panel usable — the
    field is free text, and the run's own pre-flight is the real check.
    """
    _spec, err = _resolve(agent)
    if err:
        return err
    org = os.environ.get("GITHUB_ORG", "")
    repo = os.environ.get("GITHUB_REPO_AUTOMATION", "")
    key = (org, repo)
    now = time.time()
    with _BRANCH_CACHE_LOCK:
        fresh = (_BRANCH_CACHE["key"] == key
                 and now - _BRANCH_CACHE["at"] < _BRANCH_CACHE_TTL)
        branches = list(_BRANCH_CACHE["branches"]) if fresh else None
    if branches is None:
        try:
            branches = workspace_helper.list_remote_branches(
                org, repo, os.environ.get("GITHUB_TOKEN", ""))
        except Exception as e:
            logger.debug("could not list remote branches: %s", e)
            branches = []
        with _BRANCH_CACHE_LOCK:
            _BRANCH_CACHE.update({"key": key, "at": now, "branches": list(branches)})
    return jsonify({"branches": branches, "default": default_branch()})


# ── Test catalogue (healing only) ─────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/tests", methods=["GET"])
def tests_list(agent: str):
    """Classes and @Test methods in the automation repo, for the UI picker."""
    spec, err = _resolve(agent)
    if err:
        return err
    if not spec.uses_test_catalog:
        return jsonify({"error": f"{spec.name} has no test catalogue"}), 404

    workspace = _automation_workspace()
    if workspace is None:
        # "no tests found" and "the repo is not where I was told" must not look
        # the same — one is a fact about the suite, the other is misconfiguration.
        return jsonify({
            "error": "automation repo not found",
            "detail": f"Looked in {_automation_workspace_hint()}. "
                      f"Set FRAMEWORK_DIR, or WORKSPACE_DIR and "
                      f"GITHUB_REPO_AUTOMATION, in config/.env.",
        }), 503

    try:
        with _REPO_LOCK:
            _refresh_source_caches(str(workspace))
            payload = test_catalog.list_tests(str(workspace))
        # Copied, not mutated: `payload` is test_catalog's cached dict, and the
        # checkout fields are computed fresh on every request.
        return jsonify({**payload, **_checkout_state(workspace)})
    except Exception as e:
        return jsonify({"error": "could not read the test catalogue",
                        "detail": str(e)}), 500


def _automation_workspace_hint() -> str:
    """The path the lookup actually used, for an error a reader can act on."""
    explicit = workspace_helper.configured()
    if explicit is not None:
        return f"FRAMEWORK_DIR={str(explicit)!r}"
    return (f"{os.environ.get('GITHUB_REPO_AUTOMATION', '<unset>')!r} under "
            f"WORKSPACE_DIR={os.environ.get('WORKSPACE_DIR', '<unset>')!r}")


def _automation_workspace():
    """The automation repo, resolved the way the healing agent resolves it."""
    candidate = workspace_helper.expected(
        os.environ.get("WORKSPACE_DIR", ""),
        os.environ.get("GITHUB_REPO_AUTOMATION", ""))
    return candidate if candidate and candidate.is_dir() else None


# ── Reading the automation repo as it is *now* ────────────────────────────────
#
# code_analyzer's caches are scoped to "one run", which is exactly right for an
# agent subprocess and wrong for this process: nothing here ever ended a run, so
# a file read at boot was answered from memory for the life of the server. The
# picker went on offering a test method that had been deleted hours earlier, and
# only a restart cleared it.
#
# So every request that reads the repo passes through here first. The signature
# is stat-only (~1ms over ~100 files, against ~26ms to re-parse the same tree),
# so paying it per request buys correctness for almost nothing. Per-file
# validation in read_source is not enough on its own: the file-list and
# test-file caches are per-tree, so an added or deleted file is invisible to
# them until they are dropped wholesale.
_REPO_STATE: dict = {"repo": None, "signature": None}
_REPO_LOCK = threading.Lock()


def _refresh_source_caches(repo_path: str) -> str:
    """Drop the shared source caches if anything in the repo has changed.

    Returns the current signature so callers can key their own caches on it.
    Callers hold _REPO_LOCK: invalidate_tree() clears globals that a concurrent
    rebuild would otherwise be reading half-way through, and two requests
    arriving together should not both pay for the same re-parse.
    """
    global _REPO_STATE
    signature = code_analyzer.repo_signature(repo_path)
    cached = _REPO_STATE
    if cached["repo"] != repo_path or cached["signature"] != signature:
        code_analyzer.invalidate_tree()
        _REPO_STATE = {"repo": repo_path, "signature": signature}
    return signature


def _checkout_state(workspace: Path) -> dict:
    """Which branch the catalogue was read from, and whether it is dirty.

    Read per request rather than cached with the payload: switching branches can
    leave the tree byte-identical (and so the signature unchanged) while the
    answer to "which branch am I looking at" has changed. Never fatal — a
    workspace that is not a git checkout still has tests worth listing.
    """
    try:
        ok, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace, timeout=5)
        branch = out.strip() if ok else None
        ok_status, status_out, _ = run_git(["status", "--porcelain"], workspace,
                                           timeout=5)
        dirty = bool(status_out.strip()) if ok_status else None
    except Exception as e:
        # A hung or missing git must not turn a working catalogue into a 500.
        logger.debug("could not read the checkout state of %s: %s", workspace, e)
        return {"branch": None, "dirty": None}
    return {"branch": branch, "dirty": dirty}


# `pkg.sub.Class#method`. The method half is required, not optional: `intent.derive`
# splits on the last dot, so a bare `automation.saucedemo.SauceDemoWebTest` is read
# as class `saucedemo`, method `SauceDemoWebTest` and returns an empty contract
# rather than an error — a wrong answer that looks like a real one. It also keeps
# this parameter from being anything path-shaped, since intent.path_for() builds a
# filename out of it.
_TEST_IDENT = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*#[A-Za-z_$][\w$]*$")

# member_index() re-reads every source file in the repo (~0.2s here). That is fine
# once and wrong on every keystroke in a picker, so it is cached against the same
# signature everything else here keys on: an edited test shows up without a
# server restart. Replaced as one dict so a concurrent request either sees the
# whole old entry or the whole new one — the server runs threaded.
_MEMBER_INDEX: dict = {"repo": None, "stamp": None, "index": None}


def _member_index(repo_path: str) -> dict:
    global _MEMBER_INDEX
    with _REPO_LOCK:
        stamp = _refresh_source_caches(repo_path)
        cached = _MEMBER_INDEX
        if cached["repo"] == repo_path and cached["stamp"] == stamp:
            return cached["index"]
        index = assertion_graph.member_index(repo_path)
        _MEMBER_INDEX = {"repo": repo_path, "stamp": stamp, "index": index}
        return index


# ── What a test proves, for the adaptation panel's reference pane ─────────────
@qa_bp.route(f"{_BASE}/tests/intent", methods=["GET"])
def tests_intent(agent: str):
    """One test's intent contract: the steps it narrates and what it asserts.

    Purely derived from source — no model call, no run. The adaptation UI shows
    it beside the "what changed" box so a human describes a change against what
    the test actually does today rather than from memory.
    """
    spec, err = _resolve(agent)
    if err:
        return err
    if not spec.uses_test_catalog:
        return jsonify({"error": f"{spec.name} has no test catalogue"}), 404

    test = (request.args.get("test") or "").strip()
    if not _TEST_IDENT.match(test):
        return jsonify({
            "error": "test must name a single method",
            "detail": "Expected `pkg.Class#method` (a bare class has no contract "
                      "of its own — ask for each of its methods).",
        }), 400

    workspace = _automation_workspace()
    if workspace is None:
        return jsonify({
            "error": "automation repo not found",
            "detail": f"Looked in {_automation_workspace_hint()}. "
                      f"Set FRAMEWORK_DIR, or WORKSPACE_DIR and "
                      f"GITHUB_REPO_AUTOMATION, in config/.env.",
        }), 503

    try:
        contract = intent.for_test(str(workspace), test,
                                   _member_index(str(workspace)))
    except Exception as e:
        return jsonify({"error": "could not read the test's intent",
                        "detail": str(e)}), 500

    # `unresolved` is a count here, not the list: the UI uses it to say "this may
    # be incomplete", and the call sites themselves mean nothing to a reader who
    # is not holding the source open.
    return jsonify({
        "test": test,
        "source": contract.get("source", "derived"),
        "proves": contract.get("proves") or [],
        "verifies": intent.verifies(contract),
        "identity": contract.get("identity") or [],
        "unresolved_count": len(contract.get("unresolved") or []),
    })


# ── Run trigger + control ─────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/run", methods=["POST"])
def run_start(agent: str):
    spec, err = _resolve(agent)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    user_id = current_user_id()

    # Reject a bad base branch here rather than only in build_env. build_env
    # also runs on the reap thread that drains the pending queue, where a raise
    # is swallowed and the queued run disappears without a word — and this is
    # the only place that can answer "does that branch exist" before a session
    # directory is created and the agent starts spending money.
    requested_base = (body.get("base_branch") or "").strip()
    if requested_base:
        org = os.environ.get("GITHUB_ORG", "")
        repo = os.environ.get("GITHUB_REPO_AUTOMATION", "")
        try:
            wanted = workspace_helper.normalise_branch(requested_base)
        except ValueError as e:
            return jsonify({"error": f"base_branch: {e}"}), 400
        # False is a real answer; None means the remote could not be asked, and
        # a network blip must not block a run that would work from the checkout.
        if workspace_helper.remote_branch_exists(
                org, repo, os.environ.get("GITHUB_TOKEN", ""), wanted) is False:
            return jsonify({
                "error": f"branch '{wanted}' was not found in {org}/{repo}"
            }), 400

    try:
        run = runner.start_run(body, agent=spec.name, user_id=user_id)
    except runner._QueuedNotification as q:
        return jsonify({
            "queued": True,
            "position": q.position,
            "agent": spec.name,
            "module": spec.describe_run(body),
            "session_id": q.session_id,
        }), 202
    except runner.RunnerError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify({
        "queued": False,
        "agent": run.agent,
        "session_id": run.session_id,
        "module": run.module,
        "auto_push": run.auto_push,
        "base_branch": run.base_branch,
        "status": run.status,
        "started_at": run.started_at,
    }), 201


@qa_bp.route(f"{_BASE}/run/queue", methods=["GET"])
def pending_queue_list(agent: str):
    # The queue is global — the worker pool is shared — but each caller sees only
    # its own agent's rows, so one panel never lists the other's pending runs.
    #
    # Rows carry `mine` rather than being filtered to the caller: a person whose
    # run is queued behind two of someone else's needs to see that the wait is
    # real, and `mine` is what marks the ones they may cancel. Ownership is
    # enforced on the DELETE regardless of what is listed here.
    spec, err = _resolve(agent)
    if err:
        return err
    return jsonify({"queue": runner.get_queue(spec.name, user_id=current_user_id()),
                    "capacity": runner.capacity(current_user_id())})


@qa_bp.route(f"{_BASE}/run/queue/<int:index>", methods=["DELETE"])
def pending_queue_remove(agent: str, index: int):
    spec, err = _resolve(agent)
    if err:
        return err
    # index is the row's slot in the GLOBAL queue; passing spec.name makes the
    # runner refuse it if that slot belongs to a different agent, and user_id
    # if it belongs to a different user — an index is a position, not a
    # capability, so without the latter any user could cancel anyone's queued
    # run by counting rows.
    removed = runner.remove_from_queue(
        index, spec.name, user_id=None if _is_admin() else current_user_id())
    if not removed:
        return jsonify({"error": "index out of range"}), 404
    return jsonify({"removed": True,
                    "queue": runner.get_queue(spec.name, user_id=current_user_id()),
                    "capacity": runner.capacity(current_user_id())})


@qa_bp.route(f"{_BASE}/run/active", methods=["GET"])
def run_active(agent: str):
    """The run active FOR THIS AGENT, if any.

    Runs are parallel now (each gets its own worktree), up to
    QA_MAX_CONCURRENT_RUNS — but "something is running" and
    "your run is running" are different questions. Answering the first when the
    second was asked made the authoring panel adopt a healing session and stream
    its logs under its own step labels.

    So `active` is scoped to the agent, and the shared slot is reported
    separately as `busy` / `busy_agent` for a UI that wants to explain why a new
    run would be queued.
    """
    spec, err = _resolve(agent)
    if err:
        return err

    user_id = current_user_id()
    capacity = runner.capacity(user_id)
    busy = capacity["busy"]

    # Every run this user has in flight, not just the first one found. The
    # single-session answer made a user's SECOND concurrent run invisible: which
    # one you got depended on dict ordering, and the other had no representation
    # in the UI at all. `runs` is what a session switcher renders.
    mine = runner.get_active_runs(user_id)
    this_agent = [r for r in mine if r.agent == spec.name]

    def summarise(run, full: bool = False) -> dict:
        row = {
            "session_id": run.session_id,
            "agent": run.agent,
            "module": run.module,
            "status": run.status,
            "started_at": run.started_at,
        }
        if full:
            row.update({
                "auto_push": run.auto_push,
                "base_branch": run.base_branch,
                "step_progress": run.step_progress,
                # step_progress keeps its historic string-map shape; timing and
                # cost ride alongside so no existing consumer has to change.
                "step_metrics": run.step_metrics,
                "metrics": run.metrics_totals,
                "start_from_step": run.start_from_step,
            })
        return row

    # Shared by every response shape below, so a UI can render capacity and the
    # switcher without caring whether THIS agent happens to have a run.
    envelope = {
        "busy": busy,
        "capacity": capacity,
        "runs": [summarise(r) for r in this_agent],
        # This user's runs on OTHER agents, so a global switcher can offer them
        # without polling every agent endpoint in turn.
        "other_agent_runs": [summarise(r) for r in mine if r.agent != spec.name],
    }

    if not this_agent:
        # `busy_agent` historically meant "the thing occupying the shared slot".
        # With a pool there may be several, so it names the user's own other run
        # when there is one — which is what the message built from it says.
        other = next((r for r in mine), None)
        return jsonify({**envelope, "active": False,
                        **({"busy_agent": other.agent,
                            "busy_since": other.started_at} if other else {})})

    # The primary run keeps the exact top-level shape it has always had, so
    # existing panels keep working untouched while `runs` is adopted.
    primary = this_agent[0]
    return jsonify({**envelope, "active": True, "busy_agent": primary.agent,
                    **summarise(primary, full=True)})


@qa_bp.route(f"{_BASE}/run/<session_id>/cancel", methods=["POST"])
@owns_session
def run_cancel(agent: str, session_id: str):
    spec, err = _resolve(agent)
    if err:
        return err
    run = runner.get_run(session_id)
    if run is not None and run.agent != spec.name:
        return jsonify({
            "error": f"session {session_id} belongs to {run.agent}, not {spec.name}"
        }), 409
    ok = runner.cancel_run(session_id)
    if not ok:
        return jsonify({"error": "run not found or already finished"}), 404
    return jsonify({"status": "cancelling", "session_id": session_id})


@qa_bp.route(f"{_BASE}/sessions/<session_id>/retry", methods=["POST"])
@owns_session
def session_retry(agent: str, session_id: str):
    """Re-run an existing (usually finished/failed) session starting from a
    specific step, reusing its steps-before-that output rather than starting
    the whole 01→05 pipeline over. body: {"from_step": 2-5, "auto_push": bool}.

    from_step must be >= 2 — resuming "from step 1" isn't a resume at all
    (there's nothing prior to reuse); that's just a fresh run, via POST /run.

    Gated on the spec's supports_resume, which authoring and adaptation both set
    — the adaptation UI offers it on steps 2-5 of its progress bar. The healing
    agent re-investigates failures through its own internal retry loop, so it has
    no partial pipeline to resume and returns 405.
    """
    spec, err = _resolve(agent)
    if err:
        return err
    if not spec.supports_resume:
        return jsonify({
            "error": f"{spec.name} does not support resuming from a step — it "
                     f"retries failed fixes internally. Start a fresh run instead."
        }), 405
    body = request.get_json(silent=True) or {}
    try:
        from_step = int(body.get("from_step", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "from_step must be an integer 2-5"}), 400
    if not (2 <= from_step <= 5):
        return jsonify({
            "error": "from_step must be between 2 and 5 — to restart from the "
                     "beginning, use POST /run instead"
        }), 400
    auto_push = bool(body.get("auto_push", False))

    # The base comes from the session being resumed, not from whatever the run
    # panel currently shows. auto_push above is deliberately read live because
    # it is a policy switch; the base branch is a property of the steps this
    # retry is about to reuse. Shipping step-03 output that was generated
    # against one branch onto a different one is the failure this avoids.
    payload = {"auto_push": auto_push}
    original_base, _sha = workspace_helper.read_base_marker(
        spec.audit_dir / session_id)
    if original_base:
        payload["base_branch"] = original_base

    user_id = current_user_id()

    try:
        run = runner.start_run(payload, agent=spec.name,
                               session_id=session_id, start_from_step=from_step, user_id=user_id)
    except runner._QueuedNotification as q:
        return jsonify({
            "queued": True,
            "position": q.position,
            "session_id": q.session_id,
            "start_from_step": from_step,
        }), 202
    except runner.RunnerError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify({
        "queued": False,
        "session_id": run.session_id,
        "module": run.module,
        "auto_push": run.auto_push,
        "base_branch": run.base_branch,
        "status": run.status,
        "started_at": run.started_at,
        "start_from_step": from_step,
    }), 201


# ── Live + history stream (unified endpoint) ──────────────────────────────────
@qa_bp.route(f"{_BASE}/run/<session_id>/stream", methods=["GET"])
@owns_session
def run_stream(agent: str, session_id: str):
    spec, err = _resolve(agent)
    if err:
        return err
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0

    live_run = runner.get_run(session_id)

    if live_run is not None:
        # Live run (or finished run still held in memory) — stream via the runner.
        def generate() -> Generator[bytes, None, None]:
            yield _sse_comment("stream-open live")
            last_heartbeat = time.time()
            for event in runner.subscribe_stream(session_id, offset):
                yield _format_event(event.to_dict())
                last_heartbeat = time.time()
            # Flush any trailing events the generator may not have emitted
            # (unlikely but cheap guard)
            if live_run.status in TERMINAL_STATUSES:
                yield _sse_comment(f"stream-close {live_run.status}")
        return _sse_response(generate())

    # Historical replay from audit folder
    events = audit_reader.replay_events(session_id, agent=spec.name)
    if events is None:
        return jsonify({"error": "session not found"}), 404

    def generate_history() -> Generator[bytes, None, None]:
        yield _sse_comment("stream-open history")
        for event in events:
            if event["seq"] <= offset:
                continue
            yield _format_event(event)
        yield _sse_comment("stream-close history")
    return _sse_response(generate_history())


# ── History ───────────────────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/sessions", methods=["GET"])
def sessions_list(agent: str):
    spec, err = _resolve(agent)
    if err:
        return err
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    # An admin sees everything; everyone else sees only their own. Passing the
    # raw header meant a request without one filtered on None, and
    # audit_reader's `if user_id:` then applied no filter at all — so omitting
    # the header returned every user's history.
    user_id = None if _is_admin() else current_user_id()
    return jsonify({"items": audit_reader.list_sessions(limit=limit, offset=offset,
                                                    agent=spec.name, user_id=user_id)})

# Artefacts the framework wrote next to a failure — screenshot, DOM snapshot,
# trace zip, video. The console logs them as absolute paths, but a browser will
# not follow a file:// link from an http:// page, so they have to be served.
_ARTEFACT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webm": "video/webm", ".mp4": "video/mp4",
    ".html": "text/html", ".htm": "text/html", ".json": "application/json",
    ".zip": "application/zip", ".txt": "text/plain", ".log": "text/plain",
    ".md": "text/markdown",
}


def _artefact_roots(spec) -> list:
    """Directories an artefact may legitimately come from.

    The ephemeral worktree root (QA_WORKTREE_TEMP_DIR) is deliberately NOT here.
    It was added so worktree artefacts could be served, but the worktree is
    destroyed by _wait_and_reap before the terminal event is even emitted, so
    every such link was a 404 by the time anyone could click it — while the root
    itself exposed every user's screenshots and DOM snapshots to every other
    user, and, living under world-writable /tmp, let any local process drop a
    servable file into it. Runs now copy test-output/ into their own audit
    directory instead (runner._preserve_worktree_artefacts), which is already
    the first root below and is per-session.
    """
    roots = [spec.audit_dir]
    workspace = _automation_workspace()
    if workspace:
        roots.append(Path(workspace) / "test-output")
    return [r.resolve() for r in roots if r and Path(r).exists()]


@qa_bp.route(f"{_BASE}/artifact", methods=["GET"])
def artifact(agent: str):
    """Serve one artefact file by absolute path, confined to known roots.

    The path arrives from a log line, so it is untrusted: resolve it first (which
    collapses any ..) and require the result to sit inside an allowed root, then
    check the suffix. Without both checks this is an arbitrary-file-read hole.
    """
    spec, err = _resolve(agent)
    if err:
        return err

    raw = request.args.get("path", "")
    if not raw:
        return jsonify({"error": "path is required"}), 400

    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "bad path"}), 400

    roots = _artefact_roots(spec)
    if not any(target == r or r in target.parents for r in roots):
        return jsonify({"error": "path is outside the artefact directories"}), 403

    # Confine the caller to their own sessions. spec.audit_dir/<session_id>/...
    # is shared by every user, so containment alone let anyone read anyone
    # else's screenshots and DOM snapshots by guessing a session id.
    try:
        relative = target.relative_to(spec.audit_dir.resolve())
        if relative.parts and not _owns(relative.parts[0]):
            return jsonify({"error": "forbidden"}), 403
    except ValueError:
        pass        # not under audit_dir — the shared test-output root
    if target.suffix.lower() not in _ARTEFACT_TYPES:
        return jsonify({"error": f"unsupported artefact type: {target.suffix}"}), 403
    if not target.is_file():
        return jsonify({"error": "not found"}), 404

    # A captured DOM snapshot is a full page from the app under test; rendering
    # it inline would execute its scripts in the dashboard's origin.
    inline = target.suffix.lower() not in (".html", ".htm")
    return send_file(str(target), mimetype=_ARTEFACT_TYPES[target.suffix.lower()],
                     as_attachment=not inline,
                     download_name=target.name)


@qa_bp.route(f"{_BASE}/sessions/<session_id>/events", methods=["GET"])
@owns_session
def session_events(agent: str, session_id: str):
    """The full event list for a FINISHED session, as one JSON response.

    Replaying history over SSE means holding a connection open for something
    that is already complete. Browsers cap concurrent connections per origin at
    six, so a few history views saturate the pool and every later request on the
    page stalls behind them — measured at 4-20s to open a session. A finite,
    finished list belongs in a plain GET.

    Live runs still use /run/<sid>/stream: there, the open connection is the point.
    """
    spec, err = _resolve(agent)
    if err:
        return err
    events = audit_reader.replay_events(session_id, agent=spec.name)
    if events is None:
        return jsonify({"error": "session not found"}), 404
    return jsonify({"events": events})


@qa_bp.route(f"{_BASE}/sessions/<session_id>", methods=["GET"])
@owns_session
def sessions_get(agent: str, session_id: str):
    spec, err = _resolve(agent)
    if err:
        return err

    session = audit_reader.get_session(session_id, agent=spec.name)
    if session is None:
        return jsonify({"error": "session not found"}), 404
    return jsonify(session)


@qa_bp.route(f"{_BASE}/sessions/<session_id>/metrics", methods=["GET"])
@owns_session
def session_metrics(agent: str, session_id: str):
    """Time and cost for one session: run totals plus the per-stage breakdown."""
    spec, err = _resolve(agent)
    if err:
        return err
    session_dir = spec.audit_dir / session_id
    data = metrics_reader.read_session_metrics(session_dir)
    if data is None:
        # Sessions predating metrics capture are a normal case, not an error —
        # the UI renders em-dashes for them rather than an error state.
        return jsonify({"session_id": session_id, "metrics": None,
                        "stages": [], "totals": {}})
    return jsonify({
        "session_id": session_id,
        "metrics": metrics_reader.totals(data),
        "totals": metrics_reader.totals(data),
        "stages": metrics_reader.stage_list(data),
    })


# ── Analytics (spans agents, so deliberately not under /agents/<agent>/) ───────
@qa_bp.route("/analytics/clear", methods=["DELETE"])
def analytics_clear():
    # This deletes analytics rows, the run registry and audit directories from
    # disk, irreversibly, for whoever is named. It had no check of any kind:
    # DELETE /analytics/clear?window=all with no headers wiped every user's
    # history. A member may clear only their own.
    user_id_param = (request.args.get("user_id") or "").strip() or None
    if not _is_admin():
        caller = current_user_id()
        if caller == ANONYMOUS_USER_ID:
            return jsonify({"error": "forbidden"}), 403
        user_id_param = caller
    window_param = (request.args.get("window") or "7d").strip()
    
    # 1. Clear in-memory / JSON history registry 
    from qa_agents_server import storage
    storage.clear(user_id=user_id_param, window=window_param)
    
    # 2. Clear analytics JSONL
    removed_sids = analytics.clear_history(user_id=user_id_param, window=window_param)
    
    # 3. Clear from runner's in-memory registry
    from qa_agents_server import runner
    runner.remove_history(removed_sids)

    # 4. Physically delete the audit directories
    from qa_agents_server.agents import AGENTS
    import shutil
    for sid in removed_sids:
        for spec in AGENTS.values():
            audit_path = spec.audit_dir / sid
            if audit_path.exists():
                shutil.rmtree(audit_path, ignore_errors=True)
                
    return jsonify({"success": True})


@qa_bp.route("/analytics/summary", methods=["GET"])
def analytics_summary():
    """Per-agent and overall rollups over a window: 24h | 7d | 30d | all.

    Returns raw counts, cost and duration. Time-saved is applied by the Studio,
    which owns the human-minutes baselines for every flow it reports on.
    """
    window = (request.args.get("window") or "7d").strip()
    if window not in analytics.WINDOWS:
        return jsonify({"error": f"window must be one of "
                                 f"{', '.join(analytics.WINDOWS)}"}), 400

    def _ts(name):
        raw = (request.args.get(name) or "").strip()
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    # Note: AI-Test-Studio admin portal enforces auth and passes X-User-ID.
    # Regular users can only see their own analytics; admins can filter by user_id or see all.
    user_id_param = (request.args.get("user_id") or "").strip() or None

    query_user_id = user_id_param
    if not _is_admin():
        query_user_id = current_user_id()

    return jsonify(analytics.query(
        window=window,
        agent=(request.args.get("agent") or "").strip() or None,
        since=_ts("from"), until=_ts("to"),
        user_id=query_user_id
    ))


# ── Agent settings (server-wide, not per-agent) ───────────────────────────────
# Deliberately NOT under /agents/<agent>/ — these knobs live in a single
# config/.env shared by all three agents.
@qa_bp.route("/settings", methods=["GET"])
def settings_get():
    """Schema + current values for the admin Agent Settings page."""
    return jsonify(agent_settings.get_all_for_api())


@qa_bp.route("/settings", methods=["PUT"])
def settings_put():
    """Persist a batch of setting updates to config/.env and os.environ."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        agent_settings.set_many(payload)
    except agent_settings.SettingsValidationError as e:
        return jsonify({"error": "invalid settings", "errors": e.errors}), 400
    except OSError as e:
        return jsonify({"error": f"could not write config/.env: {e}"}), 500
    return jsonify({
        "success": True,
        "message": "Settings saved. They apply from the next agent run.",
        **agent_settings.get_all_for_api(),
    })


# ── SSE helpers ───────────────────────────────────────────────────────────────
def _format_event(payload: dict) -> bytes:
    """Format a single Event dict as an SSE frame."""
    kind = payload.get("kind", "message")
    seq = payload.get("seq", 0)
    data = json.dumps(payload, default=str)
    lines = [
        f"event: {kind}",
        f"id: {seq}",
        f"data: {data}",
        "",
        "",
    ]
    return ("\n".join(lines)).encode("utf-8")


def _sse_comment(text: str) -> bytes:
    return (f": {text}\n\n").encode("utf-8")


def _sse_response(generator) -> Response:
    return Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )
