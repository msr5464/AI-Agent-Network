"""An ordered record of what a flow actually does, as observed in the product.

The authoring agent already drives a real browser and reports what it finds, but
what it reports is a flat `{name: selector}` dictionary plus two disjoint lists of
step descriptions. That is enough to generate a page object and useless for
answering "what changed": the order is gone, the action performed is not recorded,
there is no page identity, and `page_elements` is captured only on failure, so a
clean run produces nothing structural at all.

A flow map keeps the four things that turn a bag of selectors into a flow:

    index          — the order the steps happened in
    action.verb    — what was done, not just what was found
    page.identity_digest — which page it happened on
    result.resulting_url — where it ended up

The digest is what makes a structural change mechanically visible. "The 3-step
wizard is now 2 steps" is three distinct digests before and two after; no prose
comparison is involved.

One rule runs through the whole module: **a claim the model makes about the page
is not evidence.** Selector uniqueness is recounted here, in Python, against the
element inventory the browser reported. The model's own count is kept as
`claimed_by_model` and never used for anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, List, Optional, Tuple

from shared.baseline import url_shape
from shared.page_identity import normalize_selector

SCHEMA_VERSION = 1

# Actions whose outcome cannot be undone in a shared environment. Matched against
# the accessible name, the visible text and any value typed. Deliberately broad:
# a false refusal costs one escalation, a false permission costs real data.
DESTRUCTIVE: Tuple[str, ...] = (
    "delete", "remove", "deactivate", "close account", "cancel subscription",
    "pay", "purchase", "place order", "confirm & pay", "confirm and pay",
    "checkout", "buy", "transfer", "withdraw", "submit payment", "publish",
    "send invite", "archive", "reset", "revoke", "terminate", "refund",
    "unsubscribe", "disable", "wipe", "destroy",
)

# Exploration outcomes. `destructive_refused` is the one addition to the
# authoring agent's closed set — refusing is a normal result, not a failure.
CATEGORIES = ("selector_not_found", "login_failed", "timeout", "overlay_blocking",
              "network_error", "unexpected_content", "skipped",
              "destructive_refused", "other")

_MARKER = re.compile(r"^(FLOW_STEP|PAGE_STATE|PAGE_ENTER|SELECTOR_COUNT|"
                     r"OUTCOME_OBSERVED|REFUSED|UNREACHABLE_STATE|"
                     r"STEP_PASSED|STEP_FAILED|SELECTOR_FOUND):\s*(.*)$")

# Selector grammar we can honestly evaluate against an element inventory. Anything
# richer (xpath, `>>`, :has-text) returns None — unknown, which must never be
# collapsed into zero.
_SIMPLE_SELECTOR = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*)?"
    r"(?P<rest>(?:#[\w-]+|\.[\w-]+|\[[\w-]+(?:[~|^$*]?=(?:\"[^\"]*\"|'[^']*'|[^\]]*))?\])*)$")
_ATTR = re.compile(r"\[([\w-]+)(?:([~|^$*]?)=(?:\"([^\"]*)\"|'([^']*)'|([^\]]*)))?\]")


# Phrase matching alone missed the case this guard exists for. The example change
# note ends "an order is placed" — a destructive outcome by any reading — and the
# token list only held "place order", so word order defeated it and exploration
# would have walked straight through a real checkout.
#
# So: verb stems with word boundaries, plus a pairing rule for verbs that are only
# destructive together with their object ("place" + "order"). Erring toward
# refusal is deliberate, as it is everywhere else here — a false refusal costs one
# escalation, a false permission costs real data.
_DESTRUCTIVE_VERB = re.compile(
    r"\b(?:delet(?:e|es|ed|ing)|remov(?:e|es|ed|ing)"
    r"|deactivat(?:e|es|ed|ing)|terminat(?:e|es|ed|ing)"
    r"|purchas(?:e|es|ed|ing)|buy|buys|bought"
    r"|pay|pays|paid|paying|payment"
    r"|transfer|transfers|transferred|withdraw|withdraws|withdrew|withdrawn"
    r"|publish(?:es|ed|ing)?|archiv(?:e|es|ed|ing)"
    r"|revok(?:e|es|ed|ing)|refund(?:s|ed|ing)?"
    r"|unsubscrib(?:e|es|ed|ing)|wipe[sd]?|destroy(?:s|ed|ing)?"
    r"|reset|resets|resetting|disabl(?:e|es|ed|ing)"
    r"|checkout|check\s+out)\b")

# Verbs that are only destructive with their object, so each is a (verb, object)
# pair matched in either order.
_DESTRUCTIVE_PAIRS = (
    (re.compile(r"\bplac(?:e|es|ed|ing)\b"), re.compile(r"\border\b"), "place order"),
    (re.compile(r"\bsubmit(?:s|ted|ting)?\b"), re.compile(r"\bpayment\b"), "submit payment"),
    (re.compile(r"\bsend(?:s|ing)?\b|\bsent\b"), re.compile(r"\binvite\b"), "send invite"),
    (re.compile(r"\bclos(?:e|es|ed|ing)\b"), re.compile(r"\baccount\b"), "close account"),
    (re.compile(r"\bcancel(?:s|led|ling|ed|ing)?\b"),
     re.compile(r"\bsubscription\b"), "cancel subscription"),
)


def destructive_token(text: str) -> str:
    """The destructive thing `text` describes, or "" if it describes none."""
    low = " ".join((text or "").lower().split())
    if not low:
        return ""
    # Only multi-word tokens are safe to match as substrings. A single word has
    # to respect word boundaries, or "payload" reads as "pay" and every page with
    # a payload on it becomes un-explorable.
    for token in DESTRUCTIVE:
        if " " in token and token in low:
            return token
    for verb, obj, label in _DESTRUCTIVE_PAIRS:
        if verb.search(low) and obj.search(low):
            return label
    match = _DESTRUCTIVE_VERB.search(low)
    return match.group(0) if match else ""


def _digest(url: str, title: str, heading: str) -> str:
    raw = f"{url_shape(url)}|{(title or '').strip()}|{(heading or '').strip()}"
    return "sha1:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def facts_from_markers(page_enter: Dict, inventory: List[Dict]) -> Dict:
    """Page identity from the marker stream, in `page_identity`'s field names.

    `page_identity.page_facts` needs a parsed DOM; exploration produces a marker
    stream instead. Same field names on purpose, so a flow map and a failure
    snapshot can be compared without either side special-casing the other.
    """
    headings = [e.get("text", "") for e in (inventory or [])
                if str(e.get("tag", "")).lower() in ("h1", "h2") and e.get("text")]
    url = page_enter.get("url", "")
    title = page_enter.get("title", "")
    return {
        "id": page_enter.get("id") or url_shape(url) or "unknown",
        "url": url,
        "url_shape": url_shape(url),
        "title": title,
        "headings": headings[:3],
        "identity_digest": _digest(url, title, headings[0] if headings else ""),
        "inventory_size": len(inventory or []),
    }


def count_in_inventory(selector: str, elements: List[Dict]) -> Optional[int]:
    """How many inventory elements a selector matches, or None if unevaluable.

    `None` and `0` must stay distinct at every call site — the same rule
    `page_identity.normalize_selector` documents. "I could not check" is not
    "it matches nothing", and treating them alike is how an unverifiable guess
    gets recorded as a verified absence.
    """
    normalized = normalize_selector(selector or "")
    if not normalized:
        return None
    match = _SIMPLE_SELECTOR.match(normalized.strip())
    if not match:
        return None

    tag = (match.group("tag") or "").lower()
    rest = match.group("rest") or ""
    ids = re.findall(r"#([\w-]+)", rest)
    classes = re.findall(r"\.([\w-]+)", rest)
    # finditer, not findall: findall reports a non-participating alternation
    # branch as "" rather than None, so `[data-cy='go']` came back with an empty
    # expected value and matched nothing. Group participation is the signal here.
    attrs = []
    for m in _ATTR.finditer(rest):
        name, op = m.group(1), m.group(2) or ""
        value = next((g for g in (m.group(3), m.group(4), m.group(5))
                      if g is not None), None)
        attrs.append((name, op, value))

    hits = 0
    for element in elements or []:
        if tag and str(element.get("tag", "")).lower() != tag:
            continue
        if ids and element.get("id") not in ids:
            continue
        element_classes = set(str(element.get("class", "")).split())
        if classes and not set(classes) <= element_classes:
            continue
        ok = True
        for name, op, value in attrs:
            actual = element.get(name)
            if actual is None:
                actual = (element.get("attributes") or {}).get(name)
            if actual is None:
                ok = False
                break
            if value is None:
                continue
            actual = str(actual)
            if op == "*" and value not in actual:
                ok = False
            elif op == "^" and not actual.startswith(value):
                ok = False
            elif op == "$" and not actual.endswith(value):
                ok = False
            elif op == "" and actual != value:
                ok = False
            if not ok:
                break
        if ok:
            hits += 1
    return hits


def is_destructive(step: Dict) -> Optional[str]:
    """The destructive token a step's target matches, if any."""
    target = (step.get("action") or {}).get("target") or {}
    haystack = " ".join(str(v) for v in (
        target.get("accessible_name"), target.get("name"), target.get("text"),
        (step.get("action") or {}).get("value")) if v)
    return destructive_token(haystack) or None


def parse_markers(stdout: str) -> Dict:
    """Every marker in an exploration's stdout, in the order it was emitted.

    Emit-as-you-go means a run killed at its budget still yields a valid prefix,
    so a malformed line is skipped rather than fatal: losing one step is much
    better than losing the fourteen that came before it.
    """
    flow: Dict = {
        "schema_version": SCHEMA_VERSION, "steps": [], "pages": {},
        "unreachable": [], "refusals": [], "outcomes": [], "notes": [],
        "legacy_selectors": {},
    }
    inventories: Dict[str, List[Dict]] = {}
    page_enters: Dict[str, Dict] = {}

    for line in (stdout or "").splitlines():
        match = _MARKER.match(line.strip())
        if not match:
            continue
        kind, payload = match.group(1), match.group(2).strip()
        try:
            if kind == "FLOW_STEP":
                flow["steps"].append(json.loads(payload))
            elif kind == "PAGE_ENTER":
                page_id, url, title = (payload.split("|", 2) + ["", ""])[:3]
                page_enters[page_id] = {"id": page_id, "url": url, "title": title}
            elif kind == "PAGE_STATE":
                page_id, url, elements = (payload.split("|", 2) + ["", "[]"])[:3]
                inventories[page_id] = json.loads(elements)
                page_enters.setdefault(page_id, {"id": page_id, "url": url,
                                                 "title": ""})
            elif kind == "SELECTOR_COUNT":
                page_id, selector, count = (payload.split("|", 2) + ["", "0"])[:3]
                flow["notes"].append({"kind": "claimed_count", "page": page_id,
                                      "selector": selector, "count": count})
            elif kind == "OUTCOME_OBSERVED":
                invariant, seen = (payload.split("|", 1) + [""])[:2]
                flow["outcomes"].append({"invariant": invariant, "observed": seen})
            elif kind == "REFUSED":
                index, target, rule = (payload.split("|", 2) + ["", ""])[:3]
                flow["refusals"].append({"index": index, "target": target,
                                         "rule": rule})
            elif kind == "UNREACHABLE_STATE":
                reached, missing = (payload.split("|", 1) + [""])[:2]
                flow["unreachable"].append({"reached": reached, "missing": missing})
            elif kind == "SELECTOR_FOUND":
                name, _, selector = payload.partition("=")
                flow["legacy_selectors"][name.strip()] = selector.strip()
        except (ValueError, json.JSONDecodeError) as exc:
            flow["notes"].append({"kind": "unparsed", "marker": kind,
                                  "detail": str(exc)[:120]})

    for page_id, enter in page_enters.items():
        flow["pages"][page_id] = facts_from_markers(enter, inventories.get(page_id, []))
    flow["_inventories"] = inventories
    return flow


def attach_identity(flow: Dict) -> Dict:
    """Fill each step's page block from the page inventory, and renumber."""
    for position, step in enumerate(flow.get("steps") or []):
        page_ref = step.get("page")
        if isinstance(page_ref, str):
            step["page"] = flow["pages"].get(page_ref, {"id": page_ref})
        elif isinstance(page_ref, dict) and not page_ref.get("identity_digest"):
            step["page"] = {**page_ref, **flow["pages"].get(page_ref.get("id", ""), {})}
        step.setdefault("index", position)
    flow["steps"].sort(key=lambda s: s.get("index", 0))
    return flow


def verify_selectors(flow: Dict) -> Dict:
    """Recount every selector in Python. The model's own count is never trusted."""
    inventories = flow.get("_inventories") or {}
    for step in flow.get("steps") or []:
        target = (step.get("action") or {}).get("target") or {}
        candidate = target.get("selector") or ""
        page_id = (step.get("page") or {}).get("id", "")
        check = step.setdefault("selector_check", {})
        check["candidate"] = candidate
        check["normalized"] = normalize_selector(candidate) or ""
        check.setdefault("claimed_by_model", check.get("match_count"))
        elements = inventories.get(page_id)
        if not candidate:
            check.update({"match_count": None, "counted_against": "none",
                          "unique": None})
            continue
        if elements is None:
            check.update({"match_count": None, "counted_against": "none",
                          "unique": None})
            continue
        count = count_in_inventory(candidate, elements)
        check["match_count"] = count
        check["counted_against"] = "page_inventory" if count is not None else "none"
        # `True` only when measured to be exactly one. A count of one against a
        # bounded inventory is "consistent with unique", not proof — the field
        # name says which, and the PR body repeats it.
        check["unique"] = (count == 1) if count is not None else None
    return flow


def detect_refusals(flow: Dict) -> Dict:
    """Mark destructive steps, and catch the ones that were not refused.

    A refusal is a normal outcome. A destructive action that *succeeded* is a
    protocol violation: the model did the thing it was told not to do, and the
    run's own evidence can no longer be trusted to be side-effect free.
    """
    violations = []
    for step in flow.get("steps") or []:
        token = is_destructive(step)
        if not token:
            continue
        step.setdefault("result", {})["destructive_token"] = token
        if (step["result"].get("outcome") or "") == "ok":
            violations.append({"index": step.get("index"), "token": token,
                               "target": ((step.get("action") or {})
                                          .get("target") or {}).get("accessible_name")})
    flow["violations"] = violations
    return flow


def build(stdout: str) -> Dict:
    """The whole pipeline: markers in, verified ordered flow map out."""
    flow = detect_refusals(verify_selectors(attach_identity(parse_markers(stdout))))
    flow["status"] = ("unsafe" if flow.get("violations")
                      else "partial" if flow.get("unreachable")
                      else "ok" if flow.get("steps") else "empty")
    return flow


def diff_against_test(flow: Dict, test_steps: List[Dict]) -> Dict:
    """What the product does now versus what the test does today.

    `test_steps` are `{index, description, page, target}` derived from source.
    Anything on either side with no counterpart is reported; nothing is guessed.
    """
    added, removed, changed = [], [], []
    observed_pages = {(s.get("page") or {}).get("identity_digest")
                      for s in flow.get("steps") or []}
    test_targets = {re.sub(r"[^a-z0-9]+", "", (s.get("target") or "").lower()): s
                    for s in test_steps or []}

    for step in flow.get("steps") or []:
        target = (step.get("action") or {}).get("target") or {}
        key = re.sub(r"[^a-z0-9]+", "", str(target.get("name") or "").lower())
        mapping = step.setdefault("maps_to_test", {})
        if key and key in test_targets:
            mapping.update({"kind": "existing",
                            "test_step_index": test_targets[key].get("index")})
        elif step.get("action", {}).get("verb") in ("observe", "wait", "navigate"):
            mapping.setdefault("kind", "unmapped")
        else:
            mapping.update({"kind": "new"})
            added.append({"flow_index": step.get("index"),
                          "why": f"no test step reaches "
                                 f"{target.get('accessible_name') or target.get('name')}"})

    for step in test_steps or []:
        digest = step.get("page_digest")
        if digest and digest not in observed_pages:
            removed.append({"test_step_index": step.get("index"),
                            "why": f"page {step.get('page')} never observed"})

    return {"added": added, "removed": removed, "changed": changed}


def score(flow: Dict, status: str) -> tuple:
    """Rank one exploration attempt against another. Higher is better.

    Ordering copied from the authoring agent's `_score`, and for the same reason:
    a clean finish beats a crash, and thoroughness beats "zero failures because it
    died on step two".
    """
    steps = flow.get("steps") or []
    failed = [s for s in steps if (s.get("result") or {}).get("outcome") == "failed"]
    unique = [s for s in steps if (s.get("selector_check") or {}).get("unique")]
    return (1 if status == "ok" else 0, len(steps), -len(failed), len(unique))


def validate(flow: Dict) -> tuple:
    """Structural checks on a flow map. Returns (ok, problems)."""
    problems = []
    steps = flow.get("steps") or []
    indices = [s.get("index") for s in steps]
    if indices != sorted(indices):
        problems.append("steps are not in index order")
    if len(set(indices)) != len(indices):
        problems.append("duplicate step indices")
    for step in steps:
        if not (step.get("action") or {}).get("verb"):
            problems.append(f"step {step.get('index')} has no action verb")
        if not (step.get("page") or {}).get("identity_digest"):
            problems.append(f"step {step.get('index')} has no page identity")
    if flow.get("violations"):
        problems.append(f"{len(flow['violations'])} destructive action(s) were "
                        f"performed rather than refused")
    return (not problems), problems


def describe(flow: Dict) -> str:
    """The flow map as a markdown table, for the audit report and the PR body."""
    lines = [f"**Status:** {flow.get('status','?')} — "
             f"{len(flow.get('steps') or [])} step(s)", "",
             "| # | Page | Action | Target | Unique? | Result |",
             "|---|---|---|---|---|---|"]
    for step in flow.get("steps") or []:
        action = step.get("action") or {}
        target = action.get("target") or {}
        check = step.get("selector_check") or {}
        result = step.get("result") or {}
        unique = {True: "yes", False: "NO", None: "unverified"}[check.get("unique")]
        lines.append(
            f"| {step.get('index')} | {(step.get('page') or {}).get('id','?')} "
            f"| {action.get('verb','?')} "
            f"| {target.get('accessible_name') or target.get('name') or ''} "
            f"| {unique} | {result.get('outcome','?')} |")
    if flow.get("refusals"):
        lines += ["", "**Refused (destructive):**"]
        lines += [f"- step {r['index']}: {r['target']} ({r['rule']})"
                  for r in flow["refusals"]]
    if flow.get("unreachable"):
        lines += ["", "**Unreachable:**"]
        lines += [f"- reached {u['reached']}, could not reach {u['missing']}"
                  for u in flow["unreachable"]]
    if flow.get("violations"):
        lines += ["", "⚠️ **Destructive actions were performed, not refused** — "
                  "this run's evidence cannot be treated as side-effect free."]
    return "\n".join(lines)


# ── Measuring which page object a page actually is ────────────────────────────
#
# Step 02 *guesses* page-object candidates from name similarity; that guess is
# cheap and often right, and it is still a guess. Once exploration has reported
# what is on each page, the mapping can be measured instead — against the same
# `page_identity.page_object_coverage` the healing agent uses on a failure DOM,
# so both agents judge "is this that page?" by one rule.
#
# The honest limit: an inventory is a bounded sample (the prompt asks for up to
# 25 interactive elements), not the whole document. A locator missing from the
# sample is *unobserved*, not absent. So this ranks candidates and names a best
# match; it must never be read as "0 of N matched, therefore the wrong page" the
# way a full DOM capture can be. Every report carries `sampled: True` to keep
# that distinction at the call site rather than in someone's memory.

_ATTR_SAFE = re.compile(r"[^\w:.-]")


def _attr(name: str, value) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    escaped = str(value).replace("&", "&amp;").replace('"', "&quot;") \
                        .replace("<", "&lt;").replace(">", "&gt;")
    return f' {_ATTR_SAFE.sub("-", name)}="{escaped}"'


def document_from_inventory(elements: List[Dict]) -> str:
    """A minimal HTML document standing in for one page's reported elements.

    Synthesised rather than captured, so that the existing coverage code — which
    expects a parsed document — can be reused verbatim instead of growing a
    second, drifting implementation that reads inventories.
    """
    parts = ["<!DOCTYPE html><html><body>"]
    for element in elements or []:
        tag = str(element.get("tag") or "div").lower()
        tag = _ATTR_SAFE.sub("", tag) or "div"
        attrs = (_attr("id", element.get("id"))
                 + _attr("class", element.get("class"))
                 + _attr("name", element.get("name"))
                 + _attr("role", element.get("role"))
                 + _attr("type", element.get("type"))
                 + _attr("placeholder", element.get("placeholder"))
                 + _attr("aria-label", element.get("aria_label")
                         or element.get("ariaLabel")))
        for key, value in (element.get("attributes") or {}).items():
            attrs += _attr(str(key), value)
        text = str(element.get("text") or "")
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<{tag}{attrs}>{text}</{tag}>")
    parts.append("</body></html>")
    return "".join(parts)


def measure_page_objects(flow: Dict, page_objects: List[Dict]) -> Dict:
    """Attach a measured page-object ranking to every page in the flow map.

    `page_objects` are `{path, snippet}` dicts — the shape the fix step already
    builds and `page_object_coverage` already takes.
    """
    from shared.page_identity import page_object_coverage, parse as parse_html

    inventories = flow.get("_inventories") or {}
    for page_id, page in (flow.get("pages") or {}).items():
        elements = inventories.get(page_id) or []
        page["measured_sampled"] = True
        if not elements or not page_objects:
            page["page_objects"] = []
            page["best_page_object"] = None
            continue
        soup = parse_html(document_from_inventory(elements))
        reports = page_object_coverage(page_objects, soup)
        page["page_objects"] = [
            {"name": r["name"], "path": r["path"], "matched": r["matched"],
             "evaluable": r["evaluable"], "total": r["total"], "ratio": r["ratio"],
             "sampled": True}
            for r in reports]
        # A best match needs something actually evaluated and actually matched.
        # Ranking first on an inventory that evaluated nothing would hand back
        # whichever page object happened to sort first.
        best = next((r for r in reports
                     if r["evaluable"] and r["matched"]), None)
        page["best_page_object"] = (
            {"name": best["name"], "path": best["path"],
             "matched": best["matched"], "evaluable": best["evaluable"],
             "ratio": best["ratio"], "sampled": True} if best else None)

    # Also expose it per step, so the adapt prompt can say "step 4 happened on
    # ProductsPage" without the model having to join two structures itself.
    for step in flow.get("steps") or []:
        page_id = (step.get("page") or {}).get("id", "")
        measured = (flow.get("pages") or {}).get(page_id, {}).get("best_page_object")
        step.setdefault("page", {})["best_page_object"] = measured
    return flow


def describe_page_objects(flow: Dict) -> str:
    """The measured page↔page-object mapping, as markdown."""
    rows = []
    for page_id, page in sorted((flow.get("pages") or {}).items()):
        best = page.get("best_page_object")
        if best:
            rows.append(f"| `{page_id}` | `{best['name']}` | "
                        f"{best['matched']}/{best['evaluable']} locators matched |")
        elif page.get("page_objects") is not None:
            rows.append(f"| `{page_id}` | _none matched_ | "
                        f"{len(page.get('page_objects') or [])} candidate(s) checked |")
    if not rows:
        return ""
    return ("\n| Observed page | Page object | Measured |\n|---|---|---|\n"
            + "\n".join(rows)
            + "\n\nMeasured against the elements exploration reported, which is a "
              "bounded sample of each page — enough to say which page object fits "
              "best, not enough to prove one is absent.\n")
