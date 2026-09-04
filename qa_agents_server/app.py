"""Flask app factory and entry point for qa_agents_server."""

from __future__ import annotations

import atexit
import os
import signal
import sys
from pathlib import Path

from flask import Flask, jsonify
from flask_cors import CORS

from qa_agents_server.paths import REPO_ROOT

# Ensure REPO_ROOT is importable so shared/ helpers work when the server
# is launched from any cwd (e.g. via systemd).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from qa_agents_server import runner, seed_examples, storage  # noqa: E402
from qa_agents_server.routes import qa_bp  # noqa: E402


def create_app() -> Flask:
    app = Flask(__name__)

    # Restrict CORS to the AI-Test-Studio origin. The browser never talks to us
    # directly in production — the AI-Test-Studio backend proxies — but allow
    # dev-time direct calls so the server is testable with curl/Postman.
    allowed = os.getenv("AI_TEST_STUDIO_URL", "http://localhost:5001")
    extra = os.getenv("QA_AGENT_SERVER_EXTRA_ORIGINS", "")
    origins = [o.strip() for o in (allowed + "," + extra).split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": origins, "supports_credentials": False}})

    # Load any pre-existing run registry and mark stranded runs as interrupted.
    storage.init()
    runner.reconcile_on_boot()

    # Put the documented examples in each agent's queue so a fresh checkout has
    # something in the UI. Seeds once per checkout and never overwrites; see
    # seed_examples for the rules. Done here rather than in run-server.sh so it
    # also covers `python -m qa_agents_server.app` and any other entry point.
    seeded = seed_examples.seed_all(log=lambda m: print(f"[seed]{m}"))
    if seeded:
        print(f"Seeded example queue items for {len(seeded)} agent(s)")

    app.register_blueprint(qa_bp)

    @app.route("/health")
    def health():
        return jsonify({
            "status": "ok",
            "service": "qa_agents_server",
            "version": "0.1.0",
            "active_run": runner.get_active_session_id(),
        })

    @app.errorhandler(404)
    def _not_found(_e):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def _internal(_e):
        return jsonify({"error": "internal server error"}), 500

    # Ensure subprocesses are cleaned up on shutdown (Ctrl-C, SIGTERM, atexit).
    def _shutdown(*_args):
        runner.shutdown_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    atexit.register(runner.shutdown_all)

    return app


def main():
    port = int(os.getenv("QA_AGENT_SERVER_PORT", "8765"))
    host = os.getenv("QA_AGENT_SERVER_HOST", "0.0.0.0")
    app = create_app()
    print(f"QA Agent Server listening on http://{host}:{port}")
    # threaded=True is essential: SSE endpoints hold a connection open, and
    # the runner spawns background threads per run.
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
