#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/learning-agent/run.sh
# Learn from people's feedback on the agents' work (6 steps). See CLAUDE.md.
#
# Usage:
#   ./scripts/run-learning-agent.sh                 # collect, propose, report
#   AUTO_PUSH=true ./scripts/run-learning-agent.sh  # and open the lesson PRs
#   STOP_AFTER=classify ./scripts/run-learning-agent.sh
#   Valid STOP_AFTER values: collect, attribute, classify, propose, validate
#
# Output: 06-report.md in the session; with AUTO_PUSH=true, a PR changing
# config/lessons/*.yaml (and one to the automation repo's CLAUDE.md for
# conventions). Nothing reaches an agent until a person merges.
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

source "$REPO_ROOT/shared/load_env.sh"
export AGENT_DIR REPO_ROOT

source "$REPO_ROOT/shared/session.sh"

# The model has no default in code — config/.env sets it.
require_settings LEARNING_MODEL

SESSION_ID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)-learn}"
AUDIT_DIR="${AUDIT_DIR:-$AGENT_DIR/audit/$SESSION_ID}"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "learning-agent | auto_push=${AUTO_PUSH:-false}"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cat > "$AUDIT_DIR/00-session-init.md" << EOF2
# Session Init

Agent: learning-agent
Session ID: $SESSION_ID
Started: $(date +%Y-%m-%dT%H:%M:%S)
Auto push: ${AUTO_PUSH:-false}

## Env Snapshot (keys only)
$(env | grep -E '^(LEARNING_|LESSONS_|GITHUB_|SLACK_|AUTO_|FEEDBACK_)' | sed 's/=.*/=<set>/' | sort)
EOF2

STOP_AFTER="${STOP_AFTER:-}"
stop_check() {
  if [[ "$STOP_AFTER" == "$1" ]]; then
    flush_step_done
    log "STOP_AFTER=$1 — stopped after $(fmt_duration "$(elapsed_since "$SESSION_START")")"
    log "Audit: $AUDIT_DIR"
    exit 0
  fi
}

declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

run_step "[01/06] Collect"   "python3 '$AGENT_DIR/actions/01_collect.py'"   collect
stop_check collect
run_step "[02/06] Attribute" "python3 '$AGENT_DIR/actions/02_attribute.py'" attribute
stop_check attribute
run_step "[03/06] Classify"  "python3 '$AGENT_DIR/actions/03_classify.py'"  classify
stop_check classify
run_step "[04/06] Propose"   "python3 '$AGENT_DIR/actions/04_propose.py'"   propose
stop_check propose
run_step "[05/06] Validate"  "python3 '$AGENT_DIR/actions/05_validate.py'"  validate
stop_check validate
run_step "[06/06] Ship"      "python3 '$AGENT_DIR/actions/06_ship.py'"      ship

flush_step_done
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Done. Total time: $(fmt_duration "$(elapsed_since "$SESSION_START")")"
echo ""
print_step_table 50
echo ""
log "Report: $AUDIT_DIR/06-report.md"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
