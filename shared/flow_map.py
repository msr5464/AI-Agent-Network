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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shared.baseline import url_shape
from shared import frames, value_match
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

# Verbs that only read the page. A destructive *name* under one of these is not a
# destructive *act*: verifying that a Checkout button is on the cart page changes
# nothing, and treating it as a performed action escalated whole runs whose notes
# only asked for an extra assertion.
READ_ONLY_VERBS: Tuple[str, ...] = ("observe", "assert", "wait")

# Exploration outcomes. `destructive_refused` is the one addition to the
# authoring agent's closed set — refusing is a normal result, not a failure.
CATEGORIES = ("selector_not_found", "login_failed", "timeout", "overlay_blocking",
              "network_error", "unexpected_content", "skipped",
              "destructive_refused", "other")

_MARKER = re.compile(r"^(FLOW_STEP|PAGE_STATE|PAGE_ENTER|SELECTOR_COUNT|"
                     r"OUTCOME_OBSERVED|VALUE_CHECK|REFUSED|UNREACHABLE_STATE|"
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


# What the explorer is told the checker flags. The checker reads a control's
# name, not what the control does, so an explorer left to judge "would this
# change data?" walks a navigation button called Checkout and has the whole run
# thrown away afterwards. Told the words, it refuses up front and says which
# control stopped it. `destructive_token` matches every entry here, and every
# entry of DESTRUCTIVE is listed: a unit test holds both.
DESTRUCTIVE_WORDS: Tuple[str, ...] = (
    "delete", "remove", "deactivate", "terminate", "purchase", "buy", "pay",
    "payment", "transfer", "withdraw", "publish", "archive", "revoke", "refund",
    "unsubscribe", "wipe", "destroy", "reset", "disable", "checkout", "check out",
    "place order", "submit payment", "confirm & pay", "confirm and pay",
    "send invite", "close account", "cancel subscription",
)


# What to do about a walk the rule above stopped, for the messages that report one.
SANDBOX_HINT = ("If this environment is disposable, turn on Sandbox Environment "
                "(ADAPTATION_SANDBOX=true with ADAPTATION_SANDBOX_NOTE) and run again.")


def violations_text(flow: Dict) -> str:
    """`BUY NOW ('buy'), CHECKOUT ('checkout')` — the walked controls the checker
    flagged, for a message a reader can act on."""
    return ", ".join(f"{v.get('target') or 'step ' + str(v.get('index'))} "
                     f"('{v.get('token')}')" for v in flow.get("violations") or [])


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


def simple_parts(selector: str) -> Optional[Dict]:
    """A selector taken apart into what an inventory element can be checked against.

    `{"path", "tag", "ids", "classes", "attrs"}`, where `path` is the iframe hops
    the selector enters (`frames.split`) and `attrs` is `(name, op, value)` with
    `value` None for a bare `[name]`. None when the selector is richer than this
    grammar can honestly evaluate — xpath, combinators, :has-text.
    """
    path, selector = frames.split(selector or "")
    normalized = normalize_selector(selector)
    if not normalized:
        return None
    match = _SIMPLE_SELECTOR.match(normalized.strip())
    if not match:
        return None

    rest = match.group("rest") or ""
    # Ids and classes come from outside the [...] attribute parts only: SauceDemo's
    # `[data-test='add-to-cart-test.allthethings()-t-shirt-(red)']` otherwise
    # demanded a class "allthethings" and recounted a unique button as zero.
    bare = re.sub(r"\[(?:\"[^\"]*\"|'[^']*'|[^\]])*\]", "", rest)
    # finditer, not findall: findall reports a non-participating alternation
    # branch as "" rather than None, so `[data-cy='go']` came back with an empty
    # expected value and matched nothing. Group participation is the signal here.
    attrs = []
    for m in _ATTR.finditer(rest):
        name, op = m.group(1), m.group(2) or ""
        value = next((g for g in (m.group(3), m.group(4), m.group(5))
                      if g is not None), None)
        attrs.append((name, op, value))
    return {
        "path": path,
        "tag": (match.group("tag") or "").lower(),
        "ids": re.findall(r"#([\w-]+)", bare),
        "classes": re.findall(r"\.([\w-]+)", bare),
        "attrs": attrs,
    }


def element_value(element: Dict, name: str) -> Optional[str]:
    """One attribute of an inventory element: top-level first, then `attributes`."""
    actual = element.get(name)
    if actual is None:
        actual = (element.get("attributes") or {}).get(name)
    return None if actual is None else str(actual)


def _matches(element: Dict, parts: Dict) -> bool:
    if parts["tag"] and str(element.get("tag", "")).lower() != parts["tag"]:
        return False
    if parts["ids"] and element.get("id") not in parts["ids"]:
        return False
    element_classes = set(str(element.get("class", "")).split())
    if parts["classes"] and not set(parts["classes"]) <= element_classes:
        return False
    for name, op, value in parts["attrs"]:
        actual = element_value(element, name)
        if actual is None:
            return False
        if value is None:
            continue
        if op == "*" and value not in actual:
            return False
        if op == "^" and not actual.startswith(value):
            return False
        if op == "$" and not actual.endswith(value):
            return False
        if op == "" and actual != value:
            return False
    return True


def elements_matching(selector: str, elements: List[Dict]) -> Optional[List[Dict]]:
    """The inventory elements a selector matches, or None if unevaluable.

    Same contract as `count_in_inventory`, returning the elements themselves
    for a caller that needs to look at what matched, not only how many.
    """
    parts = simple_parts(selector)
    if parts is None:
        return None
    # Inside an iframe, only that iframe's inventory entries are candidates.
    return [e for e in elements or []
            if frames.prefix_path(e.get("frame") or "") == parts["path"]
            and _matches(e, parts)]


def count_in_inventory(selector: str, elements: List[Dict]) -> Optional[int]:
    """How many inventory elements a selector matches, or None if unevaluable.

    `None` and `0` must stay distinct at every call site — the same rule
    `page_identity.normalize_selector` documents. "I could not check" is not
    "it matches nothing", and treating them alike is how an unverifiable guess
    gets recorded as a verified absence.
    """
    matched = elements_matching(selector, elements)
    return None if matched is None else len(matched)


def target_label(step: Dict) -> Optional[str]:
    """The name a reader would know a step's control by."""
    target = (step.get("action") or {}).get("target") or {}
    return target.get("accessible_name") or target.get("name")


def is_destructive(step: Dict) -> Optional[str]:
    """The destructive token a step's target matches, if any."""
    target = (step.get("action") or {}).get("target") or {}
    haystack = " ".join(str(v) for v in (
        target.get("accessible_name"), target.get("name"), target.get("text"),
        (step.get("action") or {}).get("value")) if v)
    return destructive_token(haystack) or None


def _json_tail_complete(kind: str, payload: str) -> bool:
    """Whether a FLOW_STEP / PAGE_STATE payload's JSON is whole yet."""
    body = payload if kind == "FLOW_STEP" else (payload.split("|", 2) + ["", ""])[2]
    try:
        json.loads(body)
        return True
    except ValueError:
        return False


_VERB = re.compile(r"[a-z_]+")


def _unmarked_step(line: str) -> Optional[Dict]:
    """A FLOW_STEP's object on a line of its own with no marker, or None.

    Only the shape a step has: an integer index and an action with one verb. The
    rules' own example, echoed back, lists its verbs as alternatives and is not one.
    """
    text = line.strip("`* \t,")
    if not text.startswith("{"):
        return None
    try:
        step = json.loads(text)
    except ValueError:
        return None
    if not isinstance(step, dict) or not isinstance(step.get("index"), int) \
            or isinstance(step.get("index"), bool):
        return None
    action = step.get("action")
    verb = action.get("verb") if isinstance(action, dict) else None
    return step if isinstance(verb, str) and _VERB.fullmatch(verb) else None


def parse_markers(stdout: str) -> Dict:
    """Every marker in an exploration's stdout, in the order it was emitted.

    Emit-as-you-go means a run killed at its budget still yields a valid prefix,
    so a malformed line is skipped rather than fatal: losing one step is much
    better than losing the fourteen that came before it.
    """
    flow: Dict = {
        "schema_version": SCHEMA_VERSION, "steps": [], "pages": {},
        "unreachable": [], "refusals": [], "outcomes": [], "value_checks": [],
        "notes": [], "legacy_selectors": {},
    }
    inventories: Dict[str, List[Dict]] = {}
    page_enters: Dict[str, Dict] = {}
    unmarked: List[Dict] = []

    lines = (stdout or "").splitlines()
    position = 0
    while position < len(lines):
        line = lines[position]
        position += 1
        # Markdown decoration is tolerated: an observed run wrote every marker as
        # **FLOW_STEP:** `{...}`, and a walk with verified selectors parsed as empty.
        match = _MARKER.match(line.strip("`* \t"))
        if not match:
            # So is a step written as the bare object. A walk put every one of its
            # steps in one fenced block at the end, each complete on its own line
            # and none with `FLOW_STEP:` before it, and the whole walk read as empty.
            step = _unmarked_step(line)
            if step is not None:
                unmarked.append(step)
            continue
        kind, payload = match.group(1), match.group(2).strip("`* \t")
        # So is pretty-printed JSON. Every page of an observed run had its PAGE_STATE
        # element list one element per line, the parser saw only "[", and every
        # selector downstream stayed unverified. Stops at the next marker.
        if kind in ("FLOW_STEP", "PAGE_STATE"):
            while position < len(lines) and not _json_tail_complete(kind, payload) \
                    and not _MARKER.match(lines[position].strip("`* \t")):
                payload = (payload + "\n" + lines[position]).strip("`* \t")
                position += 1
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
            elif kind == "VALUE_CHECK":
                # Both sides of a comparison; the relation is measured in Python.
                check = value_match.parse_value_check(payload)
                if check:
                    flow["value_checks"].append(check)
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

    # A model that restates its walk in a closing summary emits every step twice;
    # the restatement is the same step, so the last copy of an index stands.
    by_index: Dict = {}
    for step in flow["steps"]:
        key = step.get("index") if isinstance(step.get("index"), int) else id(step)
        by_index.pop(key, None)
        by_index[key] = step
    # An unmarked object fills an index no marked step has, and never replaces one:
    # a recap after a walk that did mark its steps is the recap, not the record.
    recap: Dict = {step["index"]: step for step in unmarked}
    by_index.update({index: step for index, step in recap.items()
                     if index not in by_index})
    flow["steps"] = list(by_index.values())

    for page_id, enter in page_enters.items():
        flow["pages"][page_id] = facts_from_markers(enter, inventories.get(page_id, []))
    flow["_inventories"] = inventories
    return flow


def _unique(counted: Optional[Dict]) -> bool:
    return bool(counted) and counted.get("total") == 1 and counted.get("visible") == 1


# A selector that takes one of several matches by where it comes among them.
_POSITIONAL = re.compile(r">>\s*nth\s*=|:nth-match\(")


def positional(selector: str) -> bool:
    """Whether a selector singles its element out by position (`>> nth=1`).

    Counted, such a selector always reads as one element, and it proves nothing:
    it matches several, and which of them it picks moves with the page. A walk
    met two buttons under one selector, took the second, and it was the other
    section's.
    """
    return bool(_POSITIONAL.search(selector or ""))


def _merge_known(before: Optional[List[Dict]], now: List[Dict]) -> List[Dict]:
    """One page's known-locator counts across its states, best reading kept.

    A locator that resolved in any state of the page resolves on it: a toast
    counted while it showed is not broken because the last state came after it
    closed.
    """
    rank = lambda e: (2 if _unique(e) else 1 if e.get("total") else 0)
    best = {(e.get("owner"), e.get("name"), e.get("selector")): e for e in before or []}
    for entry in now:
        key = (entry.get("owner"), entry.get("name"), entry.get("selector"))
        if key not in best or rank(entry) > rank(best[key]):
            best[key] = entry
    return list(best.values())


def read_evidence(path) -> List[Dict]:
    """The rows page.qa wrote to its evidence file, one JSON object per line.

    [] when there is no file. A torn last line — a run killed mid-write — is
    skipped, like a malformed marker.
    """
    rows: List[Dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except (OSError, TypeError):
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# Words that name a control's kind, not which control it is.
_CONTROL_WORDS = {"button", "btn", "field", "input", "icon", "link", "text", "label",
                  "option", "tab", "the", "and"}


def naming_words(text: str) -> set:
    """camelCase or prose split into lowercase words. Plainer than
    check_provenance.subject_words, which drops "checkout" as a form of "check"."""
    return {w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+", text or "")
            if len(w) >= 3} - _CONTROL_WORDS


def clicked_controls(rows: List[Dict]) -> List[Dict]:
    """Every click the helpers recorded on exactly one visible element, in order:
    {"sel", "text", ...}, the selector counted where the control lives."""
    clicks = [r["clicked"] for r in rows or [] if isinstance(r.get("clicked"), dict)]
    return [c for c in clicks
            if c.get("sel") and c.get("total") == 1 and c.get("visible") == 1]


def clicks_named(rows: List[Dict], name: str) -> List[Dict]:
    """The recorded clicks whose text shares a word with a locator name, one per
    selector, most shared words first. Each carries "shared", the word count.

    `PageName.field` is matched on the field. `buyNowButton` shares one word with
    "Pay now" and two with "BUY NOW".
    """
    words, best = naming_words(name.rsplit(".", 1)[-1]), {}
    for c in clicked_controls(rows):
        n = len(words & naming_words(c.get("text") or ""))
        if n and n > best.get(c["sel"], {}).get("shared", 0):
            best[c["sel"]] = {**c, "shared": n}
    return sorted(best.values(), key=lambda c: -c["shared"])


# How many unreported actions a report lists before it counts the rest.
UNREPORTED_SHOWN = 12


def recorded_actions(rows: List[Dict]) -> List[Dict]:
    """Every click and keystroke the helpers recorded, in order, one per control.

    [{"kind": "click" | "fill", "sel", "text", "value", "uid", "uids", "total",
    "visible", "page"}].
    The model has no part in this record: the helpers write a row the moment the
    browser dispatches the event, so it holds what a step list can leave out.
    Rows that follow one another on the same control are one action — a field
    writes a row per change. So is a field's change row arriving late with the
    value it already had: a field reports its change when focus leaves it, which
    can be several steps after the typing. And so is the click a label hands on
    to the control it labels (`via`): one action, whose `uids` then hold both
    elements. `value` is the last value the control was given. `page` is the state
    measured next, when a step named it: the page the action led to, where its
    FLOW_STEP is filed.
    """
    actions: List[Dict] = []
    waiting: List[Dict] = []
    previous: Optional[Dict] = None
    holds: Dict = {}                  # control -> the value it was last given
    for row in rows or []:
        kind = ("click" if isinstance(row.get("clicked"), dict)
                else "fill" if isinstance(row.get("typed"), dict) else "")
        if not kind:
            # An action belongs to the next state measured and to no later one.
            for action in waiting:
                action["page"] = row.get("page") or ""
            waiting, previous = [], None
            continue
        raw = row.get("clicked") or row.get("typed")
        control = raw.get("uid") or raw.get("sel")
        if previous is not None and kind == "click" and raw.get("via") \
                and raw["via"] in previous["uids"]:
            if raw.get("uid") and raw["uid"] not in previous["uids"]:
                previous["uids"].append(raw["uid"])
            continue
        if previous is not None and control \
                and control in (previous["uids"] or [previous.get("sel")]):
            if kind == "click":
                previous["kind"] = "click"
            else:
                holds[control] = raw.get("value")
                previous["value"] = raw.get("value")
            continue
        if kind == "fill" and control:
            if control in holds and holds[control] == raw.get("value"):
                continue
            holds[control] = raw.get("value")
        previous = {"kind": kind, "sel": raw.get("sel"), "text": raw.get("text") or "",
                    "value": raw.get("value") if kind == "fill" else None,
                    "uid": raw.get("uid"), "uids": [raw["uid"]] if raw.get("uid") else [],
                    "total": raw.get("total"), "visible": raw.get("visible"), "page": ""}
        actions.append(previous)
        waiting.append(previous)
    return actions


def _counted_at(rows: List[Dict]) -> Dict[str, set]:
    """{selector: the identities of the elements the helpers counted it at}."""
    found: Dict[str, set] = {}
    for row in rows or []:
        for selector, counted in (row.get("checks") or {}).items():
            if isinstance(counted, dict) and counted.get("uid"):
                found.setdefault(selector, set()).add(counted["uid"])
        for entry in row.get("known") or []:
            if entry.get("selector") and entry.get("uid"):
                found.setdefault(entry["selector"], set()).add(entry["uid"])
    return found


def _same_control(selector: str, action: Dict, counted_at: Dict[str, set]) -> bool:
    """Whether `selector` names the control a recorded action landed on: the same
    spelling, or one the helpers counted at the same element."""
    return bool(selector) and (
        selector == action.get("sel")
        or any(uid in counted_at.get(selector, ()) for uid in _uids(action)))


def _uids(action: Dict) -> List[str]:
    return action.get("uids") or ([action["uid"]] if action.get("uid") else [])


def _step_for(action: Dict, steps: List[Dict], counted_at: Dict[str, set]) -> Optional[Dict]:
    """The reported step that used a recorded control: one with the same selector,
    or one whose selector was counted at the same element under another spelling."""
    for step in steps or []:
        target = (step.get("action") or {}).get("target") or {}
        if any(_same_control(target.get(key) or "", action, counted_at)
               for key in ("selector", "reported_selector")):
            return step
    return None


def _unique_at(rows: List[Dict]) -> Dict[str, List[str]]:
    """{element: the selectors the helpers counted on it alone, visible}.

    The repo's own locators first: a control a page object already has a working
    locator for is known by that one.
    """
    found: Dict[str, List[str]] = {}

    def keep(selector, counted):
        if selector and isinstance(counted, dict) and counted.get("uid") and _unique(counted) \
                and not positional(selector) \
                and selector not in found.setdefault(counted["uid"], []):
            found[counted["uid"]].append(selector)

    for row in rows or []:
        for entry in row.get("known") or []:
            keep(entry.get("selector"), entry)
    for row in rows or []:
        for selector, counted in (row.get("checks") or {}).items():
            keep(selector, counted)
    return found


# What a step's verb can have been recorded as. A field that was clicked and then
# typed into is recorded as a click that holds a value.
_CLICK_VERBS = ("click", "dismiss", "check", "uncheck")
_FILL_VERBS = ("fill", "type")


def _text_key(text) -> str:
    return " ".join(str(text or "").split()).lower()


def _page_of(step: Dict) -> str:
    ref = step.get("page")
    return (ref.get("id") if isinstance(ref, dict) else ref) or ""


def _named_by(step: Dict, action: Dict) -> bool:
    """Whether a recorded action is, by what the page itself said, the one a step
    reports: a click on a control whose text is the step's accessible name, or a
    field given the value the step says it typed."""
    verb = ((step.get("action") or {}).get("verb") or "").lower()
    target = (step.get("action") or {}).get("target") or {}
    if verb in _CLICK_VERBS:
        name = _text_key(target.get("accessible_name"))
        return bool(name) and name == _text_key(action.get("text"))
    if verb in _FILL_VERBS:
        value = (step.get("action") or {}).get("value")
        return value not in (None, "") and str(value) == str(action.get("value"))
    return False


def _kind_fits(step: Dict, action: Dict) -> bool:
    verb = ((step.get("action") or {}).get("verb") or "").lower()
    if verb in _CLICK_VERBS:
        return action.get("kind") == "click"
    if verb in _FILL_VERBS:
        return action.get("kind") == "fill" or action.get("value") not in (None, "")
    return verb == "select"


def _known_by(action: Dict, unique_at: Dict[str, List[str]]) -> str:
    """The selector to know a recorded control by: the one it was recorded under
    when that matched it alone, visible, else one the helpers counted on the same
    element alone. "" when nothing counted singles it out."""
    if action.get("sel") and _unique(action):
        return action["sel"]
    for uid in _uids(action):
        if unique_at.get(uid):
            return unique_at[uid][0]
    return ""


def _in_order(steps: int, actions: int, fits) -> List[tuple]:
    """The most (step, action) pairs `fits` allows with both in walk order.

    The most, not the first each step finds: a control used twice is recorded
    twice, and a step taking the later use for want of the earlier one would put
    every step between the two out of order.
    """
    best = [[0] * (actions + 1) for _ in range(steps + 1)]
    for i in range(steps - 1, -1, -1):
        for j in range(actions - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1],
                             1 + best[i + 1][j + 1] if fits(i, j) else 0)
    pairs, i, j = [], 0, 0
    while i < steps and j < actions:
        if fits(i, j) and best[i][j] == 1 + best[i + 1][j + 1]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def join_record(flow: Dict, rows: List[Dict]) -> Dict:
    """Tie each reported step to the action the browser recorded for it.

    A walk has two accounts: the steps the model typed and the clicks and
    keystrokes the helpers recorded. A step is verified only through a selector
    the helpers counted, and the model types its own spelling: a walk clicked a
    button the helpers recorded on exactly one visible element, reported the
    click under a selector nothing had counted, and the same click was listed
    twice, once as an unverified step and once as an action no step reported.
    The change item it belonged to was declined for want of evidence.

    Matched in the order the walk happened, each action to one step:

      1. by element — the step names the selector the action was recorded under,
         or one the helpers counted at the same element;
      2. by what the page said — between the actions its neighbours matched, the
         one control whose text is the step's accessible name, or the one field
         given the value the step typed;
      3. by position — the only unmatched step between two matched ones, and the
         only action of its kind recorded between theirs.

    A matched step whose own selector was not counted unique takes the selector
    the control is known by (`_known_by`); what the model typed is kept as
    `reported_selector`. Nothing is adopted from an action no count singles out,
    so a step can still end unverified. Never a step from an action alone: the
    record also holds what an explorer clicked to look around.
    """
    actions = recorded_actions(rows)
    steps = [s for s in flow.get("steps") or []
             if ((s.get("action") or {}).get("verb") or "").lower()
             in _CLICK_VERBS + _FILL_VERBS + ("select",)
             and (s.get("result") or {}).get("outcome", "ok") == "ok"]
    flow["_joined"] = []
    counted_at, unique_at = _counted_at(rows), _unique_at(rows)
    if not actions or not steps:
        _mark_modelled(steps, {}, rows, counted_at)
        return flow
    matched: Dict[int, tuple] = {}            # position in `steps` -> (action position, how)
    claimed: set = set()

    def take(index: int, position: int, how: str) -> None:
        matched[index] = (position, how)
        claimed.add(position)

    def fits_page(step: Dict, action: Dict) -> bool:
        return not action.get("page") or not _page_of(step) or action["page"] == _page_of(step)

    def same_element(selector: str, action: Dict) -> bool:
        # A spelling several elements share says nothing about which one was used.
        return bool(selector) and (
            (selector == action.get("sel") and _unique(action))
            or any(uid in counted_at.get(selector, ()) for uid in _uids(action)))

    selectors = [((s.get("action") or {}).get("target") or {}).get("selector") or ""
                 for s in steps]
    for index, position in _in_order(
            len(steps), len(actions),
            lambda i, j: same_element(selectors[i], actions[j])):
        take(index, position, "element")

    def between(index: int) -> List[int]:
        """Unclaimed actions recorded between what the step's neighbours matched."""
        lower = max((matched[i][0] for i in matched if i < index), default=-1)
        upper = min((matched[i][0] for i in matched if i > index), default=len(actions))
        return [p for p in range(lower + 1, upper) if p not in claimed]

    def one_control(positions: List[int]) -> Optional[int]:
        controls = {actions[p].get("uid") or actions[p].get("sel") for p in positions}
        return positions[0] if positions and len(controls) == 1 else None

    for index, step in enumerate(steps):
        if index in matched:
            continue
        position = one_control([p for p in between(index)
                                if _named_by(step, actions[p]) and fits_page(step, actions[p])])
        if position is not None:
            take(index, position, "name")

    for index, step in enumerate(steps):
        if index in matched:
            continue
        lower = max((i for i in matched if i < index), default=-1)
        upper = min((i for i in matched if i > index), default=len(steps))
        if any(i not in matched for i in range(lower + 1, upper) if i != index):
            continue                    # two unmatched steps share the gap: no telling
        fitting = [p for p in between(index)
                   if _kind_fits(step, actions[p]) and fits_page(step, actions[p])]
        position = one_control(fitting)
        if position is not None and _known_by(actions[position], unique_at):
            take(index, position, "position")

    live = flow.setdefault("_live", {})
    for index, (position, how) in sorted(matched.items()):
        step, action = steps[index], actions[position]
        step["recorded_action"] = position
        target = step["action"].setdefault("target", {})
        reported = target.get("selector") or ""
        if reported and not positional(reported) \
                and _unique(live.get(_page_of(step), {}).get(reported)):
            continue                    # its own selector was counted, and stands
        known = _known_by(action, unique_at)
        if not known or known == reported:
            continue
        target["reported_selector"] = reported
        target["selector"] = known
        live.setdefault(_page_of(step), {})[known] = {"total": 1, "visible": 1}
        step.setdefault("selector_check", {})["adopted"] = how
    flow["_joined"] = sorted(claimed)
    _mark_modelled(steps, {id(steps[index]): actions[position]
                           for index, (position, _how) in matched.items()}, rows, counted_at)
    return flow


def _mark_modelled(steps: List[Dict], action_of: Dict[int, Dict], rows: List[Dict],
                   counted_at: Dict[str, set]) -> None:
    """Mark each step whose control a page object already has a locator for.

    `modelled_by` is `Owner.field`: the repo's own locator, counted on the live
    page on the very element the step used. Measured rather than matched on
    names, which an explorer makes up and a page object spells its own way: read
    by name, no step of a walk was ever one the tests already made.
    """
    known_at: Dict[str, str] = {}
    for row in rows or []:
        for entry in row.get("known") or []:
            if entry.get("uid") and _unique(entry):
                known_at.setdefault(entry["uid"],
                                    f"{entry.get('owner') or '?'}.{entry.get('name') or '?'}")
    for step in steps:
        target = (step.get("action") or {}).get("target") or {}
        uids = list(_uids(action_of.get(id(step)) or {}))
        for key in ("selector", "reported_selector"):
            uids += sorted(counted_at.get(target.get(key) or "", ()))
        owner = next((known_at[uid] for uid in uids if uid in known_at), "")
        if owner:
            step["modelled_by"] = owner


def unreported_actions(flow: Dict, rows: List[Dict]) -> List[Dict]:
    """What the browser recorded being done that no reported step accounts for.

    [{"kind", "name", "selector", "page"}], in the order it happened. Only an
    action on exactly one visible element is listed: the hidden input behind a
    label fires its own click when the label is clicked, and is not a second
    action. These are shown to a reader and are not steps — an explorer also
    clicks to look around, and a step is what an edit may be justified by.
    """
    counted_at = _counted_at(rows)
    steps = flow.get("steps") or []
    joined = set(flow.get("_joined") or [])
    return [{"kind": action["kind"],
             "name": action["text"] or frames.split(action["sel"])[1],
             "selector": action["sel"], "page": action["page"]}
            for position, action in enumerate(recorded_actions(rows))
            if action.get("sel") and action.get("total") == 1 and action.get("visible") == 1
            and position not in joined
            and _step_for(action, steps, counted_at) is None]


def apply_evidence(flow: Dict, rows: List[Dict]) -> Dict:
    """Fold what the browser helpers measured into a parsed flow map.

    Measured, not reported: an inventory here was read from the page by
    page.qa, and a count was counted by it. Three things come of that.

      * A page is inventoried whether or not the model typed a PAGE_STATE for
        it — 30 of 87 explorer selectors were unverifiable only because it had
        not. The model's own inventory, when there is one, is kept alongside.
      * A selector the helpers counted on a page has a live count there
        (`_live`), which verify_selectors prefers to a recount against a
        25-element sample — and which covers what a recount cannot evaluate:
        :has-text(), role=, frame chains.
      * The repo's own locators, counted on every page state (`_known`), say
        which page object each page is and which of its locators broke.

    A row names its page by the id the model gave the step; an untagged row is
    placed by URL.
    """
    if not rows:
        return flow
    inventories = flow.setdefault("_inventories", {})
    pages = flow.setdefault("pages", {})
    live = flow.setdefault("_live", {})
    known = flow.setdefault("_known", {})
    # The order the walk first reached each page, from rows a step named. A row
    # placed by URL is left out: where several pages share one URL it cannot say
    # which of them it saw.
    order = flow.setdefault("_page_order", [])
    # And the walk as it went, a page named again each time it is come back to:
    # what a page is compared with is the page the walk was on just before it.
    walk = flow.setdefault("_page_walk", [])
    by_url = {page.get("url"): pid for pid, page in pages.items() if page.get("url")}
    grown = set()
    # Clicks and keystrokes since the last measured state, each on exactly one
    # visible element. The helpers count a control at the moment it is used, which
    # is the only time some can be counted: a tab, a close icon or the button that
    # opens the next screen is gone by the time the step's own count is taken, and
    # read 0 there although the selector had been unique.
    acted: List[Dict] = []
    for row in rows:
        action = row.get("clicked") or row.get("typed")
        if isinstance(action, dict):
            if action.get("sel") and action.get("total") == 1 \
                    and action.get("visible") == 1:
                acted.append(action)
            continue
        # An action belongs to the next state measured and to no later one.
        used, acted = acted, []
        page_id = row.get("page") or by_url.get(row.get("url"))
        if not page_id:
            continue
        if row.get("page") and page_id not in order:
            order.append(page_id)
        if row.get("page") and walk[-1:] != [page_id]:
            walk.append(page_id)
        if page_id not in pages:
            pages[page_id] = facts_from_markers(
                {"id": page_id, "url": row.get("url", ""), "title": ""}, [])
            by_url.setdefault(row.get("url"), page_id)
        seen = {json.dumps(e, sort_keys=True) for e in inventories.get(page_id, [])}
        for element in row.get("inventory") or []:
            key = json.dumps(element, sort_keys=True)
            if key not in seen:
                seen.add(key)
                inventories.setdefault(page_id, []).append(element)
                grown.add(page_id)
        counts = live.setdefault(page_id, {})
        measured = [(sel, c) for sel, c in (row.get("checks") or {}).items()
                    if isinstance(c, dict) and "total" in c]
        if row.get("known"):
            known[page_id] = _merge_known(known.get(page_id), row["known"])
            measured +=[(e["selector"], {"total": e["total"], "visible": e.get("visible")})
                         for e in row["known"] if e.get("selector") and "total" in e]
        # Filed under the page the step named — the one it lands on, which is where
        # its FLOW_STEP is filed too. A row placed by URL names no step's page.
        if row.get("page"):
            measured += [(a["sel"], {"total": 1, "visible": 1}) for a in used]
        for selector, counted in measured:
            # One page, several states: a selector unique in one of them is
            # unique, so a later state where it is gone never overwrites that.
            if not _unique(counts.get(selector)) or _unique(counted):
                counts[selector] = counted
    for page_id in grown:
        page = pages[page_id]
        pages[page_id] = facts_from_markers(
            {"id": page_id, "url": page.get("url", ""), "title": page.get("title", "")},
            inventories[page_id])
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
        if positional(candidate):
            # Whatever it counted, the position is what picked the element.
            check.update({"match_count": None, "counted_against": "none",
                          "unique": False, "positional": True})
            continue
        # Counted on the live page by the helpers: proof, where a recount against
        # the inventory is only "consistent with unique".
        counted = (flow.get("_live") or {}).get(page_id, {}).get(candidate)
        if counted is not None:
            total, visible = counted.get("total"), counted.get("visible")
            check.update({"match_count": total, "visible_count": visible,
                          "counted_against": "live",
                          "unique": total == 1 and visible == 1})
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


def detect_refusals(flow: Dict, sandbox: bool = False,
                    rows: Optional[List[Dict]] = None) -> Dict:
    """Mark destructive steps, and catch the ones that were not refused.

    A refusal is a normal outcome. A destructive action that *succeeded* is a
    protocol violation: the model did the thing it was told not to do, and the
    run's own evidence can no longer be trusted to be side-effect free.

    `sandbox` — the operator asserted a disposable environment, so the explorer
    was told to complete the flow. Walking a flagged control is then what it was
    asked to do: the token stays on the step, and nothing is a violation.

    `rows` — what the browser helpers recorded. The step list is the model's own
    account, and a click it leaves out was never checked: a walk clicked through
    to a completed payment, reported no step, and read as empty rather than
    unsafe. So every recorded click is judged too, by the text of the control it
    landed on, whatever a step says about it — unreported, reported as refused,
    or reported as only looked at.
    """
    violations = []
    for step in flow.get("steps") or []:
        token = is_destructive(step)
        if not token:
            continue
        step.setdefault("result", {})["destructive_token"] = token
        # The token is still recorded above, so a read-only look at a dangerous
        # control stays visible in the flow map — it just is not a violation.
        if ((step.get("action") or {}).get("verb") or "").strip().lower() in READ_ONLY_VERBS:
            continue
        if (step["result"].get("outcome") or "") == "ok" and not sandbox:
            violations.append({"index": step.get("index"), "token": token,
                               "target": target_label(step)})
    if rows and not sandbox:
        counted_at = _counted_at(rows)
        caught = {v["index"] for v in violations}
        seen = set()
        joined = {s["recorded_action"]: s for s in flow.get("steps") or []
                  if "recorded_action" in s}
        for position, action in enumerate(recorded_actions(rows)):
            token = destructive_token(action["text"]) if action["kind"] == "click" else ""
            step = (joined.get(position)
                    or _step_for(action, flow.get("steps") or [], counted_at)) if token else None
            index = step.get("index") if step else None
            # Clicked twice is one finding; a step already caught is that finding.
            key = index if step else action["text"].lower()
            if not token or key in seen or (step and index in caught):
                continue
            seen.add(key)
            violations.append({"index": index, "token": token,
                               "target": action["text"], "recorded": True})
    flow["violations"] = violations
    if sandbox:
        flow["sandbox"] = True
    return flow


def build(stdout: str, evidence: Optional[List[Dict]] = None,
          sandbox: bool = False) -> Dict:
    """The whole pipeline: markers in, verified ordered flow map out.

    `evidence` — the rows the browser helpers measured (read_evidence) — is
    folded in before identity and selector verification, so both use it.
    """
    rows = evidence or []
    flow = attach_identity(apply_evidence(parse_markers(stdout), rows))
    flow = verify_selectors(join_record(flow, rows))
    flow["unreported_actions"] = unreported_actions(flow, rows)
    flow = detect_refusals(flow, sandbox, rows)
    flow["status"] = ("unsafe" if flow.get("violations")
                      else "partial" if flow.get("unreachable")
                      else "ok" if flow.get("steps") else "empty")
    return flow


def diff_against_test(flow: Dict, test_steps: List[Dict]) -> Dict:
    """What the product does now that the tests do not do yet.

    `test_steps` are `{index, target}` derived from source. Each observed step is
    marked with its counterpart (`maps_to_test`), which the guards read; one with
    no counterpart is reported as added — unless the walk measured its control to
    be one a page object already has a locator for (`modelled_by`), which the
    tests can already reach whatever either side calls it. Nothing is guessed.
    What the tests do that the product no longer does is not derivable from
    `{index, target}`, so it is not reported here — the adapt step measures
    removed checks directly.
    """
    added = []
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
            if not step.get("modelled_by"):
                added.append({"flow_index": step.get("index"),
                              "why": f"no test step reaches "
                                     f"{target.get('accessible_name') or target.get('name')}"})

    return {"added": added}


def pages_without_inventory(flow: Dict) -> List[str]:
    """Pages the run walked but never described.

    A page with no `PAGE_STATE` cannot have its selectors recounted and cannot be
    matched to a page object, so everything downstream treats it as unverifiable.
    An observed run emitted `PAGE_ENTER` for three pages, an inventory for none of
    them, and still reported `ok`: no page object matched anything, every selector
    stayed `unique: unverified`, and the adapt step was told the repo had no page
    object for pages it has had for months.
    """
    inventories = flow.get("_inventories") or {}
    return sorted(p for p in (flow.get("pages") or {}) if not inventories.get(p))


def pages_acted_on(flow: Dict) -> set:
    """Ids of the pages a flow step acted on, as opposed to pages only passed through."""
    ids = set()
    for step in flow.get("steps") or []:
        ref = step.get("page")
        ids.add(ref.get("id") if isinstance(ref, dict) else ref)
    return ids - {None, ""}


def _names_control(noun: str, words: set) -> bool:
    """Whether a control's naming words carry a noun from the change note.

    Half the noun's words, rounded up, each found whole or as the stem of one: a
    control the note calls "member's passport number" is reported as
    `membersPassport`. Loose on purpose. Reading a named control as missing costs a
    second walk of the whole flow; reading a missing one as present costs nothing
    that was not already lost.
    """
    wanted = naming_words(noun)
    found = sum(1 for want in wanted
                if any(have.startswith(want) or want.startswith(have) for have in words))
    return bool(wanted) and found * 2 >= len(wanted)


def evidence_for(flow: Dict, nouns: List[str], page_hint: str = "") -> Optional[Dict]:
    """What a walk recorded for the controls one change item names.

    `{"controls", "verified", "unverified", "by_control"}`: the nouns that name a
    control, and the indices of the steps that acted on one, split by whether the
    step's selector was counted unique — over all of them, and per control. None
    when the item names no control: a noun that is a page (`page_hint`, or the id
    of a page the walk entered) is where the change happens, not something a step
    acts on.

    A step is all an edit may be justified by, so an item whose controls no
    verified step acted on cannot be adapted whatever else the browser did.
    """
    pages = [naming_words(page_hint)] + [naming_words(pid) for pid in flow.get("pages") or {}]
    controls = [noun for noun in nouns or []
                if naming_words(noun) and not any(
                    page and naming_words(noun) <= page for page in pages)]
    if not controls:
        return None
    by_control = {noun: {"verified": [], "unverified": []} for noun in controls}
    for step in flow.get("steps") or []:
        action = step.get("action") or {}
        if (action.get("verb") or "").lower() in READ_ONLY_VERBS + ("navigate",) \
                or (step.get("result") or {}).get("outcome", "ok") != "ok":
            continue
        target = action.get("target") or {}
        words = naming_words(target.get("name") or "") \
            | naming_words(target.get("accessible_name") or "")
        unique = (step.get("selector_check") or {}).get("unique") is True
        for noun in controls:
            if _names_control(noun, words):
                by_control[noun]["verified" if unique else "unverified"].append(
                    step.get("index"))

    def every(key: str) -> List:
        return sorted({index for steps in by_control.values() for index in steps[key]},
                      key=lambda index: (index is None, index))
    return {"controls": controls, "verified": every("verified"),
            "unverified": every("unverified"), "by_control": by_control}


def score(flow: Dict, status: str) -> tuple:
    """Rank one exploration attempt against another. Higher is better.

    Ordering copied from the authoring agent's `_score`, and for the same reason:
    a clean finish beats a crash, and thoroughness beats "zero failures because it
    died on step two".
    """
    steps = flow.get("steps") or []
    failed = [s for s in steps if (s.get("result") or {}).get("outcome") == "failed"]
    unique = [s for s in steps if (s.get("selector_check") or {}).get("unique")]
    # Uninventoried pages rank above step count on purpose: more steps across pages
    # nobody described is less usable than fewer steps that can be verified. But
    # having steps at all comes first — an empty walk has no uninventoried pages,
    # and it replaced a five-step one on exactly that tie-break.
    return (1 if status == "ok" else 0, 1 if steps else 0,
            -len(pages_without_inventory(flow)), len(steps), -len(failed), len(unique))


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
    uninventoried = pages_without_inventory(flow)
    if uninventoried:
        problems.append(f"{len(uninventoried)} page(s) were walked without a "
                        f"PAGE_STATE inventory ({', '.join(uninventoried[:4])}) — "
                        f"their selectors cannot be recounted and no page object "
                        f"can be matched to them")
    return (not problems), problems


def _cell(text) -> str:
    """Text for one cell of a markdown table: one line, no column break."""
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")


def describe(flow: Dict, selectors: bool = False) -> str:
    """The flow map as a markdown table, for the audit report and the PR body.

    `selectors` adds each step's name, selector and typed value: what the adapt
    step transcribes from. Shown the control's label alone, it had nothing to
    write a new control's locator from but its own guess.
    """
    # The combined explore flow carries no status of its own; "?" read as broken.
    status = f"**Status:** {flow['status']} — " if flow.get("status") else ""
    lines = [f"{status}{len(flow.get('steps') or [])} step(s)", "",
             "| # | Page | Action | Target | Unique? | Result |"
             + (" Name | Selector | Value |" if selectors else ""),
             "|---|---|---|---|---|---|" + ("---|---|---|" if selectors else "")]
    for step in flow.get("steps") or []:
        action = step.get("action") or {}
        target = action.get("target") or {}
        check = step.get("selector_check") or {}
        result = step.get("result") or {}
        unique = {True: "yes", False: "NO", None: "unverified"}[check.get("unique")]
        if check.get("positional"):
            unique = "NO (picked by position)"
        extra = ""
        if selectors:
            selector = _cell(target.get("selector")).replace("`", "'")
            extra = (f" {_cell(target.get('name'))} | "
                     + (f"`{selector}`" if selector else "") + " | "
                     + (_cell(action.get("value")) if action.get("value") not in (None, "")
                        else "") + " |")
        lines.append(
            f"| {step.get('index')} | {(step.get('page') or {}).get('id','?')} "
            f"| {action.get('verb','?')} "
            f"| {target.get('accessible_name') or target.get('name') or ''} "
            f"| {unique} | {result.get('outcome','?')} |" + extra)
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
        for v in flow["violations"]:
            named = f"{v.get('target') or '?'} (its name contains '{v.get('token')}')"
            if not v.get("recorded"):
                lines.append(f"- step {v.get('index')}: {named}")
            elif v.get("index") is None:
                lines.append(f"- {named} — clicked in the browser, not reported as a step")
            else:
                lines.append(f"- step {v['index']}: {named} — the browser recorded a "
                             f"click on it, whatever the step says")
    if flow.get("sandbox"):
        walked = [s for s in flow.get("steps") or []
                  if (s.get("result") or {}).get("destructive_token")
                  and (s.get("result") or {}).get("outcome") == "ok"]
        if walked:
            lines += ["", "**Walked under the sandbox assertion:**"]
            lines += [f"- step {s.get('index')}: {target_label(s) or '?'}"
                      for s in walked]
    unreported = flow.get("unreported_actions") or []
    if unreported:
        lines += ["", "**Done in the browser but not reported as a step:**"]
        lines += [f"- {a['kind']}: {a['name']}" + (f" (on {a['page']})" if a.get("page") else "")
                  for a in unreported[:UNREPORTED_SHOWN]]
        if len(unreported) > UNREPORTED_SHOWN:
            lines.append(f"- … {len(unreported) - UNREPORTED_SHOWN} more")
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
    known = flow.get("_known") or {}
    came_from = _came_from(flow.get("_page_walk") or [], known)
    for page_id, page in (flow.get("pages") or {}).items():
        # The repo's own locators, counted on this page live: exact, where the
        # inventory reconstruction below is a sample ("zero means not observed").
        if known.get(page_id):
            page["page_objects"], page["best_page_object"] = _live_page_objects(
                known[page_id], known.get(came_from.get(page_id)))
            page["measured_sampled"] = False
            continue
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


def _came_from(walk: List[str], known: Dict[str, List[Dict]]) -> Dict[str, str]:
    """For each page, the page the walk was on before it first reached it.

    `walk` names a page again each time the walk comes back to it. Read from the
    order pages were first reached, a dialog opened from a list on the walk's
    third visit there was compared with the form visited in between: the list's
    locators all read as new on the dialog, and the list's page object took it.

    Only pages whose locators were counted can be compared against, so one the
    helpers never measured is stepped over.
    """
    came_from, previous, reached = {}, "", set()
    for page_id in walk:
        if page_id not in reached:
            reached.add(page_id)
            if previous:
                came_from[page_id] = previous
        if known.get(page_id):
            previous = page_id
    return came_from


def _locator_key(entry: Dict) -> tuple:
    return (entry.get("owner"), entry.get("name"), entry.get("selector"))


def _shown(entry: Dict) -> bool:
    return bool(entry.get("visible", entry.get("total")))


def _live_page_objects(entries: List[Dict],
                       previous: Optional[List[Dict]] = None
                       ) -> Tuple[List[Dict], Optional[Dict]]:
    """Rank page objects by how many of their locators resolve on a page, live.

    Broken locators (no match) are named, because which of an existing page
    object's locators stopped resolving is the question an adaptation run exists
    to answer. Those matching several are listed too, but as `several`, not as a
    fault: a page object's list locator (`.row`, counted) is meant to. A locator
    the page object only uses inline (`inline`, no field declares it) that matched
    nothing is `unseen`, not broken: inline is how a page object asks whether
    something is there right now, and a calendar that opens under a date field
    was reported as that page's one broken locator.

    `previous` is the same count on the page the walk came from. A locator that
    was already showing there says nothing about this page: a form left on screen
    behind an overlay keeps every one of its locators, and by share alone it
    outranks the page object of the overlay in front of it. So the first key is
    how many locators *appeared* here, then the share that is showing.
    """
    shown_before = {_locator_key(e) for e in previous or [] if _shown(e)}
    owners: Dict[str, Dict] = {}
    for entry in entries or []:
        owner = entry.get("owner") or "?"
        report = owners.setdefault(owner, {
            "name": owner, "path": entry.get("path", ""), "matched": 0,
            "evaluable": 0, "broken": [], "several": [], "unseen": [],
            "appeared": 0, "shown": 0})
        if "total" not in entry:
            continue
        report["evaluable"] += 1
        if entry["total"] >= 1:
            report["matched"] += 1
        if _shown(entry):
            report["shown"] += 1
            if _locator_key(entry) not in shown_before:
                report["appeared"] += 1
        name = entry.get("name") or entry.get("selector") or "?"
        if entry["total"] == 0:
            report["unseen" if entry.get("inline") else "broken"].append(name)
        elif entry["total"] > 1:
            report["several"].append(name)
    reports = []
    for report in owners.values():
        ratio = round(report["matched"] / report["evaluable"], 3) if report["evaluable"] else 0.0
        reports.append({**report, "total": report["evaluable"], "ratio": ratio,
                        "sampled": False, "live": True})
    reports.sort(key=lambda r: (-r["appeared"],
                                -(r["shown"] / r["evaluable"] if r["evaluable"] else 0.0),
                                -r["ratio"], -r["matched"], r["name"]))
    best = next((r for r in reports if r["evaluable"] and r["matched"]), None)
    return reports, ({key: best[key] for key in ("name", "path", "matched", "evaluable",
                                                 "ratio", "broken", "several", "unseen",
                                                 "appeared", "sampled", "live")}
                     if best else None)


def describe_page_objects(flow: Dict) -> str:
    """The measured page↔page-object mapping, as markdown."""
    rows = []
    for page_id, page in sorted((flow.get("pages") or {}).items()):
        best = page.get("best_page_object")
        if best and best.get("live"):
            broken = ", ".join(best.get("broken") or [])
            unseen = ", ".join(f"`{name}`" for name in best.get("unseen") or [])
            rows.append(f"| `{page_id}` | `{best['name']}` | "
                        f"{best['matched']}/{best['evaluable']} locators resolve live"
                        + (f"; broken: {broken}" if broken else "")
                        + (f"; used inline and not on the page during the walk: {unseen}"
                           if unseen else "")
                        + (f"; {best['appeared']} first shown on this page"
                           if best.get("appeared") else "") + " |")
        elif best:
            rows.append(f"| `{page_id}` | `{best['name']}` | "
                        f"{best['matched']}/{best['evaluable']} locators matched |")
        elif page.get("page_objects") is not None:
            rows.append(f"| `{page_id}` | _none matched_ | "
                        f"{len(page.get('page_objects') or [])} candidate(s) checked |")
    if not rows:
        return ""
    pages = (flow.get("pages") or {}).values()
    live = [p for p in pages if (p.get("best_page_object") or {}).get("live")]
    note = ("Every locator these page objects declare with a fixed selector was "
            "counted on the live page: one that resolves nowhere is broken, not "
            "unobserved. A selector assembled when the test runs is not counted."
            if live and len(live) == len(rows) else
            "Rows marked \"resolve live\" were counted on the live page; the rest are "
            "measured against the elements exploration reported, a bounded sample — "
            "enough to say which page object fits best, not to prove one is absent."
            if live else
            "Measured against the elements exploration reported, which is a bounded "
            "sample of each page — enough to say which page object fits best, not "
            "enough to prove one is absent.")
    return ("\n| Observed page | Page object | Measured |\n|---|---|---|\n"
            + "\n".join(rows) + "\n\n" + note + "\n")
