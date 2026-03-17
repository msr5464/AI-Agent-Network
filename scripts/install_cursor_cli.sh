#!/usr/bin/env bash
# Installs Cursor CLI (agent) on macOS/Linux and ensures PATH is updated for the current shell.
set -euo pipefail

echo "📦 Installing Cursor CLI (agent)..."
curl https://cursor.com/install -fsS | bash

# Common install locations checked by Cursor installer
CANDIDATES=(
  "$HOME/.local/bin"
  "/usr/local/bin"
  "$HOME/bin"
)

added=""
for dir in "${CANDIDATES[@]}"; do
  if [ -x "$dir/agent" ]; then
    case ":$PATH:" in
      *":$dir:"*) ;; # already present
      *) export PATH="$dir:$PATH"; added="$dir";;
    esac
    break
  fi
done

if command -v agent >/dev/null 2>&1; then
  echo "✅ Cursor CLI installed. agent path: $(command -v agent)"
  if [ -n "$added" ]; then
    echo "ℹ️  PATH updated for this session (added $added)."
    echo "    Add this to your shell rc (e.g., ~/.bashrc or ~/.zshrc) to persist:"
    echo "    export PATH=\"$added:\$PATH\""
  fi
else
  echo "❌ agent not found on PATH. Check installer output and add install dir to PATH manually."
  exit 1
fi
