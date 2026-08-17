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
import re
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

CLAUDE_CLI  = os.environ.get("CLAUDE_CLI_PATH", "claude")
MODEL       = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
# Per-action wait budget handed to Claude for individual browser interactions.
PW_TIMEOUT  = int(os.environ.get("PLAYWRIGHT_TIMEOUT_MS", "30000"))
PW_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
# Wall-clock budget for the whole validation run. A login-gated flow of 10+ steps
# on a heavy site routinely needs 15-25 minutes, so the default is generous;
# lower it for simple flows or CI.
VALIDATE_TIMEOUT = int(os.environ.get("VALIDATE_WEB_TIMEOUT_S", "1800"))
# Bounded self-heal: if a pass leaves failed steps worth retrying, re-run the
# WHOLE flow this many additional times (each call gets a fresh isolated
# browser — there is no mid-flow resume), feeding back what failed and why.
# 0 disables retries.
VALIDATE_RETRY_ATTEMPTS = int(os.environ.get("VALIDATE_WEB_RETRY_ATTEMPTS", "1"))

# ── Shared helpers ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(REPO_ROOT / "shared"))
from claude import call_claude_ex       # noqa: E402  (after sys.path update)
from mcp_config import write_playwright_mcp_config  # noqa: E402


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [02-validate-web] {msg}", flush=True)


def _fmt_budget(seconds: int) -> str:
    """Human-readable wall-clock budget, e.g. '30 minutes' / '45 seconds'."""
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"


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


# Categories Claude tags onto every STEP_FAILED (see the FAILURE PROTOCOL rule
# in the prompt). This replaces guessing the cause from Claude's free-text error
# after the fact — the model states the category itself, at the moment it has
# the most context to know which one applies.
CATEGORY_FIX_HINTS = {
    "selector_not_found": "Element not found — check login state, target URL, or "
        "whether the site's DOM structure changed. Check the PAGE_DUMP/screenshot "
        "recorded for this failure.",
    "login_failed": "Login failed — verify Username/Password in the queue input "
        "file, or check the screenshot for a CAPTCHA/2FA prompt.",
    "timeout": "Action timed out — the element may exist but load slowly, be "
        "hidden, or be off-screen. Consider raising PLAYWRIGHT_TIMEOUT_MS.",
    "overlay_blocking": "A cookie-consent banner, modal, or popup blocked "
        "interaction and could not be dismissed automatically — check the "
        "screenshot for what's covering the target element.",
    "network_error": "An API/network request failed — see the console/network "
        "summary in the failure detail. Likely a backend or environment issue, "
        "not a selector problem.",
    "unexpected_content": "Page content differed from what the step expected "
        "(different layout, A/B test, maintenance page, …) — check the "
        "screenshot.",
    "skipped": "Fix the login failure above; these steps will then run.",
    "other": "See the raw error and screenshot for details.",
}

# Fallback for STEP_FAILED lines that don't carry a category= tag — the model
# didn't follow the newer protocol. Same heuristics used before categories
# existed, kept only as a safety net so an out-of-format failure still gets
# some guidance instead of none.
_LEGACY_FIX_HEURISTICS = [
    (("TEST_USERNAME", "TEST_PASSWORD", "EXPECTED_USERNAME"),
     "Set credentials in your queue input file under Username/Password fields"),
    (("could not find", "not found"),
     "Element not found — check login state or target URL"),
    (("login did not succeed",),
     "Login failed — verify Username/Password in the queue input file"),
    (("skipped",),
     "Fix the login failure above; these steps will then run"),
]


def parse_failure_category(error_detail: str) -> str | None:
    """Extract `category=<name>` from a STEP_FAILED detail string, if present."""
    m = re.search(r"category=(\w+)", error_detail, re.IGNORECASE)
    if not m:
        return None
    candidate = m.group(1).lower()
    return candidate if candidate in CATEGORY_FIX_HINTS else None


def fix_hint_for(error_detail: str) -> str | None:
    """FIX suggestion for a STEP_FAILED detail string — category-tagged first,
    falling back to the legacy text-guess heuristics if untagged."""
    category = parse_failure_category(error_detail)
    if category:
        return CATEGORY_FIX_HINTS[category]
    lowered = error_detail.lower()
    for needles, hint in _LEGACY_FIX_HEURISTICS:
        if any(n.lower() in lowered for n in needles):
            return hint
    return None


def parse_page_dumps(output: str) -> dict:
    """Parse PAGE_DUMP: label|json_array lines from Claude output.

    Logs (rather than silently swallowing) a malformed dump — the model
    pretty-printing the JSON despite the single-line instruction is a real,
    observed failure mode, and a silent drop gives no operator-visible trace
    that evidence was captured but lost.
    """
    dumps = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("PAGE_DUMP:"):
            continue
        rest = line[len("PAGE_DUMP:"):].strip()
        if "|" not in rest:
            log(f"WARNING: malformed PAGE_DUMP (no '|' separator) — dropped: {rest[:100]}")
            continue
        label, json_part = rest.split("|", 1)
        try:
            dumps[label.strip()] = json.loads(json_part.strip())
        except json.JSONDecodeError as e:
            log(f"WARNING: PAGE_DUMP for '{label.strip()}' is not valid single-line "
                f"JSON (likely emitted pretty-printed across multiple lines) — dropped: {e}")
    return dumps


def parse_interaction_hints(output: str) -> list:
    """Parse INTERACTION_HINT: <json object> lines from Claude output.

    JSON rather than a pipe-delimited format: a selector or visible-text field
    legitimately containing a literal '|' (e.g. text='Buy 1 | Get 1 Free') used
    to silently corrupt a fixed-position split() instead of failing loudly.
    """
    hints = []
    required = {"type", "name", "selector", "text"}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("INTERACTION_HINT:"):
            continue
        rest = line[len("INTERACTION_HINT:"):].strip()
        try:
            obj = json.loads(rest)
        except json.JSONDecodeError as e:
            log(f"WARNING: malformed INTERACTION_HINT (not valid single-line JSON) — dropped: {e}")
            continue
        if not (isinstance(obj, dict) and required <= obj.keys()):
            log(f"WARNING: INTERACTION_HINT JSON missing required keys {required} — dropped: {rest[:150]}")
            continue
        hints.append({k: str(obj[k]).strip() for k in ("type", "name", "selector", "text")})
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

    # Credential check — warn if login steps detected but no structured credentials found.
    # Credentials may be inline in the step text (e.g. "login using username: foo, password: bar")
    # so Claude can still use them; only hard-fail if truly absent everywhere.
    demo_creds = plan.get("demo_credentials", {})
    login_keywords = ("login", "log in", "sign in", "signin", "authenticate")
    steps_need_login = any(
        any(kw in step.lower() for kw in login_keywords)
        for step in web_steps
    )
    _input_file = plan.get("_input_file") or os.environ.get("INPUT_FILE", "")
    raw_text = Path(_input_file).read_text() if _input_file and Path(_input_file).exists() else ""
    creds_inline = bool(
        re.search(r'username[:\s]+\S', raw_text, re.IGNORECASE) and
        re.search(r'password[:\s]+\S', raw_text, re.IGNORECASE)
    )
    if steps_need_login and not (demo_creds.get("username") and demo_creds.get("password")):
        if creds_inline:
            log("WARNING: Credentials found inline in steps — Claude will use them directly.")
        else:
            log("ERROR: Login step detected but no credentials found in input file.")
            log("Add credentials as top-level fields or inline in the step:")
            log("  Username: your_username")
            log("  Password: your_password")
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

    def build_prompt(attempt_notes: str = "") -> str:
        return f"""You are a QA automation agent. Use the Playwright browser MCP tools to validate a web user flow.

TARGET URL: {base_url}

STEPS TO EXECUTE:
{steps_numbered}
{creds_section}
{locators_hint}
{attempt_notes}

══════════════════════════════════════════════════════════════
OUTPUT PROTOCOL — emit these markers on their own lines:
══════════════════════════════════════════════════════════════
• After each step succeeds:
    STEP_PASSED: <step description>

• After each step fails (see FAILURE PROTOCOL, rule 6, for what to do FIRST):
    STEP_FAILED: <step description>|category=<CATEGORY>|<error details> [url=<current page URL>] [screenshot=<path>] [console=<summary>] [network=<summary>]
  CATEGORY must be exactly one of:
    selector_not_found | login_failed | timeout | overlay_blocking |
    network_error | unexpected_content | skipped | other
  (use skipped for LOGIN GATE cascades per rule 4d/6 below — never other)
  (e.g. STEP_FAILED: Click submit button|category=selector_not_found|no element matched [name='submit'] after 3 retries [url=https://example.com/checkout])

• Whenever you find a working selector/locator:
    SELECTOR_FOUND: <camelCaseName>=<actualSelector>
  (e.g. SELECTOR_FOUND: loginButton=[name='commit'])

• Whenever you interact with an element worth recording for later code generation:
    INTERACTION_HINT: <json object>
  Valid JSON on a SINGLE LINE with exactly these keys: "type" (one of input |
  button | link | dropdown | checkbox | other), "name" (camelCase), "selector",
  "text" (visible label). Using JSON (not a delimiter) means the selector or
  text may safely contain any character, including a literal | — do not use
  a | to separate fields.
  (e.g. INTERACTION_HINT: {{"type":"input","name":"resumeHeadline","selector":"[id='resumeHeadlineTxt']","text":"Resume Headline"}})

• On every STEP_FAILED, also emit a snapshot of the page at the moment of
  failure (see FAILURE PROTOCOL, rule 6):
    PAGE_DUMP: <step description>|<json array>
  The JSON array must be valid JSON on a SINGLE LINE (no pretty-printing, no
  embedded literal newlines) — up to 15 of the most relevant visible
  interactive elements. Each element is a JSON object with a "tag" key plus
  whichever of these identifying attributes the element actually has —
  OMIT keys it doesn't have, do not emit empty strings: "data-cy",
  "data-testid", "id", "name" (the element's literal name attribute, not a
  description), "aria-label", "placeholder", "type", "text" (visible text,
  truncated to ~40 chars).
  (e.g. PAGE_DUMP: Click submit button|[{{"tag":"button","data-cy":"submit-btn","text":"Submit"}}])

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

2b. OBSTRUCTIONS — before concluding an element is not found or not clickable,
   and before spending any of rule 3's retry budget on it:
   Check whether a cookie-consent banner, promotional/interstitial modal,
   "enable notifications" prompt, or newsletter popup is covering the page.
   If one is present, look for a dismiss control (commonly labeled Accept,
   Close, ✕, "No thanks", "Got it", or similar), click it ONCE, then proceed
   to the original action (which still gets its own separate rule-3 retry
   budget — these are two independent mechanisms, not stacked). Dismissing an
   obstruction is NOT itself a failure — only emit STEP_FAILED if the original
   action still fails afterward, using category=overlay_blocking and noting
   what you dismissed in the error detail.

3. RETRIES — if an element is not immediately found or visible:
   Wait 1 second and retry up to 3 times before declaring failure.
   Allow at most {PW_TIMEOUT}ms for any single browser action to complete;
   past that, treat the action as failed and move on rather than waiting longer.

3b. EMIT AS YOU GO — print each SELECTOR_FOUND / STEP_PASSED / STEP_FAILED marker
   the moment you have it, never batched at the end. This run has a hard
   wall-clock budget of {_fmt_budget(VALIDATE_TIMEOUT)}; if it is hit, only
   markers already printed can be salvaged.

4. LOGIN GATE — after clicking the sign-in / submit button:
   a) Wait for navigation to complete.
   b) Check whether the current URL still contains '/login', '/signin', or '/session'.
   c) If it does → mark the login step FAILED with:
      STEP_FAILED: <step>|category=login_failed|Login did not succeed — still on login page [url=<url>]
      Then mark an internal flag loginSucceeded=false.
   d) For every subsequent step that requires an authenticated session:
      If loginSucceeded is false, immediately output:
        STEP_FAILED: <step desc>|category=skipped|Skipped — login did not succeed, cannot proceed
      and move on (do NOT attempt any browser interactions for that step).

5. EVERY STEP in its own try/catch. Never abort the whole run on a single failure.

6. FAILURE PROTOCOL — the moment a step fails (after retries and obstruction
   handling above are exhausted), before moving to the next step:
   a) Take a screenshot with the screenshot tool and note its path.
   b) Check the browser console for errors; if any are relevant, summarize in
      one line (e.g. "console: TypeError at checkout.js:42").
   c) Check recent network requests for failed (4xx/5xx) responses relevant to
      this action; if any, summarize in one line (e.g. "network: POST /api/login → 401").
   d) Emit PAGE_DUMP for this step (see OUTPUT PROTOCOL above).
   e) Emit STEP_FAILED with category=, folding the screenshot path / console
      summary / network summary into the bracketed fields as shown in the
      OUTPUT PROTOCOL example.
   This applies to every failure, including cascaded "Skipped" failures from
   the LOGIN GATE — categorize those as category=skipped (screenshot/console/
   network capture is not needed for pure cascades).

7. Include the current page URL in every STEP_FAILED message.

8. After major page transitions (navigation, login, form submit), take an
   accessibility snapshot to understand the current page structure before
   attempting the next interaction.

9. Complete ALL steps — do not stop early unless the browser itself crashes.

Begin executing the steps now using the browser tools.
"""

    max_attempts = 1 + max(VALIDATE_RETRY_ATTEMPTS, 0)
    log(
        f"Calling Claude with Playwright MCP for {len(web_steps)} steps against "
        f"{base_url} (budget {_fmt_budget(VALIDATE_TIMEOUT)}/attempt, up to "
        f"{max_attempts} attempt(s))..."
    )

    def _on_output(label: str, line: str) -> None:
        if label == "stdout":
            log(f"  {line}")

    def _run_attempt(attempt_notes: str):
        return call_claude_ex(
            prompt=build_prompt(attempt_notes),
            model=MODEL,
            cwd=str(REPO_ROOT),
            timeout=VALIDATE_TIMEOUT,
            on_output=_on_output,
            log_dir=str(AUDIT_DIR),
            allowed_tools=["mcp__playwright__*"],
            # Load exactly the Playwright server written above and nothing else.
            # By default the subprocess also inherits the user's global MCP
            # servers, so it spends startup connecting to unrelated ones
            # (Google Drive, …) and searching a tool registry it will never use.
            mcp_config=str(mcp_path),
            strict_mcp_config=True,
            # Stream events as they happen — otherwise `claude -p` buffers
            # everything until exit and a long run looks frozen with no way to
            # tell it apart from a hang.
            stream_json=True,
        )

    def _parsed(output: str) -> dict:
        passed, failed = parse_step_results(output)
        return {
            "output":            output,
            "selectors":         parse_selector_output(output),
            "steps_passed":      passed,
            "steps_failed":      failed,
            "page_elements":     parse_page_dumps(output),
            "interaction_hints": parse_interaction_hints(output),
        }

    def _score(result, p: dict) -> tuple:
        # A cleanly completed attempt always beats one that crashed/timed out/
        # came back empty, however few failures the crashed one happened to
        # record — dying after 2 steps isn't "better" than running all 10 and
        # failing 2. Then: attempted more of the flow beats attempted less
        # (a thorough attempt with 1 failure beats a barely-started one with
        # 0, since raw failure count alone rewards giving up early). Then:
        # among equally-thorough attempts, fewer failures wins; final tiebreak
        # is more confirmed selectors. Bigger tuple sorts as "better" for max().
        total_seen = len(p["steps_passed"]) + len(p["steps_failed"])
        return (
            1 if result.status == "ok" else 0,
            total_seen,
            -len(p["steps_failed"]),
            len(p["selectors"]),
        )

    def _failure_detail(step_failed_line: str) -> str:
        return step_failed_line.split("|", 1)[1] if "|" in step_failed_line else step_failed_line

    def _worth_retrying(result_status: str, steps_failed: list) -> bool:
        """Skip the retry when it can't plausibly help.

        A pure login/cascade failure won't be fixed by running the identical
        flow again with the identical credentials — that needs a human to fix
        the input file, not another attempt.

        No STEP_FAILED markers at all means one of two very different things:
        every step genuinely passed (status == "ok" — nothing to retry), or
        the attempt crashed/timed out/came back empty before producing any
        markers (status != "ok") — exactly the scenario retries exist to
        recover from, so that case is always worth another try regardless of
        the (empty) failure list.
        """
        if not steps_failed:
            return result_status != "ok"
        categories = [parse_failure_category(_failure_detail(s)) for s in steps_failed]
        if all(c in ("login_failed", "skipped") for c in categories):
            return False
        return True

    attempts: list = []  # list of (result, parsed_dict)

    def _persist_snapshot() -> None:
        """Persist the best result seen so far. Called after every attempt —
        without this, nothing is written until the very end of main(), so a
        cancel arriving during attempt 2 would discard attempt 1's fully
        completed, perfectly usable results too, not just attempt 2's."""
        r, p = max(attempts, key=lambda ra: _score(ra[0], ra[1]))
        _write_result(
            selectors=p["selectors"],
            steps_passed=p["steps_passed"],
            steps_failed=p["steps_failed"],
            page_elements=p["page_elements"],
            interaction_hints=p["interaction_hints"],
            skipped=False,
            reason=None if r.ok else r.describe(),
            status=r.status,
            raw_output=p["output"][-3000:] if p["output"] else "",
            attempts=len(attempts),
        )

    attempt_notes = ""
    for attempt_num in range(1, max_attempts + 1):
        if attempt_num > 1:
            log(f"Retry attempt {attempt_num}/{max_attempts} — re-running the full "
                f"flow (fresh isolated browser; no mid-flow resume is possible)")
        result = _run_attempt(attempt_notes)
        parsed = _parsed(result.stdout)
        attempts.append((result, parsed))
        _persist_snapshot()

        # Report the actual cause rather than guessing. Each of these produces
        # an empty-or-short result for a completely different reason and needs
        # a different fix.
        if result.status == "timeout":
            log(f"WARNING: validation {result.describe()}")
            log(f"  → FIX: raise VALIDATE_WEB_TIMEOUT_S (currently {VALIDATE_TIMEOUT}s) "
                f"or split the flow into fewer steps")
            if result.stdout.strip():
                log("  Partial results from this attempt were recovered.")
        elif result.status == "error":
            log(f"WARNING: Claude {result.describe()}")
            log("  → FIX: check the claude-*.log in this audit dir for the CLI error")
        elif result.status == "empty":
            log(f"WARNING: Claude {result.describe()}")
            log("  → FIX: check model availability and that the Playwright MCP server "
                "connected (look for \"MCP server 'playwright'\" above)")

        if attempt_num < max_attempts and _worth_retrying(result.status, parsed["steps_failed"]):
            if parsed["steps_failed"]:
                notes = ["\nPRIOR ATTEMPT NOTES — a previous run of this exact flow failed "
                         "on the steps below. Apply the noted fix where relevant, but still "
                         "execute every step from the start (this is a fresh browser with no "
                         "session state carried over):"]
                notes.extend(f"  - {s}" for s in parsed["steps_failed"])
            else:
                notes = ["\nPRIOR ATTEMPT NOTES — a previous run of this exact flow did not "
                         f"complete ({result.describe()}) and produced no usable output. "
                         "Execute efficiently and emit markers as you go (rule 3b) so partial "
                         "progress is captured even if this attempt also runs out of budget."]
            attempt_notes = "\n".join(notes)
            continue
        break

    result, parsed = max(attempts, key=lambda ra: _score(ra[0], ra[1]))
    selectors          = parsed["selectors"]
    steps_passed       = parsed["steps_passed"]
    steps_failed       = parsed["steps_failed"]
    page_elements      = parsed["page_elements"]
    interaction_hints  = parsed["interaction_hints"]

    if len(attempts) > 1:
        log(f"Ran {len(attempts)} attempt(s); selected the best one "
            f"(status={result.status}, {len(steps_failed)} failed, "
            f"{len(steps_passed)} passed, {len(selectors)} selectors).")

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
                hint = fix_hint_for(error_msg)
                if hint:
                    log(f"  → FIX: {hint}")
            else:
                log(f"  FAIL: {s}")

    # Final authoritative write (same helper used after every attempt above —
    # single source of truth for what "the result" means).
    _persist_snapshot()


def _write_empty(reason: str) -> None:
    """Write a deliberately-empty result — the flow had nothing to validate.

    Distinct from a failed run: status stays "skipped" so step 03 can tell an
    intentional no-op apart from a validation that died before producing anything.
    """
    _write_result({}, [], [], page_elements={}, interaction_hints=[],
                  skipped=True, reason=reason, status="skipped", attempts=0)


def _write_result(selectors, steps_passed, steps_failed,
                  page_elements=None, interaction_hints=None,
                  skipped=False, reason=None, status="ok", raw_output="",
                  attempts=1) -> None:
    data = {
        "skipped":           skipped,
        "reason":            reason,
        "status":            status,
        "attempts":          attempts,
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
        lines.append(f"Outcome:         {status}")
        if reason:
            lines.append(f"Detail:          {reason}")
        lines.append(f"Attempts:        {attempts}")
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
