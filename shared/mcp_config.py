"""Utility to write/update the project-level .mcp.json for MCP server configuration.

Called by action steps that need browser control via Playwright MCP.
The .mcp.json is written to the project root so that `claude -p` subprocess
calls launched from that cwd will automatically pick up the MCP server.
"""
import json
import os
from pathlib import Path


def write_playwright_mcp_config(project_root: Path, headless: bool = True) -> Path:
    """Write .mcp.json at project_root with the Playwright MCP server config.

    Args:
        project_root: Directory where .mcp.json is written (must be the cwd
                      passed to the `claude -p` subprocess so it is detected).
        headless:     True  → browser runs headless (CI / no display)
                      False → browser window is visible (debug / local dev)

    Returns:
        Path to the written .mcp.json file.
    """
    # @playwright/mcp is headed by default — only add --headless for CI/non-display runs.
    # Do NOT pass --no-headless; it is not a valid flag and is silently ignored.
    # --isolated: use an in-memory browser context with no persistent profile/cookies,
    # so login state from a previous run does not bleed into the next run.
    # --viewport-size: use a full-HD viewport so the full page layout is visible,
    # matching what a real user sees and avoiding elements hidden behind mobile breakpoints.
    args: list = ["@playwright/mcp@latest", "--isolated", "--viewport-size=1920,1080"]
    if headless:
        args.append("--headless")

    config = {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": args,
            }
        }
    }

    mcp_json_path = project_root / ".mcp.json"
    mcp_json_path.write_text(json.dumps(config, indent=2) + "\n")
    return mcp_json_path
