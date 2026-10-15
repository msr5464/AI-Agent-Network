#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-triaging-agent.sh
# Run the test-triaging-agent with live logs.
#
# Usage:
#   ./scripts/run-triaging-agent.sh [BUILD_TAG] [TRIAGING_INPUT_DIR] [TRIAGING_OUTPUT_DIR]
#
#   ./scripts/run-triaging-agent.sh                                 # scout: most recent unanalysed build
#   ./scripts/run-triaging-agent.sh ProdSanity-All-Tests-541        # a specific build tag
#   ./scripts/run-triaging-agent.sh ProdSanity-541 testdata reports # with custom dirs
#   STOP_AFTER=classify ./scripts/run-triaging-agent.sh ProdSanity-541
# ─────────────────────────────────────────────────────────────────────────────
[[ -n "${2:-}" ]] && export TRIAGING_INPUT_DIR="$2"
[[ -n "${3:-}" ]] && export TRIAGING_OUTPUT_DIR="$3"
exec "$(dirname "${BASH_SOURCE[0]}")/_run-agent.sh" test-triaging-agent "${1:-}"
