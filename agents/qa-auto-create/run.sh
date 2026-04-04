#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/qa-auto-create/run.sh
# Takes plain English test steps from queue/<feature>.txt, generates
# framework-compliant Java tests in Thanos-pw, validates, and raises a PR.
#
# Usage (via Makefile):
#   make run AGENT=qa-auto-create FEATURE=payments   # direct mode
#   make run AGENT=qa-auto-create                    # queue mode: picks oldest .txt
#   AUTO_PUSH=false make run AGENT=qa-auto-create FEATURE=payments  # dry-run
#
# Retry loop: if mvn test fails after generation, re-runs 04_run_and_fix.py
# up to MAX_FIX_ATTEMPTS (default: 3).
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

FEATURE="${1:-${FEATURE:-}}"

# Load env files in order: config/.env, repo root .env, agent .env
for envfile in "$REPO_ROOT/config/.env" "$REPO_ROOT/.env" "$AGENT_DIR/.env"; do
  if [[ -f "$envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
  fi
done

export FEATURE AGENT_DIR REPO_ROOT

# ── Logging helpers ───────────────────────────────────────────────────────────
SESSION_START=$(date +%s)

log() {
  echo "[$(date +%H:%M:%S)] $*"
}

elapsed_since() {
  local start=$1
  local now
  now=$(date +%s)
  echo $(( now - start ))
}

fmt_duration() {
  local secs=$1
  if (( secs >= 60 )); then
    printf "%dm %ds" $(( secs / 60 )) $(( secs % 60 ))
  else
    printf "%ds" "$secs"
  fi
}

# ── Locate input file ─────────────────────────────────────────────────────────
QUEUE_DIR="$AGENT_DIR/queue"
PROCESSED_DIR="$QUEUE_DIR/processed"
mkdir -p "$PROCESSED_DIR"

if [[ -n "$FEATURE" ]]; then
  INPUT_FILE="$QUEUE_DIR/${FEATURE}.txt"
  if [[ ! -f "$INPUT_FILE" ]]; then
    log "ERROR: Input file not found: $INPUT_FILE"
    log "Create agents/qa-auto-create/queue/${FEATURE}.txt first."
    exit 1
  fi
  MODE="direct"
else
  # Queue mode: pick the oldest .txt file
  INPUT_FILE=$(ls -t "$QUEUE_DIR"/*.txt 2>/dev/null | tail -1 || true)
  if [[ -z "$INPUT_FILE" ]]; then
    log "Queue is empty — nothing to create."
    log "Add a .txt file to agents/qa-auto-create/queue/ first."
    exit 0
  fi
  FEATURE=$(basename "$INPUT_FILE" .txt)
  MODE="queue"
fi

export INPUT_FILE FEATURE

# ── Session init ───────────────────────────────────────────────────────────────
SESSION_ID="$(date +%Y%m%d-%H%M%S)-create-${FEATURE}"
AUDIT_DIR="$AGENT_DIR/audit/$SESSION_ID"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "qa-auto-create | mode=$MODE"
log "feature=$FEATURE"
log "input=$INPUT_FILE"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Write session init markdown
cat > "$AUDIT_DIR/00-session-init.md" << EOF
# Session Init

Mode: $MODE
Session ID: $SESSION_ID
Feature: $FEATURE
Input File: $INPUT_FILE
Started: $(date +%Y-%m-%dT%H:%M:%S)

## Env Snapshot (keys only)
$(env | grep -E '^(GITHUB_|SLACK_|MAX_|AUTO_|AUTOCREATE_|CLAUDE_|WORKSPACE_|NODE_|PLAYWRIGHT_)' \
  | sed 's/=.*/=<set>/' | sort)
EOF

# ── Step runner ───────────────────────────────────────────────────────────────
declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

run_step() {
  local label="$1"
  local cmd="$2"
  local step_start
  step_start=$(date +%s)
  log "▶ $label"
  eval "$cmd"
  local dur
  dur=$(elapsed_since $step_start)
  STEP_NAMES+=("$label")
  STEP_DURATIONS+=("$dur")
  log "✓ $label — $(fmt_duration $dur)"
}

# ── Step 01 — Parse ────────────────────────────────────────────────────────────
run_step "[01/05] Parse" "python3 '$AGENT_DIR/actions/01_parse.py'"

# ── Step 02 — Validate Web (skip for API-only) ────────────────────────────────
TEST_TYPE=$(python3 -c "
import json, os
from pathlib import Path
d = json.loads(Path(os.environ['AUDIT_DIR']).joinpath('01-parse.json').read_text())
print(d.get('test_type', 'api'))
")

if [[ "$TEST_TYPE" == "web" || "$TEST_TYPE" == "both" ]]; then
  run_step "[02/05] Validate Web" "python3 '$AGENT_DIR/actions/02_validate_web.py'"
else
  log "[02/05] Validate Web — skipped (test_type=$TEST_TYPE)"
  python3 -c "
import json, os
from pathlib import Path
Path(os.environ['AUDIT_DIR']).joinpath('02-validate-web.json').write_text(
  json.dumps({'skipped': True, 'reason': 'API-only test', 'selectors': {},
              'steps_passed': [], 'steps_failed': []})
)
"
fi

# ── Step 03 — Generate ────────────────────────────────────────────────────────
run_step "[03/05] Generate" "python3 '$AGENT_DIR/actions/03_generate.py'"

# ── Step 04 — Run and Fix (with retry loop) ───────────────────────────────────
MAX_FIX_ATTEMPTS="${MAX_FIX_ATTEMPTS:-3}"
FIX_ATTEMPT=1

while true; do
  run_step "[04/05] Run+Fix (attempt $FIX_ATTEMPT/$MAX_FIX_ATTEMPTS)" \
    "FIX_ATTEMPT=$FIX_ATTEMPT python3 '$AGENT_DIR/actions/04_run_and_fix.py'"

  FIX_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed" 2>/dev/null || echo "skipped")

  if [[ "$FIX_RESULT" == "true" || "$FIX_RESULT" == "skipped" ]]; then
    break
  fi

  if [[ "$FIX_ATTEMPT" -ge "$MAX_FIX_ATTEMPTS" ]]; then
    log "Tests still failing after $FIX_ATTEMPT attempt(s) — proceeding to ship"
    break
  fi

  log "Tests failed — retrying fix (attempt $((FIX_ATTEMPT + 1)))"
  FIX_ATTEMPT=$((FIX_ATTEMPT + 1))
done

# ── Step 05 — Ship ────────────────────────────────────────────────────────────
run_step "[05/05] Ship" "python3 '$AGENT_DIR/actions/05_ship.py'"

# ── Mark input as processed ───────────────────────────────────────────────────
mv "$INPUT_FILE" "$PROCESSED_DIR/$(basename "$INPUT_FILE")"
log "Moved to processed: $PROCESSED_DIR/$(basename "$INPUT_FILE")"

# ── Final summary ─────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(elapsed_since $SESSION_START)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Done. Total time: $(fmt_duration $TOTAL_ELAPSED)"
echo ""
for i in "${!STEP_NAMES[@]}"; do
  printf "  %-55s %s\n" "${STEP_NAMES[$i]}" "$(fmt_duration ${STEP_DURATIONS[$i]})"
done
echo ""
log "Audit: $AUDIT_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
