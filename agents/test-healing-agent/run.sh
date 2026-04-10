#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/test-healing-agent/run.sh
# Picks a handoff from the queue (or a specific BUILD_TAG), attempts locator
# fixes, verifies with test runs, and creates a GitHub PR.
#
# Usage (via Makefile):
#   make run AGENT=test-healing-agent                           # queue mode: picks oldest
#   make run AGENT=test-healing-agent BUILD_TAG=ProdSanity-541  # direct: specific handoff
#   AUTO_PUSH=false make run AGENT=test-healing-agent           # dry-run: no PR
#
# Retry loop: if tests fail after fix, re-runs 01_fix.py up to MAX_FIX_ATTEMPTS.
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
BUILD_TAG="${1:-${BUILD_TAG:-}}"

# Load .env files (root → agent override)
source "$REPO_ROOT/shared/load_env.sh"

export BUILD_TAG
export AGENT_DIR
export REPO_ROOT

# ── Session helpers (log, run_step, fmt_duration, elapsed_since) ──────────────
source "$REPO_ROOT/shared/session.sh"

# ── Queue scout / direct mode ─────────────────────────────────────────────────
QUEUE_DIR="$AGENT_DIR/queue"
PROCESSED_DIR="$QUEUE_DIR/processed"
mkdir -p "$PROCESSED_DIR"

# Honour HANDOFF_FILE env var if passed directly (e.g. from run-autofix.sh with a file path)
HANDOFF_FILE="${HANDOFF_FILE:-}"

if [[ -n "$HANDOFF_FILE" ]]; then
  # File path mode — caller supplied an explicit JSON file
  if [[ ! -f "$HANDOFF_FILE" ]]; then
    log "ERROR: Handoff file not found: $HANDOFF_FILE"
    exit 1
  fi
  BUILD_TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" "$HANDOFF_FILE")
  SAFE_TAG="${BUILD_TAG//\//-}"
  MODE="file"

elif [[ -n "$BUILD_TAG" ]]; then
  # Build tag mode — look up file in queue/
  SAFE_TAG="${BUILD_TAG//\//-}"
  HANDOFF_FILE="$QUEUE_DIR/${SAFE_TAG}.json"
  if [[ ! -f "$HANDOFF_FILE" ]]; then
    log "ERROR: No handoff file for BUILD_TAG=$BUILD_TAG"
    log "Expected: $HANDOFF_FILE"
    log "Run test-triaging-agent first, or check the queue directory."
    exit 1
  fi
  MODE="direct"

else
  # Queue mode — pick the oldest .json file in queue/
  HANDOFF_FILE=$(ls -t "$QUEUE_DIR"/*.json 2>/dev/null | tail -1 || true)
  if [[ -z "$HANDOFF_FILE" ]]; then
    log "Queue is empty — nothing to fix."
    log "Run test-triaging-agent first to populate the queue."
    exit 0
  fi
  BUILD_TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" "$HANDOFF_FILE")
  SAFE_TAG="${BUILD_TAG//\//-}"
  MODE="queue"
fi

export BUILD_TAG HANDOFF_FILE

# ── Session init ───────────────────────────────────────────────────────────────
SESSION_ID="$(date +%Y%m%d-%H%M%S)-fix-${SAFE_TAG}"
AUDIT_DIR="$AGENT_DIR/audit/$SESSION_ID"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "test-healing-agent | mode=$MODE"
log "build_tag=$BUILD_TAG"
log "handoff=$HANDOFF_FILE"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Write session init
cat > "$AUDIT_DIR/00-session-init.md" << EOF
# Session Init

Mode: $MODE
Session ID: $SESSION_ID
Build Tag: $BUILD_TAG
Handoff File: $HANDOFF_FILE
Started: $(date +%Y-%m-%dT%H:%M:%S)

## Env Snapshot (keys only)
$(env | grep -E '^(GITHUB_|SLACK_|MAX_|AUTO_|AUTOFIX_|CLAUDE_|WORKSPACE_|REPO_CONTEXT_|TEST_RUNNER_)' | sed 's/=.*/=<set>/' | sort)
EOF

declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

# ── Step 01 — Fix (with retry loop) ──────────────────────────────────────────
MAX_FIX_ATTEMPTS="${MAX_FIX_ATTEMPTS:-2}"
FIX_ATTEMPT=1

while true; do
  run_step "[01/02] Fix (attempt $FIX_ATTEMPT/$MAX_FIX_ATTEMPTS)" \
    "FIX_ATTEMPT=$FIX_ATTEMPT python3 '$AGENT_DIR/actions/01_fix.py'"

  FIX_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed" 2>/dev/null || echo "skipped")

  if [[ "$FIX_RESULT" == "true" || "$FIX_RESULT" == "skipped" ]]; then
    break
  fi

  if [[ "$FIX_ATTEMPT" -ge "$MAX_FIX_ATTEMPTS" ]]; then
    log "Fixes still failing after $FIX_ATTEMPT attempt(s) — proceeding to ship (will escalate)"
    break
  fi

  log "Some tests still failing — retrying fix (attempt $((FIX_ATTEMPT + 1)))"
  FIX_ATTEMPT=$((FIX_ATTEMPT + 1))
done

# ── Step 02 — Ship (PR + Slack) ───────────────────────────────────────────────
run_step "[02/02] Ship" "python3 '$AGENT_DIR/actions/02_ship.py'"

# ── Mark handoff as processed ─────────────────────────────────────────────────
mv "$HANDOFF_FILE" "$PROCESSED_DIR/$(basename "$HANDOFF_FILE")"
log "Moved handoff to processed: $PROCESSED_DIR/$(basename "$HANDOFF_FILE")"

# ── Final summary ─────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(elapsed_since $SESSION_START)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Done. Total time: $(fmt_duration $TOTAL_ELAPSED)"
echo ""
for i in "${!STEP_NAMES[@]}"; do
  printf "  %-50s %s\n" "${STEP_NAMES[$i]}" "$(fmt_duration ${STEP_DURATIONS[$i]})"
done
echo ""
log "Audit: $AUDIT_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
