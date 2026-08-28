#!/bin/bash
set -Eeuo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/test-adaptation-agent/run.sh
# Reads a human-written change note describing how the PRODUCT changed, works out
# which tests it reaches, explores the new flow against the live product, updates
# the affected tests, verifies them, and opens a PR — always NEEDS-REVIEW.
#
#   make run AGENT=test-adaptation-agent MODULE=checkout
#   make run AGENT=test-adaptation-agent                     # queue: oldest .txt
#   EXPLORE_ONLY=true make run AGENT=test-adaptation-agent MODULE=checkout
#   ADAPT_APPLY=false make run AGENT=test-adaptation-agent MODULE=checkout
#   START_FROM_STEP=4 SESSION_ID=<sid> make run AGENT=test-adaptation-agent
#
# Unlike healing, the expensive step here is exploration, not the edit. So this
# supports resume (a 30-minute browser run must never be re-paid to retry an
# edit) and caches steps 01-03 under TESTING_MODE.
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
MODULE="${1:-${MODULE:-}}"

source "$REPO_ROOT/shared/load_env.sh"

export MODULE
export AGENT_DIR
export REPO_ROOT

source "$REPO_ROOT/shared/session.sh"

QUEUE_DIR="$AGENT_DIR/queue"
PROCESSED_DIR="$QUEUE_DIR/processed"
mkdir -p "$PROCESSED_DIR"

START_FROM_STEP="${START_FROM_STEP:-1}"
EXPLORE_ONLY="${EXPLORE_ONLY:-false}"
TESTING_MODE="${TESTING_MODE:-false}"

# ── Testing-mode cache ────────────────────────────────────────────────────────
# Steps 01-03 are the expensive, non-deterministic half. Developing step 04
# against a cached exploration costs no browser time and no model calls.
# Both halves of a step's output move together. Caching only the .json left a
# restored session with no report to read, so the History detail view showed a
# run that had apparently produced nothing — and TESTING_MODE is on in
# config/.env, so that was every run on this machine.
_cache_hit()     { [[ "$TESTING_MODE" == "true" ]] && [[ -f "$CACHE_DIR/$1" ]]; }
_cache_restore() {
  cp "$CACHE_DIR/$1" "$AUDIT_DIR/$1"
  local md="${1%.json}.md"
  [[ -f "$CACHE_DIR/$md" ]] && cp "$CACHE_DIR/$md" "$AUDIT_DIR/$md"
  log "TESTING_MODE: restored $1 from cache"
}
_cache_save() {
  [[ "$TESTING_MODE" == "true" ]] || return 0
  mkdir -p "$CACHE_DIR"
  [[ -f "$AUDIT_DIR/$1" ]] && cp "$AUDIT_DIR/$1" "$CACHE_DIR/$1"
  local md="${1%.json}.md"
  [[ -f "$AUDIT_DIR/$md" ]] && cp "$AUDIT_DIR/$md" "$CACHE_DIR/$md"
  return 0
}

# ── Mode resolution ───────────────────────────────────────────────────────────
if [[ "$START_FROM_STEP" -gt 1 ]]; then
  if [[ -z "${SESSION_ID:-}" ]]; then
    log "ERROR: START_FROM_STEP requires SESSION_ID"; exit 1
  fi
  AUDIT_DIR="${AUDIT_DIR:-$AGENT_DIR/audit/$SESSION_ID}"
  if [[ ! -d "$AUDIT_DIR" ]]; then
    log "ERROR: cannot resume — no session at $AUDIT_DIR"; exit 1
  fi
  if [[ -z "$MODULE" ]]; then
    MODULE=$(grep -m1 '^Module: ' "$AUDIT_DIR/00-session-init.md" 2>/dev/null | sed 's/^Module: //')
    [[ -n "$MODULE" ]] || { log "ERROR: could not recover MODULE — pass it explicitly"; exit 1; }
  fi
  # The step before the resume point must actually have produced output, or there
  # is nothing to resume from. (Plain case, not an associative array — macOS
  # ships bash 3.2, which predates `declare -A`.)
  case "$START_FROM_STEP" in
    2) _prereqs="01-parse-change.json" ;;
    3) _prereqs="01-parse-change.json 02-scope.json" ;;
    4) _prereqs="02-scope.json 03-explore.json" ;;
    5) _prereqs="03-explore.json 04-adapt.json" ;;
    *) _prereqs="" ;;
  esac
  for _f in $_prereqs; do
    [[ -f "$AUDIT_DIR/$_f" ]] || { log "ERROR: cannot resume from step $START_FROM_STEP — missing $_f"; exit 1; }
  done
  log "Resuming $SESSION_ID from step $START_FROM_STEP — clearing stale output"
  for _n in 02 03 04 05; do
    if (( 10#$_n >= START_FROM_STEP )); then rm -f "$AUDIT_DIR"/"${_n}"-*; fi
  done
  rm -f "$AUDIT_DIR/.fix-passed" "$AUDIT_DIR/.verdict" "$AUDIT_DIR/.cancelled" "$AUDIT_DIR/.skip-reason"
  INPUT_FILE="$QUEUE_DIR/${MODULE}.txt"
  [[ -f "$INPUT_FILE" ]] || INPUT_FILE="$PROCESSED_DIR/${MODULE}.txt"
  MODE="resume"

elif [[ -n "$MODULE" ]]; then
  INPUT_FILE="$QUEUE_DIR/${MODULE}.txt"
  if [[ ! -f "$INPUT_FILE" ]]; then
    log "ERROR: change note not found: $INPUT_FILE"
    log "Create agents/test-adaptation-agent/queue/${MODULE}.txt first."
    exit 1
  fi
  MODE="direct"
else
  INPUT_FILE=$(ls -t "$QUEUE_DIR"/*.txt 2>/dev/null | tail -1 || true)
  if [[ -z "$INPUT_FILE" ]]; then
    log "Queue is empty — nothing to adapt. Exiting cleanly."
    exit 0
  fi
  MODULE="$(basename "$INPUT_FILE" .txt)"
  MODE="queue"
fi
export INPUT_FILE MODULE
CACHE_DIR="$AGENT_DIR/cache/$MODULE"

SAFE_MODULE="$(printf '%s' "$MODULE" | tr -c 'a-zA-Z0-9_-' '-' | sed 's/-\{2,\}/-/g; s/-$//')"
SESSION_ID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)-adapt-${SAFE_MODULE}}"
AUDIT_DIR="${AUDIT_DIR:-$AGENT_DIR/audit/$SESSION_ID}"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "test-adaptation-agent | mode=$MODE"
log "module=$MODULE"
log "change note=$INPUT_FILE"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [[ "$MODE" != "resume" ]]; then
cat > "$AUDIT_DIR/00-session-init.md" << EOF
# Session Init

Mode: $MODE
Session ID: $SESSION_ID
Module: $MODULE
Change Note: $INPUT_FILE
Started: $(date +%Y-%m-%dT%H:%M:%S)

## Env Snapshot (keys only)
$(env | grep -E '^(GITHUB_|SLACK_|MAX_|AUTO_|ADAPT_|CLAUDE_|WORKSPACE_|PLAYWRIGHT_|TEST_)' | sed 's/=.*/=<set>/' | sort)
EOF
fi

declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

# ── Crash handler ─────────────────────────────────────────────────────────────
# Beyond telling somebody: this agent may have half-applied a multi-file change
# item when it died, and the automation repo is shared. Restoring is not optional.
on_error() {
  local exit_code=$?
  local line_no="${1:-?}"
  log "ERROR: step failed (exit $exit_code, near run.sh:$line_no)"
  echo "Crashed at run.sh:$line_no with exit $exit_code" > "$AUDIT_DIR/.crashed"

  if [[ -f "$AUDIT_DIR/.snapshots.json" ]]; then
    log "Restoring files this run had edited — a crash must not leave the shared checkout dirty"
    python3 "$AGENT_DIR/actions/restore_snapshots.py" || \
      log "WARNING: snapshot restore failed — inspect the automation repo by hand"
  fi

  if [[ -n "${SLACK_BOT_TOKEN:-}" && -n "${SLACK_ALERT_CHANNEL:-${SLACK_NOTIFY_CHANNEL:-}}" ]]; then
    MODULE="$MODULE" SESSION_ID="$SESSION_ID" EXIT_CODE="$exit_code" LINE_NO="$line_no" \
    python3 - <<'PYALERT' || true
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
from shared.slack import send_slack
channel = os.environ.get("SLACK_ALERT_CHANNEL") or os.environ.get("SLACK_NOTIFY_CHANNEL", "")
send_slack(
    os.environ.get("SLACK_BOT_TOKEN", ""), channel,
    ":rotating_light: *QA Adaptation CRASHED* — `{mod}`\n"
    "The run aborted (exit {code}, near run.sh:{line}); no PR was created.\n"
    "_Audit: `{sid}`_".format(
        mod=os.environ.get("MODULE", "unknown"),
        line=os.environ.get("LINE_NO", "?"),
        code=os.environ.get("EXIT_CODE", "?"),
        sid=os.environ.get("SESSION_ID", "unknown"),
    ),
)
PYALERT
    log "Crash reported to Slack"
  fi
  log "Change note left in the queue: $INPUT_FILE"
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

# ── Step 01 — Parse the change note ──────────────────────────────────────────
if [[ "$START_FROM_STEP" -le 1 ]]; then
  if _cache_hit "01-parse-change.json"; then
    _cache_restore "01-parse-change.json"
  else
    run_step "[01/05] Parse Change" "python3 '$AGENT_DIR/actions/01_parse_change.py'" parse_change
    _cache_save "01-parse-change.json"
  fi
fi

# ── Step 02 — Scope: blast radius + frozen intent contracts ──────────────────
if [[ "$START_FROM_STEP" -le 2 ]]; then
  run_step "[02/05] Scope" "python3 '$AGENT_DIR/actions/02_scope.py'" scope
  _cache_save "02-scope.json"
fi

# ── Step 03 — Explore the live product ───────────────────────────────────────
# Two halves sharing one numbered slot, as authoring does. They write their own
# files and then a combined 03-explore.json, which is what the server polls:
# keying the step on one half means an API-only run never completes it.
if [[ "$START_FROM_STEP" -le 3 ]]; then
  if _cache_hit "03-explore.json"; then
    _cache_restore "03-explore.json"
    _cache_hit "03-explore-api.json" && _cache_restore "03-explore-api.json"
    _cache_hit "03-explore-web.json" && _cache_restore "03-explore-web.json"
  else
    run_step "[03/05] Explore" \
      "python3 '$AGENT_DIR/actions/03_explore_api.py' && python3 '$AGENT_DIR/actions/03_explore_web.py' && python3 '$AGENT_DIR/actions/03_combine_explore.py'" explore
    _cache_save "03-explore.json"; _cache_save "03-explore-api.json"; _cache_save "03-explore-web.json"
  fi
fi

if [[ "$EXPLORE_ONLY" == "true" ]]; then
  log "EXPLORE_ONLY=true — stopping after exploration. Flow map: $AUDIT_DIR/03-explore.md"
  echo "skipped" > "$AUDIT_DIR/.fix-passed"
  echo "explore-only" > "$AUDIT_DIR/.skip-reason"
  TOTAL_ELAPSED=$(elapsed_since $SESSION_START)
  log "Done. Total time: $(fmt_duration $TOTAL_ELAPSED)"
  log "Audit: $AUDIT_DIR"
  exit 0
fi

# ── Step 04 — Adapt (with retry loop) ────────────────────────────────────────
MAX_ADAPT_ATTEMPTS="${MAX_ADAPT_ATTEMPTS:-2}"
if [[ "$START_FROM_STEP" -le 4 ]]; then
  ADAPT_ATTEMPT=1
  while true; do
    export STEP_ATTEMPT="$ADAPT_ATTEMPT"
    run_step "[04/05] Adapt (attempt $ADAPT_ATTEMPT/$MAX_ADAPT_ATTEMPTS)" \
      "ADAPT_ATTEMPT=$ADAPT_ATTEMPT python3 '$AGENT_DIR/actions/04_adapt.py'" adapt
    ADAPT_RESULT="skipped"
    [[ -f "$AUDIT_DIR/.fix-passed" ]] && ADAPT_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed")
    if [[ "$ADAPT_RESULT" == "true" || "$ADAPT_RESULT" == "skipped" ]]; then break; fi
    if [[ "$ADAPT_ATTEMPT" -ge "$MAX_ADAPT_ATTEMPTS" ]]; then
      log "Still failing after $ADAPT_ATTEMPT attempt(s) — shipping for review"
      break
    fi
    ADAPT_ATTEMPT=$((ADAPT_ATTEMPT + 1))
  done
fi

# ── Step 05 — Ship ───────────────────────────────────────────────────────────
run_step "[05/05] Ship" "python3 '$AGENT_DIR/actions/05_ship.py'" ship

# ── Consume the change note ──────────────────────────────────────────────────
# `cmd < missing 2>/dev/null` does not hide the error: the shell sets up the
# redirection before the command runs and reports the failure itself.
SKIP_REASON=""
[[ -f "$AUDIT_DIR/.skip-reason" ]] && SKIP_REASON=$(tr -d '\n' < "$AUDIT_DIR/.skip-reason")
if [[ "$SKIP_REASON" == "infra" ]]; then
  log "Infra skip — leaving the change note queued for retry: $INPUT_FILE"
elif [[ -f "$INPUT_FILE" && "$(dirname "$INPUT_FILE")" != "$PROCESSED_DIR" ]]; then
  mv "$INPUT_FILE" "$PROCESSED_DIR/$(basename "$INPUT_FILE")"
  log "Change note moved to processed/"
fi

TOTAL_ELAPSED=$(elapsed_since $SESSION_START)
echo ""
log "Done. Total time: $(fmt_duration $TOTAL_ELAPSED)"
for i in "${!STEP_NAMES[@]}"; do
  printf "  %-50s %s\n" "${STEP_NAMES[$i]}" "$(fmt_duration ${STEP_DURATIONS[$i]})"
done

# Roll up now so the spend is on screen with the timings rather than only in
# metrics.json. The EXIT trap re-runs this; a rollup is idempotent.
_METRICS=$(cd "${REPO_ROOT:-.}" && python3 -m shared.metrics 2>/dev/null || true)
[[ -n "$_METRICS" ]] && log "Spend: $_METRICS"

log "Audit: $AUDIT_DIR"
