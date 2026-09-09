"""The browser MCP server the agents drive, and the .mcp.json that configures it.

**This is always Playwright, whatever framework the target repo uses**, and that
is a design decision rather than a limitation.

The agents use a browser in two unrelated roles. One is the target repo's
convention — the syntax its tests are written in — which is what
shared/frameworks abstracts. The other is the agents' own instrument: opening a
page, reading the DOM, checking whether a candidate locator resolves. This is
the instrument, and it speaks CDP, which is a browser-level protocol. A browser
does not know or care which framework drove it there, so a Selenium repo is
inspected with exactly the same tool as a Playwright one.

This used to be an `MCPProvider` interface with one implementation per plugin,
which bought nothing: the Selenium implementation returned the Playwright MCP
server, and both returned the same allowed-tools list. The plan that introduced
it framed that as a regrettable "Selenium MCP fallback strategy"; it is simply
the right answer, so the abstraction is gone and this is the one implementation.

.mcp.json placement matters under parallel execution: write it to the run's own
audit dir, never the shared repo root, or concurrent runs clobber each other's
browser settings.
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from shared import browser_mode


def mcp_server_config(project_root: Path, headless: Optional[bool] = None,
                      cdp_endpoint: Optional[str] = None,
                      storage_state: Optional[str] = None) -> Dict:
    """The mcpServers block for the browser the agents inspect with."""
    version = os.environ.get("PLAYWRIGHT_MCP_VERSION", "0.0.79")
    command = f"@playwright/mcp@{version}"

    # Attaching to an already-running browser by CDP: this is also how a
    # Selenium-launched browser is inspected, since Selenium 4 exposes the same
    # DevTools port.
    if cdp_endpoint:
        return {"mcpServers": {"playwright": {
            "command": "npx", "args": [command, "--cdp-endpoint", str(cdp_endpoint)]}}}

    args = [command, "--isolated", "--viewport-size=1920,1080"]
    if headless is None:
        headless = browser_mode.headless()
    if headless:
        args.append("--headless")
    if storage_state:
        args.extend(["--storage-state", str(storage_state)])

    return {"mcpServers": {"playwright": {"command": "npx", "args": args}}}


def allowed_tools() -> List[str]:
    """The tool allowlist for a `claude -p` call that drives the browser."""
    return ["mcp__playwright__*"]


def write_mcp_config(project_root: Path, headless: Optional[bool] = None,
                     storage_state=None, cdp_endpoint=None) -> Path:
    """Write .mcp.json at project_root. Returns the path written.

    `project_root` should be the run's audit dir under concurrent execution —
    see this module's docstring.
    """
    config = mcp_server_config(project_root, headless, cdp_endpoint, storage_state)
    mcp_json_path = Path(project_root) / ".mcp.json"
    mcp_json_path.write_text(json.dumps(config, indent=2) + "\n")
    return mcp_json_path
