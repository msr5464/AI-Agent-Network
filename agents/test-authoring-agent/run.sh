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
# up to AUTHORING_FIX_RETRY_COUNT (default: 2).
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MODULE="${1:-${MODULE:-}}"

# START_FROM_STEP > 1 resumes an EXISTING session (identified by SESSION_ID)
# from a specific step instead of starting a fresh 01→05 run — see the
# "Resume mode" block below for what that requires.
START_FROM_STEP="${START_FROM_STEP:-1}"
if ! [[ "$START_FROM_STEP" =~ ^[1-5]$ ]]; then
  echo "ERROR: START_FROM_STEP must be an integer 1-5, got: $START_FROM_STEP" >&2
  exit 1
fi

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

if [[ "$START_FROM_STEP" -gt 1 ]]; then
  # ── Resume mode — continue an existing session from a specific step ────────
  # SESSION_ID must name a session that already has valid output for every
  # step before START_FROM_STEP (checked below). The queue .txt file this
  # session originally parsed may already have been moved to processed/ —
  # that's fine, step 01's output is already on disk and won't be re-read.
  if [[ -z "${SESSION_ID:-}" ]]; then
    log "ERROR: START_FROM_STEP=$START_FROM_STEP requires SESSION_ID to name the session being resumed"
    exit 1
  fi
  AUDIT_DIR="${AUDIT_DIR:-$AGENT_DIR/audit/$SESSION_ID}"
  if [[ ! -d "$AUDIT_DIR" ]]; then
    log "ERROR: cannot resume — no existing session at $AUDIT_DIR"
    exit 1
  fi

  if [[ -z "$MODULE" ]]; then
    MODULE=$(grep -m1 '^Module: ' "$AUDIT_DIR/00-session-init.md" 2>/dev/null | sed 's/^Module: //')
    if [[ -z "$MODULE" ]]; then
      log "ERROR: could not recover MODULE from $AUDIT_DIR/00-session-init.md — pass MODULE explicitly"
      exit 1
    fi
  fi

  # The step immediately before START_FROM_STEP must have produced valid
  # output, or there is nothing to resume from. (Plain case/if, not an
  # associative array — macOS ships bash 3.2 as /bin/bash, which predates
  # `declare -A`; the rest of this script only ever uses `declare -a`.)
  case "$START_FROM_STEP" in
    2) _prereqs="01-parse.json" ;;
    3) _prereqs="01-parse.json 02-validate-api.json 02-validate-web.json" ;;
    4) _prereqs="01-parse.json 03-generate.json" ;;
    5) _prereqs="03-generate.json 04-run-and-fix.json" ;;
    *) _prereqs="" ;;
  esac
  for _f in $_prereqs; do
    if [[ ! -f "$AUDIT_DIR/$_f" ]]; then
      log "ERROR: cannot resume from step $START_FROM_STEP — missing $AUDIT_DIR/$_f"
      log "  (that step never completed in this session; resume from an earlier step instead)"
      exit 1
    fi
  done

  # Best-effort — only used for the final "mark processed" step below; the
  # session resumes fine even if this file can't be found in either place.
  INPUT_FILE="$QUEUE_DIR/${MODULE}.txt"
  [[ -f "$INPUT_FILE" ]] || INPUT_FILE="$PROCESSED_DIR/${MODULE}.txt"

  MODE="resume"

  # Clear stale output for START_FROM_STEP and every step after it, so this
  # rerun's fresh files never mix with a previous (failed) attempt's — 05_ship.py
  # in particular reads every 04-run-and-fix-attempt-*.json file present, and a
  # leftover one from a prior attempt would get treated as real, current data.
  log "Resuming session $SESSION_ID from step $START_FROM_STEP — clearing stale output for steps $START_FROM_STEP-05"
  for _n in 02 03 04 05; do
    if (( 10#$_n >= START_FROM_STEP )); then
      rm -f "$AUDIT_DIR"/"${_n}"-*
    fi
  done
  # .fix-history.json included: a resumed step 04 must not inherit the attempts of
  # the run it replaces, or its first attempt is told not to repeat work that no
  # longer exists on disk — and can be stopped early for "bringing nothing new".
  rm -f "$AUDIT_DIR/.fix-passed" "$AUDIT_DIR/.verdict" "$AUDIT_DIR/.cancelled" \
        "$AUDIT_DIR/.fix-history.json"

elif [[ -n "$MODULE" ]]; then
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
# knows where the agent will write audit files before it starts, and by resume
# mode above to continue an existing session). Falls back to a fresh one only
# for a brand-new CLI / Makefile invocation.
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
[[ "$MODE" == "resume" ]] && log "resuming from step $START_FROM_STEP"
# Show what's actually being automated right up front, before step 01 even
# starts — credentials masked. This is generic pattern-based masking only
# (matches "password:"/"token:"/etc. style lines) — the stronger,
# value-based pass (using 01_parse.py's own extracted demo_credentials)
# isn't possible yet here, since Parse hasn't run — that fuller redaction is
# what the PR description shows once it's available. Best-effort: silently
# skipped if the input file can't be found (e.g. a resumed session whose
# original file already moved to processed/).
if [[ -f "$INPUT_FILE" ]]; then
  echo ""
  echo "── Test Case (credentials masked) ──"
  python3 -c "
import os, sys
sys.path.insert(0, os.environ['REPO_ROOT'])
from shared.credential_masking import mask_credential_lines
print(mask_credential_lines(open(os.environ['INPUT_FILE']).read()).rstrip())
"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Write session init markdown — resuming preserves the ORIGINAL session's
# record instead of overwriting it; append a short resume marker instead.
if [[ "$MODE" == "resume" ]]; then
  cat >> "$AUDIT_DIR/00-session-init.md" << EOF

## Resumed
Started from step $START_FROM_STEP at $(date +%Y-%m-%dT%H:%M:%S)
EOF
else
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
fi

declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

# ── Prerequisite — sync GITHUB_DEFAULT_BRANCH before any step runs ───────────────
WORKSPACE_DIR="${WORKSPACE_DIR:-}"
# Normalised and exported so the Python steps resolve the same checkout
# this block syncs — `set -u` would abort on the bare reference otherwise.
export FRAMEWORK_DIR="${FRAMEWORK_DIR:-}"
GITHUB_REPO_AUTOMATION="${GITHUB_REPO_AUTOMATION:-}"
# Deliberately NOT defaulted to main: an explicitly empty value means "branch
# from current HEAD", which actions/05_ship.py has always honoured. Defaulting
# it here made the two halves of one run disagree about the same setting.
export GITHUB_DEFAULT_BRANCH="${GITHUB_DEFAULT_BRANCH-main}"

# Same order as shared/workspace.py: FRAMEWORK_DIR names the checkout outright,
# otherwise it is WORKSPACE_DIR/GITHUB_REPO_AUTOMATION. GITHUB_REPO_AUTOMATION
# is still required either way — it is the repo name on GitHub, and the clone
# and push URLs below are built from it.
AUTOMATION_FRAMEWORK_DIR="${FRAMEWORK_DIR:-${WORKSPACE_DIR}/${GITHUB_REPO_AUTOMATION}}"

if [[ -z "$GITHUB_REPO_AUTOMATION" ]]; then
  log "ERROR: GITHUB_REPO_AUTOMATION is not set — cannot reach the automation repo"
  exit 1
fi
if [[ -z "$FRAMEWORK_DIR" && -z "$WORKSPACE_DIR" ]]; then
  log "ERROR: set FRAMEWORK_DIR, or WORKSPACE_DIR — cannot locate the automation repo"
  exit 1
fi
export GIT_TERMINAL_PROMPT=0

# Clone if absent, make origin/$GITHUB_DEFAULT_BRANCH current, and land the
# checkout on it — all four steps in shared/workspace.py, which is where the
# other two agents already get them. The bash this replaces had three faults
# that only a non-default base makes visible:
#
#   * `git clone "$_PUSH_URL"` persists the token into .git/config for the life
#     of the checkout. workspace.clone() strips it back out afterwards.
#   * `git fetch <url> <branch>` creates neither a local branch nor a
#     remote-tracking ref, so the retry it fed was guaranteed to fail the same
#     way and the run dead-ended at `exit 1` for any branch not already checked
#     out here. An explicit destination refspec is what actually fixes it.
#   * `pull` needs a merge and can conflict; `checkout -f -B` cannot.
#
# An explicitly empty GITHUB_DEFAULT_BRANCH means "branch from current HEAD",
# which the CLI honours — matching actions/05_ship.py, which has always read a
# blank value that way while this block was quietly defaulting it to main.
log "Prerequisite: preparing $AUTOMATION_FRAMEWORK_DIR on ${GITHUB_DEFAULT_BRANCH:-<current HEAD>} ..."
if ! (cd "$REPO_ROOT" && python3 -m shared.workspace prepare-base --checkout 2>&1); then
  log "ERROR: could not prepare ${GITHUB_DEFAULT_BRANCH:-the checkout} in $AUTOMATION_FRAMEWORK_DIR — aborting"
  exit 1
fi
log "Prerequisite: ${GITHUB_DEFAULT_BRANCH:-current HEAD} is ready"

# ── Step 01 — Parse ────────────────────────────────────────────────────────────
if [[ "$START_FROM_STEP" -gt 1 ]]; then
  log "✓ [01/05] Parse — reused from resumed session"
  STEP_NAMES+=("[01/05] Parse")
  STEP_DURATIONS+=(0)
  record_stage "parse" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
elif _cache_hit "01-parse.json"; then
  _cache_restore "01-parse.json"
  [[ -f "$CACHE_DIR/01-parse.md" ]] && cp "$CACHE_DIR/01-parse.md" "$AUDIT_DIR/01-parse.md"
  log "✓ [01/05] Parse — skipped (TESTING_MODE cache hit)"
  STEP_NAMES+=("[01/05] Parse")
  STEP_DURATIONS+=(0)
  record_stage "parse" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
else
  run_step "[01/05] Parse" "python3 '$AGENT_DIR/actions/01_parse.py'" parse
  _cache_save "01-parse.json"
  _cache_save "01-parse.md"
fi

# ── Step 02 — Validate API + Validate Web (each self/env-gated by test_type) ──
TEST_TYPE=$(python3 -c "
import json, os
from pathlib import Path
d = json.loads(Path(os.environ['AUDIT_DIR']).joinpath('01-parse.json').read_text())
print(d.get('test_type', 'api'))
")

if [[ "$START_FROM_STEP" -gt 2 ]]; then
  log "✓ [02/05] Validate API — reused from resumed session"
  STEP_NAMES+=("[02/05] Validate API")
  STEP_DURATIONS+=(0)
  record_stage "validate_api" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
  log "✓ [02/05] Validate Web — reused from resumed session"
  STEP_NAMES+=("[02/05] Validate Web")
  STEP_DURATIONS+=(0)
  record_stage "validate_web" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
else

# -- Validate API — 02_validate_api.py self-skips (writes a "skipped" stub) when
# test_type isn't api/both, so it's always safe to invoke unconditionally. --
if _cache_hit "02-validate-api.json"; then
  _cache_restore "02-validate-api.json"
  [[ -f "$CACHE_DIR/02-validate-api.md" ]] && cp "$CACHE_DIR/02-validate-api.md" "$AUDIT_DIR/02-validate-api.md"
  log "✓ [02/05] Validate API — skipped (TESTING_MODE cache hit)"
  STEP_NAMES+=("[02/05] Validate API")
  STEP_DURATIONS+=(0)
  record_stage "validate_api" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
else
  run_step "[02/05] Validate API" "python3 '$AGENT_DIR/actions/02_validate_api.py'" validate_web
  # Only cache if it actually ran a real validation (not skipped as non-API/no-endpoints)
  if python3 -c "
import json, os, sys
from pathlib import Path
d = json.loads(Path(os.environ['AUDIT_DIR']).joinpath('02-validate-api.json').read_text())
sys.exit(0 if not d.get('skipped', True) else 1)
" 2>/dev/null; then
    _cache_save "02-validate-api.json"
    _cache_save "02-validate-api.md"
  fi
fi

if [[ "$TEST_TYPE" == "web" || "$TEST_TYPE" == "both" ]]; then
  if _cache_hit "02-validate-web.json"; then
    _cache_restore "02-validate-web.json"
    [[ -f "$CACHE_DIR/02-validate-web.md" ]] && cp "$CACHE_DIR/02-validate-web.md" "$AUDIT_DIR/02-validate-web.md"
    log "✓ [02/05] Validate Web — skipped (TESTING_MODE cache hit)"
    STEP_NAMES+=("[02/05] Validate Web")
    STEP_DURATIONS+=(0)
    record_stage "validate_web" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
  else
    run_step "[02/05] Validate Web" "python3 '$AGENT_DIR/actions/02_validate_web.py'" validate_web
    # Only cache if Claude actually returned data (selectors or step results present)
    if python3 -c "
import json, os, sys
from pathlib import Path
d = json.loads(Path(os.environ['AUDIT_DIR']).joinpath('02-validate-web.json').read_text())
sys.exit(0 if (d.get('selectors') or d.get('steps_passed') or d.get('steps_failed')
              or d.get('steps_unverified')) else 1)
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
              'steps_passed': [], 'steps_failed': [], 'steps_unverified': []})
)
"
fi

fi  # end START_FROM_STEP -gt 2 else-branch (Validate API + Validate Web)

# ── Step 03 — Generate ────────────────────────────────────────────────────────
if [[ "$START_FROM_STEP" -gt 3 ]]; then
  log "✓ [03/05] Generate — reused from resumed session"
  STEP_NAMES+=("[03/05] Generate")
  STEP_DURATIONS+=(0)
  record_stage "generate" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
else
  run_step "[03/05] Generate" "python3 '$AGENT_DIR/actions/03_generate.py'" generate
fi

# ── Step 04 — Run & Fix (with retry loop) ─────────────────────────────────────
# Per-agent, deliberately: this used to read MAX_FIX_ATTEMPTS, which test-healing-agent
# read too. Healing earns a bigger budget — each of its attempts fixes one locator and
# uncovers the next, so the loop walks a chain. This one re-attacks the same failure, so
# it wants a smaller number. One shared knob meant setting healing's budget silently set
# this one as well.
AUTHORING_FIX_RETRY_COUNT="${AUTHORING_FIX_RETRY_COUNT:-2}"
if [[ -n "${MAX_FIX_ATTEMPTS:-}" ]]; then
  log "NOTE: MAX_FIX_ATTEMPTS is set but no longer read — use AUTHORING_FIX_RETRY_COUNT (currently $AUTHORING_FIX_RETRY_COUNT)"
fi

if [[ "$START_FROM_STEP" -gt 4 ]]; then
  log "✓ [04/05] Run & Fix — reused from resumed session"
  STEP_NAMES+=("[04/05] Run & Fix")
  STEP_DURATIONS+=(0)
  record_stage "run_and_fix" "${STEP_NAMES[$((${#STEP_NAMES[@]}-1))]}" "${#STEP_NAMES[@]}" 0 0 0 true
else
  # Initial test run — not counted as a fix attempt
  run_step "[04/05] Run & Fix (initial)" \
    "FIX_ATTEMPT=0 python3 '$AGENT_DIR/actions/04_run_and_fix.py'" run_and_fix

  FIX_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed" 2>/dev/null || echo "skipped")

  if [[ "$FIX_RESULT" != "true" && "$FIX_RESULT" != "skipped" && "$FIX_RESULT" != "stuck" ]]; then
    FIX_ATTEMPT=1
    while true; do
      export STEP_ATTEMPT="$FIX_ATTEMPT"
      run_step "[04/05] Run & Fix (attempt $FIX_ATTEMPT/$AUTHORING_FIX_RETRY_COUNT)" \
        "FIX_ATTEMPT=$FIX_ATTEMPT python3 '$AGENT_DIR/actions/04_run_and_fix.py'" run_and_fix

      FIX_RESULT=$(tr -d '\n' < "$AUDIT_DIR/.fix-passed" 2>/dev/null || echo "skipped")

      # "stuck" (not just "skipped") also stops the loop early — 04_run_and_fix.py
      # sets it when a fix attempt had no effect on the failure's exact location,
      # meaning further attempts are unlikely to converge either.
      if [[ "$FIX_RESULT" == "true" || "$FIX_RESULT" == "skipped" || "$FIX_RESULT" == "stuck" ]]; then
        break
      fi

      if [[ "$FIX_ATTEMPT" -ge "$AUTHORING_FIX_RETRY_COUNT" ]]; then
        log "Tests still failing after $FIX_ATTEMPT fix attempt(s) — proceeding to ship"
        break
      fi

      log "Tests failed — retrying (fix attempt $((FIX_ATTEMPT + 1)))"
      FIX_ATTEMPT=$((FIX_ATTEMPT + 1))
    done
  fi
fi

# ── Step 05 — Ship ────────────────────────────────────────────────────────────
run_step "[05/05] Ship" "python3 '$AGENT_DIR/actions/05_ship.py'" ship

# ── Mark input as processed ───────────────────────────────────────────────────
# Guarded (not unconditional) because a resumed run's input file may already
# have been moved to processed/ by the original run that this one continues.
if [[ -f "$INPUT_FILE" && "$(dirname "$INPUT_FILE")" != "$PROCESSED_DIR" ]]; then
  mv "$INPUT_FILE" "$PROCESSED_DIR/$(basename "$INPUT_FILE")"
  log "Moved to processed: $PROCESSED_DIR/$(basename "$INPUT_FILE")"
else
  log "Input file already processed or not found — nothing to move"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(elapsed_since $SESSION_START)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Done. Total time: $(fmt_duration $TOTAL_ELAPSED)"
echo ""
for i in "${!STEP_NAMES[@]}"; do
  printf "  %-55s %s\n" "${STEP_NAMES[$i]}" "$(fmt_duration ${STEP_DURATIONS[$i]})"
done

# Roll up now so the spend is on screen with the timings rather than only in
# metrics.json. The EXIT trap re-runs this; a rollup is idempotent.
_METRICS=$(cd "${REPO_ROOT:-.}" && python3 -m shared.metrics 2>/dev/null || true)
[[ -n "$_METRICS" ]] && log "Spend: $_METRICS"

echo ""
log "Audit: $AUDIT_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
