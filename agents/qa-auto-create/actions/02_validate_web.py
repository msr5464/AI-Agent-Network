#!/usr/bin/env python3
"""
Step 02 — Validate Web
Generates a headless Playwright (Node.js) script from the parsed web steps,
runs it against the target URL, and extracts confirmed DOM selectors.

Skipped automatically by run.sh when test_type=api.

Reads:  $AUDIT_DIR/01-parse.json
Writes: $AUDIT_DIR/02-validate-web.js     (generated Playwright script)
        $AUDIT_DIR/02-validate-web.json   (selector map + step results)
        $AUDIT_DIR/02-validate-web.md     (human-readable summary)
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

CLAUDE_CLI   = os.environ.get("CLAUDE_CLI_PATH", "claude")
MODEL        = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
NODE_PATH    = os.environ.get("NODE_PATH", "node")
PW_TIMEOUT   = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "30000"))

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [02-validate-web] {msg}", flush=True)


def call_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt, "--model", MODEL],
            capture_output=True, text=True, timeout=900, cwd=str(REPO_ROOT)
        )
    except subprocess.TimeoutExpired:
        log("ERROR: Claude CLI timed out (900s)")
        return ""
    if result.returncode != 0:
        log(f"Claude rc={result.returncode} stderr={result.stderr[:200]} stdout={result.stdout[:200]}")
        return ""
    if not result.stdout.strip():
        log(f"Claude returned empty stdout (rc=0) stderr={result.stderr[:200]}")
        return ""
    return result.stdout


def extract_js_block(text: str) -> str:
    """Extract JavaScript code from Claude response."""
    m = re.search(r"```(?:javascript|js)\s*([\s\S]*?)\s*```", text)
    if m:
        return m.group(1)
    # If no code block, try to extract anything that looks like Node.js
    m = re.search(r"(const \{[\s\S]*)", text)
    if m:
        return m.group(1)
    return text.strip()


def node_available() -> bool:
    try:
        result = subprocess.run([NODE_PATH, "--version"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def playwright_available() -> bool:
    """Check if playwright npm package is accessible."""
    try:
        result = subprocess.run(
            [NODE_PATH, "-e", "require('playwright'); console.log('ok')"],
            capture_output=True, text=True, timeout=5
        )
        return "ok" in result.stdout
    except Exception:
        return False


def parse_selector_output(output: str) -> dict:
    """Parse SELECTOR_FOUND: lines from script stdout."""
    selectors = {}
    for line in output.splitlines():
        if line.startswith("SELECTOR_FOUND:"):
            rest = line[len("SELECTOR_FOUND:"):].strip()
            if "=" in rest:
                name, selector = rest.split("=", 1)
                selectors[name.strip()] = selector.strip()
    return selectors


def parse_step_results(output: str) -> tuple:
    """Parse STEP_PASSED / STEP_FAILED lines from script stdout."""
    passed = []
    failed = []
    for line in output.splitlines():
        if line.startswith("STEP_PASSED:"):
            passed.append(line[len("STEP_PASSED:"):].strip())
        elif line.startswith("STEP_FAILED:"):
            failed.append(line[len("STEP_FAILED:"):].strip())
    return passed, failed


def parse_page_dumps(output: str) -> dict:
    """Parse PAGE_DUMP: label|json_array lines → {label: [elements]}"""
    import json as _json
    dumps = {}
    for line in output.splitlines():
        if line.startswith("PAGE_DUMP:"):
            rest = line[len("PAGE_DUMP:"):].strip()
            if "|" in rest:
                label, json_part = rest.split("|", 1)  # maxsplit=1 preserves | inside JSON
                try:
                    dumps[label.strip()] = _json.loads(json_part.strip())
                except Exception:
                    pass
    return dumps


def parse_interaction_hints(output: str) -> list:
    """Parse INTERACTION_HINT: type|name|selector|text lines → list of dicts"""
    hints = []
    for line in output.splitlines():
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    plan = json.loads((AUDIT_DIR / "01-parse.json").read_text())

    base_url = plan.get("web_base_url", "")
    web_steps = plan.get("web_steps_for_validation", [])
    web_pages = plan.get("web_pages", [])
    feature_class = plan.get("feature_class", "Feature")

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

    # Build list of locator names needed from all page objects
    all_locators = []
    for page_def in web_pages:
        all_locators.extend(page_def.get("locators_needed", []))

    demo_creds = plan.get("demo_credentials", {})
    creds_section = ""
    if demo_creds:
        creds_section = f"""
CREDENTIALS (use these for login — do NOT hardcode anything else):
  username/email: {demo_creds.get('username', '')}
  password:       {demo_creds.get('password', '')}"""
        if demo_creds.get("otp"):
            creds_section += f"""
  OTP:            {demo_creds.get('otp')}
  IMPORTANT: After entering the password and clicking login, an OTP/2FA screen may appear.
  If it does, enter the OTP above into the OTP field and submit it before proceeding."""

    log(f"Generating Playwright validation script for {len(web_steps)} steps...")

    prompt = f"""You are a Playwright automation expert. Generate a Node.js script that:
1. Uses the `playwright` npm package (not @playwright/test)
2. Runs headlessly against {base_url}
3. Executes these steps: {json.dumps(web_steps, indent=2)}
{creds_section}
4. For each locator needed, tries multiple selector strategies in this order:
   - [data-cy='...'] or [data-testid='...'] or [data-test='...'] (preferred if present)
   - [id='...']
   - [name='...']
   - [placeholder='...']
   - [aria-label='...']
   - role-based: page.getByRole(...)
   - text-based: page.getByText(...)
   IMPORTANT: Emit SELECTOR_FOUND for WHATEVER strategy works — not just data-cy.
   Many apps do not use data-cy; fall back to id, name, aria-label, or text.
5. For each successfully located element, emits exactly:
   SELECTOR_FOUND: <locatorName>=<actualSelector>
6. For each step, emits exactly:
   STEP_PASSED: <description>
   or STEP_FAILED: <description>|<error message>
7. Does NOT throw on failures — catch all errors and log STEP_FAILED instead
8. Times out each action at {PW_TIMEOUT}ms
9. Closes the browser at the end (even on error)

Locators to find and report: {json.dumps(all_locators)}

IMPORTANT — After reaching each significant page state, emit a full DOM snapshot:
  PAGE_DUMP: <label>|<json_array>
  where <label> is a short snake_case name (e.g. login, dashboard, users_list, add_user_form)
  and <json_array> is a JSON array of ALL elements with [data-cy] attributes on the page:
  [{{"data-cy": "...", "tag": "input", "type": "text", "placeholder": "Enter name", "text": ""}}]
  Include: data-cy value, tag name, type attribute (for inputs), placeholder (for inputs), inner text (trimmed, max 50 chars).
  Emit PAGE_DUMP immediately after navigation completes and after each major state change.

IMPORTANT — For any dropdown/select encountered, click it open and emit its options:
  INTERACTION_HINT: dropdown|<locatorName>|<optionSelector>|<optionText>
  e.g. INTERACTION_HINT: dropdown|roleDropdown|[data-cy='user-invite-form-item-role-customers.card-only']|Employee
  Use the actual [data-cy] of each option element. If options have no data-cy, use text-based selector.
  After emitting hints, close the dropdown before continuing.

IMPORTANT — For any [role='radio'] or [role='radiogroup'] elements found, emit:
  INTERACTION_HINT: radio|<groupName>|[role='radio'][aria-label='<value>']|<value>
  e.g. INTERACTION_HINT: radio|companyRole|[role='radio'][aria-label='Non-director']|Non-director
  Enumerate ALL radio options in the group.

IMPORTANT — After clicking a submit/confirm button, wait 3000ms then emit another PAGE_DUMP
  with label ending in _result (e.g. add_user_result) so the success/confirmation state is captured.

The script should follow this skeleton:
```javascript
const {{ chromium }} = require('playwright');

async function dumpPage(page, label) {{
  try {{
    // Collect data-cy elements
    const cyEls = await page.$$eval('[data-cy]', els => els.map(el => ({{
      'data-cy': el.getAttribute('data-cy'),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: (el.innerText || '').trim().slice(0, 50)
    }})));
    // Collect inputs/buttons/selects that lack data-cy (captures apps without data-cy attributes)
    const formEls = await page.$$eval('input,button,select,textarea,a[href]', els =>
      els.filter(el => !el.hasAttribute('data-cy')).map(el => ({{
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        id: el.getAttribute('id') || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        'aria-label': el.getAttribute('aria-label') || '',
        'data-testid': el.getAttribute('data-testid') || '',
        text: (el.innerText || '').trim().slice(0, 50)
      }}))
    );
    console.log(`PAGE_DUMP: ${{label}}|${{JSON.stringify([...cyEls, ...formEls])}}`);
  }} catch(e) {{}}
}}

(async () => {{
  const browser = await chromium.launch({{ headless: true }});
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout({PW_TIMEOUT});

  try {{
    // Step 1: Navigate
    await page.goto('{base_url}');
    console.log('STEP_PASSED: Navigate to base URL');
    await dumpPage(page, 'initial');

    // ... more steps (login, navigate, interact) ...
    // Remember to call dumpPage(page, '<label>') after each major state change

    // Probe for selectors
    const selectorCandidates = {{
      // locatorName: [list of selectors to try in order]
    }};

    for (const [name, candidates] of Object.entries(selectorCandidates)) {{
      for (const sel of candidates) {{
        try {{
          const el = page.locator(sel);
          if (await el.count() > 0) {{
            console.log(`SELECTOR_FOUND: ${{name}}=${{sel}}`);
            break;
          }}
        }} catch (e) {{}}
      }}
    }}

  }} catch (e) {{
    console.log(`STEP_FAILED: Unhandled error|${{e.message}}`);
  }} finally {{
    await browser.close();
  }}
}})();
```

Output ONLY the complete JavaScript code, no prose.
"""

    js_response = call_claude(prompt)
    js_code = extract_js_block(js_response)

    script_path = AUDIT_DIR / "02-validate-web.js"
    script_path.write_text(js_code)
    log(f"Script written to: {script_path}")

    # Check prerequisites before running
    if not node_available():
        log("WARNING: node not found — skipping script execution")
        _write_result(
            selectors={}, steps_passed=[], steps_failed=[],
            skipped=True, reason="node binary not found",
            script_path=script_path
        )
        return

    if not playwright_available():
        log("WARNING: playwright npm package not found — skipping script execution")
        log("To install: npm install -g playwright && npx playwright install chromium")
        _write_result(
            selectors={}, steps_passed=[], steps_failed=[],
            skipped=True, reason="playwright npm package not installed",
            script_path=script_path
        )
        return

    # Run the script
    log(f"Running Playwright validation script against {base_url}...")
    try:
        result = subprocess.run(
            [NODE_PATH, str(script_path)],
            capture_output=True, text=True,
            timeout=300,
            cwd=str(AUDIT_DIR)
        )
        output = result.stdout + result.stderr
        log(f"Script exited with code {result.returncode}")
    except subprocess.TimeoutExpired:
        log("WARNING: Playwright script timed out (300s)")
        output = ""
        result = type("R", (), {"returncode": 1})()

    selectors = parse_selector_output(output)
    steps_passed, steps_failed = parse_step_results(output)
    page_elements = parse_page_dumps(output)
    interaction_hints = parse_interaction_hints(output)

    log(f"Selectors found: {len(selectors)} | Steps passed: {len(steps_passed)} | "
        f"Steps failed: {len(steps_failed)} | Page dumps: {len(page_elements)} | "
        f"Interaction hints: {len(interaction_hints)}")

    if selectors:
        for name, sel in selectors.items():
            log(f"  {name} = {sel}")
    if interaction_hints:
        for h in interaction_hints:
            log(f"  HINT [{h['type']}] {h['name']} → {h['selector']} ({h['text']})")

    _write_result(
        selectors=selectors,
        steps_passed=steps_passed,
        steps_failed=steps_failed,
        page_elements=page_elements,
        interaction_hints=interaction_hints,
        skipped=False,
        reason=None,
        script_path=script_path,
        raw_output=output[-3000:]
    )


def _write_empty(reason: str) -> None:
    _write_result({}, [], [], page_elements={}, interaction_hints=[], skipped=True, reason=reason)


def _write_result(selectors, steps_passed, steps_failed,
                  page_elements=None, interaction_hints=None,
                  skipped=False, reason=None, script_path=None, raw_output="") -> None:
    data = {
        "skipped": skipped,
        "reason": reason,
        "selectors": selectors,
        "steps_passed": steps_passed,
        "steps_failed": steps_failed,
        "page_elements": page_elements or {},
        "interaction_hints": interaction_hints or [],
        "script_path": str(script_path) if script_path else None,
    }
    if raw_output:
        data["raw_output_tail"] = raw_output
    (AUDIT_DIR / "02-validate-web.json").write_text(json.dumps(data, indent=2))

    # Markdown summary
    lines = ["# Web Validation Results", ""]
    if skipped:
        lines.append(f"Skipped: {reason}")
    else:
        lines.append(f"Steps passed:  {len(steps_passed)}")
        lines.append(f"Steps failed:  {len(steps_failed)}")
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
