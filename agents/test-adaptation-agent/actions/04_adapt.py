#!/usr/bin/env python3
"""
Step 04 — Adapt

The only step that writes Java, and the only one that holds the guards.

Work is organised by **change item**, not by file. One item — "a workspace picker
now appears" — routinely spans a new page object, a helper method and a call site.
Applying those one file at a time leaves the repo uncompilable between writes and
makes rollback ambiguous, so each item is a transaction:

    snapshot every target → apply all edits → guards → compile → verify
                          → on any failure, restore every file

Compiling before running anything is not an optimisation. `00_reproduce.py`
classifies "cannot find symbol" as INFRA_BUILD, which routes to *skip, don't call
the model* — correct when the repo arrived broken, and completely wrong when our
own edit broke it. Compiling immediately after the edit tells the two apart: clean
before, broken after, is our fault.

`ADAPTATION_APPLY=false` (the default while the guards earn trust) runs everything up to
and including the guards, records the complete diff, and applies nothing.

Reads:   01-parse-change.json, 02-scope.json, 03-explore.json
Writes:  04-adapt.json + .md, .fix-passed, .snapshots.json (transient)
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.log import log as _log
from shared import workspace as workspace_helper
def log(msg): _log("adapt", msg)

from shared import (assertion_graph, code_analyzer, edit_guards, fix_history,
                    flow_map, url_properties, verdict_feedback)
from shared.claude import call_claude_ex as _call_claude_ex
from shared.code_analyzer import invalidate_file, read_source
from shared.test_runner import run_test

from lib import check_changes
from lib.transaction import Transaction

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
MODEL = os.environ.get("ADAPTATION_MODEL", "claude-opus-5")
ATTEMPT = int(os.environ.get("ADAPT_ATTEMPT", "1"))
APPLY = os.environ.get("ADAPTATION_APPLY", "false").lower() == "true"
MAX_FILES = int(os.environ.get("ADAPTATION_MAX_FILES_PER_RUN", "6"))
MAX_TOTAL_DIFF = int(os.environ.get("ADAPTATION_MAX_TOTAL_DIFF_LINES", "200"))
TEST_TIMEOUT_S = int(os.environ.get("ADAPTATION_TEST_TIMEOUT_S", "300"))
COMPILE_CMD = os.environ.get("ADAPTATION_TEST_COMPILE_CMD", "mvn -q test-compile -DskipTests")
RULES_FILE = REPO_ROOT / "config" / "prompts" / "adapt.md"
SYSTEM_PROMPT = REPO_ROOT / "config" / "skills" / "automation-repo.md"
# The automation repo's own CLAUDE.md goes into the prompt: the system prompt is
# framework-neutral and defers to it for APIs, and without it the model was
# writing Java for a framework it had never been shown.
MAX_CONVENTIONS_CHARS = 64000

# Per-edit-class budgets. The kind comes from the change note, so the authority an
# edit gets is decided by what a human said changed rather than by what the model
# would like to do.
DIFF_BUDGETS = {
    # 6 is right for replacing one selector string, and wrong for anything that
    # also adds an accessor — a real run had a correct edit rejected because
    # "add a locator for the new sort dropdown" was classified as `locator`.
    "locator": 6, "interaction": 20, "route": 10, "step_insert": 40,
    "step_merge": 40, "field_added": 60, "api_contract": 30, "test_data": 30,
    "page_object_new": 300,
    # Extra steps and checks usually mean new locators and accessors in a page
    # object plus the calls and assertions in the test — field_added's shape.
    # Changing or dropping steps and checks is the same size of edit.
    "coverage_added": 60, "coverage_changed": 60, "outcome_changed": 60,
    # A changed expected string is one literal, and should look like one.
    "content_changed": 10,
}
DEFAULT_BUDGET = 40
# A step_insert may legitimately touch a call site and a page object; the
# per-file budget alone would let it do that in six files and stay inside every
# individual limit. coverage_added touches the same two, at field_added's size.
CLUSTER_BUDGETS = {"step_insert": 80, "coverage_added": 120, "coverage_changed": 120,
                   "outcome_changed": 120}

# A URL belongs in the properties file the tests already read, never inline.
# Which framework wrapper a control type implies. An `interaction` change is only
# real if the code stops using one and starts using another.
_WRAPPER_FOR_KIND = {
    "select": "selectOption", "combobox": "fillText", "date": "fillText",
    "checkbox": "check", "radio": "check", "text": "fillText",
    "button": "click", "link": "click",
}


def load_adapt_rules() -> str:
    if RULES_FILE.exists():
        text = RULES_FILE.read_text(encoding="utf-8")
        marker = re.search(r"^## Instructions\s*$", text, re.MULTILINE)
        return text[marker.start():] if marker else text
    return "## Instructions\nUpdate the tests to match the product.\n"


def write_gate(value: str):
    (AUDIT_DIR / ".fix-passed").write_text(value)


LANDED = ("applied", "partial", "covered")


def carry_forward(result: dict) -> None:
    """Keep what earlier attempts landed and this one did not re-land.

    An earlier attempt's committed edit is still on disk — a later attempt only
    rolls back its own — but 04-adapt.json is rewritten per attempt and ship
    commits only what it lists. So a retry that answered "no edits" for an item
    left its edit out of the PR, and one that stopped early ("stuck", "nothing
    to change") listed no items at all, and no PR was raised.
    """
    if ATTEMPT <= 1:
        return
    try:
        earlier = json.loads((AUDIT_DIR / "04-adapt.json").read_text())
    except (OSError, ValueError):
        return
    if not earlier.get("applied_mode"):
        return
    kept = {i["index"]: {**i, "attempt": i.get("attempt", earlier.get("attempt"))}
            for i in earlier.get("items") or [] if i.get("status") in LANDED}
    if not kept:
        return
    items = []
    for item in result["items"]:
        old = kept.pop(item["index"], None)
        # A covered record commits nothing, so it never replaces an applied one.
        if old and (item.get("status") in ("applied", "partial")
                    or item.get("status") == old["status"] == "covered"):
            item["files"] = sorted(set(item.get("files") or []) | set(old.get("files") or []))
            item["check_changes"] = ((old.get("check_changes") or [])
                                     + (item.get("check_changes") or []))
        elif old:
            item = {**old, "retry": {"attempt": ATTEMPT, "status": item.get("status"),
                                     "reason": item.get("reason", "")}}
        items.append(item)
    result["items"] = sorted(items + list(kept.values()), key=lambda i: i["index"])
    if not result["verified"] and not result["failed"]:
        # Nothing of this attempt's own was verified, so the tree is the one the
        # earlier attempt's tests ran on.
        result["verified"] = earlier.get("verified") or []
        result["failed"] = earlier.get("failed") or []


def finish(result: dict, gate: str, skip_reason: str = ""):
    carry_forward(result)
    if not result.get("items"):
        result["status"] = "skipped"  # the UI greys the stage instead of calling it done
    result["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (AUDIT_DIR / "04-adapt.json").write_text(json.dumps(result, indent=2, default=str))
    (AUDIT_DIR / "04-adapt.md").write_text(render_md(result))
    write_gate(gate)
    if skip_reason:
        (AUDIT_DIR / ".skip-reason").write_text(skip_reason)
    log(f"Gate: .fix-passed = {gate}" + (f" ({skip_reason})" if skip_reason else ""))


def render_md(result: dict) -> str:
    md = ["# Adapt", "",
          f"Mode: **{'apply' if result.get('applied_mode') else 'propose-only'}** "
          f"— attempt {result.get('attempt')}", ""]
    if not result.get("applied_mode"):
        md += ["_Proposals are measured one item at a time, so an item that depends "
               "on an earlier item's edit is measured without it._", ""]
    for item in result.get("items", []):
        md += [f"## Item {item['index']} — `{item['kind']}` — {item['status']}"
               + (f" (from attempt {item['attempt']})" if item.get("attempt") else ""),
               "",
               item.get("summary") or item.get("reason") or "", ""]
        for guard in item.get("guards", []):
            mark = "✅" if guard["ok"] else "❌"
            md.append(f"- {mark} `{guard['guard']}` {guard.get('reason','')}")
        if item.get("diff"):
            md += ["", "```diff", item["diff"][:4000], "```", ""]
        if item.get("justification"):
            md += ["", "| edit | justified by flow step |", "|---|---|"]
            md += [f"| `{j['file']}` | {j['step']} |" for j in item["justification"]]
        if item.get("check_changes"):
            md += ["", "**Checks changed**", ""]
            md += check_changes.render_table(item["check_changes"])
        if item.get("unmeasured"):
            md += ["", "⚠️ Also edits values a check may read, which is not measured: "
                   + "; ".join(item["unmeasured"])]
        md.append("")
    if result.get("escalations"):
        md += ["## Escalations", ""]
        md += [f"- **{e['what']}** — {e['why']}" for e in result["escalations"]]
    return "\n".join(md) + "\n"


def excerpt(path: Path, limit: int = 8000) -> str:
    text = read_source(path) or ""
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def build_adapt_prompt(item: dict, plan: dict, scope: dict, flow: dict,
                       workspace: Path, rules: str, retry_note: str,
                       checks: list = None) -> str:
    # Order by what exploration MEASURED, not by what step 02 guessed. The file
    # a page actually turned out to be belongs at the top of the prompt; a
    # name-similarity guess that nothing corroborated belongs below it.
    measured = {}
    for page in (flow.get("pages") or {}).values():
        best = page.get("best_page_object")
        if best:
            measured[best["path"]] = best

    # A web change lands in page objects, so they outrank builders and API
    # clients even unmeasured: sorted by path alone, three API files once pushed
    # ProductsPage past the cut below.
    web = plan.get("type") in ("web", "both")
    candidates = sorted(
        scope.get("edit_candidates") or [],
        key=lambda c: (0 if c["path"] in measured else 1,
                       0 if web and c.get("role") == "page_object" else 1, c["path"]))
    # The tests under adaptation always go in: a step_insert edits the test body
    # itself, and leaving the file out read to the model as "not editable".
    tests = sorted({row["path"] for row in (scope.get("tiers") or {}).get("named", [])
                    if row.get("path") and (not web or row.get("is_web", True))})
    shown = candidates[:6] + [{"path": p, "role": "test"} for p in tests]
    files = "\n".join(
        f"\n### {workspace / c['path']}  ({c['role']})"
        + (f"  — **measured**: this is the page object for observed page "
           f"`{measured[c['path']]['name']}` "
           f"({measured[c['path']]['matched']}/{measured[c['path']]['evaluable']} "
           f"of its locators matched what the browser reported)"
           if c["path"] in measured
           else "  — a test under adaptation" if c["role"] == "test"
           else "  — nominated by name similarity only")
        + f"\n```java\n{excerpt(workspace / c['path'])}\n```"
        for c in shown)

    steps = [s for s in flow.get("steps") or []]
    flow_table = flow_map.describe({"steps": steps, "status": "ok",
                                    "refusals": flow.get("refusals") or [],
                                    "unreachable": flow.get("unreachable") or []})

    contracts = scope.get("intent_contracts") or {}
    # The checks as they are right now — measured just before this item, so an
    # earlier item's change shows as done rather than as still to do.
    if checks is None:
        checks = assertion_graph.merge(
            {t: {"asserts": c.get("_asserts") or {}} for t, c in contracts.items()})["checks"]
    found = check_changes.reports(flow)
    check_text = "\n".join(check_changes.list_lines(
        checks, extra=lambda c: (check_changes.fenced_report(found[c["id"]])
                                 if c["id"] in found else ""))) or "_No checks measured._"
    if item["kind"] in check_changes.CHECK_CHANGING:
        permission = (f"This item is `{item['kind']}`: it may remove or change the checks "
                      f"listed here, but only the ones you declare in `check_changes`. "
                      f"Every other check must still be made exactly as it is.")
    else:
        permission = (f"This item is `{item['kind']}`: it may not remove or change any "
                      f"check listed here. Adding checks is fine.")
    narrated = "\n".join(
        f"- **{t.split('.')[-1]}**: " + "; ".join(c.get("proves") or [])[:500]
        for t, c in list(contracts.items())[:5] if c.get("proves"))

    conventions = repo_conventions(workspace)
    conventions_section = (
        "\n## PROJECT CONVENTIONS — the automation repo's CLAUDE.md\n"
        "Its wrappers, waits, assertions and naming are the only APIs to use.\n\n"
        f"{conventions}\n" if conventions else "")

    page_object_map = flow_map.describe_page_objects(flow) or (
        "_Nothing measured — treat the files below as candidates, not confirmed "
        "matches._")

    return f"""You are updating automation tests because the product changed.

## The change item you are working on
**{item['index']}. [{item['kind']}] {item['text']}**
{item.get('rationale', '')}

## The full change note (written by a human)
{plan.get('note_masked', '')}

## 🗺️ MEASURED PAGE OBJECTS
Which page object each observed page turned out to be, measured against the
elements the browser reported — not inferred from the name.
{page_object_map}

## 🔎 FLOW MAP — what a browser actually observed just now
This is the evidence. Every interaction you add must correspond to a row here.
A row whose Unique? column is not `yes` justifies nothing.

{flow_table}

## 📜 WHAT THESE TESTS CHECK — measured just before this edit
{permission}

{check_text}

What the tests narrate (their logStep lines):
{narrated or '_none_'}

## Tests that must still pass
{chr(10).join('- ' + t for t in (scope.get('verify') or [])[:20])}

## Files you may edit
{files}
{conventions_section}{retry_note}
{rules}
"""


def repo_conventions(workspace: Path) -> str:
    """The automation repo's CLAUDE.md, or "" when it has none."""
    try:
        return (Path(workspace) / "CLAUDE.md").read_text(encoding="utf-8")[:MAX_CONVENTIONS_CHARS]
    except OSError:
        return ""


def done_section(done: list) -> str:
    """What earlier items of THIS attempt applied, for the items after them.

    Without it a later item sees an earlier item's passing edit in the file, reads
    it as a previous attempt's failed one, and declines — which escalates a change
    that is already done. The file excerpt is truncated at 8k, so the diff goes in
    too: the edit may not be visible in the file at all.
    """
    if not done:
        return ""
    parts = ["\n## ✅ Already applied earlier in THIS attempt — on disk, tests passed",
             "Lines starting with `-` no longer exist; anchor any `old_string` on the "
             "current file above, not on these diffs.", ""]
    # ponytail: last 4 only, keeps prompt growth ~8KB; cap by bytes if notes get long
    for d in done[-4:]:
        diff = d["diff"][:2000] + ("\n… (truncated)" if len(d["diff"]) > 2000 else "")
        parts += [f"### Item {d['index']}: {d['summary']}",
                  f"Verified: {', '.join(d['verified'])}",
                  f"```diff\n{diff}\n```", ""]
    parts.append("If one of these already does everything your item asks, return "
                 "`covered_by` with its number and no edits.\n")
    return "\n".join(parts)


def covering_item(payload: dict, done: list):
    """The earlier item this one claims already did its work — only if the claim holds.

    Checked, not trusted: a claim with edits attached, or naming an item this
    attempt did not apply and verify, is ignored and the response is handled
    exactly as it would be without it.
    """
    raw = str(payload.get("covered_by") or "").strip()
    # isdecimal, not isdigit: "²" is a digit, and int("²") raises mid-loop.
    if payload.get("edits") or not raw.isdecimal():
        return None
    return next((d for d in done if d["index"] == int(raw)), None)


_INTERACTION_TARGET = re.compile(
    r"\bElement\s*\.\s*\w+\s*\(\s*\w+\s*,\s*(\w+)"
    r"|\b(\w+)\s*\.\s*(?:click|select|enter|choose|goTo|open|add)\w*\s*\(")


def test_steps_from_source(scope: dict, workspace: Path) -> list:
    """What the tests currently do, as a list of interaction targets.

    Approximate on purpose: it exists to answer one question — has this change
    already been applied? — not to reconstruct the flow. A human-triggered agent
    gets re-run, and without this a second run inserts the workspace-picker step
    a second time.
    """
    steps, index = [], 0
    paths = [c["path"] for c in (scope.get("edit_candidates") or [])]
    paths += sorted({row.get("path") for tier in ("named", "shared_surface")
                     for row in (scope.get("tiers") or {}).get(tier, [])
                     if row.get("path")})
    for rel in paths:
        content = read_source(workspace / rel)
        if not content:
            continue
        for match in _INTERACTION_TARGET.finditer(content):
            target = match.group(1) or match.group(2)
            if not target or target in ("testConfig", "config", "this"):
                continue
            steps.append({"index": index, "target": target, "source": rel})
            index += 1
    return steps


# Shared with the authoring agent: tolerates an unclosed ```json fence and braces
# in the prose before the object, both of which the greedy regex here lost.
from shared.json_extract import extract_json  # noqa: E402


def run_guards(item: dict, edits_by_file: dict, snapshots: dict, flow: dict,
               scope: dict, workspace: Path, index_before: dict) -> list:
    """Every mechanical check, over the combined diff of one change item."""
    guards = []

    def add(name, ok, reason=""):
        guards.append({"guard": name, "ok": bool(ok), "reason": reason or ""})

    budget = DIFF_BUDGETS.get(item["kind"], DEFAULT_BUDGET)
    total_changed = 0
    for path, updated in edits_by_file.items():
        original = snapshots[path]
        name = Path(path).name
        is_new_page = item["kind"] == "page_object_new"
        ok, reason = edit_guards.validate_fix(original, updated, name, budget)
        # A brand-new page object has no prior content to silently drop, which is
        # the only thing the lost-method half of that guard protects against.
        if not ok and is_new_page and "lines (limit" not in reason:
            ok, reason = True, ""
        add(f"validate_fix[{name}]", ok, reason)

        add(f"no_new_swallowing[{name}]", *edit_guards.no_new_swallowing(original, updated))
        add(f"wrapper_compliance[{name}]", *edit_guards.wrapper_compliance(original, updated))
        is_test = name.endswith(("Test.java", "Tests.java", "Test.kt"))
        add(f"logstep_present[{name}]", *edit_guards.logstep_present(original, updated, is_test))
        add(f"steps_justified[{name}]",
            *edit_guards.steps_justified(original, updated, flow.get("steps") or []))
        total_changed += len([l for l in updated.splitlines()
                              if l not in original.splitlines()])

    # ── Per-class guards, from the plan's budget table ────────────────────────
    kind = item["kind"]
    added = "\n".join(
        line for path, updated in edits_by_file.items()
        for line in edit_guards._added_lines(snapshots[path], updated))

    if kind == "locator":
        # The same fit check healing applies: never broaden a selector, never
        # weaken a page-identity assertion into one that passes anywhere.
        for path, updated in edits_by_file.items():
            ok, reason = edit_guards.validate_diagnosis_fit(
                snapshots[path], updated, "LOCATOR_STALE", None)
            add(f"diagnosis_fit[{Path(path).name}]", ok, reason)

    # The anti-tautology check: an anchor that also resolves on the logged-out or
    # error page would pass there too, so it proves nothing about this flow.
    negatives = negative_documents(flow)
    if negatives and added.strip():
        add("matches_negative",
            *edit_guards.matches_negative(edit_guards._selectors_in(added), negatives))

    if kind == "route":
        # Same detector authoring generates against, so "no literal URLs" means
        # one thing across the network rather than one per agent.
        found = url_properties.hardcoded_urls(added)
        add("no_hardcoded_url", not found,
            f"added a literal URL {found[0][:60]} — routes belong in "
            f"parameters/*.properties, which the tests already read "
            f"(CONVENTIONS.md §13)" if found else "")

    if kind == "interaction":
        # "A <select> became a combobox" is only an interaction change if the
        # code actually changes how it drives the control. Otherwise it is a
        # locator edit wearing a bigger budget.
        kinds = {((s_.get("action") or {}).get("target") or {}).get("control_kind")
                 for s_ in (flow.get("steps") or [])}
        expected = {_WRAPPER_FOR_KIND.get(k) for k in kinds if k}
        expected.discard(None)
        add("wrapper_changed", bool(expected & set(re.findall(r"\b(\w+)\s*\(", added))),
            f"marked `interaction`, but no observed control type "
            f"({', '.join(sorted(k for k in kinds if k)) or 'none recorded'}) has "
            f"its wrapper in the added code — if only the selector changed this "
            f"is a `locator` edit" if not expected else "")

    if kind == "test_data":
        # A builder default is depended on by every test that constructs it —
        # a data blast radius, not a local edit.
        touched_default = any(
            Path(path).name.endswith(("Builder.java", "Data.java"))
            for path in edits_by_file)
        dependents_covered = len(scope.get("verify") or []) >= len(
            [r for tier in ("named", "shared_surface")
             for r in (scope.get("tiers") or {}).get(tier, [])])
        add("shared_default_covered", not touched_default or dependents_covered,
            "changes a shared Data/Builder default while some dependent tests are "
            "outside the verify set — every test that constructs this builder has "
            "to be re-run, or this escalates" if touched_default and not dependents_covered else "")

    cluster_budget = CLUSTER_BUDGETS.get(kind)
    add("cluster_diff_budget", cluster_budget is None or total_changed <= cluster_budget,
        f"{total_changed} changed lines across the item (cluster limit "
        f"{cluster_budget})" if cluster_budget and total_changed > cluster_budget else "")

    add("total_diff_budget", total_changed <= MAX_TOTAL_DIFF,
        f"{total_changed} changed lines across the item (limit {MAX_TOTAL_DIFF})"
        if total_changed > MAX_TOTAL_DIFF else "")
    add("file_budget", len(edits_by_file) <= MAX_FILES,
        f"{len(edits_by_file)} files (limit {MAX_FILES})"
        if len(edits_by_file) > MAX_FILES else "")
    return guards


def already_applied(diff: dict, items: list) -> bool:
    """Whether exploration saw nothing the tests do not already do — and that
    means the note is done.

    Only for items whose job is adding interactions. Removing a step, changing a
    check or adding a check adds no interaction, so any of those would always
    look done; notes with only unclassified items go on to escalate instead.
    """
    kinds = {i.get("kind") for i in items} - {check_changes.UNCLASSIFIED}
    return (bool(kinds) and not diff["added"]
            and not kinds & (check_changes.CHECK_CHANGING | {"coverage_added"}))


def judge_checks(item: dict, payload: dict, txn, scope: dict, workspace: Path,
                 flow: dict, before: dict, record: dict) -> tuple:
    """The `check_changes` guard, over an edit that is on disk. Returns (ok, why).

    Two measurements, because each sees what the other cannot: the call graph
    sees a check that disappeared because a step stopped calling a helper; the
    edited files show a check changed in code no in-scope test reaches.
    """
    after = check_changes.measure(scope, workspace)
    graph = assertion_graph.delta(before["checks"], after["checks"])
    file_before = check_changes.file_checks(txn.snapshots)
    files = assertion_graph.delta(file_before, check_changes.file_checks(txn.staged))
    listed = {c["id"]: c for c in file_before}
    listed.update({c["id"]: c for c in before["checks"]})

    touched = files["removed"] + [b for b, _ in files["changed"]]
    index = before["index"]

    def fingerprint(test: str) -> dict:
        klass, _, method = test.replace("#", ".").rpartition(".")
        return assertion_graph.fingerprints(klass, method, index,
                                            follow_constructors=True)

    # Outside means not re-run: a test measured but left out of the verify set
    # (same class as the named test, or shared surface under named_only) would
    # have its check changed with nothing to prove it still passes.
    outside = check_changes.reached_outside(
        touched, check_changes.enabled_tests(workspace) if touched else [],
        set(scope.get("verify") or []), fingerprint)
    ok, why, rows = check_changes.validate(
        payload.get("check_changes"), graph, files, item["kind"], listed,
        check_changes.reports(flow), outside)

    holes = sorted(set(after["unresolved"]) - set(before["unresolved"]))
    record["check_changes"] = rows
    record["unmeasured"] = check_changes.unmeasured_edits(txn.snapshots, txn.staged)
    record["new_unresolved"] = holes
    record.setdefault("guards", []).append(
        {"guard": "check_changes", "ok": ok, "reason": why})
    log(f"  {'OK' if ok else 'REJECT'} check_changes — "
        + (why or f"{len(rows)} change(s) listed")
        + (f"; PLAUSIBLE: {len(holes)} new call(s) could not be followed" if holes else ""))
    return ok, why


def log_rows(item: dict, rows: list) -> None:
    """Accepted check changes, kept across attempts for the PR's why column.

    Never cleared: the PR table is measured, so a row that matches nothing on
    disk any more is simply not used.
    """
    if not rows:
        return
    path = AUDIT_DIR / ".check-changes.json"
    try:
        logged = json.loads(path.read_text()) if path.exists() else []
    except ValueError:
        logged = []
    path.write_text(json.dumps(logged + [{**row, "item": item["index"]} for row in rows],
                               indent=2))


# Pages an adapted test must never be sitting on at the end of a step.
_NEGATIVE_PAGE_HINTS = ("login", "signin", "sign-in", "logged-out", "error",
                        "denied", "expired")


def negative_documents(flow: dict) -> list:
    """The pages this flow must NOT be on, as DOM the anti-tautology guard can query.

    `matches_negative` has been in the guard table since the start and has never
    had anything to compare against — the healing agent passes it an empty list —
    while the flow map has been carrying the logged-out page's own inventory the
    whole time. A selector that also matches the login page is not proof the flow
    got past it.
    """
    inventories = flow.get("_inventories") or {}
    pages = flow.get("pages") or {}
    docs = []
    for page_id, elements in inventories.items():
        page = pages.get(page_id) or {}
        identity = " ".join(str(v) for v in (page_id, page.get("url", ""),
                                             page.get("title", ""))).lower()
        if elements and any(hint in identity for hint in _NEGATIVE_PAGE_HINTS):
            try:
                docs.append(flow_map.document_from_inventory(elements))
            except Exception:
                continue
    return docs


def verify_proposal(txn, scope: dict, workspace: Path, record: dict,
                    judge=None) -> tuple:
    """Compile a proposal and re-measure its assertions. Returns (ok, why).

    Propose-only used to stop at the diff-shaped guards, so the diff handed to a
    human as the agent's recommendation had never been compiled and had never been
    checked against the frozen contracts — the one promise this agent makes. Both
    need the edit on disk, so the caller applies first and rolls back afterwards
    whatever the answer is.

    A compiler that cannot run at all is infra, not a bad proposal: recorded and
    passed, the same way the apply path treats it.
    """
    ok, output = txn.compile(workspace, COMPILE_CMD)
    if ok:
        record["compile_status"] = "compiles"
    elif output.startswith("could not run the compiler"):
        record["compile_status"] = f"not compiled — {output}"
        log(f"    (compiler unavailable: {output})")
    else:
        record["compile_output"] = output
        return False, "the proposal does not compile"

    if judge is None:
        # Nothing to hold the proposal to — no model answer, no contracts.
        record.setdefault("check_changes", [])
        return True, ""
    ok, why = judge()
    return (True, "") if ok else (False, why)


def compile_ok(workspace: Path) -> tuple:
    """Compile before running anything. Returns (ok, output)."""
    try:
        proc = subprocess.run(COMPILE_CMD.split(), cwd=str(workspace),
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run the compiler: {exc}"
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stdout or "") + (proc.stderr or "")
    return False, tail[-3000:]


def main():
    plan = json.loads((AUDIT_DIR / "01-parse-change.json").read_text())
    scope = json.loads((AUDIT_DIR / "02-scope.json").read_text())
    explore = json.loads((AUDIT_DIR / "03-explore.json").read_text())
    flow = explore.get("flow") or {}

    result = {"attempt": ATTEMPT, "applied_mode": APPLY, "items": [],
              "escalations": [], "verified": [], "failed": [], "proposed": []}

    if scope.get("skipped"):
        result["escalations"].append({"what": "scope", "why": scope.get("reason", "")})
        finish(result, "skipped", "no-work")
        return

    workspace = workspace_helper.resume_workspace(scope["workspace"], log=log)

    # Hard gates, before any model call.
    if explore.get("unexplained_failures"):
        for entry in explore["unexplained_failures"]:
            result["escalations"].append({
                "what": f"unexplained failure at step {entry.get('index')}",
                "why": (f"{entry.get('target') or entry.get('endpoint')} failed with "
                        f"{entry.get('category')}, and no line of the change note "
                        f"accounts for it. A human asserted one change; that says "
                        f"nothing about a second, unrelated defect.")})
        log(f"STOP: {len(explore['unexplained_failures'])} unexplained failure(s) — "
            f"this is the change-vs-bug gate, and it escalates rather than adapting")
        finish(result, "skipped", "escalate")
        return

    if explore.get("status") == "unsafe":
        result["escalations"].append({
            "what": "destructive action performed",
            "why": "exploration performed an action it should have refused, so its "
                   "evidence cannot be treated as side-effect free"})
        finish(result, "skipped", "unsafe")
        return

    if not (flow.get("steps") or []):
        result["escalations"].append({
            "what": "no flow map",
            "why": "exploration recorded no steps, so no edit could be justified by "
                   "an observation"})
        finish(result, "skipped", "unreachable")
        return

    # Idempotency. Without this a second run on the same note inserts the same
    # step twice, and this agent is triggered by hand — it *will* be re-run.
    current = test_steps_from_source(scope, workspace)
    diff = flow_map.diff_against_test(flow, current)
    result["flow_diff"] = diff
    log(f"Flow map: {len(flow['steps'])} step(s) observed, {len(diff['added'])} not "
        f"yet in the tests (which make {len(current)} interaction(s) today)")
    if already_applied(diff, plan["items"]):
        log("Every observed step already exists in the tests — this change looks "
            "applied already. Nothing to do.")
        result["escalations"].append({
            "what": "nothing to change",
            "why": "every step exploration observed already has a counterpart in "
                   "the tests; re-applying would duplicate it"})
        finish(result, "skipped", "no-work")
        return

    rules = load_adapt_rules()
    index_before = {}
    # Every attempt, not just the last one. 04-adapt.json is overwritten per
    # attempt, so reading it back showed attempt 3 only what attempt 2 did — and
    # left it free to re-propose the edit attempt 1 had already had rejected.
    history = fix_history.load(AUDIT_DIR)
    retry_note = ""
    if history:
        stop, why = fix_history.exhausted(history)
        if stop:
            log(f"Not attempting again — {why}")
            result["escalations"].append(
                {"what": "further attempts cannot differ", "why": why})
            finish(result, "skipped", "stuck")
            return
        # History is per attempt, not per item: on attempt 2 there has been one
        # failure, and saying "two" talked a later item out of a correct answer.
        failures = sum(1 for h in history if h.get("outcome") != fix_history.PASSED)
        retry_note = ("\n## ⚠️ What earlier attempts already tried\n"
                      + fix_history.render(history)
                      + ("\nTwo failed attempts are evidence the approach is wrong, "
                         "not a reason to try a wider edit." if failures >= 2 else "")
                      + "\nIf you cannot justify an edit from the flow map, "
                        "return adaptable: false.\n")

    # Decided by kind, not by the stored `escalate_only` flag: a session parsed
    # before outcome_changed/content_changed became actionable still carries it.
    actionable = [i for i in plan["items"] if i.get("kind") != check_changes.UNCLASSIFIED]
    for item in plan["items"]:
        if item.get("kind") == check_changes.UNCLASSIFIED:
            result["escalations"].append({
                "what": f"item {item['index']} (not classified): {item['text']}",
                "why": ("the classifier could not place this item, so it gets no "
                        "authority to edit — say more plainly what changed, or split "
                        "it into separate items")})
            result["items"].append({**item, "status": "escalated",
                                    "reason": result["escalations"][-1]["why"],
                                    "guards": []})
            log(f"Item {item['index']} [{item['kind']}] — escalated, not attempted")

    if not actionable:
        log("No actionable items — everything escalates to a human")
        finish(result, "skipped", "escalate")
        return

    done = []  # items this attempt applied and verified, shown to the items after them
    for item in actionable:
        log(f"Item {item['index']} [{item['kind']}] — {item['text']}")
        try:
            before = check_changes.measure(scope, workspace)
        except Exception as exc:  # noqa: BLE001 — fail closed: nothing to judge against
            why = f"could not measure the tests' checks before editing: {exc}"
            result["items"].append({**item, "status": "rejected", "reason": why,
                                    "guards": [{"guard": "check_changes", "ok": False,
                                                "reason": why}]})
            log(f"  REJECT {why}")
            continue
        log("  asking the model…")
        prompt = build_adapt_prompt(item, plan, scope, flow, workspace, rules,
                                    retry_note + done_section(done),
                                    checks=before["checks"])
        call = _call_claude_ex(prompt=prompt, model=MODEL, cwd=str(REPO_ROOT),
                               timeout=900, log_dir=str(AUDIT_DIR),
                               system_prompt_file=(str(SYSTEM_PROMPT)
                                                   if SYSTEM_PROMPT.exists() else None))
        if call.status != "ok":
            # The call did not happen: a usage cap, an API error, a timeout. None
            # of those are the change note's fault, so stop as infra and leave it
            # queued rather than consuming it and reporting its items as failures.
            # Status, not text: the text wrapper returns "" for every one of these,
            # which is how a 429 came to be reported to a human as "could not parse
            # the model's response as JSON".
            why = call.describe()
            log(f"ERROR: the model call {why}")
            result["escalations"].append(
                {"what": "the model call did not complete",
                 "why": f"{why} — nothing was attempted; re-run when it clears"})
            finish(result, "skipped", "infra")
            return

        payload = extract_json(call.stdout or "")
        record = {**item, "status": "failed", "guards": [], "reason": ""}

        if not payload:
            record["reason"] = "could not parse the model's response as JSON"
            result["items"].append(record); log(f"  ERROR: {record['reason']}"); continue
        covering = covering_item(payload, done)
        if covering and payload.get("check_changes"):
            why = ("covered_by came with check_changes — an item covered by another "
                   "makes no edits, so it cannot change a check")
            record.update({"status": "rejected", "reason": why,
                           "guards": [{"guard": "check_changes", "ok": False,
                                       "reason": why}]})
            result["items"].append(record)
            log(f"  REJECT {why}")
            continue
        if covering:
            # An earlier item's verified edit already does this one. Not an
            # escalation — but visible in the PR, Slack and the UI counts, since it
            # is the one outcome here that lands no edit of its own.
            record.update({"status": "covered", "covered_by": covering["index"],
                           "summary": f"Covered by item {covering['index']} (applied "
                                      f"and verified in this attempt): "
                                      f"{payload.get('summary') or ''}".strip()})
            result["items"].append(record)
            log(f"  covered by item {covering['index']} — no edit of its own")
            continue
        if not payload.get("adaptable", False):
            record.update({"status": "declined",
                           "reason": payload.get("unadaptable_reason")
                                     or "declared unadaptable"})
            result["escalations"].append({"what": f"item {item['index']}: {item['text']}",
                                          "why": record["reason"]})
            result["items"].append(record)
            log(f"  declined: {record['reason']}")
            continue

        edits = payload.get("edits") or []
        if not edits:
            # An answer rather than a failure — the model reports it cannot do this
            # from the files it can see. Staging an empty edit list would otherwise
            # produce an empty diff that passes every guard and reads as a proposal.
            record.update({"status": "failed", "no_edits": True,
                           "reason": "the model proposed no edits"})
            result["items"].append(record)
            log("  no edits proposed")
            continue
        record["summary"] = payload.get("summary", "")
        if record["summary"]:
            log(f"  plan: {record['summary']}")
        record["justification"] = [
            {"file": Path(e.get("file", "")).name, "step": e.get("justified_by")}
            for e in edits]

        # ── Transaction: all of this item's edits, or none of them ───────────
        txn = Transaction(AUDIT_DIR, log)
        failure = txn.stage(edits)
        if failure:
            record["reason"] = failure
            result["items"].append(record); log(f"  {failure}"); continue

        record["diff"] = txn.diff()
        record["files"] = sorted(txn.staged)
        # Content hashes, so a later attempt can tell "the same edit again" from
        # "a different idea" without storing whole files in the audit trail.
        record["fingerprint"] = fix_history.fingerprint(txn.staged, None)

        guards = run_guards(item, txn.staged, txn.snapshots, flow, scope, workspace,
                            index_before)
        record["guards"] = guards
        rejected = [g for g in guards if not g["ok"]]
        passed_guards = [g["guard"] for g in guards if g["ok"]]
        if passed_guards:
            log(f"  guards OK ({len(passed_guards)}): {', '.join(passed_guards)}")
        for guard in rejected:
            log(f"  REJECT {guard['guard']}"
                + (f" — {guard['reason']}" if guard["reason"] else ""))
        if rejected:
            record.update({"status": "rejected",
                           "reason": "; ".join(g["reason"] for g in rejected if g["reason"])})
            result["items"].append(record)
            continue

        if not APPLY:
            # Measured, then put back. "Nothing is written" is still true at the
            # end of this block — but a proposal that does not compile, or that
            # quietly drops an assertion, is now caught here rather than by the
            # human who trusted the diff.
            # try/finally, not two statements: a propose-only run is often working
            # in the developer's own checkout, and anything raising between the
            # write and the restore would leave our edit sitting in it.
            txn.apply()
            try:
                sound, why = verify_proposal(
                    txn, scope, workspace, record,
                    judge=lambda: judge_checks(item, payload, txn, scope, workspace,
                                               flow, before, record))
            except Exception as exc:                       # noqa: BLE001 — see above
                sound, why = False, f"could not verify the proposal: {exc}"
                record["guards"].append({"guard": "check_changes", "ok": False,
                                         "reason": why})
            finally:
                txn.rollback("propose-only — nothing is kept")
            if not sound:
                record.update({"status": "rejected", "reason": why})
                result["items"].append(record)
                log(f"  REJECT {why}")
                continue
            record["status"] = "proposed"
            result["proposed"].append({"item": item["index"], "diff": record["diff"],
                                       "summary": record["summary"]})
            # Recorded with accepted=None: nobody has judged it yet, and "not
            # reviewed" must not average in with "rejected". Whether people accept
            # proposals verbatim is the promotion criterion for turning ADAPTATION_APPLY
            # on, and it is only knowable if somebody writes it down.
            try:
                verdict_feedback.record_proposal(
                    AGENT_DIR / "feedback" / "proposals.json",
                    os.environ.get("SESSION_ID", ""), plan.get("module", ""),
                    item["index"], item["kind"], None, record["summary"][:200])
            except Exception as exc:
                log(f"  (could not record the proposal: {exc})")
            result["items"].append(record)
            log("  proposed (ADAPTATION_APPLY=false — nothing written)")
            continue

        txn.apply()
        log(f"  applied {len(txn.staged)} file(s)")

        ok, output = txn.compile(workspace, COMPILE_CMD)
        if not ok:
            txn.rollback("our edit broke compilation — the edit's fault, not an "
                         "infrastructure problem")
            record.update({"status": "rolled_back",
                           "reason": "our edit broke compilation",
                           "compile_output": output})
            result["items"].append(record)
            continue
        log("  compiles")

        try:
            sound, why = judge_checks(item, payload, txn, scope, workspace, flow,
                                      before, record)
        except Exception as exc:  # noqa: BLE001 — fail closed: an unmeasured edit is not safe
            sound, why = False, f"could not measure the checks after the edit: {exc}"
            record["guards"].append({"guard": "check_changes", "ok": False,
                                     "reason": why})
        if not sound:
            txn.rollback(why)
            record.update({"status": "rolled_back", "reason": why})
            result["items"].append(record)
            continue

        passed, failed = [], []
        for test in scope.get("verify") or []:
            log(f"  verifying {test}…")
            status, out = run_test(test, workspace, timeout_s=TEST_TIMEOUT_S, log=log)
            (passed if status == "passed" else failed).append((test, status, out))
            log(f"  {status}: {test.rpartition('#')[2]}")
        record["verified"] = [t for t, _, _ in passed]
        record["failed"] = [{"test": t, "status": s} for t, s, _ in failed]

        if failed and not passed:
            why = f"every verified test still fails ({len(failed)})"
            txn.rollback(why)
            record.update({"status": "rolled_back", "reason": why})
            result["items"].append(record)
            continue

        txn.commit()
        log_rows(item, record.get("check_changes"))
        record["status"] = "applied" if not failed else "partial"
        log(f"  {record['status']} — {len(passed)}/{len(passed) + len(failed)} "
            f"verified test(s) pass")
        result["verified"] += record["verified"]
        result["failed"] += [f["test"] for f in record["failed"]]
        result["items"].append(record)
        # Only a fully verified edit can cover a later item: "partial" had failing
        # tests, and an empty verify set proves nothing ran at all.
        if record["status"] == "applied" and record["verified"]:
            done.append({"index": item["index"], "summary": record["summary"],
                         "diff": record["diff"], "verified": record["verified"]})

    statuses = {i["status"] for i in result["items"]}
    # What this attempt tried, for the next one to read and for the stop rule at
    # the top to prove that another attempt could bring nothing new.
    if {"applied", "partial", "proposed"} & statuses:
        outcome = fix_history.PASSED
    elif statuses and statuses <= {"rejected"}:
        outcome = fix_history.ALL_REJECTED
    elif any(i.get("no_edits") for i in result["items"]):
        # Only when the model actually answered "no edits" — a response that could
        # not be parsed is a failure, and calling it "no edits" would stop the loop
        # on the strength of something the model never said.
        outcome = fix_history.NO_EDITS
    else:
        # Nothing applied, partial or proposed means nothing from this attempt is
        # on disk — every failing item was restored. FAILED would tell the next
        # prompt the opposite ("applied, still failing, already on disk").
        outcome = fix_history.ROLLED_BACK
    fix_history.append(AUDIT_DIR, fix_history.record(
        attempt=ATTEMPT,
        proposed=[fp for i in result["items"] for fp in (i.get("fingerprint") or [])],
        applied=sorted({Path(f).name for i in result["items"]
                        if i.get("status") in ("applied", "partial")
                        for f in (i.get("files") or [])}),
        rejections=[{"guard": g.get("guard", ""), "reason": g.get("reason", "")}
                    for i in result["items"] for g in (i.get("guards") or [])
                    if not g.get("ok")],
        outcome=outcome))

    if not APPLY:
        gate = "skipped" if "proposed" not in statuses else "true"
        finish(result, gate, "" if "proposed" in statuses else "no-work")
    elif "applied" in statuses or "partial" in statuses:
        finish(result, "true" if not result["failed"] else "false")
    else:
        finish(result, "false")


if __name__ == "__main__":
    main()
