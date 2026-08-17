#!/bin/bash
# Setup script for macOS/Linux

set -euo pipefail

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

function write_step() {
    echo -e "${CYAN}==> $1${NC}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 1. Verify Python
write_step "Verifying Python 3"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 not found. Please install Python 3.9+ and try again.${NC}"
    exit 1
fi

# 2. Verify / install Claude CLI
write_step "Checking Claude CLI"
CLAUDE_CLI="${CLAUDE_CLI_PATH:-claude}"
if command -v "$CLAUDE_CLI" &> /dev/null; then
    echo "  Claude CLI found: $(command -v "$CLAUDE_CLI")"
else
    echo -e "  ${YELLOW}Claude CLI not found — attempting install via npm...${NC}"
    if command -v npm &> /dev/null; then
        npm install -g @anthropic-ai/claude-code
        if command -v claude &> /dev/null; then
            echo "  Claude CLI installed: $(command -v claude)"
        else
            echo -e "  ${RED}Install appeared to succeed but 'claude' not on PATH.${NC}"
            echo -e "  ${YELLOW}Restart your terminal or set CLAUDE_CLI_PATH in config/.env.${NC}"
        fi
    else
        echo -e "  ${RED}npm not found — cannot auto-install Claude CLI.${NC}"
        echo -e "  ${YELLOW}Install Node.js from https://nodejs.org then re-run this script,${NC}"
        echo -e "  ${YELLOW}or install manually: npm install -g @anthropic-ai/claude-code${NC}"
    fi
fi

# 3. Create virtualenv if needed, then install dependencies
write_step "Setting up virtual environment"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "  Created .venv"
else
    echo "  .venv already exists (skipping creation)"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

write_step "Installing Python dependencies"
if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}Error: requirements.txt not found!${NC}"
    exit 1
fi
python3 -m pip install --upgrade pip -q
python3 -m pip install -r requirements.txt

# 4. Initialize config
ENV_PATH="config/.env"
EXAMPLE_PATH="config/.env.example"

if [ ! -f "$ENV_PATH" ] && [ -f "$EXAMPLE_PATH" ]; then
    write_step "Creating config/.env from template"
    cp "$EXAMPLE_PATH" "$ENV_PATH"
elif [ ! -f "$EXAMPLE_PATH" ]; then
    echo -e "${YELLOW}Warning: config/.env.example missing; skipping auto-copy.${NC}"
else
    write_step "config/.env already exists (skipping copy)"
fi

write_step "Setup complete. Next steps:"
echo -e "  1. ${YELLOW}source .venv/bin/activate${NC}  — activate the virtual environment in your shell."
echo -e "  2. ${YELLOW}Edit config/.env${NC} — fill in DB credentials, Claude CLI path, GitHub token, Slack token."
echo -e "  3. Analyse a build:  ${YELLOW}./scripts/run-analyse.sh${NC}"
echo -e "  4. Fix & raise PR:   ${YELLOW}./scripts/run-autofix.sh${NC}"
