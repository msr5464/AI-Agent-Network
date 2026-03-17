#!/bin/bash
# High-performance setup script for macOS/Linux

set -euo pipefail

# Colors for output
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

function write_step() {
    echo -e "${CYAN}==> $1${NC}"
}

# 1. Verify Python
write_step "Verifying Python 3"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 not found. Please install Python 3.9+ and try again.${NC}"
    exit 1
fi

# 2. Create Virtual Environment
if [ ! -d "venv" ]; then
    write_step "Creating virtual environment at venv"
    python3 -m venv venv
else
    write_step "Virtual environment already exists (skipping creation)"
fi

# 3. Upgrade Pip and Install Dependencies
write_step "Upgrading pip and installing dependencies"
source venv/bin/activate
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
else
    echo -e "${RED}Error: requirements.txt not found!${NC}"
    exit 1
fi

# 4. Initialize Config
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
echo -e "  1. ${YELLOW}Update config/.env${NC} with your database and AI provider details."
echo -e "  2. Run the agent via: ${YELLOW}./scripts/run.sh${NC}"
