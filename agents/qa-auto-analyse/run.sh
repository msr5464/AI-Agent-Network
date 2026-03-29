#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/qa-auto-analyse/run.sh
# Orchestrates a full qa-auto-analyse session (5 steps).
#
# Usage (via Makefile):
#   make run AGENT=qa-auto-analyse                          # scout mode
#   make run AGENT=qa-auto-analyse BUILD_TAG=ProdSanity-541 # direct mode
#
# Stop early:
#   STOP_AFTER=collect make run AGENT=qa-auto-analyse
#   Valid values: scout, collect, classify, review
#
# Output: HTML report + agents/qa-auto-fix/queue/<tag>.json (if APPROVED)
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
BUILD_TAG="${1:-${BUILD_TAG:-}}"

# Load root .env first, then agent .env (agent overrides root)
for envfile in "$REPO_ROOT/config/.env" "$REPO_ROOT/.env" "$AGENT_DIR/.env"; do
  if [[ -f "$envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
  fi
done

export BUILD_TAG
export AGENT_DIR
export REPO_ROOT

# ── Logging helper ────────────────────────────────────────────────────────────
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

# ── Session init ───────────────────────────────────────────────────────────────
if [[ -n "$BUILD_TAG" ]]; then
  SESSION_ID="$(date +%Y%m%d-%H%M%S)-${BUILD_TAG//\//-}"
  MODE="direct"
else
  SESSION_ID="$(date +%Y%m%d-%H%M%S)-scout"
  MODE="scout"
fi

AUDIT_DIR="$AGENT_DIR/audit/$SESSION_ID"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "qa-auto-analyse | mode=$MODE"
[[ -n "$BUILD_TAG" ]] && log "build_tag=$BUILD_TAG"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Write session init
cat > "$AUDIT_DIR/00-session-init.md" << EOF
# Session Init

Trigger: $MODE
Session ID: $SESSION_ID
Build Tag: ${BUILD_TAG:-"(scout will select)"}
Started: $(date +%Y-%m-%dT%H:%M:%S)

## Env Snapshot (keys only)
$(env | grep -E '^(DB_|INPUT_|OUTPUT_|GITHUB_|SLACK_|MAX_|AUTO_|CLASSIFIER_|REVIEWER_|CLAUDE_|SCOUT_)' | sed 's/=.*/=<set>/' | sort)
EOF

# ── Stop early helper ─────────────────────────────────────────────────────────
STOP_AFTER="${STOP_AFTER:-}"
stop_check() {
  if [[ "$STOP_AFTER" == "$1" ]]; then
    local total
    total=$(elapsed_since $SESSION_START)
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "STOP_AFTER=$1 — stopped after $(fmt_duration $total)"
    log "Audit: $AUDIT_DIR"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 0
  fi
}

# ── Step runner with timing ───────────────────────────────────────────────────
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

# ── Step 01 — Scout ────────────────────────────────────────────────────────────
if [[ "$MODE" == "scout" ]]; then
  run_step "[01/05] Scout" "python3 '$AGENT_DIR/actions/01_scout.py'"

  BUILD_TAG=$(cat "$AUDIT_DIR/.selected-buildtag" 2>/dev/null || true)
  if [[ -z "$BUILD_TAG" ]]; then
    log "ERROR: Scout did not select a build tag"
    exit 1
  fi
  export BUILD_TAG

  # Rename session folder to include build tag
  SAFE_TAG="${BUILD_TAG//\//-}"
  NEW_AUDIT_DIR="$AGENT_DIR/audit/$(date +%Y%m%d-%H%M%S)-${SAFE_TAG}"
  mv "$AUDIT_DIR" "$NEW_AUDIT_DIR"
  AUDIT_DIR="$NEW_AUDIT_DIR"
  SESSION_ID="$(basename "$AUDIT_DIR")"
  export AUDIT_DIR SESSION_ID
  log "Selected: $BUILD_TAG"
else
  log "[01/05] Scout — skipped (direct mode, build_tag=$BUILD_TAG)"
  # Write minimal scout output so step 02 can proceed
  python3 -c "
import json, os
from pathlib import Path
build_tag = os.environ['BUILD_TAG']
audit_dir = Path(os.environ['AUDIT_DIR'])
scout = {'selected_build_tag': build_tag, 'mode': 'direct', 'scored': []}
with open(audit_dir / '01-scout.json', 'w') as f:
    json.dump(scout, f, indent=2)
with open(audit_dir / '.selected-buildtag', 'w') as f:
    f.write(build_tag)
print(f'Direct mode: using build_tag={build_tag}')
"
fi

stop_check scout

# ── Step 02 — Collect ─────────────────────────────────────────────────────────
run_step "[02/05] Collect" "python3 '$AGENT_DIR/actions/02_collect.py'"

stop_check collect

# ── Step 03 — Classify ────────────────────────────────────────────────────────
run_step "[03/05] Classify" "python3 '$AGENT_DIR/actions/03_classify.py'"

stop_check classify

# ── Step 04 — Review ──────────────────────────────────────────────────────────
run_step "[04/05] Review" "python3 '$AGENT_DIR/actions/04_review.py'"

stop_check review

# ── Step 05 — Ship ────────────────────────────────────────────────────────────
run_step "[05/05] Ship" "python3 '$AGENT_DIR/actions/05_ship.py'"

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

# Show if handoff was queued for qa-auto-fix
SAFE_BT="${BUILD_TAG//\//-}"
QUEUE_FILE="$REPO_ROOT/agents/qa-auto-fix/queue/${SAFE_BT}.json"
if [[ -f "$QUEUE_FILE" ]]; then
  log "Queued for qa-auto-fix: agents/qa-auto-fix/queue/${SAFE_BT}.json"
  log "Run: make run AGENT=qa-auto-fix BUILD_TAG=$BUILD_TAG"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
