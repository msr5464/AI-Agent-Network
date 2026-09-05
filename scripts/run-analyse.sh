#!/usr/bin/env bash
set -euo pipefail
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-analyse.sh
# Run the test-triaging-agent agent.
#
# Usage:
#   ./scripts/run-analyse.sh [BUILD_TAG] [TRIAGING_INPUT_DIR] [TRIAGING_OUTPUT_DIR]
#
# Examples:
#   ./scripts/run-analyse.sh                                     # scout mode, dirs from .env
#   ./scripts/run-analyse.sh ProdSanity-All-Tests-541            # direct mode
#   ./scripts/run-analyse.sh ProdSanity-541 testdata reports     # with custom dirs
#   STOP_AFTER=classify ./scripts/run-analyse.sh ProdSanity-541
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

BUILD_TAG="${1:-${BUILD_TAG:-}}"
TRIAGING_INPUT_DIR="${2:-${TRIAGING_INPUT_DIR:-}}"
TRIAGING_OUTPUT_DIR="${3:-${TRIAGING_OUTPUT_DIR:-}}"

export BUILD_TAG
[[ -n "$TRIAGING_INPUT_DIR" ]]  && export TRIAGING_INPUT_DIR
[[ -n "$TRIAGING_OUTPUT_DIR" ]] && export TRIAGING_OUTPUT_DIR

make run AGENT=test-triaging-agent BUILD_TAG="$BUILD_TAG"
