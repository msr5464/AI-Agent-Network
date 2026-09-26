#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-healing-agent.sh
# Run the test-healing-agent with live logs.
#
# Usage:
#   ./scripts/run-healing-agent.sh                                  # queue mode: oldest handoff
#   ./scripts/run-healing-agent.sh ProdSanity-All-Tests-541         # a specific build tag
#   ./scripts/run-healing-agent.sh /path/to/handoff.json            # a handoff file
#   ./scripts/run-healing-agent.sh --test LoginTest#testLogin       # standalone: run one test and fix it
#   ./scripts/run-healing-agent.sh --test automation.saucedemo.SauceDemoWebTest   # a whole class
#   REPAIR=true     ./scripts/run-healing-agent.sh --test ...       # park the browser for inspection
#   FORCE=true      ./scripts/run-healing-agent.sh --test ...       # non-locator failures too
#   AUTO_PUSH=false ./scripts/run-healing-agent.sh                  # dry-run (no PR)
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--test" ]]; then
  [[ -n "${2:-}" ]] || { echo "Usage: $0 --test <Class#method | Class.method | pkg.Class.method | Class>"; exit 1; }
  export TEST_NAME="$2"
  set --
elif [[ -f "${1:-}" ]]; then
  export HANDOFF_FILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
  set --
fi
exec "$(dirname "${BASH_SOURCE[0]}")/_run-agent.sh" test-healing-agent "$@"
