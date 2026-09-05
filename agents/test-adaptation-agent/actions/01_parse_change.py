#!/usr/bin/env python3
"""
Step 01 — Parse Change

Turns a human's plain-English change note into a structured plan.

The split of labour matters. Headers (`Module:`, `Type:`, `Affects:`, URLs) are
parsed in Python because they are unambiguous and a model adds only the chance of
getting them wrong. What the model is for is the part that genuinely needs
judgement: deciding what *kind* of change each numbered item is, because the kind
is what decides how much authority the repair gets.

That is the whole payoff of being triggered by a change note rather than by a
failure. The reactive design had to infer the kind from evidence — telling
"a step was inserted" apart from "the previous step silently did nothing" is the
hardest inference in the whole problem. A human writing "a workspace picker now
appears" has already answered it.

Reads:   $INPUT_FILE
Writes:  $AUDIT_DIR/01-parse-change.json + .md
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
def log(msg): _log("parse-change", msg)

from shared.claude import call_claude as _call_claude
from shared.credential_masking import mask_credential_lines
from shared.flow_map import destructive_token

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
INPUT_FILE = Path(os.environ["INPUT_FILE"])
MODULE = os.environ.get("MODULE", "")
MODEL = os.environ.get("ADAPTATION_MODEL", "claude-opus-5")

# What a change item can be. The kind selects the edit budget and the guards, so
# it is a closed set — an unrecognised kind escalates rather than defaulting to
# something permissive.
KINDS = ("locator", "interaction", "route", "step_insert", "step_merge",
         "field_added", "api_contract", "test_data", "page_object_new",
         "content_changed", "outcome_changed")

# Kinds no agent may apply. `outcome_changed` means the spec moved, not the test;
# `content_changed` is where a real product bug hides most comfortably.
ESCALATE_ONLY = ("outcome_changed", "content_changed")

_HEADER = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.+?)\s*$")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s*(.+)$")


def parse_headers(text: str) -> dict:
    """Everything before the first numbered item. Pure text, no model."""
    headers, body_start = {}, 0
    for i, line in enumerate(text.splitlines()):
        if _NUMBERED.match(line):
            body_start = i
            break
        match = _HEADER.match(line)
        if match:
            headers[match.group(1).strip().lower()] = match.group(2).strip()
        body_start = i + 1
    return headers


def parse_items(text: str) -> list:
    """The numbered change items, in order, with their prose intact.

    A `#` line is commentary, never a continuation. `adaptation_handoff` writes
    drafts with a banner at the top and an "observed by <test> on <timestamp>"
    footer at the bottom, and the footer sits after the last numbered item —
    so without this it was appended to that item's prose and classified as part
    of the change.
    """
    items, current = [], None
    for line in text.splitlines():
        match = _NUMBERED.match(line)
        if match:
            if current:
                items.append(current)
            current = {"index": int(match.group(1)), "text": match.group(2).strip()}
        elif (current and line.strip() and not _HEADER.match(line)
                and not line.lstrip().startswith("#")):
            current["text"] += " " + line.strip()
    if current:
        items.append(current)
    return items


def expected_outcome(text: str) -> str:
    for line in text.splitlines():
        low = line.lower()
        if "expected outcome" in low or low.startswith("outcome"):
            return line.split(":", 1)[-1].strip() if ":" in line else line.strip()
    return ""


def looks_destructive(text: str) -> str:
    """Shared with the explorer, deliberately: the step that decides to stop and
    the step that must actually stop have to agree on what counts."""
    return destructive_token(text)


def classify_prompt(module: str, items: list, note: str) -> str:
    kinds = "\n".join(f"  - {k}" for k in KINDS)
    listed = "\n".join(f"{i['index']}. {i['text']}" for i in items)
    return f"""You are classifying how a product changed, so a QA agent knows how much
authority it has to edit the automation tests. Module: {module}.

The full change note:
---
{note}
---

Classify EACH numbered item into exactly one kind:
{kinds}

What the kinds mean:
- locator          — an EXISTING selector changed: the element was renamed or
                     moved, and one selector string is replaced by another.
                     Adding a brand-new locator is NOT this — see field_added.
- interaction      — the control TYPE changed (a <select> became a combobox, a
                     text field became a date picker). The wrapper call changes too.
- route            — a URL or route changed.
- step_insert      — a NEW step now exists in the flow (a modal, an interstitial,
                     a confirmation screen).
- step_merge       — steps were merged or removed (a 3-page wizard became 2).
- field_added      — the page gained something the page object does not model at
                     all: a newly required form field, or a new control the tests
                     have no locator for. This needs a locator AND an accessor,
                     so it is a larger edit than a rename.
- api_contract     — a request/response shape, status code or header changed.
- test_data        — a fixture, default or seeded record changed.
- page_object_new  — a genuinely new page exists that has no page object yet.
- content_changed  — only visible copy/label/expected text changed.
- outcome_changed  — what the feature DOES changed, so what the test should prove
                     has changed too.

Two of these stop the agent rather than directing it, so do not reach for them
loosely and do not avoid them when they fit:
- `outcome_changed` means the specification moved. No edit to the test is correct,
  because the test is not what is broken.
- `content_changed` is where a genuine product bug hides most comfortably — a
  changed expected string looks identical whether it was intended or is a defect.

Also extract, per item, the product nouns a reader would use to find the affected
page objects (e.g. "workspace", "checkout", "cart").

Respond with a JSON object ONLY, no prose and no markdown fences:

{{
  "items": [
    {{"index": 1, "kind": "<one of the kinds>", "nouns": ["..."],
      "page_hint": "<the page or screen this happens on, or empty>",
      "rationale": "<one short sentence>"}}
  ]
}}

The item list:
{listed}
"""


def main():
    if not INPUT_FILE.exists():
        log(f"ERROR: change note not found: {INPUT_FILE}")
        sys.exit(1)

    note = INPUT_FILE.read_text(encoding="utf-8", errors="ignore")
    headers = parse_headers(note)
    items = parse_items(note)

    if not items:
        log("ERROR: the change note lists no numbered items — nothing to adapt.")
        log("Expected lines like '1. After login a workspace picker now appears.'")
        sys.exit(1)

    affects = [a.strip() for a in (headers.get("affects", "")).split(",") if a.strip()]
    named = [t.strip() for t in (headers.get("tests", "")).split(",") if t.strip()]
    outcome = expected_outcome(note)

    log(f"Module: {headers.get('module', MODULE)} | type={headers.get('type','web')} "
        f"| {len(items)} change item(s)")
    if affects:
        log(f"Affects: {', '.join(affects)}")
    else:
        log("No Affects: given — scope will be derived from Module:, which is a "
            "weaker claim and will be reported as such")

    classified = {}
    try:
        response = _call_claude(classify_prompt(headers.get("module", MODULE),
                                                items, note),
                                MODEL, str(REPO_ROOT), timeout=600)
        payload = response.strip()
        match = re.search(r"\{[\s\S]*\}", payload)
        if match:
            for row in (json.loads(match.group(0)).get("items") or []):
                classified[int(row.get("index", 0))] = row
    except Exception as exc:
        log(f"Classification call failed ({exc}) — every item will be escalated")

    for item in items:
        row = classified.get(item["index"], {})
        kind = row.get("kind", "")
        item["kind"] = kind if kind in KINDS else "outcome_changed"
        item["nouns"] = row.get("nouns") or []
        item["page_hint"] = row.get("page_hint", "")
        item["rationale"] = row.get("rationale", "")
        if not kind:
            item["rationale"] = ("not classified — escalating rather than "
                                 "guessing at how much authority this needs")
        item["escalate_only"] = item["kind"] in ESCALATE_ONLY
        log(f"  {item['index']}. {item['kind']}"
            + ("  [escalate]" if item["escalate_only"] else "")
            + f" — {item['text'][:70]}")

    destructive = looks_destructive(outcome)
    if destructive:
        log(f"Expected outcome is destructive ('{destructive}') — exploration will "
            f"stop before the final action and the edit will be marked escalate")

    plan = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "module": headers.get("module", MODULE),
        "type": (headers.get("type", "web") or "web").lower(),
        "flow_style": (headers.get("flow style", "parallel") or "parallel").lower(),
        "affects": affects,
        "named_tests": named,
        "web_base_url": headers.get("url", "") or headers.get("web url", ""),
        "api_base_url": headers.get("api url", ""),
        "environment": headers.get("environment", os.environ.get("ADAPT_ENVIRONMENT", "staging")),
        "items": items,
        "expected_outcome": outcome,
        "outcome_is_destructive": bool(destructive),
        "destructive_token": destructive,
        "escalate_only_items": [i["index"] for i in items if i["escalate_only"]],
        "note_masked": mask_credential_lines(note),
        "input_file": str(INPUT_FILE),
    }
    (AUDIT_DIR / "01-parse-change.json").write_text(json.dumps(plan, indent=2))

    md = [f"# Parse Change — {plan['module']}", "",
          f"- **Type:** {plan['type']}",
          f"- **Affects:** {', '.join(affects) if affects else '_derived from Module_'}",
          f"- **Expected outcome:** {outcome or '_not stated_'}",
          f"- **Destructive outcome:** {'yes — ' + destructive if destructive else 'no'}",
          "", "## Change items", "",
          "| # | Kind | Item |", "|---|---|---|"]
    md += [f"| {i['index']} | `{i['kind']}`"
           + (" ⚠️ escalate" if i["escalate_only"] else "")
           + f" | {i['text'][:100]} |" for i in items]
    if plan["escalate_only_items"]:
        md += ["", "> Items marked escalate are reported to a human and never "
               "applied: `outcome_changed` means the specification moved, and "
               "`content_changed` is where a real product bug hides."]
    (AUDIT_DIR / "01-parse-change.md").write_text("\n".join(md) + "\n")
    log(f"Wrote {AUDIT_DIR / '01-parse-change.json'}")


if __name__ == "__main__":
    main()
