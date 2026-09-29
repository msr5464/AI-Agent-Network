#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-authoring-agent.sh
# Run the test-authoring-agent with live logs: plain English steps in
# agents/test-authoring-agent/queue/<module>.txt → Java tests → PR.
#
# Usage:
#   ./scripts/run-authoring-agent.sh                             # queue mode: oldest .txt
#   ./scripts/run-authoring-agent.sh payments                    # queue/payments.txt
#   AUTO_PUSH=false ./scripts/run-authoring-agent.sh payments    # dry-run (no PR)
#   START_FROM_STEP=4 SESSION_ID=<sid> ./scripts/run-authoring-agent.sh   # resume
# ─────────────────────────────────────────────────────────────────────────────
exec "$(dirname "${BASH_SOURCE[0]}")/_run-agent.sh" test-authoring-agent "$@"
