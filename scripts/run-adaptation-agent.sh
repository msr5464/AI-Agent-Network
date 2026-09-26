#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-adaptation-agent.sh
# Run the test-adaptation-agent with live logs: a change note in
# agents/test-adaptation-agent/queue/<module>.txt → updated tests → PR.
#
# Usage:
#   ./scripts/run-adaptation-agent.sh                                   # queue mode: oldest .txt
#   ./scripts/run-adaptation-agent.sh checkout                          # queue/checkout.txt
#   EXPLORE_ONLY=true ./scripts/run-adaptation-agent.sh checkout        # flow map only
#   ADAPTATION_APPLY=true ./scripts/run-adaptation-agent.sh checkout    # apply + verify (default: propose)
#   START_FROM_STEP=4 SESSION_ID=<sid> ./scripts/run-adaptation-agent.sh   # resume
# ─────────────────────────────────────────────────────────────────────────────
exec "$(dirname "${BASH_SOURCE[0]}")/_run-agent.sh" test-adaptation-agent "$@"
