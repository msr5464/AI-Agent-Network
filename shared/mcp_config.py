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

from shared import browser_mode, frames

# The helpers every browser-driving prompt calls instead of carrying code: the
# in-page half (every document, iframes included) and the page-object half.
_BROWSER_DIR = Path(__file__).resolve().parent / "browser"
HELPERS_JS = (_BROWSER_DIR / "qa-helpers.js").read_text()
INIT_PAGE = _BROWSER_DIR / "qa-page.js"


def mcp_server_config(project_root: Path, headless: Optional[bool] = None,
                      cdp_endpoint: Optional[str] = None,
                      storage_state: Optional[str] = None,
                      evidence_file: Optional[Path] = None,
                      known_locators_file: Optional[Path] = None) -> Dict:
    """The mcpServers block for the browser the agents inspect with.

    `evidence_file` — where page.qa writes what it measures (inventories, live
    counts), for Python to read instead of the model's retyping of it.
    `known_locators_file` — locators the repo already has, counted live on every
    distinct page state.
    """
    env = {}
    if evidence_file:
        env["QA_EVIDENCE_FILE"] = str(evidence_file)
    if known_locators_file:
        env["QA_KNOWN_LOCATORS"] = str(known_locators_file)
    extra = {"env": env} if env else {}
    version = os.environ.get("PLAYWRIGHT_MCP_VERSION", "0.0.79")
    command = f"@playwright/mcp@{version}"
    # The iframe selector rule and the measuring helpers (window.__qa), defined in
    # every page before its own scripts run. Beside .mcp.json, so parallel runs
    # never share it.
    init_script = Path(project_root) / "qa-init.js"
    init_script.write_text(frames.INIT_JS + HELPERS_JS)
    # page.qa — the Node half: frames, one-call steps, brief-screen recording.
    helpers = ["--init-script", str(init_script), "--init-page", str(INIT_PAGE)]

    # Attaching to an already-running browser by CDP: this is also how a
    # Selenium-launched browser is inspected, since Selenium 4 exposes the same
    # DevTools port.
    if cdp_endpoint:
        return {"mcpServers": {"playwright": {
            "command": "npx", "args": [command, "--cdp-endpoint", str(cdp_endpoint),
                                       *helpers], **extra}}}

    # Playwright's own Chromium, not the machine's branded Chrome (the server's
    # default). It is the engine the generated tests run on, and it is the one
    # that is not slow: on a checkout demo, installed Chrome 154 held a renderer at
    # 100% CPU for ~80s after every page load, so a trivial evaluate took 2-10s and
    # a step 15-55s — about 6 minutes of every 15-minute validation. Chromium on
    # the same page: 2-15ms. PLAYWRIGHT_MCP_BROWSER=chrome restores the old one.
    browser = os.environ.get("PLAYWRIGHT_MCP_BROWSER", "chromium")
    args = [command, "--browser", browser, "--isolated", "--viewport-size=1920,1080",
            *helpers]
    if headless is None:
        headless = browser_mode.headless()
    if headless:
        args.append("--headless")
    if storage_state:
        args.extend(["--storage-state", str(storage_state)])

    return {"mcpServers": {"playwright": {"command": "npx", "args": args, **extra}}}


def allowed_tools() -> List[str]:
    """The tool allowlist for a `claude -p` call that drives the browser."""
    return ["mcp__playwright__*"]


# Prompt text for every agent that walks a flow through this server: two things a
# plain browser_evaluate cannot see. An embedded checkout run hit both. The model
# saw the amount inside the bank's cross-origin iframe but had no way to count a
# selector for it, so the check was downgraded. The payment result screen closed
# about 10s after the click, and the model's next look came 12s after it: one
# model turn plus the 5s it chose to wait.
_CAPTURE_RULES = """
FRAMES — browser_evaluate runs in the top document only. Inside an iframe from
another origin (embedded checkouts, payment and 3-D Secure pages, chat widgets) it
sees nothing, or throws "Blocked a frame ... from accessing a cross-origin frame".
page.qa.step, page.qa.harvest and page.qa.check work in every frame. An element
inside an iframe comes back as its whole chain, the way into the frame followed by
its own selector:
    #checkout >> internal:control=enter-frame >> #amount
Every hop of that chain was proven unique on its own and the element was counted
inside its frame, so report it exactly as returned. (A count through an unchecked
hop proves nothing: Playwright counts inside the first matching iframe and then
refuses to act when there are two.) An element returned with sel null because its
iframe has no stable unique selector cannot be located reliably: report no selector
for it and say why. Act inside a frame with page.frameLocator('<iframe selector>').
A count read off a snapshot or a screenshot was not measured, so never report one.

BRIEF SCREENS — capture them in the same call as the action that shows them. Each
tool call, and each turn between two calls, costs seconds. A screen that shows
briefly and then closes or redirects (a payment result, a confirmation
interstitial, a toast) is gone before a second call can look at it, and waiting
first only makes sure of missing it. When a step says a screen closes or redirects
quickly, or checks a result, message or toast that an action produces, do the
action AND the recording in ONE call:

    async (page) => page.qa.record(
      () => page.frameLocator('#checkout').locator('button.pay').click(), 20000)

It starts recording, then performs the action, and returns every new state of
every frame for 20 seconds: { action, states: [{ ms, frame, els: [{ tag, text,
sel, total, visible }] }] } — each element counted while it was on screen, with
its frame chain on. These are real observations: report them exactly as you would
a live one. A screen that is not in the result did not appear within 20 seconds of
the action; raise the 20 seconds only when the step says the screen takes longer
to show up.
"""


def write_mcp_config(project_root: Path, headless: Optional[bool] = None,
                     storage_state=None, cdp_endpoint=None,
                     evidence_file=None, known_locators_file=None) -> Path:
    """Write .mcp.json at project_root. Returns the path written.

    `project_root` should be the run's audit dir under concurrent execution —
    see this module's docstring.
    """
    config = mcp_server_config(project_root, headless, cdp_endpoint, storage_state,
                               evidence_file, known_locators_file)
    mcp_json_path = Path(project_root) / ".mcp.json"
    mcp_json_path.write_text(json.dumps(config, indent=2) + "\n")
    return mcp_json_path

CAPTURE_RULES = _CAPTURE_RULES
