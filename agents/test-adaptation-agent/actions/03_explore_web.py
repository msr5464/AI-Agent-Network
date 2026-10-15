#!/usr/bin/env python3
"""
Step 03 (web half) — Explore the live product in a browser

The only step that touches a running application, and the only one whose output
later steps are allowed to treat as evidence. Everything the adapt step writes has
to correspond to something recorded here; that is what stops the model inventing
plausible steps.

Reuses the authoring agent's machinery — `call_claude_ex` with the Playwright MCP
server, the line-oriented marker protocol, emit-as-you-go, per-attempt scoring —
and changes four things that matter for running against a real product rather than
a scratch environment:

  * **`storage_state` for the entry login.** The browser starts signed in the way
    the test's own login leaves it. An expired session is a hard stop, not a
    fallback to signing in. A sign-in that is one of the test's own steps is
    walked like any other step, with the values in the test's data row.
  * **`.mcp.json` in the audit dir, not the repo root.** The root is shared mutable
    state; the server can run one agent while a developer runs another.
  * **Destructive actions are refused**, and a destructive action that *succeeded*
    is recorded as a protocol violation rather than a success.
  * **An ordered flow map** with page identity and a Python-recounted
    `match_count`, instead of a flat selector dictionary.

Reads:   $AUDIT_DIR/01-parse-change.json, $AUDIT_DIR/02-scope.json
Writes:  $AUDIT_DIR/03-explore-web.json + .md
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.log import log as _log
from shared import workspace as workspace_helper
def log(msg): _log("explore-web", msg)

from shared import browser_mode, entry_path, flow_map, lessons, mint_session, session_state
from shared.code_analyzer import read_source
from shared.claude import call_claude_ex
from shared.mcp_config import write_mcp_config, allowed_tools as mcp_allowed_tools, CAPTURE_RULES

from lib import check_changes

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
# Set in config/.env, no default here: run.sh stops the run when it is missing.
MODEL = os.environ.get("ADAPTATION_MODEL", "")
# This step drives a browser, so it takes the effort every agent's browser step
# shares; empty, it falls back to ADAPTATION_EFFORT.
EFFORT = os.environ.get("BROWSER_EFFORT") or os.environ.get("ADAPTATION_EFFORT") or None
TIMEOUT_S = int(os.environ.get("ADAPTATION_EXPLORE_TIMEOUT_S", "1800"))
ATTEMPTS = int(os.environ.get("ADAPTATION_EXPLORE_ATTEMPTS", "1"))
HEADLESS = browser_mode.headless()
SANDBOX = os.environ.get("ADAPTATION_SANDBOX", "false").lower() == "true"
# Minting is on by default. The hard stop exists to stop us exploring with a
# *bad* session — one that lands on a login page and makes the whole flow look
# changed. A freshly minted one is strictly better than stopping, and the
# credential still never reaches a prompt: it goes from the framework's own
# properties file into a local browser and nowhere else.
MINT = os.environ.get("ADAPT_MINT_SESSION", "true").lower() != "false"
SANDBOX_NOTE = os.environ.get("ADAPTATION_SANDBOX_NOTE", "")
RULES_FILE = REPO_ROOT / "config" / "prompts" / "explore.md"
SYSTEM_PROMPT = REPO_ROOT / "config" / "skills" / "automation-repo.md"


# Rule 4 is written twice in the rules file; one variant is sent.
_RULE_4 = re.compile(r"<!-- rule-4: default -->\n(.*?)<!-- rule-4: sandbox -->\n(.*?)"
                     r"<!-- rule-4: end -->\n?", re.DOTALL)


def load_explore_rules(sandbox_note: str = "") -> str:
    """The static rules, with the destructive-action rule that fits this run.

    Without a sandbox assertion the explorer refuses, and is told the words the
    checker flags so the two agree. With one, it is told to finish the flow.
    """
    if not RULES_FILE.exists():
        return "## Instructions\nExplore the flow and emit FLOW_STEP markers.\n"
    text = RULES_FILE.read_text(encoding="utf-8")
    marker = re.search(r"^## Instructions\s*$", text, re.MULTILINE)
    text = text[marker.start():] if marker else text

    def pick(match):
        if sandbox_note:
            return match.group(2).replace("{sandbox_note}", sandbox_note)
        return match.group(1).replace("{destructive_words}",
                                      ", ".join(flow_map.DESTRUCTIVE_WORDS))
    return _RULE_4.sub(pick, text)


def start_url(plan: dict, scope: dict, workspace) -> str:
    """Where the walk begins: the note's URL, else the one the framework is
    configured with for this module. "" when neither says.

    The properties file keys a module's URL by a name of its own, which is as
    often the test's package as the note's `Module:`, so both are tried.
    """
    if plan.get("web_base_url"):
        return plan["web_base_url"]
    names = [plan.get("module", "")]
    for test in (scope or {}).get("verify") or []:
        package = test.split("#", 1)[0].split(".")
        if len(package) >= 2:
            names.append(package[-2])
    for name in dict.fromkeys(n for n in names if n):
        url = mint_session.entry_url(workspace, name)
        if url:
            return url
    return ""


def walk_section(scope: dict) -> str:
    """The test's own step log and the values it types, as step 02 read them."""
    walks = (scope or {}).get("test_walks") or []
    steps = [w for w in walks if w.get("steps")]
    data = [w for w in walks if w.get("data")]
    out = ""
    if steps:
        out += ("\n## What the test does today\n"
                "Its own step log, in order. Walk these steps in this order, changing "
                "only what the change note says changes.\n")
        for walk in steps:
            out += f"\n`{walk['test']}`\n" + "\n".join(
                f"{n}. {text}" for n, text in enumerate(walk["steps"], 1)) + "\n"
    if data:
        out += ("\n## The values the test types\n"
                "Read from the test's own data: a row it loads, an object it builds, "
                "the login its helper signs in with. Type these, exactly, except "
                "where the change note gives a new value for one.\n")
        for walk in data:
            for row in walk["data"]:
                out += (f"\n`{row['file']}`"
                        + (f" — typed by `{row['typed_by']}`" if row.get("typed_by") else "")
                        + "\n" + "\n".join(f"- {column}: {value}"
                                           for column, value in row["row"].items()) + "\n")
    return out


def known_locators(sources: list) -> list:
    """Every exact locator the in-scope page objects declare, as
    `{owner, path, name, selector}` — what the browser helpers count live.

    `selector` is the locator as the browser can count it, which the framework
    plugin decides (`live_selector`). An approximated one (a selector assembled
    at runtime) is left out: counting the approximation would report a working
    locator as broken. One no field declares is marked `inline`.
    """
    from shared.frameworks import get_active_plugin
    code = get_active_plugin().code
    out = []
    for source in sources:
        owner = Path(source["path"]).stem
        for loc in code.extract_locators(source["snippet"]) or []:
            selector = code.live_selector(loc)
            if selector:
                out.append({"owner": owner, "path": source["path"],
                            # An inline locator (`page.locator(".row").count()`)
                            # has no field name; its selector is what names it.
                            "name": loc.get("name") or selector,
                            "selector": selector,
                            **({} if loc.get("name") else {"inline": True})})
    return out


def uncovered_items(plan: dict, flow: dict) -> list:
    """Change items that add a control, whose controls the walk left unproven.

    `[{"index", "controls", "unverified"}]`. An edit may be justified only by a
    step whose selector was counted unique, so an item none of whose controls
    has one will be declined however much of it the browser did: a walk filled
    two new fields, read them back, and reported a step for neither. Listed too
    is a control a step did act on under a selector nothing counted.

    Only for kinds whose work is adding interactions, and only when the walk was
    not stopped: past a refusal, a second walk stops at the same control.
    """
    if flow.get("refusals") or flow.get("violations") or flow.get("unreachable"):
        return []
    found = []
    for item in plan.get("items") or []:
        if item.get("kind") not in check_changes.ADDITIVE:
            continue
        evidence = flow_map.evidence_for(flow, item.get("nouns") or [],
                                         item.get("page_hint") or "")
        if evidence is None:
            continue
        lacking = (evidence["controls"] if not evidence["verified"] else
                   [noun for noun, steps in evidence["by_control"].items()
                    if steps["unverified"] and not steps["verified"]])
        if lacking:
            found.append({"index": item["index"], "controls": lacking,
                          "unverified": evidence["unverified"]})
    return found


def evidence_note(uncovered: list, unreported: list) -> str:
    """What a second walk is told when the first left change items unproven."""
    lines = ["\n## Your last walk left change items without evidence",
             "A test edit can only be justified by a FLOW_STEP whose selector was "
             "counted on exactly one visible element. These items have none for the "
             "controls they name:"]
    for entry in uncovered:
        steps = ", ".join(str(n) for n in entry["unverified"])
        lines.append(f"- item {entry['index']}: {', '.join(entry['controls'])} — "
                     + (f"step {steps} acted on one under a selector nothing counted"
                        if steps else "no step reported acting on any of them"))
    if unreported:
        lines += ["", "The browser recorded these being done, with no FLOW_STEP for them:"]
        lines += [f"- {a['kind']} {a['name']}" + (f" (on {a['page']})" if a.get("page") else "")
                  for a in unreported[:flow_map.UNREPORTED_SHOWN]]
    lines += ["", "Walk the flow again from the start. Act on every control by naming "
                  "its selector — `page.qa.step({ click: '<selector>' }, …)`, "
                  "`{ fill: '<selector>', value: '…' }` — so it is counted as it is "
                  "used, and write one FLOW_STEP for every control you act on, with "
                  "that selector. Each field of a form is a step of its own.", ""]
    return "\n".join(lines)


def locators_section(known: list) -> str:
    """The page objects' own locators, by page object, for the explorer to act
    through on the steps the test already makes.

    The helpers count these on every page; the explorer was never shown them.
    Left to find each existing control again, a walk met two Save buttons under
    one selector, took the second by position, and saved a different section:
    nothing it had typed persisted, and every check on it read as failing.
    Inline ones are left out: no field names them, so nothing says what for.
    """
    owners = {}
    for entry in known or []:
        if not entry.get("inline"):
            owners.setdefault(entry["owner"], []).append(entry)
    if not owners:
        return ""
    out = ("\n## The locators the tests use today\n"
           "The page objects' own, by page object. Where a step the test already makes "
           "acts on one of these controls, act through this locator when it matches "
           "exactly one element.\n")
    for owner, entries in sorted(owners.items()):
        out += f"\n`{owner}`\n" + "\n".join(
            f"- {entry['name']}: `{entry['selector']}`" for entry in entries) + "\n"
    return out


def checks_section(scope: dict) -> str:
    """What the named tests check today, for the explorer to judge on the new flow.

    Step 04 rebuilds this list from the same frozen contracts, so the ids the
    explorer reports against are the ids the adapt step looks up.
    """
    listed = check_changes.explore_checks(scope or {})
    if not listed:
        return ""
    return ("\n## The checks the tests make today\n"
            "Judge each one you can on the new flow — see \"What the tests check\" "
            "below.\n" + "\n".join(check_changes.list_lines(listed)) + "\n")


def build_prompt(plan: dict, rules: str, attempt_notes: str, stop_before: str,
                 scope: dict = None, start: dict = None, known: list = None) -> str:
    """`start` is `{"url", "signed_in"}` — where the walk begins and whether the
    browser opens with a saved session. `known` is `known_locators()`."""
    start = start or {}
    # GET /agents/<a>/artifact serves from the audit dir; the workspace's
    # test-output does not survive the automation repo being re-cloned.
    shots = AUDIT_DIR / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    items = "\n".join(f"{i['index']}. [{i['kind']}] {i['text']}"
                      for i in plan.get("items", []))
    destructive_note = ""
    if stop_before:
        destructive_note = f"""
## ⚠️ This flow ends in something that cannot be undone

The stated outcome involves **{stop_before}**. Walk the flow up to but NOT
including that final action. When you reach it, emit:

    REFUSED: <index>|<the control>|destructive_outcome
    UNREACHABLE_STATE: <the step before it>|{stop_before}

and stop. A human will confirm that last step. Do not look for a way around this.
"""
    signed = ("The browser is already signed in through a saved session."
              if start.get("signed_in") else
              "The browser starts signed out: no saved session is loaded for this test.")
    return f"""You are exploring a web application to record how one of its flows works NOW.

## The product change (written by a human on the team)
{plan.get('note', '')}

## The change items, already classified
{items}

## Where to put screenshots
SCREENSHOT DIR: {shots}
Anything you save elsewhere cannot be shown next to the run.

## Where to start
URL: {start.get('url') or plan.get('web_base_url') or '(use the module entry point)'}
{signed}
{walk_section(scope)}{locators_section(known)}
## What the flow is supposed to achieve
{plan.get('expected_outcome') or '(not stated — record what the flow does)'}
{checks_section(scope)}{destructive_note}{attempt_notes}
{rules}
## Frames and brief screens
{CAPTURE_RULES}
"""


def inventory_note(uninventoried: list) -> str:
    """What a second walk is told when the first left pages it acted on unmeasured.

    It names both calls that measure a page. Told only about the step call, a walk
    whose unmeasured page was a screen that closes by itself had no way to fix it,
    and one such walk came back with no FLOW_STEP lines at all.
    """
    return ("\n## You skipped the inventory\nYou emitted PAGE_ENTER for "
            + ", ".join(uninventoried)
            + " but never measured them. Make every step one "
              "page.qa.step(action, { page: '<pageId>' }) call, with the "
              "pageId of the page the step lands on — the helpers inventory "
              "that page and count its selectors for you. A screen that "
              "closes by itself is measured only by the call that records "
              "it, so name it there: page.qa.record(action, ms, "
              "{ page: '<pageId>' }). Without it the selectors you report "
              "cannot be verified and the page cannot be matched to its "
              "page object.\n\nWalk the flow as before otherwise: a "
              "PAGE_ENTER for every page and a FLOW_STEP for every action. "
              "A walk with no FLOW_STEP lines records nothing.\n")


def silent_note() -> str:
    """What a second walk is told when the first drove the browser and reported
    no step. Two walks did that: one never wrote a step line, the other left them
    all for one block at the end."""
    return ("\n## You reported no steps\nYour last walk drove the browser — its "
            "pages were measured — but wrote no FLOW_STEP line, so nothing it did "
            "was recorded. Walk the flow again. After every action, the moment it "
            "returns, write its line exactly as `FLOW_STEP: {...}` on one line, "
            "and a PAGE_ENTER for every page you reach. Do not leave them for a "
            "summary at the end.\n")


def run_attempt(plan: dict, rules: str, notes: str, mcp_path: Path,
                stop_before: str, scope: dict = None, evidence_path: Path = None,
                start: dict = None, sandbox: bool = False, known: list = None) -> tuple:
    """One exploration. Returns (flow, ClaudeResult).

    The flow map is built from the model's markers AND what the browser helpers
    measured (`evidence_path`): inventories and live counts it never had to type.
    `sandbox` — the explorer was told to finish the flow, so a flagged control it
    walked is not a violation.
    """
    seen = {"steps": 0}

    def on_output(_label, line):
        text = (line or "").strip().strip("`")
        if text.startswith("FLOW_STEP:"):
            seen["steps"] += 1
            log(f"    step {seen['steps']}")
        elif text.startswith(("REFUSED:", "UNREACHABLE_STATE:", "VALUE_CHECK:")):
            log(f"    {text[:110]}")
        # What the browser is actually doing, whether its server ever came up, and
        # whether the API is making us wait. Without these, half an hour of
        # exploration prints a handful of step lines and is indistinguishable from
        # a hang — which is why the authoring agent already surfaces them
        # (03_generate.py, 02_validate_web.py). The decoder emits them; only this
        # filter was dropping them.
        elif text.startswith(("→ ", "MCP server", "API retry")):
            log(f"    {text[:110]}")

    log("  Sending to Claude for exploration...")
    result = call_claude_ex(
        prompt=build_prompt(plan, rules, notes, stop_before, scope, start, known)
        + lessons.block("test-adaptation-agent", log=log),
        model=MODEL, effort=EFFORT, cwd=str(REPO_ROOT), timeout=TIMEOUT_S,
        on_output=on_output, log_dir=str(AUDIT_DIR),
        allowed_tools=mcp_allowed_tools(),
        # The browser and nothing else. allowed_tools only gates permission, and
        # the user's own allow rules still admit built-ins past it: explorers have
        # grepped the framework's properties files, where its credentials live,
        # read test sources, and tried to install a browser from Bash. The prompt
        # already carries every source this step needs.
        tools="",
        disable_slash_commands=True,
        mcp_config=str(mcp_path), strict_mcp_config=True,
        stream_json=True,
        system_prompt_file=str(SYSTEM_PROMPT) if SYSTEM_PROMPT.exists() else None,
    )
    evidence = flow_map.read_evidence(evidence_path) if evidence_path else []
    if evidence:
        log(f"  {len(evidence)} measured page state(s) from the browser helpers")
    flow = flow_map.build(result.stdout or "", evidence, sandbox=sandbox)
    if result.status != "ok" and flow["status"] == "ok":
        flow["status"] = "partial"
    return flow, result


def write(result: dict):
    (AUDIT_DIR / "03-explore-web.json").write_text(json.dumps(result, indent=2))
    md = ["# Explore — Web", "", f"Status: **{result['status']}**"]
    if result.get("reason"):
        md.append(f"\n{result['reason']}")
    if result.get("flow", {}).get("steps"):
        md += ["", flow_map.describe(result["flow"])]
    (AUDIT_DIR / "03-explore-web.md").write_text("\n".join(md) + "\n")


def main():
    plan = json.loads((AUDIT_DIR / "01-parse-change.json").read_text())
    scope_path = AUDIT_DIR / "02-scope.json"
    scope = json.loads(scope_path.read_text()) if scope_path.exists() else {}

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ran": False, "status": "skipped", "reason": "", "attempts": 0,
        "flow": {"steps": [], "pages": {}}, "unexplained_failures": [],
    }

    if plan.get("type") not in ("web", "both"):
        result["reason"] = f"Type: {plan.get('type')} — no web half to explore"
        log(result["reason"]); write(result); return
    if scope.get("skipped"):
        result["reason"] = "scope was skipped"
        write(result); return

    workspace = workspace_helper.resume_workspace(scope.get("workspace", ""), log=log)
    module = plan.get("module", "")

    # What the test under adaptation does to sign itself in, measured in step 02.
    # Three shapes, and only one of them involves minting anything.
    entry = scope.get("entry_path") or {"mode": "none",
                                        "reason": "step 02 recorded no entry path"}
    log(f"Entry path (from {entry.get('test', 'the test in scope')}): "
        f"{entry_path.describe(entry)}")
    result["entry_path"] = entry

    if entry.get("mode") == "none":
        # A test that loads no session does not need one, and demanding one is how
        # this step used to refuse flows it could have explored.
        log("No saved session to load — "
            + ("signing in is one of the test's own steps, walked with the values "
               "its helper holds" if entry.get("login_call") else
               "the test signs in nowhere, so the walk starts signed out"))
        session = {"ok": True, "path": None, "report": {}, "reason": ""}
    else:
        session = session_state.usable(workspace, module)

    if session["ok"] and session.get("path") is None:
        pass                                   # unauthenticated, nothing to report
    elif not session["ok"] and MINT:
        log(f"No usable session: {session['reason']}")
        log("Establishing one the way the test does…")
        minted = mint_session.mint(workspace, module, entry, headless=HEADLESS,
                                   log=log)
        if minted["ok"] and not minted.get("minted"):
            log(f"  reusing {Path(minted['path']).name} — the file the test loads")
            session = session_state.usable(workspace, module)
        elif minted["ok"]:
            log(f"  signed in, landed on {minted.get('landed_on', '?')} "
                f"({minted.get('cookies', 0)} cookies)")
            if minted.get("degraded"):
                # Authenticated, but the login helper still threw — almost always
                # because the page it lands on is the one being adapted. Worth
                # saying out loud: it is evidence, not noise.
                log(f"  ⚠️  the login authenticated but its landing page did not "
                    f"load: {minted['post_login_error']}")
                result["post_login_error"] = minted["post_login_error"]
            log(f"  saved {Path(minted['path']).name} — the framework reads the "
                f"same file")
            session = session_state.usable(workspace, module)
        else:
            log(f"  could not establish a session: {minted['reason']}")
            session = {**session, "mint_error": minted["reason"]}

    if not session["ok"]:
        # Lead with what was actually tried. "There is no session file" is the
        # symptom; the reason establishing one failed is the finding.
        reason = session.get("mint_error") or session["reason"]
        result.update({"status": "no_session", "reason": reason})
        log(f"STOP: {reason}")
        (AUDIT_DIR / ".skip-reason").write_text("no-session")
        write(result); return
    if session.get("path"):
        log(f"Session: {session['path'].name} "
            f"({session['report'].get('cookies', 0)} cookies, valid)")

    # The assertion covers the whole walk, not only a destructive last step: a
    # flow can pass through controls the checker flags by name long before its
    # outcome, and the checker has to be told as well as the explorer.
    sandbox_note = SANDBOX_NOTE.strip() if SANDBOX else ""
    if SANDBOX and not sandbox_note:
        log("ADAPTATION_SANDBOX=true is ignored — ADAPTATION_SANDBOX_NOTE is empty, "
            "and the assertion needs its reason")
    stop_before = ""
    if sandbox_note:
        log(f"ADAPTATION_SANDBOX=true — operator asserts a disposable environment: "
            f"{sandbox_note}")
        result["sandbox_note"] = sandbox_note
    elif plan.get("outcome_is_destructive"):
        stop_before = plan.get("destructive_token", "the final action")
        log(f"Flow ends in '{stop_before}' — exploration will stop before it "
            f"and the terminal step will be escalated, not adapted")

    start = {"url": start_url(plan, scope, workspace),
             "signed_in": bool(session.get("path"))}
    if start["url"]:
        log(f"Start URL: {start['url']}"
            + ("" if plan.get("web_base_url") else " (the module's configured URL)"))
    else:
        log("WARNING: no start URL — the note has no URL: line and the framework's "
            "properties name none for this module, so the explorer has to find the "
            "entry point itself")

    # Every page object in scope, not just the ones step 02's noun filter
    # nominated: measurement is cheap, and narrowing it to the guess would leave a
    # page whose page object the guess missed with nothing to match against.
    by_path = {c["path"]: c for c in (scope.get("edit_candidates") or [])
               if c.get("role") == "page_object"}
    for candidate in (scope.get("page_object_candidates") or []):
        by_path.setdefault(candidate["path"], candidate)
    sources = []
    for candidate in by_path.values():
        snippet = read_source(workspace / candidate["path"])
        if snippet:
            sources.append({"path": candidate["path"], "snippet": snippet})

    # Their locators, for the browser helpers to count live on every page state:
    # which still resolve and which broke is the question this agent answers.
    known_path = AUDIT_DIR / "03-known-locators.json"
    known = known_locators(sources)
    known_path.write_text(json.dumps(known, indent=2))
    log(f"{len(known)} locator(s) from {len(sources)} page object(s) will be counted live "
        f"on every page")

    rules = load_explore_rules(sandbox_note)
    best, best_score, notes = None, None, ""
    for attempt in range(1, ATTEMPTS + 2):
        log(f"Exploration attempt {attempt}/{ATTEMPTS + 1} (budget {TIMEOUT_S}s)")
        # A retry is a fresh browser: its measurements go in a file of their own.
        evidence_path = AUDIT_DIR / f"03-explore-evidence-{attempt}.jsonl"
        evidence_path.unlink(missing_ok=True)
        # .mcp.json goes in the audit dir: the repo root is shared mutable state,
        # and the server can be running another agent against it at the same time.
        mcp_path = write_mcp_config(AUDIT_DIR, headless=HEADLESS,
                                    storage_state=session.get("path"),
                                    evidence_file=evidence_path,
                                    known_locators_file=known_path)
        if attempt == 1:
            log(f"Playwright MCP: {browser_mode.label(HEADLESS)}, "
                f"config {mcp_path.name}, "
                + ("storage-state reused (no credential in the prompt)"
                   if session.get("path") else "no saved session — the test loads none"))
        flow, claude = run_attempt(plan, rules, notes, mcp_path, stop_before, scope,
                                   evidence_path, start=start,
                                   sandbox=bool(sandbox_note), known=known)
        status, raw = claude.status, claude.stdout or ""
        if status == "usage_limit":
            # An empty flow map here would be read as "the flow could not be
            # walked", which is a finding about the product. This is a finding
            # about the account, so stop and leave the note queued.
            log("ERROR: Claude usage limit reached — exploration did not run")
            result.update({"ran": False, "status": "skipped", "attempts": attempt,
                           "reason": "Claude usage limit reached — re-run once it resets"})
            write(result)
            (AUDIT_DIR / ".skip-reason").write_text("infra")
            sys.exit(1)
        unavailable = claude.browser_unavailable()
        if unavailable:
            # The same kind of stop as the cap: a finding about this machine, not
            # the product. A browser that cannot launch was explored as "0 steps,
            # status empty", closed with a ✓, and moved the note to processed/.
            # A retry launches the same browser, so there is none.
            log(f"ERROR: no browser answered — {unavailable}\n"
                f"       → FIX: see the claude-*.log in this audit dir; a browser that "
                f"is not installed names its install command there")
            result.update({"ran": False, "status": "skipped", "attempts": attempt,
                           "reason": f"No browser answered — {unavailable}"})
            write(result)
            (AUDIT_DIR / ".skip-reason").write_text("infra")
            sys.exit(1)
        # A walk that proved more of the note outranks one with more steps: what
        # the run is for comes before how much of the flow was written down.
        uncovered = uncovered_items(plan, flow)
        score = flow_map.score(flow, status)
        score = score[:2] + (-len(uncovered),) + score[2:]
        log(f"  → {len(flow['steps'])} step(s), status {flow['status']}")
        for entry in uncovered:
            log(f"  WARNING: item {entry['index']} has no verified step for "
                f"{', '.join(entry['controls'])} — nothing an edit could be justified by")
        missing = flow_map.pages_without_inventory(flow)
        if missing:
            log(f"  WARNING: no inventory for {', '.join(missing)} — no "
                f"page.qa.step call named that page and no PAGE_STATE described it, "
                f"so its selectors cannot be verified and no page object matched")
        # Only a page a step acted on is worth a retry. One the run passed through
        # (the start page, with a saved session) has no selector to verify, and a
        # whole second attempt was spent on exactly that.
        uninventoried = [p for p in missing if p in flow_map.pages_acted_on(flow)]
        if missing and not uninventoried:
            log("  not retrying for it — no step acted on that page")
        if best is None or score > best_score:
            best, best_score = flow, score
            # Persist after every attempt: a cancel during attempt 2 must not
            # discard a complete flow map from attempt 1.
            result.update({"ran": True, "attempts": attempt,
                           "status": flow["status"], "flow": flow,
                           "uncovered_items": uncovered,
                           "raw_output_tail": raw[-3000:]})
            write(result)
        failures = [s for s in flow["steps"]
                    if (s.get("result") or {}).get("outcome") == "failed"]
        categories = {(s.get("result") or {}).get("category") for s in failures}
        # The browser was driven — pages were measured — and no step was reported.
        # Such a walk ended as "empty" and the run stopped for want of a flow map,
        # having paid for a walk that happened.
        silent = not flow["steps"] and bool(flow.get("_inventories"))
        if silent:
            log(f"  WARNING: the walk measured {len(flow['_inventories'])} page(s) "
                f"and reported no steps — nothing it did is recorded")
        if silent and flow.get("violations"):
            # The browser's own record shows what it clicked. Walked again, it
            # would click the same controls again.
            log("  not walking again — the browser recorded it acting on a control "
                "named as destructive")
            silent = False
        if attempt > ATTEMPTS:
            break
        if categories and categories <= {"login_failed", "skipped",
                                          "destructive_refused"} and not uninventoried:
            log("  not retrying — every failure was a login or a refusal, and a "
                "retry would reproduce both exactly")
            break
        if not failures and not uninventoried and not silent and not uncovered:
            break
        notes = ""
        if silent:
            notes += silent_note()
        if uncovered:
            # Worth a whole attempt for the same reason an uninventoried page is:
            # step 04 can only decline an item the walk left no verified step for.
            notes += evidence_note(uncovered, flow.get("unreported_actions") or [])
        if failures:
            # The other reasons to walk again each log a warning above. This one
            # said nothing, and a second attempt started for no reason a reader
            # of the console could find.
            log(f"  walking again — {len(failures)} step(s) failed: "
                + ", ".join(f"{s['action']['target'].get('name') or '?'} "
                            f"({(s.get('result') or {}).get('category') or 'no category'})"
                            for s in failures[:8]))
            notes += ("\n## Previous attempt\nThese steps failed; try a different "
                      "approach for them:\n"
                      + "\n".join(f"- {s['action']['target'].get('name')}: "
                                  f"{(s.get('result') or {}).get('category')}"
                                  for s in failures[:8]) + "\n")
        if uninventoried:
            # Worth a whole attempt: without an inventory nothing downstream can
            # verify a selector or recognise the page, so the walk is unusable
            # however complete it looked.
            notes += inventory_note(uninventoried)

    # Guess -> measure -> edit. Step 02 nominated page objects by name
    # similarity; now that the pages have actually been looked at, measure which
    # one each observed page really is, using the same coverage rule the healing
    # agent applies to a failure DOM. Step 04 then edits against the measurement
    # rather than the guess.
    flow = result["flow"]
    if sources and flow.get("pages"):
        flow_map.measure_page_objects(flow, sources)
        for page_id, page in sorted(flow["pages"].items()):
            best = page.get("best_page_object")
            broken = ", ".join((best or {}).get("broken") or [])
            log(f"  page {page_id} → "
                + (f"{best['name']} ({best['matched']}/{best['evaluable']} locators "
                   + ("resolve live" if best.get("live") else "matched") + ")"
                   + (f"; broken: {broken}" if broken else "") if best
                   else "no candidate page object matched what was reported"))
        write(result)

    described = " ".join(i["text"].lower() for i in plan.get("items", []))
    for step in flow.get("steps", []):
        outcome = (step.get("result") or {}).get("outcome")
        category = (step.get("result") or {}).get("category", "")
        if outcome != "failed" or category not in ("network_error",
                                                    "unexpected_content"):
            continue
        name = str((step.get("action") or {}).get("target", {})
                   .get("accessible_name", "")).lower()
        if name and name not in described:
            result["unexplained_failures"].append(
                {"index": step.get("index"), "target": name, "category": category,
                 "detail": (step.get("result") or {}).get("detail", "")})
    if result["unexplained_failures"]:
        log(f"⚠️  {len(result['unexplained_failures'])} failure(s) the change note "
            f"does not account for — a human asserted change A, which says nothing "
            f"about B. This escalates rather than adapts.")

    ok, problems = flow_map.validate(flow)
    if not ok:
        log("Flow map problems: " + "; ".join(problems[:4]))
        result["problems"] = problems
    # What the browser recorded being done that the explorer's step list leaves
    # out. Listed for whoever reads the run; not steps, and not shown to step 04.
    unreported = flow.get("unreported_actions") or []
    for action in unreported[:flow_map.UNREPORTED_SHOWN]:
        log(f"  done in the browser, not reported as a step: {action['kind']} "
            f"{action['name']}" + (f" (on {action['page']})" if action.get("page") else ""))
    if len(unreported) > flow_map.UNREPORTED_SHOWN:
        log(f"  … and {len(unreported) - flow_map.UNREPORTED_SHOWN} more")
    # Name what stopped the walk and what would let it through. "3 destructive
    # action(s)" sends the reader to the JSON to find out which.
    if flow.get("violations"):
        log(f"  flagged by name: {flow_map.violations_text(flow)}")
    if (flow.get("violations") or flow.get("refusals")) and not sandbox_note:
        log(f"  {flow_map.SANDBOX_HINT}")
    write(result)
    log(f"Wrote {AUDIT_DIR / '03-explore-web.json'}")


if __name__ == "__main__":
    main()
