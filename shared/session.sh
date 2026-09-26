#!/bin/bash
# shared/session.sh — shared session timing and step runner for all agent run.sh files
#
# Usage (source, do not execute):
#   source "$REPO_ROOT/shared/session.sh"
#
# Provides: log(), elapsed_since(), fmt_duration(), run_step(), flush_step_done(),
#           print_step_table()
# Sets:     SESSION_START (epoch seconds at time of sourcing)
#
# Callers track per-step timing with:
#   declare -a STEP_NAMES=()
#   declare -a STEP_DURATIONS=()
# Then print the table at the end:
#   print_step_table 50        # 50 is the label width used by the fallback

SESSION_START=$(date +%s)

# Defense-in-depth redaction — mirrors shared/log.py's Python-side redaction.
# The real fix for the one known leak (a token embedded in a git remote URL,
# echoed by git's own error output) is in run.sh/shared/git.py, which now
# never embeds a token in a URL at all; this is a backstop in case some other
# command's raw output ever contains a secret literal.
_redact_secrets() {
  local msg="$1"
  if [[ -n "${GITHUB_TOKEN:-}" && ${#GITHUB_TOKEN} -ge 8 ]]; then
    msg="${msg//$GITHUB_TOKEN/***REDACTED(GITHUB_TOKEN)***}"
  fi
  if [[ -n "${SLACK_BOT_TOKEN:-}" && ${#SLACK_BOT_TOKEN} -ge 8 ]]; then
    msg="${msg//$SLACK_BOT_TOKEN/***REDACTED(SLACK_BOT_TOKEN)***}"
  fi
  printf '%s' "$msg"
}

# ── Severity colouring ────────────────────────────────────────────────────────
# The bash half of shared/log.py's severity vocabulary — same prefixes, same
# colours, same terminal-only gate — so an ERROR line from run.sh and one from a
# Python action look identical. Under qa_agents_server stdout is a pipe, so
# nothing is emitted and the Studio console keeps colouring by prefix.
_log_color_enabled() {
  case "$(printf '%s' "${QA_LOG_COLOR:-auto}" | tr '[:upper:]' '[:lower:]')" in
    always|force|1|true|yes) return 0 ;;
    never|off|0|false|no)    return 1 ;;
  esac
  [[ -z "${NO_COLOR:-}" ]] && [[ -t 1 ]]
}

# _severity_color <message> → the ANSI code for its leading marker, or empty.
_severity_color() {
  local head="${1%%$'\n'*}"
  case "$head" in
    ERROR*|FATAL*|FAILED*|BLOCKED:*)  printf '\033[1;31m' ;;   # bold red
    WARNING*|Warning*|WARN*|CAUTION*) printf '\033[0;33m' ;;   # yellow
  esac
}

log() {
  # A "✓ <step>" marker closes a step, so it also closes any earlier step still
  # holding its own ✓ back (see flush_step_done). This keeps the skip/reuse
  # markers a run.sh logs directly in order with the ones run_step defers.
  [[ "$*" == "✓ "* ]] && flush_step_done
  local msg
  msg="$(_redact_secrets "$*")"
  local color=""
  _log_color_enabled && color="$(_severity_color "$msg")"
  if [[ -n "$color" ]]; then
    printf '%b[%s] %s%b\n' "$color" "$(date +%H:%M:%S)" "$msg" '\033[0m'
  else
    echo "[$(date +%H:%M:%S)] $msg"
  fi
}

elapsed_since() {
  local start=$1
  local now
  now=$(date +%s)
  echo $(( now - start ))
}

# The "✓ <step>" line is held back until the step's block is really over. Each
# run.sh logs an epilogue after run_step returns — the handoff it wrote, the
# change note it consumed, the retry it decided on — and those lines belong to
# the step that produced them, so the ✓ that closes the step has to come after
# them. Flushed by the next run_step, by each run.sh before its final summary,
# and by the EXIT trap for the paths that exit early.
_STEP_DONE_LINE=""

flush_step_done() {
  [[ -z "$_STEP_DONE_LINE" ]] && return 0
  local line="$_STEP_DONE_LINE"
  _STEP_DONE_LINE=""
  log "$line"
}

fmt_duration() {
  local secs=$1
  if (( secs >= 60 )); then
    printf "%dm %ds" $(( secs / 60 )) $(( secs % 60 ))
  else
    printf "%ds" "$secs"
  fi
}

# The end-of-run table: every step, its duration, and what it spent.
#
# Built from the metrics streams, so the spend is per ATTEMPT — a retry loop that
# walks a chain of broken locators is the case where "which attempt cost the
# money" is the only interesting question, and a rollup folds that away. Falls
# back to the names and durations this shell collected if the streams cannot be
# read, so the table never disappears just because metrics did.
print_step_table() {
  local width="${1:-50}" table=""
  table=$(cd "${REPO_ROOT:-.}" && python3 -m shared.metrics --table 2>/dev/null || true)
  if [[ -n "$table" ]]; then
    echo "$table"
    return 0
  fi
  local i
  for i in "${!STEP_NAMES[@]}"; do
    printf "  %-${width}s %s\n" "${STEP_NAMES[$i]}" "$(fmt_duration ${STEP_DURATIONS[$i]})"
  done
}

# Append one stage record to $AUDIT_DIR/metrics/stages.jsonl.
# Also used directly by branches that skip a step (they push a 0 duration today).
record_stage() {
  local key="$1" label="$2" index="$3" start="$4" end="$5" rc="$6" skipped="${7:-false}"
  [[ -z "${AUDIT_DIR:-}" ]] && return 0
  mkdir -p "$AUDIT_DIR/metrics" 2>/dev/null || return 0
  python3 - "$AUDIT_DIR/metrics/stages.jsonl" "$key" "$label" "$index" \
             "$start" "$end" "$rc" "$skipped" "${STEP_ATTEMPT:-1}" <<'PYSTAGE' 2>/dev/null || true
import json, sys
path, key, label, index, start, end, rc, skipped, attempt = sys.argv[1:10]
def num(v, cast=float, default=0):
    try: return cast(v)
    except (TypeError, ValueError): return default
rec = {"index": num(index, int), "key": key, "label": label,
       "attempt": num(attempt, int, 1),
       "started_at": num(start), "ended_at": num(end),
       "duration_s": max(0.0, num(end) - num(start)),
       "exit_code": num(rc, int), "skipped": skipped == "true"}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\n")
PYSTAGE
}

# run_step "<label>" "<cmd>" ["<step_key>"]
#
# step_key joins this stage to the server's step keys in qa_agents_server/agents.py.
# It is exported (along with the label and attempt) so the Python action tags the
# LLM calls it makes with the stage they belong to.
run_step() {
  local label="$1"
  local cmd="$2"
  local step_key="${3:-}"
  local step_start
  step_start=$(date +%s)
  # Close the previous step first: its ✓ waited for that step's epilogue.
  flush_step_done
  # A blank line before each step, so the log reads as one block per step.
  echo
  log "▶ $label"

  # Exported so the Python action tags the LLM calls it makes with this stage.
  # A plain prefix assignment would not reliably reach the action's subprocess,
  # since eval is a shell builtin.
  export STEP_KEY="$step_key" STEP_LABEL="$label"
  export STEP_ATTEMPT="${STEP_ATTEMPT:-1}"

  # Deliberately not capturing $? here: under `set -e` a failing step aborts at
  # this line, exactly as before this function recorded anything. The ERR trap in
  # each run.sh rolls up what was spent before the crash.
  eval "$cmd"

  local dur
  dur=$(elapsed_since $step_start)
  STEP_NAMES+=("$label")
  STEP_DURATIONS+=("$dur")
  # Record before clearing — record_stage reads STEP_ATTEMPT.
  record_stage "$step_key" "$label" "${#STEP_NAMES[@]}" \
               "$step_start" "$(date +%s)" 0 false
  # Spend goes on this line, as the stage ends and in stream order. The GUI used
  # to append its own "✓ <stage> — <time> · <cost>" when the step event's metrics
  # arrived, which landed after later stages had already logged output and read
  # as the run looping back to a stage that had long finished.
  #
  # The label goes with the key so a retried stage reports THIS attempt's spend
  # rather than every attempt's so far.
  local spend=""
  [[ -n "$step_key" ]] && spend=$(cd "${REPO_ROOT:-.}" &&
    python3 -m shared.metrics --stage "$step_key" "$label" 2>/dev/null || true)
  unset STEP_KEY STEP_LABEL STEP_ATTEMPT
  _STEP_DONE_LINE="✓ $label — $(fmt_duration $dur)${spend:+ · $spend}"
}

# ── Metrics rollup ────────────────────────────────────────────────────────────
# Reads metrics/*.jsonl and writes metrics.json. Registered as an EXIT trap so it
# runs on every path out of the script: normal completion, an ERR trap that calls
# exit, and an early `exit 0` such as healing's "nothing to fix". A run that
# crashed still spent money, and its rollup is how that spend gets reported.
finalize_metrics() {
  local rc=$?
  flush_step_done
  [[ -z "${AUDIT_DIR:-}" ]] && return $rc
  # scripts/_run-agent.sh saves the console into the session after the run, and
  # this is where the session finally landed — triaging renames it mid-run.
  [[ -n "${QA_AUDIT_DIR_OUT:-}" ]] && printf '%s\n' "$AUDIT_DIR" > "$QA_AUDIT_DIR_OUT"
  METRICS_SUMMARY=$(cd "${REPO_ROOT:-.}" && python3 -m shared.metrics 2>/dev/null || true)

  # Durable analytics row. Written here rather than only server-side because a
  # plain `make run` never touches the server's run registry at all — and
  # written from the EXIT trap rather than the success path so a crashed or
  # early-exiting run still reports what it spent. Status is derived from the
  # real exit code; the adaptation agent's own ladder overrides it downstream.
  local run_status="completed"
  if [[ -f "$AUDIT_DIR/.cancelled" ]]; then
    run_status="cancelled"
  elif [[ -f "$AUDIT_DIR/.crashed" || $rc -ne 0 ]]; then
    run_status="failed"
  fi
  RUN_STATUS="$run_status" AUDIT_DIR="$AUDIT_DIR" \
    python3 -m qa_agents_server.analytics "$AUDIT_DIR" >/dev/null 2>&1 || true
  return $rc
}
trap finalize_metrics EXIT
