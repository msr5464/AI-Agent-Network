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
                                     get_agent)
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


# ── Module file CRUD ──────────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/queue", methods=["GET"])
def queue_list(agent: str):
    spec, err = _resolve(agent)
    if err:
        return err
    if spec.queue_kind == "txt":
        return jsonify({"items": feature_files.list_features(spec.name)})
    # A json queue holds handoffs written by another agent — read-only here;
    # nothing but that agent should be putting work in it.
    items = []
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
    if spec.queue_kind != "txt":
        return jsonify({"error": f"{spec.name}'s queue is written by "
                                 f"another agent, not through this API"}), 405
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    content = body.get("content")
    try:
        result = feature_files.write_feature(name, content, spec.name)
    except feature_files.FeatureFileError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify(result), 201


@qa_bp.route(f"{_BASE}/queue/<name>", methods=["GET"])
def queue_read(agent: str, name: str):
    spec, err = _resolve(agent)
    if err:
        return err
    if spec.queue_kind != "txt":
        path = spec.queue_dir / f"{name}.json"
        if not path.exists():
            return jsonify({"error": f"handoff not found: {name}"}), 404
        return jsonify({"name": name, "content": path.read_text()})
    try:
        return jsonify(feature_files.read_feature(name, spec.name))
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
    })


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
    try:
        run = runner.start_run(body, agent=spec.name)
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
        "status": run.status,
        "started_at": run.started_at,
    }), 201


@qa_bp.route(f"{_BASE}/run/queue", methods=["GET"])
def pending_queue_list(agent: str):
    # The queue is global — the run slot is shared — but each caller sees only
    # its own agent's rows, so one panel never lists the other's pending runs.
    spec, err = _resolve(agent)
    if err:
        return err
    return jsonify({"queue": runner.get_queue(spec.name)})


@qa_bp.route(f"{_BASE}/run/queue/<int:index>", methods=["DELETE"])
def pending_queue_remove(agent: str, index: int):
    spec, err = _resolve(agent)
    if err:
        return err
    # index is the row's slot in the GLOBAL queue; passing spec.name makes the
    # runner refuse it if that slot belongs to a different agent.
    removed = runner.remove_from_queue(index, spec.name)
    if not removed:
        return jsonify({"error": "index out of range"}), 404
    return jsonify({"removed": True, "queue": runner.get_queue(spec.name)})


@qa_bp.route(f"{_BASE}/run/active", methods=["GET"])
def run_active(agent: str):
    """The run active FOR THIS AGENT, if any.

    The execution slot is global — every agent drives the same automation-repo
    checkout, so only one may run at a time — but "something is running" and
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

    session_id = runner.get_active_session_id()
    run = runner.get_run(session_id) if session_id else None
    if run is None:
        return jsonify({"active": False, "busy": False})

    if run.agent != spec.name:
        return jsonify({
            "active": False,
            "busy": True,
            "busy_agent": run.agent,
            "busy_since": run.started_at,
        })

    return jsonify({
        "active": True,
        "busy": True,
        "busy_agent": run.agent,
        "agent": run.agent,
        "session_id": run.session_id,
        "module": run.module,
        "auto_push": run.auto_push,
        "status": run.status,
        "started_at": run.started_at,
        "step_progress": run.step_progress,
        # step_progress keeps its historic string-map shape; timing and cost ride
        # alongside so no existing consumer has to change.
        "step_metrics": run.step_metrics,
        "metrics": run.metrics_totals,
        "start_from_step": run.start_from_step,
    })


@qa_bp.route(f"{_BASE}/run/<session_id>/cancel", methods=["POST"])
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

    try:
        run = runner.start_run({"auto_push": auto_push}, agent=spec.name,
                               session_id=session_id, start_from_step=from_step)
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
        "status": run.status,
        "started_at": run.started_at,
        "start_from_step": from_step,
    }), 201


# ── Live + history stream (unified endpoint) ──────────────────────────────────
@qa_bp.route(f"{_BASE}/run/<session_id>/stream", methods=["GET"])
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
    return jsonify({"items": audit_reader.list_sessions(limit=limit, offset=offset,
                                                    agent=spec.name)})


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
    """Directories an artefact may legitimately come from."""
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
def sessions_get(agent: str, session_id: str):
    spec, err = _resolve(agent)
    if err:
        return err
    session = audit_reader.get_session(session_id, agent=spec.name)
    if session is None:
        return jsonify({"error": "session not found"}), 404
    return jsonify(session)


@qa_bp.route(f"{_BASE}/sessions/<session_id>/metrics", methods=["GET"])
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

    return jsonify(analytics.query(
        window=window,
        agent=(request.args.get("agent") or "").strip() or None,
        since=_ts("from"), until=_ts("to"),
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
