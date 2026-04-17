#!/usr/bin/env python3
"""
Step 02 — Validate Web
Uses Claude + Playwright MCP to directly control a browser and validate web flows.
Claude navigates, clicks, and fills forms using browser tools; outputs structured
STEP_PASSED / STEP_FAILED / SELECTOR_FOUND markers that are parsed into a selector map.

Skipped automatically by run.sh when test_type=api.

Reads:  $AUDIT_DIR/01-parse.json
Writes: $AUDIT_DIR/02-validate-web.json   (selector map + step results)
        $AUDIT_DIR/02-validate-web.md     (human-readable summary)
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

CLAUDE_CLI  = os.environ.get("CLAUDE_CLI_PATH", "claude")
MODEL       = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
PW_TIMEOUT  = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "30000"))
PW_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"

# ── Shared helpers ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(REPO_ROOT / "shared"))
from claude import call_claude          # noqa: E402  (after sys.path update)
from mcp_config import write_playwright_mcp_config  # noqa: E402


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [02-validate-web] {msg}", flush=True)


# ── Output parsers ─────────────────────────────────────────────────────────────

def parse_selector_output(output: str) -> dict:
    """Parse SELECTOR_FOUND: lines from Claude output."""
    selectors = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("SELECTOR_FOUND:"):
            rest = line[len("SELECTOR_FOUND:"):].strip()
            if "=" in rest:
                name, selector = rest.split("=", 1)
                selectors[name.strip()] = selector.strip()
    return selectors


def parse_step_results(output: str) -> tuple:
    """Parse STEP_PASSED / STEP_FAILED lines from Claude output."""
    passed = []
    failed = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("STEP_PASSED:"):
            passed.append(line[len("STEP_PASSED:"):].strip())
        elif line.startswith("STEP_FAILED:"):
            failed.append(line[len("STEP_FAILED:"):].strip())
    return passed, failed


def parse_page_dumps(output: str) -> dict:
    """Parse PAGE_DUMP: label|json_array lines from Claude output."""
    dumps = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("PAGE_DUMP:"):
            rest = line[len("PAGE_DUMP:"):].strip()
            if "|" in rest:
                label, json_part = rest.split("|", 1)
                try:
                    dumps[label.strip()] = json.loads(json_part.strip())
                except Exception:
                    pass
    return dumps


def parse_interaction_hints(output: str) -> list:
    """Parse INTERACTION_HINT: type|name|selector|text lines from Claude output."""
    hints = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("INTERACTION_HINT:"):
            rest = line[len("INTERACTION_HINT:"):].strip()
            parts = rest.split("|", 3)
            if len(parts) == 4:
                hints.append({
                    "type":     parts[0].strip(),
                    "name":     parts[1].strip(),
                    "selector": parts[2].strip(),
                    "text":     parts[3].strip(),
                })
    return hints


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    plan = json.loads((AUDIT_DIR / "01-parse.json").read_text())

    base_url   = plan.get("web_base_url", "")
    web_steps  = plan.get("web_steps_for_validation", [])
    web_pages  = plan.get("web_pages", [])

    if not web_steps:
        log("No web steps found in plan — writing empty selector map")
        _write_empty(reason="no web steps in plan")
        return

    if not base_url:
        log("No web_base_url in plan — writing empty selector map")
        _write_empty(reason="no web_base_url in plan")
        return

    if not base_url.startswith(("http://", "https://")):
        log(f"ERROR: web_base_url is not a valid URL: '{base_url}' — check input file (add 'URL: https://...')")
        _write_empty(reason=f"invalid web_base_url: '{base_url}' — must start with http:// or https://")
        return

    # Credential check — fail fast if login steps require credentials not provided
    demo_creds = plan.get("demo_credentials", {})
    login_keywords = ("login", "log in", "sign in", "signin", "authenticate")
    steps_need_login = any(
        any(kw in step.lower() for kw in login_keywords)
        for step in web_steps
    )
    if steps_need_login and not (demo_creds.get("username") and demo_creds.get("password")):
        log("ERROR: Login step detected but no credentials found in input file.")
        log("Add the following to your queue input file:")
        log("  Username: your_username")
        log("  Password: your_password")
        log("Example:")
        log("  Feature: github")
        log("  Type: web")
        log("  Username: octocat")
        log("  Password: mypassword")
        log("  Steps:")
        log("    1. Login and navigate to dashboard")
        _write_empty(reason="login step detected but no credentials in input file — add Username/Password fields")
        sys.exit(1)

    creds_section = ""
    if demo_creds:
        creds_section = f"""
CREDENTIALS (use exactly these — do NOT use any other values):
  username / email : {demo_creds.get('username', '')}
  password         : {demo_creds.get('password', '')}"""
        if demo_creds.get("otp"):
            creds_section += f"""
  OTP / 2FA code   : {demo_creds.get('otp')}
  IMPORTANT: After entering the password and clicking login, an OTP/2FA prompt may appear.
  If it does, enter the OTP code above and submit before continuing."""

    # Build locator names needed across all page objects
    all_locators = []
    for page_def in web_pages:
        all_locators.extend(page_def.get("locators_needed", []))

    # Write .mcp.json so the `claude -p` subprocess can use the Playwright MCP server
    mode_label = "headless" if PW_HEADLESS else "headed (browser window visible)"
    log(f"Browser mode: {mode_label}")
    mcp_path = write_playwright_mcp_config(REPO_ROOT, headless=PW_HEADLESS)
    log(f"Playwright MCP config written: {mcp_path}")

    steps_numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(web_steps))
    locators_hint  = (
        f"\nLocators to discover and report (use these names in SELECTOR_FOUND): "
        f"{json.dumps(all_locators)}"
        if all_locators else ""
    )

    prompt = f"""You are a QA automation agent. Use the Playwright browser MCP tools to validate a web user flow.

TARGET URL: {base_url}

STEPS TO EXECUTE:
{steps_numbered}
{creds_section}
{locators_hint}

══════════════════════════════════════════════════════════════
OUTPUT PROTOCOL — emit these markers on their own lines:
══════════════════════════════════════════════════════════════
• After each step succeeds:
    STEP_PASSED: <step description>

• After each step fails:
    STEP_FAILED: <step description>|<error details> [url=<current page URL>]

• Whenever you find a working selector/locator:
    SELECTOR_FOUND: <camelCaseName>=<actualSelector>
  (e.g. SELECTOR_FOUND: loginButton=[name='commit'])

══════════════════════════════════════════════════════════════
EXECUTION RULES — follow exactly:
══════════════════════════════════════════════════════════════
1. Execute every step in order. Do NOT skip or reorder steps.

2. SELECTOR STRATEGY — try each strategy in priority order until one works:
   a) [data-cy='...'] or [data-testid='...'] or [data-test='...']
   b) [id='...']
   c) [name='...']
   d) [aria-label='...']
   e) role-based  (e.g. role=button[name='Sign in'])
   f) text-based  (e.g. text='Sign in')

   UNIQUENESS CHECK — mandatory before emitting SELECTOR_FOUND:
   While the browser is still open on that page, immediately after finding a
   candidate selector, use the browser tools to count how many elements it matches:
   • count == 1 → emit SELECTOR_FOUND and proceed.
   • count > 1  → selector is NOT unique. Do NOT emit it. Narrow it by:
       - Adding a parent scope:        form >> [name='commit']
       - Combining attributes:         button[type='submit'][name='commit']
       - Using a more specific attribute from the DOM snapshot
     Repeat the count check with the narrowed selector until count == 1,
     then emit SELECTOR_FOUND.
   Never emit a selector that matches more than one element.

3. RETRIES — if an element is not immediately found or visible:
   Wait 1 second and retry up to 3 times before declaring failure.

4. LOGIN GATE — after clicking the sign-in / submit button:
   a) Wait for navigation to complete.
   b) Check whether the current URL still contains '/login', '/signin', or '/session'.
   c) If it does → mark the login step FAILED with reason:
      "Login did not succeed — still on login page [url=<url>]"
      Then mark an internal flag loginSucceeded=false.
   d) For every subsequent step that requires an authenticated session:
      If loginSucceeded is false, immediately output:
        STEP_FAILED: <step desc>|Skipped — login did not succeed, cannot proceed
      and move on (do NOT attempt any browser interactions for that step).

5. EVERY STEP in its own try/catch. Never abort the whole run on a single failure.

6. Include the current page URL in every STEP_FAILED message.

7. After major page transitions (navigation, login, form submit), take an
   accessibility snapshot to understand the current page structure before
   attempting the next interaction.

8. Complete ALL steps — do not stop early unless the browser itself crashes.

Begin executing the steps now using the browser tools.
"""

    log(f"Calling Claude with Playwright MCP for {len(web_steps)} steps against {base_url}...")

    output_lines: list = []

    def _on_output(label: str, line: str) -> None:
        if label == "stdout":
            output_lines.append(line)
            log(f"  {line}")

    output = call_claude(
        prompt=prompt,
        model=MODEL,
        cwd=str(REPO_ROOT),
        timeout=600,
        on_output=_on_output,
        log_dir=str(AUDIT_DIR),
        allowed_tools=["mcp__playwright__*"],
    )

    if not output:
        log("WARNING: Claude returned no output — check model / MCP server availability")

    selectors         = parse_selector_output(output)
    steps_passed, steps_failed = parse_step_results(output)
    page_elements     = parse_page_dumps(output)
    interaction_hints = parse_interaction_hints(output)

    log(
        f"Selectors found: {len(selectors)} | "
        f"Steps passed: {len(steps_passed)} | "
        f"Steps failed: {len(steps_failed)} | "
        f"Page dumps: {len(page_elements)} | "
        f"Interaction hints: {len(interaction_hints)}"
    )

    if selectors:
        for name, sel in selectors.items():
            log(f"  {name} = {sel}")
    if interaction_hints:
        for h in interaction_hints:
            log(f"  HINT [{h['type']}] {h['name']} → {h['selector']} ({h['text']})")
    if steps_failed:
        log("Failed steps:")
        for s in steps_failed:
            if "|" in s:
                step_desc, error_msg = s.split("|", 1)
                log(f"  FAIL [{step_desc.strip()}]: {error_msg.strip()}")
                if any(v in error_msg for v in ("TEST_USERNAME", "TEST_PASSWORD", "EXPECTED_USERNAME")):
                    log("  → FIX: Set credentials in your queue input file under Username/Password fields")
                elif "Could not find" in error_msg or "not found" in error_msg.lower():
                    log("  → FIX: Element not found — check login state or target URL")
                elif "login did not succeed" in error_msg.lower():
                    log("  → FIX: Login failed — verify Username/Password in the queue input file")
                elif "Skipped" in error_msg:
                    log("  → FIX: Fix the login failure above; these steps will then run")
            else:
                log(f"  FAIL: {s}")

    _write_result(
        selectors=selectors,
        steps_passed=steps_passed,
        steps_failed=steps_failed,
        page_elements=page_elements,
        interaction_hints=interaction_hints,
        skipped=False,
        reason=None,
        raw_output=output[-3000:] if output else "",
    )


def _write_empty(reason: str) -> None:
    _write_result({}, [], [], page_elements={}, interaction_hints=[], skipped=True, reason=reason)


def _write_result(selectors, steps_passed, steps_failed,
                  page_elements=None, interaction_hints=None,
                  skipped=False, reason=None, raw_output="") -> None:
    data = {
        "skipped":           skipped,
        "reason":            reason,
        "selectors":         selectors,
        "steps_passed":      steps_passed,
        "steps_failed":      steps_failed,
        "page_elements":     page_elements or {},
        "interaction_hints": interaction_hints or [],
    }
    if raw_output:
        data["raw_output_tail"] = raw_output
    (AUDIT_DIR / "02-validate-web.json").write_text(json.dumps(data, indent=2))

    lines = ["# Web Validation Results", ""]
    if skipped:
        lines.append(f"Skipped: {reason}")
    else:
        lines.append(f"Steps passed:    {len(steps_passed)}")
        lines.append(f"Steps failed:    {len(steps_failed)}")
        lines.append(f"Selectors found: {len(selectors)}")
        if selectors:
            lines.append("")
            lines.append("## Confirmed Selectors")
            for name, sel in selectors.items():
                lines.append(f"- `{name}` → `{sel}`")
        if steps_failed:
            lines.append("")
            lines.append("## Failed Steps (step 03 will use inferred selectors)")
            for s in steps_failed:
                lines.append(f"- {s}")
    (AUDIT_DIR / "02-validate-web.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
