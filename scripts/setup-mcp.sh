#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# scripts/setup-mcp.sh
# Reads connectors/mcp/*.json and writes the mcpServers block into ~/.claude.json.
# Run once before first use, or whenever connector config changes.
#
# Usage:
#   make setup-mcp
#   bash scripts/setup-mcp.sh
# ─────────────────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONNECTORS_DIR="$REPO_ROOT/connectors/mcp"
CLAUDE_JSON="$HOME/.claude.json"

# ── Install dependencies ──────────────────────────────────────────────────────
install_if_missing() {
  local cmd="$1" pkg="${2:-$1}"
  if ! command -v "$cmd" &>/dev/null; then
    echo "[setup] Installing $pkg..."
    if command -v brew &>/dev/null; then
      brew install "$pkg"
    else
      echo "[setup] ERROR: $cmd not found and brew not available. Install $pkg manually."
      exit 1
    fi
  fi
}

install_if_missing jq
install_if_missing gh

# ── Load env files (config + all agents) ─────────────────────────────────────
for envfile in "$REPO_ROOT/config/.env" "$REPO_ROOT"/agents/*/.env; do
  if [[ -f "$envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
    echo "[setup] Loaded $(echo "$envfile" | sed "s|$REPO_ROOT/||")"
  fi
done

# ── Auth gh if not already ────────────────────────────────────────────────────
if command -v gh &>/dev/null && ! gh auth status &>/dev/null; then
  echo "[setup] GitHub CLI not authenticated."
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "[setup] Authenticating gh with GITHUB_TOKEN..."
    echo "$GITHUB_TOKEN" | gh auth login --with-token
  else
    echo "[setup] GITHUB_TOKEN not set. Run: gh auth login"
    echo "[setup] Or set GITHUB_TOKEN in config/.env"
  fi
fi

EXISTING=$([[ -f "$CLAUDE_JSON" ]] && cat "$CLAUDE_JSON" || echo "{}")
MCP_SERVERS="{}"

for connector_file in "$CONNECTORS_DIR"/*.json; do
  [[ -f "$connector_file" ]] || continue

  status=$(jq -r '.status // "active"' "$connector_file")
  [[ "$status" != "active" ]] && continue

  conn_name=$(jq -r '.name' "$connector_file")
  conn_url=$(jq -r '.url' "$connector_file")
  auth_type=$(jq -r '.auth.type' "$connector_file")
  env_var=$(jq -r '.auth.env_var' "$connector_file")

  token="${!env_var:-}"
  if [[ -z "$token" ]]; then
    echo "[setup-mcp] WARN: $env_var not set — skipping: $conn_name"
    continue
  fi

  if [[ "$auth_type" == "bearer" ]]; then
    server_entry=$(jq -n \
      --arg url "$conn_url" \
      --arg token "$token" \
      '{type: "url", url: $url, headers: {Authorization: ("Bearer " + $token)}}')
  else
    echo "[setup-mcp] WARN: Unknown auth type '$auth_type' for $conn_name — skipping"
    continue
  fi

  MCP_SERVERS=$(echo "$MCP_SERVERS" | jq --arg name "$conn_name" --argjson entry "$server_entry" '. + {($name): $entry}')
  echo "[setup-mcp] Registered: $conn_name"
done

echo "$EXISTING" | jq --argjson mcp "$MCP_SERVERS" '. + {mcpServers: $mcp}' > "$CLAUDE_JSON"
echo "[setup-mcp] Done → $CLAUDE_JSON"
