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

`ADAPT_APPLY=false` (the default while the guards earn trust) runs everything up to
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
def log(msg): _log("adapt", msg)

from shared import (assertion_graph, code_analyzer, edit_guards, flow_map,
                    intent, url_properties, verdict_feedback)
from shared.claude import call_claude as _call_claude
from shared.code_analyzer import invalidate_file, read_source
from shared.test_runner import run_test

from lib.transaction import Transaction

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
MODEL = os.environ.get("ADAPT_MODEL", "claude-opus-5")
ATTEMPT = int(os.environ.get("ADAPT_ATTEMPT", "1"))
APPLY = os.environ.get("ADAPT_APPLY", "false").lower() == "true"
MAX_FILES = int(os.environ.get("ADAPT_MAX_FILES_PER_RUN", "6"))
MAX_TOTAL_DIFF = int(os.environ.get("ADAPT_MAX_TOTAL_DIFF_LINES", "200"))
TEST_TIMEOUT_S = int(os.environ.get("ADAPT_TEST_TIMEOUT_S", "300"))
COMPILE_CMD = os.environ.get("TEST_COMPILE_CMD", "mvn -q test-compile -DskipTests")
RULES_FILE = REPO_ROOT / "config" / "prompts" / "adapt.md"
SYSTEM_PROMPT = REPO_ROOT / "config" / "skills" / "automation-repo.md"

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
}
DEFAULT_BUDGET = 40
# A step_insert may legitimately touch a call site and a page object; the
# per-file budget alone would let it do that in six files and stay inside every
# individual limit.
CLUSTER_BUDGETS = {"step_insert": 80}

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


def finish(result: dict, gate: str, skip_reason: str = ""):
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
    for item in result.get("items", []):
        md += [f"## Item {item['index']} — `{item['kind']}` — {item['status']}", "",
               item.get("summary") or item.get("reason") or "", ""]
        for guard in item.get("guards", []):
            mark = "✅" if guard["ok"] else "❌"
            md.append(f"- {mark} `{guard['guard']}` {guard.get('reason','')}")
        if item.get("diff"):
            md += ["", "```diff", item["diff"][:4000], "```", ""]
        if item.get("justification"):
            md += ["", "| edit | justified by flow step |", "|---|---|"]
            md += [f"| `{j['file']}` | {j['step']} |" for j in item["justification"]]
        md.append("")
    if result.get("escalations"):
        md += ["## Escalations", ""]
        md += [f"- **{e['what']}** — {e['why']}" for e in result["escalations"]]
    return "\n".join(md) + "\n"


def excerpt(path: Path, limit: int = 8000) -> str:
    text = read_source(path) or ""
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def build_adapt_prompt(item: dict, plan: dict, scope: dict, flow: dict,
                       workspace: Path, rules: str, retry_note: str) -> str:
    # Order by what exploration MEASURED, not by what step 02 guessed. The file
    # a page actually turned out to be belongs at the top of the prompt; a
    # name-similarity guess that nothing corroborated belongs below it.
    measured = {}
    for page in (flow.get("pages") or {}).values():
        best = page.get("best_page_object")
        if best:
            measured[best["path"]] = best

    candidates = sorted(
        scope.get("edit_candidates") or [],
        key=lambda c: (0 if c["path"] in measured else 1, c["path"]))
    files = "\n".join(
        f"\n### {workspace / c['path']}  ({c['role']})"
        + (f"  — **measured**: this is the page object for observed page "
           f"`{measured[c['path']]['name']}` "
           f"({measured[c['path']]['matched']}/{measured[c['path']]['evaluable']} "
           f"of its locators matched what the browser reported)"
           if c["path"] in measured else "  — nominated by name similarity only")
        + f"\n```java\n{excerpt(workspace / c['path'])}\n```"
        for c in candidates[:6])

    steps = [s for s in flow.get("steps") or []]
    flow_table = flow_map.describe({"steps": steps, "status": "ok",
                                    "refusals": flow.get("refusals") or [],
                                    "unreachable": flow.get("unreachable") or []})

    contracts = scope.get("intent_contracts") or {}
    contract_text = "\n\n".join(
        intent.describe(c) for c in list(contracts.values())[:5])

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

## 📜 WHAT THESE TESTS PROVE — measured before any edit, and must still hold
{contract_text}

## Tests that must still pass
{chr(10).join('- ' + t for t in (scope.get('verify') or [])[:20])}

## Files you may edit
{files}
{retry_note}
{rules}
"""


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


def extract_json(response: str):
    for candidate in (response.strip(),):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    for pattern in (r"```json\s*([\s\S]*?)\s*```", r"(\{[\s\S]*\})"):
        match = re.search(pattern, response)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    return None


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


def check_conservation(scope: dict, workspace: Path) -> list:
    """Assertion conservation for every test in scope, against the frozen copy."""
    code_analyzer.reset_caches()
    from shared import blast_radius
    blast_radius._cache.clear()
    index_after = assertion_graph.member_index(str(workspace))

    reports = []
    for test, frozen in (scope.get("intent_contracts") or {}).items():
        klass, _, method = test.replace("#", ".").rpartition(".")
        simple = klass.rsplit(".", 1)[-1]
        try:
            after = assertion_graph.fingerprints(simple, method, index_after)
        except Exception as exc:
            reports.append({"test": test, "ok": True, "verdict": "PLAUSIBLE",
                            "reason": f"could not re-measure: {exc}"})
            continue
        report = assertion_graph.conserved(intent.thaw(frozen), after)
        report["test"] = test
        reports.append(report)
    return reports


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

    workspace = Path(scope["workspace"])

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
    log(f"Flow vs tests: {len(diff['added'])} added, {len(diff['removed'])} removed "
        f"(against {len(current)} interaction(s) in the current tests)")
    if not diff["added"] and not diff["removed"]:
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
    retry_note = ""
    if ATTEMPT > 1:
        previous = AUDIT_DIR / "04-adapt.json"
        if previous.exists():
            prior = json.loads(previous.read_text())
            failures = [i.get("reason", "") for i in prior.get("items", [])
                        if i.get("status") not in ("applied", "verified")]
            retry_note = ("\n## ⚠️ Previous attempt\n"
                          + "\n".join(f"- {f}" for f in failures[:6])
                          + "\nTwo failures on the same item is evidence the "
                            "approach is wrong, not a reason to try a wider edit. "
                            "If you cannot justify an edit from the flow map, "
                            "return adaptable: false.\n")

    actionable = [i for i in plan["items"] if not i.get("escalate_only")]
    for item in plan["items"]:
        if item.get("escalate_only"):
            result["escalations"].append({
                "what": f"item {item['index']} ({item['kind']})",
                "why": ("the specification moved — no edit to the test is correct, "
                        "because the test is not what is broken"
                        if item["kind"] == "outcome_changed" else
                        "a changed expected string is where a real product bug "
                        "hides most comfortably — proposed, never applied")})
            result["items"].append({**item, "status": "escalated",
                                    "reason": result["escalations"][-1]["why"],
                                    "guards": []})
            log(f"Item {item['index']} [{item['kind']}] — escalated, not attempted")

    if not actionable:
        log("No actionable items — everything escalates to a human")
        finish(result, "skipped", "escalate")
        return

    for item in actionable:
        log(f"Item {item['index']} [{item['kind']}] — {item['text'][:70]}")
        prompt = build_adapt_prompt(item, plan, scope, flow, workspace, rules,
                                    retry_note)
        response = _call_claude(prompt, MODEL, str(REPO_ROOT), timeout=900)
        payload = extract_json(response or "")
        record = {**item, "status": "failed", "guards": [], "reason": ""}

        if not payload:
            record["reason"] = "could not parse the model's response as JSON"
            result["items"].append(record); log(f"  {record['reason']}"); continue
        if not payload.get("adaptable", False):
            record.update({"status": "declined",
                           "reason": payload.get("unadaptable_reason")
                                     or "declared unadaptable"})
            result["escalations"].append({"what": f"item {item['index']}",
                                          "why": record["reason"]})
            result["items"].append(record)
            log(f"  declined: {record['reason']}")
            continue

        edits = payload.get("edits") or []
        record["summary"] = payload.get("summary", "")
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

        guards = run_guards(item, txn.staged, txn.snapshots, flow, scope, workspace,
                            index_before)
        record["guards"] = guards
        rejected = [g for g in guards if not g["ok"]]
        for guard in guards:
            log(f"    {'OK' if guard['ok'] else 'REJECT'} {guard['guard']}"
                + (f" — {guard['reason']}" if guard["reason"] else ""))
        if rejected:
            record.update({"status": "rejected",
                           "reason": "; ".join(g["reason"] for g in rejected if g["reason"])})
            result["items"].append(record)
            continue

        if not APPLY:
            record["status"] = "proposed"
            result["proposed"].append({"item": item["index"], "diff": record["diff"],
                                       "summary": record["summary"]})
            # Recorded with accepted=None: nobody has judged it yet, and "not
            # reviewed" must not average in with "rejected". Whether people accept
            # proposals verbatim is the promotion criterion for turning ADAPT_APPLY
            # on, and it is only knowable if somebody writes it down.
            try:
                verdict_feedback.record_proposal(
                    AGENT_DIR / "feedback" / "proposals.json",
                    os.environ.get("SESSION_ID", ""), plan.get("module", ""),
                    item["index"], item["kind"], None, record["summary"][:200])
            except Exception as exc:
                log(f"  (could not record the proposal: {exc})")
            result["items"].append(record)
            log("  proposed (ADAPT_APPLY=false — nothing written)")
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

        conservation = check_conservation(scope, workspace)
        broken = [c for c in conservation if not c["ok"]]
        record["conservation"] = conservation
        for report in conservation:
            log(f"    conservation {report.get('test','')}: "
                f"{assertion_graph.describe(report)}")
        if broken:
            why = ("assertion conservation failed: "
                   + "; ".join(b.get("reason", "") for b in broken[:2]))
            txn.rollback(why)
            record.update({"status": "rolled_back", "reason": why})
            result["items"].append(record)
            continue

        passed, failed = [], []
        for test in scope.get("verify") or []:
            log(f"  verifying {test}…")
            status, out = run_test(test, workspace, timeout_s=TEST_TIMEOUT_S, log=log)
            (passed if status == "passed" else failed).append((test, status, out))
        record["verified"] = [t for t, _, _ in passed]
        record["failed"] = [{"test": t, "status": s} for t, s, _ in failed]

        if failed and not passed:
            why = f"every verified test still fails ({len(failed)})"
            txn.rollback(why)
            record.update({"status": "rolled_back", "reason": why})
            result["items"].append(record)
            continue

        txn.commit()
        record["status"] = "applied" if not failed else "partial"
        result["verified"] += record["verified"]
        result["failed"] += [f["test"] for f in record["failed"]]
        result["items"].append(record)

    statuses = {i["status"] for i in result["items"]}
    if not APPLY:
        gate = "skipped" if "proposed" not in statuses else "true"
        finish(result, gate, "" if "proposed" in statuses else "no-work")
    elif "applied" in statuses or "partial" in statuses:
        finish(result, "true" if not result["failed"] else "false")
    else:
        finish(result, "false")


if __name__ == "__main__":
    main()
