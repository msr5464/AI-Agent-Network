#!/bin/bash
# shared/load_env.sh — shared .env loader for all agent run.sh files
#
# Usage (source, do not execute):
#   source "$REPO_ROOT/shared/load_env.sh"
#
# Requires: $REPO_ROOT and $AGENT_DIR to be set before sourcing.
# Loads: config/.env, .env (repo root), then <agent>/.env in order.
# Agent-level .env overrides repo-root settings.

if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi

for _envfile in "$REPO_ROOT/config/.env" "$REPO_ROOT/.env" "$AGENT_DIR/.env"; do
  if [[ -f "$_envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$_envfile"
    set +a
  fi
done
unset _envfile
