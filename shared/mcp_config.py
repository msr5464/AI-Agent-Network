"""Utility to write/update the project-level .mcp.json for MCP server configuration.

Called by action steps that need browser control via Playwright MCP.
The .mcp.json is written to the project root so that `claude -p` subprocess
calls launched from that cwd will automatically pick up the MCP server.
"""
import json
import os
from pathlib import Path
from typing import Optional

from shared import browser_mode

# Pinned rather than "@latest": npx re-resolves a floating tag against the npm
# registry on EVERY launch, which is a network round-trip on the startup path of
# every browser step (twice, when validation retries). It has been observed to
# leave the server still "status": "pending" when the model takes its first turn,
# at which point the run has no browser tools at all and reports the flow as
# unvalidatable. Pinning also stops the server changing version underneath a run.
# Override with PLAYWRIGHT_MCP_VERSION to move it; "latest" restores the old
# floating behaviour.
PLAYWRIGHT_MCP_VERSION = os.environ.get("PLAYWRIGHT_MCP_VERSION", "0.0.79")


def _mcp_package() -> str:
    return f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"


def write_playwright_mcp_config(project_root: Path, headless: Optional[bool] = None,
                                storage_state=None, cdp_endpoint=None) -> Path:
    """Write .mcp.json at project_root with the Playwright MCP server config.

    Args:
        project_root: Directory where .mcp.json is written (must be the cwd
                      passed to the `claude -p` subprocess so it is detected,
                      unless the path is passed explicitly via --mcp-config).
        headless:     True  → browser runs headless (CI / no display)
                      False → browser window is visible (debug / local dev)
                      None  → PLAYWRIGHT_HEADLESS decides, defaulting to headless.
                      Left to the default so a new caller cannot accidentally
                      pin a mode the rest of the run is not using.
        cdp_endpoint: Optional CDP URL (e.g. http://localhost:9222) of a browser
                      that is ALREADY running. Used by repair mode, where the
                      automation framework parked the browser on the failing page:
                      attaching means inspecting the real broken state, including
                      counting how many elements a candidate selector matches,
                      rather than trying to navigate back to it.
        storage_state: Optional path to a Playwright storage-state JSON
                      (cookies + localStorage), as written by the automation
                      framework's BrowserHelper.storeSession(). Starting from a
                      saved session skips the login flow entirely, so no
                      credentials need to be handed to the model.

    Returns:
        Path to the written .mcp.json file.
    """
    # @playwright/mcp is headed by default — only add --headless for CI/non-display runs.
    # Do NOT pass --no-headless; it is not a valid flag and is silently ignored.
    # --isolated: use an in-memory browser context with no persistent profile/cookies,
    # so login state from a previous run does not bleed into the next run.
    # --viewport-size: use a full-HD viewport so the full page layout is visible,
    # matching what a real user sees and avoiding elements hidden behind mobile breakpoints.
    if cdp_endpoint:
        # Attaching to an existing browser: --isolated/--headless/--storage-state
        # all describe how to LAUNCH one, and conflict with connecting to one.
        args = [_mcp_package(), "--cdp-endpoint", str(cdp_endpoint)]
        config = {"mcpServers": {"playwright": {"command": "npx", "args": args}}}
        mcp_json_path = project_root / ".mcp.json"
        mcp_json_path.write_text(json.dumps(config, indent=2) + "\n")
        return mcp_json_path

    args: list = [_mcp_package(), "--isolated", "--viewport-size=1920,1080"]
    if headless is None:
        headless = browser_mode.headless()
    if headless:
        args.append("--headless")
    if storage_state:
        # --isolated keeps the profile in memory; --storage-state seeds that
        # in-memory context with a previously saved session.
        args.extend(["--storage-state", str(storage_state)])

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
