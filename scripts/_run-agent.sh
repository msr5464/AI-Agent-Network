#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/_run-agent.sh
# Shared tail of scripts/run-<name>-agent.sh — call those, not this.
#
#   _run-agent.sh <agent> [args for agents/<agent>/run.sh...]
#
# Streams the agent's console live, in colour, and once the run ends saves it as
# <session>/stdout.log — the file a Studio run writes — so History and
# `make dashboard` replay CLI runs too.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

AGENT="$1"; shift
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Through tee the agent no longer writes to a terminal: keep Python unbuffered so
# lines arrive as they happen rather than in 4 KB bursts, and keep the colour.
export PYTHONUNBUFFERED=1
[[ -t 1 && -z "${NO_COLOR:-}" ]] && export QA_LOG_COLOR="${QA_LOG_COLOR:-always}"

CONSOLE="$(mktemp -t "qa-$AGENT.XXXXXX")"
export QA_AUDIT_DIR_OUT="$(mktemp -t "qa-$AGENT-dir.XXXXXX")"
trap 'rm -f "$CONSOLE" "$QA_AUDIT_DIR_OUT"' EXIT
# Ctrl-C still stops the agent (a handler, unlike an ignore, is not inherited),
# while this script and tee outlive it, so a cancelled run keeps its console too.
trap ':' INT

bash "agents/$AGENT/run.sh" "$@" 2>&1 | (trap '' INT; exec tee "$CONSOLE")
rc=${PIPESTATUS[0]}

SESSION_DIR="$(cat "$QA_AUDIT_DIR_OUT" 2>/dev/null || true)"
if [[ -n "$SESSION_DIR" && -d "$SESSION_DIR" ]]; then
  sed $'s/\033\\[[0-9;]*m//g' "$CONSOLE" > "$SESSION_DIR/stdout.log"
  echo "Console: ${SESSION_DIR#"$PROJECT_ROOT"/}/stdout.log"
fi
exit "$rc"
