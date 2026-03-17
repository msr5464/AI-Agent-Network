#!/bin/bash
# Unified run script for the AI QA Agent.
# Usage examples:
#   ./scripts/run.sh
#     Runs with default input directory (testdata/ProdSanity-All-Tests-535)
#     and output directory (reports).
#
#   ./scripts/run.sh --input-dir testdata/Regression-Smoke-Tests-420 --output-dir custom-reports
#     Runs against a custom input directory with a custom output directory.
#
#   ./scripts/run.sh --table-name results_custom_project
#     Runs with explicit database table name, overriding auto-detection.

set -euo pipefail

# Default arguments when none are provided.
if [ "$#" -eq 0 ]; then
  set -- --input-dir testdata/ProdSanity-All-Tests-541 --output-dir reports
fi

# Activate virtual environment and run the agent with the resolved args.
source venv/bin/activate

python3 src/main.py "$@" --skip-autofix
