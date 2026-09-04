#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-server.sh
# Boots the qa_agents_server HTTP + SSE wrapper around test-authoring-agent.
# The server is separate from the Makefile CLI entry points — both coexist.
#
# Env overrides:
#   QA_AGENT_SERVER_PORT      (default 8765)
#   QA_AGENT_SERVER_HOST      (default 0.0.0.0)
#   AI_TEST_STUDIO_URL        (default http://localhost:5001) — CORS allowlist
#   QA_SEED_EXAMPLES          (default true) — copy docs/examples/queue/<agent>/
#                             into each agent's queue on boot, so the UI has
#                             something in it on a fresh checkout. Seeds once per
#                             checkout: it never overwrites a queued file, never
#                             re-creates one already in processed/, and stops
#                             once each queue has its .examples-seeded marker.
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR=""  # not used here, but shared/load_env.sh accepts empty
cd "$REPO_ROOT"

# Load repo-level .env (the sourced script accepts an empty AGENT_DIR)
export REPO_ROOT AGENT_DIR
if [[ -f "$REPO_ROOT/shared/load_env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/shared/load_env.sh"
fi

export QA_AGENT_SERVER_PORT="${QA_AGENT_SERVER_PORT:-8765}"
export QA_AGENT_SERVER_HOST="${QA_AGENT_SERVER_HOST:-0.0.0.0}"
export AI_TEST_STUDIO_URL="${AI_TEST_STUDIO_URL:-http://localhost:5001}"
export QA_SEED_EXAMPLES="${QA_SEED_EXAMPLES:-true}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "QA Agent Server"
echo "  host            : $QA_AGENT_SERVER_HOST"
echo "  port            : $QA_AGENT_SERVER_PORT"
echo "  cors allowed    : $AI_TEST_STUDIO_URL"
echo "  repo root       : $REPO_ROOT"
echo "  seed examples   : $QA_SEED_EXAMPLES"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exec python3 -m qa_agents_server.app
