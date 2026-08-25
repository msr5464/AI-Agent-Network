#!/usr/bin/env bash
set -euo pipefail
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-fix-test.sh
# Standalone healing: run one test (or a whole class) locally, reproduce the
# failure, fix the broken locator, verify, and raise a PR.
#
# No triaging run and no queue involved — this is the "I already know which test
# is broken, fix it now" path.
#
# Usage:
#   ./scripts/run-fix-test.sh LoginTest#testLogin
#   ./scripts/run-fix-test.sh automation.saucedemo.SauceDemoWebTest   # whole class
#   REPAIR=true    ./scripts/run-fix-test.sh LoginTest#testLogin      # park the browser
#   FORCE=true     ./scripts/run-fix-test.sh LoginTest#testLogin      # non-locator failures too
#   AUTO_PUSH=false ./scripts/run-fix-test.sh LoginTest#testLogin     # no PR
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "Usage: $0 <Class#method | Class.method | pkg.Class.method | Class>"
  echo ""
  echo "Examples:"
  echo "  $0 LoginTest#testLogin"
  echo "  $0 automation.saucedemo.SauceDemoWebTest"
  echo "  REPAIR=true $0 LoginTest#testLogin"
  exit 1
fi

export TEST_NAME="$1"
exec make run AGENT=test-healing-agent TEST="$TEST_NAME"
