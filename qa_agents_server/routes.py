"""REST + SSE endpoints for qa_agents_server.

All routes are scoped under /agents/test-authoring-agent/* with a single
top-level /health check. The frontend in AI-Test-Studio proxies these paths
through its backend, so auth is enforced at the proxy layer — this server
does not implement its own auth (it is expected to bind to localhost).
"""

from __future__ import annotations

import json
import time
from typing import Generator

from flask import Blueprint, Response, jsonify, request, stream_with_context

from qa_agents_server import audit_reader, feature_files, runner
from qa_agents_server.runner import TERMINAL_STATUSES, AGENT

qa_bp = Blueprint("qa_agents", __name__)

_BASE = f"/agents/{AGENT}"


# ── Module file CRUD ──────────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/queue", methods=["GET"])
def queue_list():
    return jsonify({"items": feature_files.list_features()})


@qa_bp.route(f"{_BASE}/queue", methods=["POST"])
def queue_create():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    content = body.get("content")
    try:
        result = feature_files.write_feature(name, content)
    except feature_files.FeatureFileError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify(result), 201


@qa_bp.route(f"{_BASE}/queue/<name>", methods=["GET"])
def queue_read(name: str):
    try:
        return jsonify(feature_files.read_feature(name))
    except feature_files.FeatureFileError as e:
        return jsonify({"error": str(e)}), e.status


# ── Run trigger + control ─────────────────────────────────────────────────────
@qa_bp.route(f"{_BASE}/run", methods=["POST"])
def run_start():
    body = request.get_json(silent=True) or {}
    module = body.get("module")
    auto_push = bool(body.get("auto_push", False))
    try:
        run = runner.start_run(module=module, auto_push=auto_push)
    except runner._QueuedNotification as q:
        return jsonify({
            "queued": True,
            "position": q.position,
            "module": module,
            "session_id": q.session_id,
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
    }), 201


@qa_bp.route(f"{_BASE}/run/queue", methods=["GET"])
def pending_queue_list():
    return jsonify({"queue": runner.get_queue()})


@qa_bp.route(f"{_BASE}/run/queue/<int:index>", methods=["DELETE"])
def pending_queue_remove(index: int):
    removed = runner.remove_from_queue(index)
    if not removed:
        return jsonify({"error": "index out of range"}), 404
    return jsonify({"removed": True, "queue": runner.get_queue()})


@qa_bp.route(f"{_BASE}/run/active", methods=["GET"])
def run_active():
    session_id = runner.get_active_session_id()
    if session_id is None:
        return jsonify({"active": False})
    run = runner.get_run(session_id)
    if run is None:
        return jsonify({"active": False})
    return jsonify({
        "active": True,
        "session_id": run.session_id,
        "module": run.module,
        "auto_push": run.auto_push,
        "status": run.status,
        "started_at": run.started_at,
        "step_progress": run.step_progress,
        "start_from_step": run.start_from_step,
    })


@qa_bp.route(f"{_BASE}/run/<session_id>/cancel", methods=["POST"])
def run_cancel(session_id: str):
    ok = runner.cancel_run(session_id)
    if not ok:
        return jsonify({"error": "run not found or already finished"}), 404
    return jsonify({"status": "cancelling", "session_id": session_id})


@qa_bp.route(f"{_BASE}/sessions/<session_id>/retry", methods=["POST"])
def session_retry(session_id: str):
    """Re-run an existing (usually finished/failed) session starting from a
    specific step, reusing its steps-before-that output rather than starting
    the whole 01→05 pipeline over. body: {"from_step": 2-5, "auto_push": bool}.

    from_step must be >= 2 — resuming "from step 1" isn't a resume at all
    (there's nothing prior to reuse); that's just a fresh run, via POST /run.
    """
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
        run = runner.start_run(module=None, auto_push=auto_push,
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
def run_stream(session_id: str):
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
    events = audit_reader.replay_events(session_id)
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
def sessions_list():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0
    return jsonify({"items": audit_reader.list_sessions(limit=limit, offset=offset)})


@qa_bp.route(f"{_BASE}/sessions/<session_id>", methods=["GET"])
def sessions_get(session_id: str):
    session = audit_reader.get_session(session_id)
    if session is None:
        return jsonify({"error": "session not found"}), 404
    return jsonify(session)


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
