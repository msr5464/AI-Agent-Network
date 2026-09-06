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


def write_mcp_config(project_root: Path, headless: Optional[bool] = None,
                     storage_state=None, cdp_endpoint=None) -> Path:
    """Write .mcp.json at project_root with the active framework's MCP server config.
    
    Returns:
        Path to the written .mcp.json file.
    """
    from shared.frameworks import get_active_plugin
    config = get_active_plugin().mcp.get_server_config(project_root, headless, cdp_endpoint, storage_state)
    
    mcp_json_path = project_root / ".mcp.json"
    mcp_json_path.write_text(json.dumps(config, indent=2) + "\n")
    return mcp_json_path
