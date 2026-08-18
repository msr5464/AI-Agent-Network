#!/bin/bash
# shared/load_env.sh — shared .env loader for all agent run.sh files
#
# Usage (source, do not execute):
#   source "$REPO_ROOT/shared/load_env.sh"
#
# Requires: $REPO_ROOT and $AGENT_DIR to be set before sourcing.
# Loads: config/.env, .env (repo root), then <agent>/.env in order.
# Agent-level .env overrides repo-root settings.
#
# A variable the CALLER already exported before this script ran always wins
# over any .env file — .env files are DEFAULTS, not overrides, for anything
# the caller explicitly set for this specific invocation. Without this, e.g.
# qa_agents_server setting AUTO_PUSH=true for one request (from a UI
# checkbox) would get silently clobbered back to config/.env's own
# AUTO_PUSH=false the moment this script sources it — a real bug this fixes,
# not a hypothetical one.

if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi

# Snapshot every var the caller already exported (name AND value) to a temp
# file, re-sourced after loading the .env files to restore anything they
# clobbered. A temp file, not a bash associative array, because macOS ships
# bash 3.2 as /bin/bash, which predates `declare -A`.
_env_snapshot="$(mktemp)"
for _name in $(compgen -e); do
  printf 'export %q=%q\n' "$_name" "${!_name}" >> "$_env_snapshot"
done

for _envfile in "$REPO_ROOT/config/.env" "$REPO_ROOT/.env" "$AGENT_DIR/.env"; do
  if [[ -f "$_envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$_envfile"
    set +a
  fi
done

# shellcheck disable=SC1090
source "$_env_snapshot"
rm -f "$_env_snapshot"
unset _envfile _name _env_snapshot
