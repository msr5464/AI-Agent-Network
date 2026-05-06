#!/usr/bin/env bash
set -euo pipefail
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-autofix.sh
# Run the test-healing-agent agent.
#
# Usage:
#   ./scripts/run-autofix.sh                                     # queue mode: picks oldest
#   ./scripts/run-autofix.sh ProdSanity-All-Tests-541            # direct: by build tag
#   ./scripts/run-autofix.sh /path/to/my-handoff.json            # direct: by file path
#   AUTO_PUSH=false ./scripts/run-autofix.sh                     # dry-run (no PR)
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

ARG="${1:-}"

if [[ -z "$ARG" ]]; then
  # Queue mode — run.sh will pick oldest from queue/
  export BUILD_TAG=""
  unset HANDOFF_FILE 2>/dev/null || true

elif [[ -f "$ARG" ]]; then
  # Argument is a path to a JSON file — pass it directly
  export HANDOFF_FILE="$(cd "$(dirname "$ARG")" && pwd)/$(basename "$ARG")"
  BUILD_TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" "$HANDOFF_FILE")
  export BUILD_TAG

else
  # Argument is a build tag — queue/run.sh resolves the file
  export BUILD_TAG="$ARG"
  unset HANDOFF_FILE 2>/dev/null || true
fi

make run AGENT=test-healing-agent BUILD_TAG="${BUILD_TAG:-}"
