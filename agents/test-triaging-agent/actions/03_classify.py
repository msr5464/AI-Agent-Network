#!/usr/bin/env python3
"""
Step 03 — Classify
Batch-classify test failures using Claude CLI (claude -p).
Replaces the LangChain-based TestAnalyzer with a direct subprocess call.
Outputs: audit/<session>/03-classify.json + 03-classify.md

Uses Claude CLI — NOT LangChain.
"""

import os, sys, json, re
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
from shared import diagnosis
from lib.root_cause_groups import (group_failures, pick_representative,
                                    is_groupable, signature as cause_signature)
def log(msg): _log("classify", msg)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))

TRIAGING_CLASSIFIER_MODEL = os.environ.get("TRIAGING_CLASSIFIER_MODEL", "claude-opus-4-6")
TRIAGING_CLASSIFIER_EFFORT = os.environ.get("TRIAGING_CLASSIFIER_EFFORT", "medium")

MAX_LOG_CHARS = 4000   # Truncate execution log per failure to fit context
BATCH_SIZE = 10        # Failures per Claude call (avoid context limits)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(filename):
    path = AUDIT_DIR / filename
    if not path.exists():
        log(f"ERROR: {filename} not found")
        sys.exit(1)
    return json.loads(path.read_text())


from shared.claude import call_claude as _call_claude
def call_claude(prompt: str) -> str:
    output = _call_claude(prompt, TRIAGING_CLASSIFIER_MODEL, str(REPO_ROOT))
    if not output:
        log("Claude CLI returned empty response")
    return output


def extract_json_block(text: str) -> list | dict | None:
    """Extract the first JSON array or object from Claude's response."""
    # Try ```json ... ``` block first
    match = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON array or object
    match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


VALID_CLASSIFICATIONS = {"PRODUCT_BUG", "AUTOMATION_ISSUE", "UNKNOWN"}
VALID_CONFIDENCE      = {"HIGH", "MEDIUM", "LOW"}
# The original six, plus the verdicts the diagnosis engine can now distinguish.
# The old values stay valid: historical reports, category_rules.py and the HTML
# report all key off them, and ELEMENT_NOT_FOUND remains what an LLM answers
# when it only has the error text to go on.
_LEGACY_CATEGORIES = {"ELEMENT_NOT_FOUND", "TIMEOUT", "ASSERTION_FAILURE",
                      "ENVIRONMENT_ISSUE", "CODE_ISSUE", "OTHER"}
VALID_CATEGORIES      = _LEGACY_CATEGORIES | set(diagnosis.STOP) | set(diagnosis.ACTIONS)


def validate_classification(c: dict) -> dict:
    """Enforce valid enum values — reject unexpected strings with safe defaults."""
    if c.get("classification") not in VALID_CLASSIFICATIONS:
        c["classification"] = "UNKNOWN"
    if c.get("confidence") not in VALID_CONFIDENCE:
        c["confidence"] = "LOW"
    if c.get("root_cause_category") not in VALID_CATEGORIES:
        c["root_cause_category"] = "OTHER"
    return c


def build_batch_prompt(failures: list[dict]) -> str:
    """Build a classification prompt for a batch of failures."""
    items_text = ""
    for i, f in enumerate(failures, 1):
        exec_log = (f.get("execution_log") or "")[:MAX_LOG_CHARS]
        error_type = f.get("error_type") or ""
        error_msg = (f.get("error_message") or "")[:500]
        stack = (f.get("stack_trace") or "")[:500]

        items_text += f"""
### Failure {i}: {f['full_name']}
- Error Type: {error_type}
- Error Message: {error_msg}
- Stack Trace (truncated):
{stack}
- Execution Log (truncated):
{exec_log}
"""

    return f"""You are a QA classification engine. Classify each test failure below as either PRODUCT_BUG or AUTOMATION_ISSUE.

## Classification Rules

**PRODUCT_BUG** — the application has a defect:
- Assertion failures on business logic (wrong data, wrong status code, wrong count)
- OTP failures → always PRODUCT_BUG + ASSERTION_FAILURE category
- API returning unexpected responses (wrong HTTP status, wrong body)
- Features behaving incorrectly

**AUTOMATION_ISSUE** — the test code or infrastructure has a problem:
- NoSuchElementException, ElementClickInterceptedException, ElementNotInteractableException
- TimeoutException, page load timeout ("'X' NOT loaded even after Y seconds")
- NullPointerException originating from test code (not application stack)
- StaleElementReferenceException, WebDriver/session issues
- CSS/XPath locators that no longer match the DOM

## Root Cause Categories
- ELEMENT_NOT_FOUND — locator mismatch, element not present
- TIMEOUT — wait exceeded, page not loaded
- ASSERTION_FAILURE — expected vs actual mismatch
- ENVIRONMENT_ISSUE — server 500, connectivity problems
- CODE_ISSUE — NullPointerException in test code
- OTHER — unclassified

## Confidence Levels
- HIGH — unambiguous error pattern with clear cause
- MEDIUM — pattern fits but could be interpreted either way
- LOW — ambiguous, genuinely unclear

## Failures to Classify

{items_text}

## Response Format

Respond with ONLY a JSON array. No explanation, no markdown prose before or after.

```json
[
  {{
    "test_name": "<full_name of failure 1>",
    "classification": "PRODUCT_BUG" | "AUTOMATION_ISSUE",
    "confidence": "HIGH" | "MEDIUM" | "LOW",
    "root_cause_category": "ELEMENT_NOT_FOUND" | "TIMEOUT" | "ASSERTION_FAILURE" | "ENVIRONMENT_ISSUE" | "CODE_ISSUE" | "OTHER",
    "root_cause": "<1-2 sentence explanation>",
    "failure_signature": "<concise signature for grouping, e.g. 'NoSuchElementException: #login-btn'>",
    "recommended_action": "<what the QA engineer should do>"
  }},
  ...
]
```
"""


def apply_category_rules(classifications: list[dict], all_failures: list[dict]) -> list[dict]:
    """
    Post-process with category_rules.py to ensure root_cause_category is consistent.
    Converts dict → FailureClassification → re-categorize → back to dict.
    """
    try:
        from lib.agent.analyzer import FailureClassification
        from lib.reporters.category_rules import CategoryRuleEngine
        from lib.utils import TestDataCache
        from lib.parsers.models import TestResult, TestStatus

        # Build a minimal TestResult for each failure so TestDataCache works
        tr_map = {}
        for f in all_failures:
            status_str = f.get("status", "FAIL").upper()
            try:
                status = TestStatus(status_str)
            except ValueError:
                status = TestStatus.FAIL
            tr = TestResult(
                class_name=f.get("class_name", ""),
                method_name=f.get("method_name", ""),
                status=status,
                duration_seconds=f.get("duration_seconds") or 0.0,
                error_type=f.get("error_type"),
                error_message=f.get("error_message"),
                stack_trace=f.get("stack_trace"),
                execution_log=f.get("execution_log"),
                known_failure=f.get("known_failure"),
            )
            tr_map[f["full_name"]] = tr

        cache = TestDataCache(list(tr_map.values()), {})
        rule_engine = CategoryRuleEngine()

        updated = []
        for c in classifications:
            fc = FailureClassification(
                test_name=c["test_name"],
                classification=c["classification"],
                confidence=c["confidence"],
                root_cause=c["root_cause"],
                recommended_action=c["recommended_action"],
                root_cause_category=c["root_cause_category"],
                failure_signature=c.get("failure_signature", ""),
            )
            # Override category with rule-based logic
            try:
                rule_category = rule_engine.classify(fc, cache)
                if rule_category != "OTHER":
                    c["root_cause_category"] = rule_category
            except Exception:
                pass
            updated.append(c)

        return updated
    except Exception as e:
        log(f"Warning: category_rules post-processing failed: {e}")
        return classifications

# ── Main ──────────────────────────────────────────────────────────────────────

def diagnose_failures(failures: list, report_dir: str) -> dict:
    """Deterministic verdicts for the failures whose evidence supports one.

    Runs before the model. Where the engine is confident it is authoritative — it
    measured the page, while the classifier can only read the sentence describing
    it — and the model is left to judge what the engine abstained on. On a large
    build this also removes most of the classification work entirely.
    """
    verdicts = {}
    for failure in failures:
        try:
            evidence = diagnosis.collect(failure, workspace=report_dir or None)
            verdict = diagnosis.diagnose(evidence)
        except Exception as e:
            log(f"  Diagnosis failed for {failure.get('full_name')}: {e}")
            continue
        if verdict["verdict"] == diagnosis.ABSTAIN or verdict["confidence"] != "HIGH":
            continue
        verdicts[failure["full_name"]] = {
            "test_name": failure["full_name"],
            # A measured non-locator cause is an automation issue only when the
            # automation is what went wrong. An error page or a dead host is the
            # environment, and a product bug is neither.
            "classification": ("PRODUCT_BUG" if verdict["verdict"] == "ELEMENT_GONE"
                               else "AUTOMATION_ISSUE"),
            "confidence": "HIGH",
            "root_cause_category": verdict["verdict"],
            "root_cause": "; ".join(verdict.get("reasons") or [])[:400],
            "failure_signature": f"{verdict['verdict']}: {failure.get('error_type', '')}",
            "recommended_action": verdict.get("action") or verdict.get("remediation", ""),
            "source": "diagnosis",
            "actionable": verdict.get("actionable", False),
        }
    return verdicts


def main():
    collect = load_json("02-collect.json")
    failures = collect.get("failures", [])

    if not failures:
        log("No failures to classify — writing empty output")
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "timestamp": ts,
            "build_tag": collect.get("build_tag"),
            "total_failures": 0,
            "classifications": [],
            "summary": {},
        }
        (AUDIT_DIR / "03-classify.json").write_text(json.dumps(result, indent=2))
        (AUDIT_DIR / "03-classify.md").write_text("# Classify Results\n\nNo failures to classify.\n")
        log("Done — no failures")
        return

    # Deduplicate: same test in multiple suites → keep the one with execution_log or FAIL status
    seen: dict[str, dict] = {}
    for f in failures:
        key = f["full_name"]
        if key not in seen:
            seen[key] = f
        else:
            existing = seen[key]
            if f.get("execution_log") and not existing.get("execution_log"):
                seen[key] = f
            elif f["status"] in ("FAIL", "ERROR") and existing["status"] not in ("FAIL", "ERROR"):
                seen[key] = f

    deduplicated = list(seen.values())
    if len(deduplicated) != len(failures):
        log(f"Deduplicated: {len(failures)} → {len(deduplicated)} unique failures")

    # ── Group by root cause before classifying ────────────────────────────────
    # One broken locator fails every test that walks past it. Classifying each
    # separately costs more AND is inconsistent — the same defect can come back
    # HIGH for one test and MEDIUM for another, so only some siblings reach the
    # healing agent and the rest stay red for no discoverable reason.
    # One representative is classified; the verdict is shared with its siblings.
    groups = group_failures(deduplicated)
    representatives = [pick_representative(g) for g in groups.values()]
    rep_to_group = {pick_representative(g)["full_name"]: g for g in groups.values()}

    multi = {k: g for k, g in groups.items() if len(g) > 1}
    if multi:
        saved = len(deduplicated) - len(representatives)
        log(f"Grouped by root cause: {len(deduplicated)} failures → {len(groups)} distinct "
            f"cause(s) ({saved} redundant classification(s) avoided)")
        for group in multi.values():
            rep = pick_representative(group)
            log(f"  ×{len(group)}  {(rep.get('error_type') or 'failure')}: "
                f"{(rep.get('error_message') or '')[:70]}")
            for f in group:
                log(f"        {f['full_name']}")

    # ── Diagnose deterministically first ──────────────────────────────────────
    # The engine measured the page; the classifier can only read the sentence
    # describing it. Where the engine is confident, it is the better answer and
    # the model is not asked at all.
    diagnosed = diagnose_failures(representatives, collect.get("report_dir", ""))
    if diagnosed:
        log(f"Diagnosed {len(diagnosed)} of {len(representatives)} representative(s) "
            f"from evidence — no model call needed for those")
        for verdict in diagnosed.values():
            log(f"  {verdict['test_name'].split('.')[-1]}: "
                f"{verdict['root_cause_category']}")
    representatives = [r for r in representatives
                       if r["full_name"] not in diagnosed]

    all_classifications = list(diagnosed.values())

    if not representatives:
        log("Every representative was diagnosed from evidence — skipping classification")

    log(f"Classifying {len(representatives)} representative failure(s) "
        f"in batches of {BATCH_SIZE}...")

    batches = [representatives[i:i + BATCH_SIZE]
               for i in range(0, len(representatives), BATCH_SIZE)]

    for batch_num, batch in enumerate(batches, 1):
        log(f"Batch {batch_num}/{len(batches)} ({len(batch)} failures)...")
        prompt = build_batch_prompt(batch)
        response = call_claude(prompt)

        if not response:
            log(f"Warning: empty response for batch {batch_num} — marking as UNKNOWN")
            for f in batch:
                all_classifications.append({
                    "test_name": f["full_name"],
                    "classification": "UNKNOWN",
                    "confidence": "LOW",
                    "root_cause_category": "OTHER",
                    "root_cause": "Claude did not return a response",
                    "failure_signature": "Unknown",
                    "recommended_action": "Manual review required",
                })
            continue

        parsed = extract_json_block(response)
        if not parsed or not isinstance(parsed, list):
            log(f"Warning: could not parse JSON from batch {batch_num} response — marking as UNKNOWN")
            for f in batch:
                all_classifications.append({
                    "test_name": f["full_name"],
                    "classification": "UNKNOWN",
                    "confidence": "LOW",
                    "root_cause_category": "OTHER",
                    "root_cause": "Parse error in Claude response",
                    "failure_signature": "Unknown",
                    "recommended_action": "Manual review required",
                })
            continue

        # Align parsed results to batch by test_name or position
        parsed_by_name = {p.get("test_name", ""): p for p in parsed}
        for f in batch:
            c = parsed_by_name.get(f["full_name"])
            if not c and parsed:
                # Fall back to positional match if name doesn't align
                idx = batch.index(f)
                c = parsed[idx] if idx < len(parsed) else None
            if c:
                # Normalise, ensure required fields, and validate enum values
                all_classifications.append(validate_classification({
                    "test_name": f["full_name"],
                    "classification": c.get("classification", "UNKNOWN"),
                    "confidence": c.get("confidence", "LOW"),
                    "root_cause_category": c.get("root_cause_category", "OTHER"),
                    "root_cause": c.get("root_cause", ""),
                    "failure_signature": c.get("failure_signature", ""),
                    "recommended_action": c.get("recommended_action", ""),
                }))
            else:
                all_classifications.append({
                    "test_name": f["full_name"],
                    "classification": "UNKNOWN",
                    "confidence": "LOW",
                    "root_cause_category": "OTHER",
                    "root_cause": "No classification returned for this test",
                    "failure_signature": "Unknown",
                    "recommended_action": "Manual review required",
                })

    # ── Share each verdict with the siblings it was decided for ───────────────
    # Only the representative was classified. Its siblings get the same verdict,
    # tagged so the report and the healing agent can see they are one defect —
    # and so every test showing that defect reaches healing together, instead of
    # some being dropped over an inconsistent confidence score.
    fanned_out = []
    for classification in all_classifications:
        siblings = rep_to_group.get(classification["test_name"], [])
        classification["cause_group_size"] = len(siblings) or 1
        classification["cause_group_key"] = cause_signature(
            next((f for f in siblings if f["full_name"] == classification["test_name"]), {})
        )
        classification["is_group_representative"] = True
        fanned_out.append(classification)

        if len(siblings) <= 1:
            continue
        if not is_groupable(classification):
            # An assertion failure or a low-confidence guess must not be
            # inherited by tests nobody actually looked at. Mark the siblings
            # for individual review instead of copying a verdict onto them.
            for failure in siblings:
                if failure["full_name"] == classification["test_name"]:
                    continue
                fanned_out.append({
                    **classification,
                    "test_name": failure["full_name"],
                    "confidence": "LOW",
                    "is_group_representative": False,
                    "recommended_action": (
                        "Shares a failure signature with "
                        f"{classification['test_name']}, but that verdict is not safe "
                        "to inherit — review individually."),
                })
            continue

        for failure in siblings:
            if failure["full_name"] == classification["test_name"]:
                continue
            fanned_out.append({
                **classification,
                "test_name": failure["full_name"],
                "is_group_representative": False,
                "root_cause": (f"{classification['root_cause']} "
                               f"(same root cause as {classification['test_name']})"),
            })

    if len(fanned_out) != len(all_classifications):
        log(f"Verdicts shared across siblings: {len(all_classifications)} classified → "
            f"{len(fanned_out)} tests covered")
    all_classifications = fanned_out

    # Post-process with category_rules.py
    all_test_results = collect.get("test_results", [])
    all_classifications = apply_category_rules(all_classifications, all_test_results)

    # Build summary
    summary: dict[str, int] = {}
    for c in all_classifications:
        key = c["classification"]
        summary[key] = summary.get(key, 0) + 1

    category_breakdown: dict[str, int] = {}
    for c in all_classifications:
        key = c["root_cause_category"]
        category_breakdown[key] = category_breakdown.get(key, 0) + 1

    log(f"Classification summary: {summary}")
    log(f"Category breakdown: {category_breakdown}")

    # ── Write JSON ─────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp": ts,
        "build_tag": collect.get("build_tag"),
        "total_failures": len(deduplicated),
        "distinct_root_causes": len(groups),
        "classifications": all_classifications,
        "summary": summary,
        "category_breakdown": category_breakdown,
    }

    json_path = AUDIT_DIR / "03-classify.json"
    json_path.write_text(json.dumps(result, indent=2))
    log(f"Wrote 03-classify.json ({json_path.stat().st_size // 1024}KB)")

    # ── Write Markdown ─────────────────────────────────────────────────────────
    product_bugs = [c for c in all_classifications if c["classification"] == "PRODUCT_BUG"]
    auto_issues = [c for c in all_classifications if c["classification"] == "AUTOMATION_ISSUE"]
    high_conf = [c for c in all_classifications if c["confidence"] == "HIGH"]

    md_lines = [
        "# Classify Results",
        "",
        f"**Build Tag:** {collect.get('build_tag')}  ",
        f"**Timestamp:** {ts}",
        "",
        "## Summary",
        "",
        f"| Classification | Count |",
        "|----------------|-------|",
    ]
    for cls, cnt in sorted(summary.items()):
        md_lines.append(f"| {cls} | {cnt} |")

    md_lines += [
        "",
        "## Category Breakdown",
        "",
        f"| Category | Count |",
        "|----------|-------|",
    ]
    for cat, cnt in sorted(category_breakdown.items(), key=lambda x: -x[1]):
        md_lines.append(f"| {cat} | {cnt} |")

    md_lines += [
        "",
        f"**HIGH confidence:** {len(high_conf)} | "
        f"**PRODUCT_BUG:** {len(product_bugs)} | "
        f"**AUTOMATION_ISSUE:** {len(auto_issues)}",
        "",
        "## Classifications",
        "",
        "| Test | Classification | Category | Confidence | Signature |",
        "|------|----------------|----------|------------|-----------|",
    ]
    for c in all_classifications[:50]:
        md_lines.append(
            f"| {c['test_name'][:60]} "
            f"| {c['classification']} "
            f"| {c['root_cause_category']} "
            f"| {c['confidence']} "
            f"| {c.get('failure_signature', '')[:50]} |"
        )
    if len(all_classifications) > 50:
        md_lines.append(f"| ... and {len(all_classifications) - 50} more | | | | |")

    (AUDIT_DIR / "03-classify.md").write_text("\n".join(md_lines) + "\n")
    log(f"Done — {len(all_classifications)} failures classified")


if __name__ == "__main__":
    main()
