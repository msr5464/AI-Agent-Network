#!/bin/bash
# shared/session.sh — shared session timing and step runner for all agent run.sh files
#
# Usage (source, do not execute):
#   source "$REPO_ROOT/shared/session.sh"
#
# Provides: log(), elapsed_since(), fmt_duration(), run_step()
# Sets:     SESSION_START (epoch seconds at time of sourcing)
#
# Callers track per-step timing with:
#   declare -a STEP_NAMES=()
#   declare -a STEP_DURATIONS=()
# Then print the table at the end:
#   for i in "${!STEP_NAMES[@]}"; do
#     printf "  %-50s %s\n" "${STEP_NAMES[$i]}" "$(fmt_duration ${STEP_DURATIONS[$i]})"
#   done

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
