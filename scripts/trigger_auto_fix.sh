#!/usr/bin/env bash
set -euo pipefail

# Auto-fix only: skip report generation, enable auto-fix.
# Usage: ./scripts/trigger_auto_fix.sh --input-dir <report_dir> [--output-dir ... --table-name ...] [--autofix-tests "Test1,Test2"] [--autofix-tests-file path/to/file.txt]
# <report_dir> must point to the already generated report folder (same path you used for generate_report.sh)
# --autofix-tests: Comma-separated list of test names to fix (optional)
# --autofix-tests-file: Path to file with test names, one per line (optional)

# Default arguments when none are provided.
if [ "$#" -eq 0 ]; then
  set -- --input-dir testdata/ProdSanity-All-Tests-535 --output-dir reports
fi

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

source venv/bin/activate

# Check and install browser dependencies if needed
if ! python3 -c "import selenium" 2>/dev/null || ! command -v chromedriver >/dev/null 2>&1; then
    echo "📦 Installing browser dependencies (Selenium & ChromeDriver)..."
    bash "$SCRIPT_DIR/install_browser_deps.sh"
fi

# Ensure auto-fix is on unless explicitly set otherwise.
export AUTO_FIX_ENABLED=${AUTO_FIX_ENABLED:-true}

python3 src/main.py "$@" --skip-report
