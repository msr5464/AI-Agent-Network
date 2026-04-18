#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/test-authoring-agent/run.sh
# Takes plain English test steps from queue/<feature>.txt, generates
# framework-compliant Java tests in Thanos-pw, validates, and raises a PR.
#
# Usage (via Makefile):
#   make run AGENT=test-authoring-agent MODULE=payments    # direct mode
#   make run AGENT=test-authoring-agent                    # queue mode: picks oldest .txt
#   AUTO_PUSH=false make run AGENT=test-authoring-agent MODULE=payments   # dry-run
#
# Retry loop: if mvn test fails after generation, re-runs 04_run_and_fix.py
# up to MAX_FIX_ATTEMPTS (default: 3).
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MODULE="${1:-${MODULE:-}}"

# Load .env files (root → agent override)
source "$REPO_ROOT/shared/load_env.sh"

export MODULE AGENT_DIR REPO_ROOT

# ── Session helpers (log, run_step, fmt_duration, elapsed_since) ──────────────
source "$REPO_ROOT/shared/session.sh"

# ── Testing-mode cache helpers ────────────────────────────────────────────────
# When TESTING_MODE=true, step-01 and step-02 outputs are cached under
# agents/test-authoring-agent/cache/<module>/ so they are reused on every
# subsequent run of the same input file — saving ~3 minutes per iteration.
# Clear the cache manually to force a fresh run:
#   rm -rf agents/test-authoring-agent/cache/<module>/
TESTING_MODE="${TESTING_MODE:-false}"
CACHE_DIR="$AGENT_DIR/cache/$MODULE"

# _cache_hit <filename>  → returns 0 if cache exists and TESTING_MODE=true
_cache_hit() {
  [[ "$TESTING_MODE" == "true" ]] && [[ -f "$CACHE_DIR/$1" ]]
}
# _cache_restore <filename>  → copies file from cache into current AUDIT_DIR
_cache_restore() {
  cp "$CACHE_DIR/$1" "$AUDIT_DIR/$1"
  log "TESTING_MODE: restored $1 from cache ($CACHE_DIR)"
}
# _cache_save <filename>  → copies file from current AUDIT_DIR into cache
_cache_save() {
  if [[ "$TESTING_MODE" == "true" ]] && [[ -f "$AUDIT_DIR/$1" ]]; then
    mkdir -p "$CACHE_DIR"
    cp "$AUDIT_DIR/$1" "$CACHE_DIR/$1"
    log "TESTING_MODE: cached $1 → $CACHE_DIR"
  fi
}

# ── Locate input file ─────────────────────────────────────────────────────────
QUEUE_DIR="$AGENT_DIR/queue"
PROCESSED_DIR="$QUEUE_DIR/processed"
mkdir -p "$PROCESSED_DIR"

if [[ -n "$MODULE" ]]; then
  INPUT_FILE="$QUEUE_DIR/${MODULE}.txt"
  if [[ ! -f "$INPUT_FILE" ]]; then
    log "ERROR: Input file not found: $INPUT_FILE"
    log "Create agents/test-authoring-agent/queue/${MODULE}.txt first."
    exit 1
  fi
  MODE="direct"
else
  # Queue mode: pick the oldest .txt file
  INPUT_FILE=$(ls -t "$QUEUE_DIR"/*.txt 2>/dev/null | tail -1 || true)
  if [[ -z "$INPUT_FILE" ]]; then
    log "Queue is empty — nothing to create."
    log "Add a .txt file to agents/test-authoring-agent/queue/ first."
    exit 0
  fi
  MODULE=$(basename "$INPUT_FILE" .txt)
  MODE="queue"
fi

export INPUT_FILE MODULE

# ── Session init ───────────────────────────────────────────────────────────────
# Honor a pre-set SESSION_ID / AUDIT_DIR (used by qa_agents_server so the wrapper
# knows where the agent will write audit files before it starts). When invoked
# via the CLI / Makefile neither is set, so we fall back to the historical default.
SESSION_ID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)-create-${MODULE}}"
AUDIT_DIR="${AUDIT_DIR:-$AGENT_DIR/audit/$SESSION_ID}"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "test-authoring-agent | mode=$MODE"
log "module=$MODULE"
log "input=$INPUT_FILE"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Write session init markdown
cat > "$AUDIT_DIR/00-session-init.md" << EOF
# Session Init

Mode: $MODE
Session ID: $SESSION_ID
Module: $MODULE
Input File: $INPUT_FILE
Started: $(date +%Y-%m-%dT%H:%M:%S)

## Env Snapshot (keys only)
$(env | grep -E '^(GITHUB_|SLACK_|MAX_|AUTO_|AUTOCREATE_|CLAUDE_|WORKSPACE_|NODE_|PLAYWRIGHT_)' \
  | sed 's/=.*/=<set>/' | sort)
EOF

declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

# ── Prerequisite — sync GITHUB_DEFAULT_BRANCH before any step runs ───────────────
WORKSPACE_DIR="${WORKSPACE_DIR:-}"
GITHUB_REPO_AUTOMATION="${GITHUB_REPO_AUTOMATION:-Jarvis}"
GITHUB_DEFAULT_BRANCH="${GITHUB_DEFAULT_BRANCH:-main}"
AUTOMATION_FRAMEWORK_DIR="${WORKSPACE_DIR}/${GITHUB_REPO_AUTOMATION}"

if [[ -z "$WORKSPACE_DIR" ]]; then
  log "ERROR: WORKSPACE_DIR is not set — cannot sync automation repo"
  exit 1
fi
if [[ ! -d "$AUTOMATION_FRAMEWORK_DIR/.git" ]]; then
  log "ERROR: Automation repo not found at $AUTOMATION_FRAMEWORK_DIR"
  exit 1
fi

log "Prerequisite: syncing $AUTOMATION_FRAMEWORK_DIR to origin/$GITHUB_DEFAULT_BRANCH ..."
if ! git -C "$AUTOMATION_FRAMEWORK_DIR" checkout -f "$GITHUB_DEFAULT_BRANCH" 2>&1; then
  log "Prerequisite: checkout failed — fetching from origin and retrying ..."
  git -C "$AUTOMATION_FRAMEWORK_DIR" fetch origin
  if ! git -C "$AUTOMATION_FRAMEWORK_DIR" checkout -f "$GITHUB_DEFAULT_BRANCH" 2>&1; then
    log "ERROR: Could not checkout $GITHUB_DEFAULT_BRANCH in $AUTOMATION_FRAMEWORK_DIR — aborting"
    exit 1
  fi
fi
if ! git -C "$AUTOMATION_FRAMEWORK_DIR" pull origin "$GITHUB_DEFAULT_BRANCH" 2>&1; then
  log "ERROR: git pull origin/$GITHUB_DEFAULT_BRANCH failed — aborting to avoid stale base"
  exit 1
fi
log "Prerequisite: $GITHUB_DEFAULT_BRANCH is up to date"

# ── Step 01 — Parse ────────────────────────────────────────────────────────────
if _cache_hit "01-parse.json"; then
  _cache_restore "01-parse.json"
  [[ -f "$CACHE_DIR/01-parse.md" ]] && cp "$CACHE_DIR/01-parse.md" "$AUDIT_DIR/01-parse.md"
  log "✓ [01/05] Parse — skipped (TESTING_MODE cache hit)"
  STEP_NAMES+=("[01/05] Parse")
  STEP_DURATIONS+=(0)
else
  run_step "[01/05] Parse" "python3 '$AGENT_DIR/actions/01_parse.py'"
  _cache_save "01-parse.json"
  _cache_save "01-parse.md"
fi

# ── Step 02 — Validate Web (skip for API-only) ────────────────────────────────
TEST_TYPE=$(python3 -c "
import json, os
from pathlib import Path
d = json.loads(Path(os.environ['AUDIT_DIR']).joinpath('01-parse.json').read_text())
print(d.get('test_type', 'api'))
")

if [[ "$TEST_TYPE" == "web" || "$TEST_TYPE" == "both" ]]; then
  if _cache_hit "02-validate-web.json"; then
    _cache_restore "02-validate-web.json"
    [[ -f "$CACHE_DIR/02-validate-web.md" ]] && cp "$CACHE_DIR/02-validate-web.md" "$AUDIT_DIR/02-validate-web.md"
    log "✓ [02/05] Validate Web — skipped (TESTING_MODE cache hit)"
    STEP_NAMES+=("[02/05] Validate Web")
    STEP_DURATIONS+=(0)
  else
    run_step "[02/05] Validate Web" "python3 '$AGENT_DIR/actions/02_validate_web.py'"
    # Only cache if Claude actually returned data (selectors or step results present)
    if python3 -c "
import json, os, sys
from pathlib import Path
d = json.loads(Path(os.environ['AUDIT_DIR']).joinpath('02-validate-web.json').read_text())
sys.exit(0 if (d.get('selectors') or d.get('steps_passed') or d.get('steps_failed')) else 1)
" 2>/dev/null; then
      _cache_save "02-validate-web.json"
      _cache_save "02-validate-web.md"
    else
      log "TESTING_MODE: step-02 result is empty — not caching (will re-run next time)"
    fi
  fi
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

# Initial test run — not counted as a fix attempt
run_step "[04/05] Run (initial)" \
  "FIX_ATTEMPT=0 python3 '$AGENT_DIR/actions/04_run_and_fix.py'"

FIX_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed" 2>/dev/null || echo "skipped")

if [[ "$FIX_RESULT" != "true" && "$FIX_RESULT" != "skipped" ]]; then
  FIX_ATTEMPT=1
  while true; do
    run_step "[04/05] Fix (attempt $FIX_ATTEMPT/$MAX_FIX_ATTEMPTS)" \
      "FIX_ATTEMPT=$FIX_ATTEMPT python3 '$AGENT_DIR/actions/04_run_and_fix.py'"

    FIX_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed" 2>/dev/null || echo "skipped")

    if [[ "$FIX_RESULT" == "true" || "$FIX_RESULT" == "skipped" ]]; then
      break
    fi

    if [[ "$FIX_ATTEMPT" -ge "$MAX_FIX_ATTEMPTS" ]]; then
      log "Tests still failing after $FIX_ATTEMPT fix attempt(s) — proceeding to ship"
      break
    fi

    log "Tests failed — retrying (fix attempt $((FIX_ATTEMPT + 1)))"
    FIX_ATTEMPT=$((FIX_ATTEMPT + 1))
  done
fi

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
