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

  * **`storage_state`, never a password.** `shared/claude.py` writes the whole
    command line into its debug log, so a credential in the prompt is a credential
    on disk. An expired session is a hard stop, not a fallback to signing in.
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
def log(msg): _log("explore-web", msg)

from shared import entry_path, flow_map, mint_session, session_state
from shared.code_analyzer import read_source
from shared.claude import call_claude_ex
from shared.mcp_config import write_playwright_mcp_config

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
MODEL = os.environ.get("ADAPT_MODEL", "claude-opus-5")
TIMEOUT_S = int(os.environ.get("ADAPT_EXPLORE_TIMEOUT_S", "1800"))
ATTEMPTS = int(os.environ.get("ADAPT_EXPLORE_ATTEMPTS", "1"))
HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
SANDBOX = os.environ.get("ADAPT_SANDBOX", "false").lower() == "true"
# Minting is on by default. The hard stop exists to stop us exploring with a
# *bad* session — one that lands on a login page and makes the whole flow look
# changed. A freshly minted one is strictly better than stopping, and the
# credential still never reaches a prompt: it goes from the framework's own
# properties file into a local browser and nowhere else.
MINT = os.environ.get("ADAPT_MINT_SESSION", "true").lower() != "false"
SANDBOX_NOTE = os.environ.get("ADAPT_SANDBOX_NOTE", "")
RULES_FILE = REPO_ROOT / "config" / "prompts" / "explore.md"
SYSTEM_PROMPT = REPO_ROOT / "config" / "skills" / "automation-repo.md"


def load_explore_rules() -> str:
    if RULES_FILE.exists():
        text = RULES_FILE.read_text(encoding="utf-8")
        marker = re.search(r"^## Instructions\s*$", text, re.MULTILINE)
        return text[marker.start():] if marker else text
    return "## Instructions\nExplore the flow and emit FLOW_STEP markers.\n"


def build_prompt(plan: dict, rules: str, attempt_notes: str, stop_before: str) -> str:
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
    return f"""You are exploring a web application to record how one of its flows works NOW.

## The product change (written by a human on the team)
{plan.get('note_masked', '')}

## The change items, already classified
{items}

## Where to put screenshots
SCREENSHOT DIR: {shots}
Anything you save elsewhere cannot be shown next to the run.

## Where to start
URL: {plan.get('web_base_url') or '(use the module entry point)'}
The browser is already signed in through a saved session.

## What the flow is supposed to achieve
{plan.get('expected_outcome') or '(not stated — record what the flow does)'}
{destructive_note}{attempt_notes}
{rules}
"""


def run_attempt(plan: dict, rules: str, notes: str, mcp_path: Path,
                stop_before: str) -> tuple:
    """One exploration. Returns (flow, status, raw_stdout)."""
    seen = {"steps": 0}

    def on_output(_label, line):
        text = (line or "").strip()
        if text.startswith("FLOW_STEP:"):
            seen["steps"] += 1
            log(f"    step {seen['steps']}")
        elif text.startswith(("REFUSED:", "UNREACHABLE_STATE:")):
            log(f"    {text[:110]}")

    result = call_claude_ex(
        prompt=build_prompt(plan, rules, notes, stop_before),
        model=MODEL, cwd=str(REPO_ROOT), timeout=TIMEOUT_S,
        on_output=on_output, log_dir=str(AUDIT_DIR),
        allowed_tools=["mcp__playwright__*"],
        mcp_config=str(mcp_path), strict_mcp_config=True,
        stream_json=True,
        system_prompt_file=str(SYSTEM_PROMPT) if SYSTEM_PROMPT.exists() else None,
    )
    flow = flow_map.build(result.stdout or "")
    if result.status != "ok" and flow["status"] == "ok":
        flow["status"] = "partial"
    return flow, result.status, (result.stdout or "")


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

    workspace = Path(scope.get("workspace", ""))
    module = plan.get("module", "")

    # What the test under adaptation does to sign itself in, measured in step 02.
    # Three shapes, and only one of them involves minting anything.
    entry = scope.get("entry_path") or {"mode": "none",
                                        "reason": "step 02 recorded no entry path"}
    log(f"Entry path (from {entry.get('test', 'the test in scope')}): "
        f"{entry_path.describe(entry)}")
    result["entry_path"] = entry

    if entry.get("mode") == "none":
        # A test that never signs in does not need a session, and demanding one
        # is how this step used to refuse flows it could have explored.
        log("The test signs in nowhere — exploring unauthenticated rather than "
            "stopping for a session it never uses")
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

    stop_before = ""
    if plan.get("outcome_is_destructive"):
        if SANDBOX and SANDBOX_NOTE:
            log(f"ADAPT_SANDBOX=true — operator asserts a disposable environment: "
                f"{SANDBOX_NOTE}")
            result["sandbox_note"] = SANDBOX_NOTE
        else:
            stop_before = plan.get("destructive_token", "the final action")
            log(f"Flow ends in '{stop_before}' — exploration will stop before it "
                f"and the terminal step will be escalated, not adapted")

    # .mcp.json goes in the audit dir: the repo root is shared mutable state, and
    # the server can be running another agent against it at the same time.
    mcp_path = write_playwright_mcp_config(AUDIT_DIR, headless=HEADLESS,
                                           storage_state=session.get("path"))
    log(f"Playwright MCP: {'headless' if HEADLESS else 'headed'}, "
        f"config {mcp_path.name}, "
        + ("storage-state reused (no credential in the prompt)"
           if session.get("path") else "no session — this flow does not sign in"))

    rules = load_explore_rules()
    best, best_score, notes = None, None, ""
    for attempt in range(1, max(1, ATTEMPTS + 1) + 0 or 1):
        log(f"Exploration attempt {attempt}/{ATTEMPTS + 1} (budget {TIMEOUT_S}s)")
        flow, status, raw = run_attempt(plan, rules, notes, mcp_path, stop_before)
        score = flow_map.score(flow, status)
        log(f"  → {len(flow['steps'])} step(s), status {flow['status']}")
        if best is None or score > best_score:
            best, best_score = flow, score
            # Persist after every attempt: a cancel during attempt 2 must not
            # discard a complete flow map from attempt 1.
            result.update({"ran": True, "attempts": attempt,
                           "status": flow["status"], "flow": flow,
                           "raw_output_tail": raw[-3000:]})
            write(result)
        failures = [s for s in flow["steps"]
                    if (s.get("result") or {}).get("outcome") == "failed"]
        categories = {(s.get("result") or {}).get("category") for s in failures}
        if attempt > ATTEMPTS:
            break
        if categories and categories <= {"login_failed", "skipped",
                                          "destructive_refused"}:
            log("  not retrying — every failure was a login or a refusal, and a "
                "retry would reproduce both exactly")
            break
        notes = ("\n## Previous attempt\nThese steps failed; try a different "
                 "approach for them:\n"
                 + "\n".join(f"- {s['action']['target'].get('name')}: "
                             f"{(s.get('result') or {}).get('category')}"
                             for s in failures[:8]) + "\n")

    # Guess -> measure -> edit. Step 02 nominated page objects by name
    # similarity; now that the pages have actually been looked at, measure which
    # one each observed page really is, using the same coverage rule the healing
    # agent applies to a failure DOM. Step 04 then edits against the measurement
    # rather than the guess.
    flow = result["flow"]
    # Measure against EVERY page object in scope, not just the ones step 02's
    # noun filter nominated. Measurement is cheap, and narrowing it to the guess
    # would mean a page whose page object the guess missed has nothing to match
    # against — which is the failure this whole step exists to remove.
    by_path = {c["path"]: c for c in (scope.get("edit_candidates") or [])
               if c.get("role") == "page_object"}
    for candidate in (scope.get("page_object_candidates") or []):
        by_path.setdefault(candidate["path"], candidate)

    sources = []
    for candidate in by_path.values():
        snippet = read_source(workspace / candidate["path"])
        if snippet:
            sources.append({"path": candidate["path"], "snippet": snippet})
    if sources and flow.get("pages"):
        flow_map.measure_page_objects(flow, sources)
        for page_id, page in sorted(flow["pages"].items()):
            best = page.get("best_page_object")
            log(f"  page {page_id} → "
                + (f"{best['name']} ({best['matched']}/{best['evaluable']} "
                   f"locators matched)" if best
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
    write(result)
    log(f"Wrote {AUDIT_DIR / '03-explore-web.json'}")


if __name__ == "__main__":
    main()
