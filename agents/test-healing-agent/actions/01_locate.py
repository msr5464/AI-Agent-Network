#!/usr/bin/env python3
"""
Step 01 — Locate

Work out which element a broken locator meant, from the evidence the failing run
already wrote. No model call and no browser: the answer comes from comparing the
fingerprint recorded while the locator still worked against the fingerprint
capture taken at the moment it failed, and the new selector is proved unique
against the DOM saved beside it.

Locate used to drive the application to recreate the failure — signing in,
minting sessions, filling a form — so it could click the candidate and watch it
work. That is where it spent its time and where it broke: one `Page.fill`
timeout, five tests, nothing resolved, having asked a live site for something
that was already sitting in two JSON files on disk. It was also duplicated work.
Fix applies the edit and re-runs the real test, which is a stronger proof than a
click in a scratch browser and happens either way.

So the division of labour is: Locate proposes, with its evidence and with
refusals it can defend; Fix applies, and the test proves.

Writes a resolution per locator. Applying it is 01_fix's job; this step never
edits a file, so a wrong answer here cannot reach the repo on its own.

Reads:   HANDOFF_FILE, AUDIT_DIR, WORKSPACE_DIR, GITHUB_REPO_AUTOMATION,
         HEALING_LOCATE_MODE (shadow|enforce)
Outputs: audit/<session>/01-locate.json + 01-locate.md

Exits 0 in every non-crash case. "No baseline for this locator" and "this is not
locator drift" are both legitimate outcomes, not errors.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → shared.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
def log(msg): _log("locate", msg)

import yaml

from shared import baseline as baseline_store
from shared import dom_snapshot, failure_context, failure_identity, frames
from shared import locator_assertions, locator_patch, page_identity
from shared import locator_capture as capture
from shared import locator_emit as emit_mod
from shared import workspace as workspace_helper
from shared import locator_resolve as engine          # baseline_for: pure, no browser
from shared.locator_candidates import Candidate
from shared import locator_decide as decide_mod
from shared.locator_score import Volatility

AUDIT_DIR    = Path(os.environ["AUDIT_DIR"])
REPO_ROOT    = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
HANDOFF_FILE = Path(os.environ["HANDOFF_FILE"])
CONFIG_FILE  = Path(os.environ.get("HEALING_LOCATE_CONFIG", REPO_ROOT / "config" / "locator.yaml"))

# shadow: locate, record what we would have done, and change nothing.
# enforce: 01_fix applies a resolution instead of calling the model.
#
# Shadow is the default for the same reason DIAGNOSIS_MODE is: this step can
# refuse work the agent used to attempt, and that risk deserves a measurement
# rather than a leap.
HEALING_LOCATE_MODE = os.environ.get("HEALING_LOCATE_MODE", "shadow").strip().lower()

# Which Fix attempt this run precedes. One broken locator hides the next: the
# test cannot reach locator #2 until #1 is repaired and it is re-run, so Locate
# runs once per attempt and works from what the last verification run wrote.
FIX_ATTEMPT = int(os.environ.get("FIX_ATTEMPT", "1") or 1)

HEALED, NO_HEAL = "HEALED", "NO_HEAL"


def _workspace() -> Path | None:
    """The automation checkout: FRAMEWORK_DIR, else WORKSPACE_DIR/repo."""
    candidate = workspace_helper.expected(
        os.environ.get("WORKSPACE_DIR", ""),
        os.environ.get("GITHUB_REPO_AUTOMATION", ""))
    return candidate if candidate and candidate.is_dir() else None


def _page_object_sources(workspace: Path) -> dict:
    """Every page object in the automation repo, by simple class name."""
    sources = {}
    modules = workspace / "src" / "main" / "java"
    if not modules.is_dir():
        return sources
    for path in modules.rglob("*.java"):
        try:
            sources[path.stem] = path.read_text(errors="ignore")
        except OSError:
            continue
    return sources


# BasePage helpers take the locator first and a human element name last:
#   click(editProfileSummaryButton, "Edit Profile Summary button")
#   fillText(summaryTextArea, text, "Profile Summary Text Area")
# The runtime error quotes that name, which makes it a second way to identify the
# field when the selector string itself no longer appears in the source.
_ELEMENT_NAME = re.compile(r"element '([^']{2,80})'")


def _field_by_element_name(sources: dict, error_text: str, prefer: str = ""):
    """Recover the field from the element name the failure quotes.

    Selector matching is exact and therefore brittle in one specific way: if the
    page object has been edited since the failing run — a partially applied fix, a
    stale handoff, a workspace on a different commit — the selector that failed is
    no longer in the file and nothing matches. The element name survives those
    edits, because it names the thing rather than how to find it.
    """
    match = _ELEMENT_NAME.search(error_text or "")
    if not match:
        return None, None, None
    wanted = re.escape(match.group(1))
    call = re.compile(r"\(\s*(\w+)\s*,[^;()]{0,120}?\"" + wanted + r"\"")
    # Same collision hazard as `_declaring_field`: "Login button" is a name several
    # pages give their own button. Look in the page object the failure named first.
    ordered = sorted(sources.items(), key=lambda kv: kv[0] != prefer)
    for class_name, source in ordered:
        found = call.search(source)
        if not found:
            continue
        field = found.group(1)
        for declared in page_identity.extract_locators(source):
            if declared.get("name") == field:
                return class_name, field, declared["raw"]
    return None, None, None


# "at automation.modules.naukari.web.NaukriLoginPage.doLogin(NaukriLoginPage.java:36)"
_TRACE_CLASS = re.compile(r"\b([A-Z]\w+)\.java\b")


def _owner_hint(issue: dict) -> str:
    """The page object the failure itself names. Empty when nothing does.

    Two independent records point at it: the failure context the framework wrote
    while the element was failing, which names the page object it belongs to, and
    the stack frame the assertion was raised from.
    """
    context = issue.get("failure_context") or ""
    if isinstance(context, dict):
        named = context.get("page_object") or ""
    elif context and Path(context).exists():
        named = failure_context.load(context).get("page_object") or ""
    else:
        named = ""
    if named:
        return named
    match = _TRACE_CLASS.search(issue.get("stack_trace") or "")
    return match.group(1) if match else ""


def _declaring_field(sources: dict, failed_selector: str, prefer: str = ""):
    """Which page object and field declares this selector.

    Matched on the normalised selector so a runtime-reported locator that differs
    only in quoting or whitespace still finds its declaration.

    `prefer` is the page object the failure named, and it is what makes the answer
    a fact rather than a coincidence. A selector like "button[type=\'submit\']" is
    declared by several unrelated pages in any real repo; taking the first class
    that matched let `rglob` order decide, which sent this step off to find a
    baseline for a page the test never opened. When the failure names none of the
    candidates, refuse: a confident wrong owner is worse than no owner.
    """
    wanted = page_identity.normalize_selector(failed_selector) or failed_selector
    found = []
    for class_name, source in sources.items():
        for declared in page_identity.extract_locators(source):
            if not declared.get("name"):
                continue
            raw = declared["raw"]
            if raw == failed_selector or (declared.get("selector") or raw) == wanted:
                found.append((class_name, declared["name"], raw))
    if not found:
        return None, None, None
    if len(found) == 1:
        return found[0]

    listed = ", ".join(f"{c}#{f}" for c, f, _ in found)
    chosen = next((entry for entry in found if entry[0] == prefer), None)
    if not chosen:
        log(f"  {failed_selector!r} is declared by {len(found)} page objects "
            f"({listed}) and the failure names "
            f"{prefer or 'none of them'} — refusing to guess which one broke")
        return None, None, None
    log(f"  {failed_selector!r} is declared by {len(found)} page objects ({listed}) "
        f"— using {chosen[0]}#{chosen[1]}, the one the failure names")
    return chosen


def _snapshot_path(issue: dict) -> Path | None:
    path = issue.get("dom_snapshot") or issue.get("dom_snapshot_path") or ""
    return Path(path) if path and Path(path).exists() else None


def _failure_capture(issue: dict) -> tuple[dict, dict]:
    """(snapshot header, element capture) written at failure time.

    The capture is the whole input to the search. Re-deriving it from the saved
    HTML would lose bounding boxes and computed ARIA roles, which is most of what
    separates two similar candidates — and computed visibility, which is what
    separates a candidate from one nobody can click. The header comes back too
    because it carries `capturedAt`, which decides which baselines may be used.
    """
    path = _snapshot_path(issue)
    if path is None:
        return {}, {}
    try:
        header = dom_snapshot.parse_header(path.read_text(errors="ignore")[:2000])
    except OSError:
        return {}, {}
    sidecar = header.get("fingerprints") or ""
    if not sidecar or not Path(sidecar).exists():
        return header, {}
    try:
        return header, json.loads(Path(sidecar).read_text()) or {}
    except (OSError, ValueError):
        return header, {}


def _flat(value) -> str:
    return " ".join(str(value or "").split())


def _unsettled_anchors(stored: dict, locators: list, field: str, soup) -> dict:
    """Hint attributes this page changed on a locator that still works, with the
    witness for each: {attribute: sentence}.

    A repair is proved unique against one capture, which is one moment of the
    page. A value the page rewrites as it is used — a placeholder blanked while
    its field has focus — is unique in that moment and gone in the next, so a
    selector built on it finds the element right up until the test touches it.

    The capture cannot show that about the element being repaired. Its
    neighbours can: a field whose own locator still finds exactly one element,
    holding a different value than the last good run recorded, is the page
    showing that it changes that attribute. The failing field is never its own
    witness — its locator is the one that broke.
    """
    recorded = stored.get("fingerprints") or {}
    out: dict = {}
    for locator in locators:
        name = locator.get("name") or ""
        was = recorded.get(name)
        # A locator narrowed by an accessible name is not judged by its selector
        # alone, so "exactly one node" would not mean "that element".
        if not was or name == field or locator.get("accessible_name"):
            continue
        found = dom_snapshot.select_nodes(locator.get("selector") or "", soup)
        if not found or len(found[0]) != 1 or found[0][0].name != was.get("tag"):
            continue
        node = found[0][0]
        for attribute in emit_mod.HINT_ANCHORS:
            before, now = _flat(was.get(attribute)), _flat(node.get(attribute))
            if before != now and attribute not in out:
                out[attribute] = (f"{name} still matches, and its {attribute} was "
                                  f"{before!r} on the last good run and is {now!r} "
                                  f"in the failure capture")
    return out


def _emit_offline(el: dict, vol: Volatility, soup, prints: dict,
                  frame_path: list | None = None, unsettled=()) -> dict | None:
    """The first selector on the ladder that matches THIS element and only it.

    `locator_emit.emit` asks a live page the same question with `count()`.
    `selector_visibility` asks the saved DOM, and cross-checks the one node it
    finds against the capture's own visibility record — so a selector that
    resolves to something nobody could have clicked is rejected here too.

    Requiring (1, 1) also disposes of a latent bug for free: `candidates_for`
    emits `[data-testid=…]` for any test id, which matches nothing on a page that
    spells the attribute `data-test`. That candidate scores (0, 0) and the ladder
    moves on, exactly as the live count used to make it.
    """
    for cand in emit_mod.candidates_for(el, vol, frame_path=frame_path,
                                        unsettled=unsettled):
        if emit_mod.VOLATILE_SELECTOR.search(cand["sel"]):
            continue
        # A framework that cannot put this rung inside an iframe emits no code for
        # it; a top-document locator in its place would search the wrong page.
        if frame_path and not cand.get("java"):
            continue
        if dom_snapshot.selector_visibility(cand["sel"], soup, prints) == (1, 1):
            return emit_mod._flag(cand)
    return None


def _resolve_offline(baseline: dict, prints: dict, soup, cfg: dict, vol: Volatility,
                     frame: tuple = (None, None), unsettled=()):
    """Rank every captured element against the baseline, then write a locator.

    Returns (emitted, candidate, decision). `emitted` is None when the decision
    refused, or when no candidate could be expressed as a unique selector — two
    outcomes the caller must keep apart, which is why the decision comes back too.
    """
    base_el = baseline["element"]
    # Inside an iframe the replacement is one of that frame's elements, so rank
    # the frame's own capture; the page's would offer the wrong document.
    frame_path, frame_prints = frame
    cands = []
    for el in capture.scorable((frame_prints or prints).get("elements") or []):
        c = Candidate(index=el["index"], el=el)
        # The tier the live search would have assigned by querying the page for
        # the baseline's own identity attributes. It is what lets `decide` accept
        # a unique test id without demanding a margin over the runner-up.
        if base_el.get("testid") and el.get("testid") == base_el["testid"]:
            c.tiers.add("T1_identity")
        elif base_el.get("id") and el.get("id") == base_el["id"]:
            c.tiers.add("T1_identity")
        cands.append(c)

    ranked = decide_mod.rank(cands, baseline, cfg, vol)
    decision = decide_mod.decide(ranked, cfg)
    if not decision.proceed:
        return None, None, decision

    # Genuine near-ties only. Falling through to a lower-scoring DIFFERENT element
    # because the winner could not be expressed is how a healer silently rebinds a
    # test; that is a failure of emit, not evidence for the runner-up.
    pool = [c for c in ranked[:cfg["budgets"]["candidates_to_verify"]]
            if decision.top.score - c.score < cfg["thresholds"]["margin"]]
    for cand in pool:
        emitted = _emit_offline(cand.el, vol, soup, prints, frame_path, unsettled)
        if emitted:
            return emitted, cand, decision
    return None, None, decision


def _no_fingerprint_reason(class_name: str, field: str, stored: dict) -> str:
    """Why a recorded page has nothing to compare this locator against.

    Three different things, and they used to share one sentence — "it did not
    resolve on the last good run" — which was false for the commonest of them: a
    locator the page-load count shows matching, whose fingerprint simply was not
    captured. Said that way, a hole in the baseline read as evidence about the
    element.
    """
    count = (stored.get("coverage") or {}).get(field)
    head = f"{class_name} has a baseline but no fingerprint for {field} — "
    if isinstance(count, int) and count > 0:
        return head + (f"it matched {count} element(s) on the last good run and was "
                       f"not fingerprinted: the baseline is incomplete, which says "
                       f"nothing about the element")
    if count == 0:
        return head + ("it matched nothing when the page loaded on the last good "
                       "run, and the test did not use it afterwards")
    return head + ("it could not be evaluated when the page loaded on the last good "
                   "run, and the test did not use it afterwards")


def _heals_in_window(baseline: dict, cfg: dict) -> int:
    """How many times this locator has been healed inside the history window.

    A locator that keeps breaking needs a stable test id, not a fourth heal.
    """
    window = datetime.now(timezone.utc) - timedelta(
        days=cfg["budgets"]["heal_history_window_days"])
    recent = 0
    for entry in baseline.get("history") or []:
        try:
            if datetime.fromisoformat(entry["healed_at"]) > window:
                recent += 1
        except (KeyError, TypeError, ValueError):
            continue
    return recent


def _previous_attempt() -> tuple[dict, list]:
    """(last Fix result, last Locate resolutions). Empty on the first attempt.

    Both files sit in the audit session and hold the previous attempt's view: Fix
    rewrites 01-fix.json as it finishes, Locate rewrites 01-locate.json as it
    starts. Reading them is what lets this run work on the failure that exists
    NOW rather than the one the handoff describes.
    """
    if FIX_ATTEMPT <= 1:
        return {}, []

    def load(name: str) -> dict:
        try:
            return json.loads((AUDIT_DIR / name).read_text()) or {}
        except (OSError, ValueError):
            return {}

    return load("01-fix.json"), load("01-locate.json").get("resolutions") or []


def _issues(handoff: dict, previous_fix: dict) -> list:
    """The failures to work on: the handoff's, or what is left of them.

    An attempt that repaired one locator and uncovered the next wrote the NEW
    failure down, refreshed from the artifacts its verification run produced — a
    new selector, a new DOM capture, a new page object. Carrying the handoff
    forward instead would hand this run a selector that has already been repaired
    and a capture taken before the edit. Filtered and overlaid exactly as 01_fix
    does it, so both steps work from the same set.
    """
    issues = handoff.get("automation_issues") or []
    if FIX_ATTEMPT <= 1 or not previous_fix:
        return issues
    failed = previous_fix.get("failed_fixes") or []
    refreshed = {f["test_name"]: f["next_issue"] for f in failed if f.get("next_issue")}
    names = {f.get("test_name") for f in failed}
    return [refreshed.get(i.get("test_name"), i) for i in issues
            if i.get("test_name") in names]


def _already_tried(previous_fix: dict, previous_locate: list) -> set:
    """Selectors whose located answer was applied and left the test still failing.

    Fix reverts an edit that helped nobody, so the source holds the broken
    selector again and this step would resolve it to the same answer, for ever.
    The retry exists to try something ELSE, which is the model's job — so refuse
    here rather than hand Fix an answer it has already disproved.

    Disproved once is disproved for the session. The refusal replaces the answer
    in the file, so an attempt later than the next one finds no answer to match
    against — and without the record of what was tried it resolved the selector
    afresh and offered the same thing again.
    """
    healed = {r.get("failed_selector") for r in previous_locate
              if r.get("verdict") == HEALED}
    fresh = {f.get("failed_selector") for f in (previous_fix.get("failed_fixes") or [])
             if f.get("status") == "test_failed" and f.get("failed_selector") in healed}
    return fresh | {r.get("failed_selector") for r in previous_locate if "tried" in r}


def _merge(previous: list, current: list) -> list:
    """This attempt's resolutions over the earlier ones, keyed by test+selector.

    A chain is repaired one link per attempt, so the file has to hold all of
    them: reporting only the last link would credit one heal to a run that made
    four. Keys are distinct per link — a different selector every time — and an
    entry for the same key is this attempt's, which is the newer answer.
    """
    keyed = {(r.get("test_name"), r.get("failed_selector")): r for r in previous}
    keyed.update({(r.get("test_name"), r.get("failed_selector")): r for r in current})
    return list(keyed.values())


def locate_one(issue: dict, sources: dict, assertion_used: dict, cfg: dict,
               vol: Volatility, workspace: Path) -> dict:
    """One locator, with the rule for locators an assertion reads applied on top.

    Pointed at a different element, a presence check passes on anything visible
    and a caught regression becomes a green build. So a locator an assertion
    reads is repaired only when the fingerprint proves the replacement is the
    same element. Every other outcome is reported under one verdict that Fix
    honours without asking the model — the one path that can pick an element
    nothing proved.
    """
    record = _locate(issue, sources, assertion_used, cfg, vol, workspace)
    site = assertion_used.get(record.get("field") or "")
    if not site or locator_assertions.heal_mode(cfg) != locator_assertions.PROVEN:
        return record
    if record["verdict"] == HEALED:
        record["assertion_site"] = site
        return record
    why = record.get("reason") or record.get("classification") or record["verdict"]
    record["unproven_as"] = record.get("classification") or record["verdict"]
    record["verdict"], record["classification"] = NO_HEAL, "ASSERTION_LOCATOR"
    record["reason"] = locator_assertions.unproven(site, why)
    return record


def _locate(issue: dict, sources: dict, assertion_used: dict, cfg: dict,
            vol: Volatility, workspace: Path) -> dict:
    """One locator: identify it, rank it against the failure capture, write it."""
    failed = issue.get("failed_selector") or ""
    record = {
        "test_name": issue.get("test_name", ""),
        "failed_selector": failed,
        "verdict": "SKIPPED",
        "reason": "",
        "locator_id": "",
    }
    if not failed:
        # What is missing, not what it means: an absent selector is a gap in the
        # evidence, and says nothing about which kind of failure this was.
        record["reason"] = "no failing selector recorded — nothing to locate"
        return record

    owner = _owner_hint(issue)
    class_name, field, raw = _declaring_field(sources, failed, prefer=owner)
    if not field:
        # The selector is not in the source. Usually that means the page object
        # was edited after the run that failed, so fall back to the element name,
        # which survives a locator change.
        class_name, field, raw = _field_by_element_name(
            sources, (issue.get("error_message") or "") + (issue.get("root_cause") or ""),
            prefer=owner)
        if field:
            log(f"  {failed!r} is no longer in the source — matched by element name "
                f"to {class_name}#{field} ({raw!r})")
    if not field:
        record["reason"] = (f"no page object declares {failed!r}, and no element "
                            f"name in the failure matched a locator field either")
        record["verdict"] = "NO_DECLARATION"
        return record
    return _resolve_field(issue, record, class_name, field, raw, sources,
                          assertion_used, cfg, vol, workspace, with_siblings=True)


def _resolve_field(issue: dict, record: dict, class_name: str, field: str, raw: str,
                   sources: dict, assertion_used: dict, cfg: dict, vol: Volatility,
                   workspace: Path, with_siblings: bool = False) -> dict:
    """One page-object field, once it is known which one: rank it, write it.

    Apart from `_locate` because the field is not always found from a failure.
    The page's other locators that went missing with the failing one are resolved
    through here too, and they are known by name, not by a selector that failed.
    """
    failed = record["failed_selector"]
    record["locator_id"] = f"{class_name}#{field}"
    record["page_object"], record["field"] = class_name, field

    # Configured never to touch a locator an assertion reads: refuse before
    # spending anything on it. The default — repair on proof — is decided by the
    # caller, once there is a resolution to judge.
    if (field in assertion_used
            and locator_assertions.heal_mode(cfg) == locator_assertions.NEVER):
        record["verdict"], record["classification"] = NO_HEAL, "ASSERTION_LOCATOR"
        record["reason"] = ("locator is read by an assertion — reported for review, "
                            "never auto-healed")
        return record

    snapshot = _snapshot_path(issue)
    header, prints = _failure_capture(issue)
    if not prints.get("elements"):
        record["verdict"] = "NO_CAPTURE"
        record["reason"] = (
            "the failure DOM was shipped without its element capture — nothing to "
            "search. The framework writes one beside every snapshot; check the "
            "handoff's dom_snapshot path still exists" if snapshot else
            "the handoff carries no DOM snapshot for this failure — nothing to search")
        return record
    html = snapshot.read_text(errors="ignore")
    soup = page_identity.parse(html)
    if soup is None:
        record["verdict"] = "NO_CAPTURE"
        record["reason"] = "the failure DOM could not be parsed — cannot prove a selector unique"
        return record

    # Is this locator drift at all? The old live search answered that by resolving
    # the failing selector on the page before searching for a replacement; the
    # capture answers it just as well. A selector that still matches was not what
    # broke — the element was hidden, late or covered — and a new selector cannot
    # fix any of those. None means the selector could not be evaluated, which is
    # not the same as matching nothing, so it falls through rather than refusing.
    still_matching = dom_snapshot.selector_visibility(failed, soup, prints)
    if still_matching is not None and still_matching[0]:
        matches, visible = still_matching
        record["verdict"], record["classification"] = NO_HEAL, "NOT_LOCATOR"
        record["reason"] = (
            f"{failed!r} still matches {matches} element(s) in the DOM captured at "
            f"failure" + (f", {visible} of them visible — the element was there, so "
                          f"this is a timing or obstruction problem, not locator drift"
                          if visible else
                          ", none of them visible — the element was present but "
                          "hidden, which a new selector cannot fix"))
        return record

    # Which baselines may stand as evidence. `baseline_not_after` is set by Fix
    # once it has repaired something this session and pins the cutoff to the
    # ORIGINAL failure: a baseline younger than that was written during the
    # repair — a passing sibling test promotes every locator on the page it
    # touched — and a record made while repairing says nothing about the page
    # before it. This capture's own timestamp is the right answer on a first look.
    cutoff = issue.get("baseline_not_after") or header.get("capturedAt", "")
    # The copy taken before this session ran anything, when there is one. Without
    # it the live tree is read — and a run that greens a test re-records the very
    # page the next link of the chain needs, so the answer that was on disk at
    # session start is gone by the time anything asks for it.
    stored = baseline_store.load(class_name, workspace,
                                 issue.get("healing_baseline_dir") or None,
                                 not_after=cutoff,
                                 module=baseline_store.module_of(issue.get("test_name", "")))
    if not stored.get("available"):
        record["verdict"] = "NO_BASELINE"
        record["reason"] = stored.get("rejected") or (
            f"no recorded good run for {class_name} — nothing to compare against")
        return record

    baseline = engine.baseline_for(class_name, field, raw, stored)
    if baseline is None:
        # Its own name: the page was recorded, this one locator was not. Counted
        # with "no recorded good run", a hole in a baseline read as a missing one.
        record["verdict"], record["classification"] = "NO_BASELINE", "NO_FINGERPRINT"
        record["reason"] = _no_fingerprint_reason(class_name, field, stored)
        return record

    healed_before = _heals_in_window(baseline, cfg)
    if healed_before >= cfg["budgets"]["heal_history_max"]:
        record["verdict"], record["classification"] = NO_HEAL, "UNSTABLE_LOCATOR"
        record["reason"] = (f"healed {healed_before}x in "
                            f"{cfg['budgets']['heal_history_window_days']}d — "
                            f"needs a stable test id, not another selector")
        return record

    # Is this even the right page? Searching a page the test never reached finds a
    # plausible element every time, which is the most expensive way to be wrong.
    # baseline.diff weighs url shape, title, body class and locator coverage
    # together and demands corroboration before calling a page "different".
    # Every locator on this page object EXCEPT the one that failed. Counting the
    # failing one is circular: it is absent by definition, that is the whole
    # report — and on a page object whose only evaluable locator is the broken
    # one it made "nothing matches" the verdict every time, refusing a page the
    # test was demonstrably on.
    declared = page_identity.extract_locators(sources.get(class_name, ""))
    coverage = page_identity.locator_coverage(declared, soup)
    details = {d["name"]: d["count"] for d in coverage.get("details") or []
               if d.get("name") and d["name"] != field and isinstance(d.get("count"), int)}
    comparison = baseline_store.diff(
        stored, page_identity.page_facts(html, soup), {"details": details})
    if baseline_store.is_different_page(comparison):
        record["verdict"], record["classification"] = NO_HEAL, "WRONG_STATE"
        record["reason"] = ("the DOM captured at failure is not the page this locator "
                            "belongs to: "
                            + "; ".join((comparison.get("mismatches") or ["identity differs"])[:2]))
        return record

    # A locator inside an iframe is resolved in that iframe. The capture has to
    # hold the frame; one that does not is missing evidence, not a verdict.
    frame_path, _inner = frames.split(raw)
    frame = (None, None)
    if frame_path:
        doc = page_identity.in_frame(soup, raw)[0]
        if doc in (None, False):
            record["verdict"] = "NO_CAPTURE"
            record["reason"] = (f"{raw!r} lives inside an iframe the failure capture "
                                f"does not hold — nothing to search")
            return record
        frame = (frame_path, getattr(doc, "qa_prints", None) or {})

    unsettled = _unsettled_anchors(stored, declared, field, soup)
    started = time.time()
    emitted, cand, decision = _resolve_offline(baseline, prints, soup, cfg, vol, frame,
                                               unsettled)
    record["elapsed_ms"] = int((time.time() - started) * 1000)
    # Only the ones that changed the answer: an attribute the chosen element does
    # not carry was never going to be its anchor.
    chosen = cand.el if cand is not None else (decision.top.el if decision.top else {})
    passed_over = {a: why for a, why in unsettled.items() if chosen.get(a)}
    if passed_over:
        record["anchors_passed_over"] = passed_over
    record["rejected"] = [
        {"tag": r.el["tag"], "name": r.el.get("accessible_name"),
         "score": round(r.score, 3), "tier": r.best_tier}
        for r in (decision.runners or [])]

    restored = None
    if emitted is None:
        absent = _absent_from_capture(stored, field, decision, cfg)
        if absent:
            record["absent_from_capture"] = absent
        restored = _restored(stored, field, raw, bool(frame_path), soup, prints, absent)

    if restored:
        # The candidates passed over were ranked against a fingerprint; this
        # answer was not chosen from among them.
        record.pop("rejected", None)
        record.update(restored)
    elif emitted is None:
        record["verdict"] = NO_HEAL
        if decision.proceed:
            record["classification"] = "NO_STABLE_LOCATOR"
            record["reason"] = ("found the element but could not express it as a stable "
                                "unique locator — it needs a test id")
            if passed_over:
                record["reason"] += ("; " + ", ".join(passed_over) + " not used, "
                                     "because the page changes it: "
                                     + "; ".join(passed_over.values()))
        elif decision.outcome == decide_mod.NONE:
            record["classification"] = "ELEMENT_GONE"
            record["reason"] = decision.reason
        else:
            record["classification"] = "LOW_CONFIDENCE"
            record["reason"] = decision.reason
        if decision.top is not None:
            record["score"] = round(decision.top.score, 3)
            record["margin"] = round(decision.margin, 3)
        return record

    # Nothing to change. The answer is already in the file — an earlier attempt
    # applied it, or a PR did — so the failure this was read from is older than
    # the source. Reporting it as a heal produces an edit that changes nothing
    # and spends an attempt saying so.
    elif emitted["sel"] == raw:
        record["verdict"], record["classification"] = NO_HEAL, "ALREADY_CURRENT"
        record["reason"] = (f"{class_name}#{field} already declares {raw!r} — this "
                            f"failure predates the file and has nothing left to fix")
        return record

    else:
        record.update({
            "verdict": HEALED,
            "classification": "LOCATOR_STALE",
            "reason": decision.reason,
            "score": round(cand.score, 3),
            "margin": round(decision.margin, 3),
            "tier": cand.best_tier,
            # What stands behind the answer. Not "executed": Fix applies the edit
            # and runs the real test, and claiming a proof this step did not
            # perform is how an unverified fix gets reported as a verified one.
            "verification": "unique in the failure capture",
            "new_locator": emitted["sel"],
            "new_expression": emitted.get("java") or emit_mod.code_for(emitted["sel"])["java"],
            "strategy": emitted.get("strategy"),
            "fragile": emitted.get("fragile"),
        })

    if with_siblings:
        also = _siblings(issue, class_name, field, raw, stored, declared, details,
                         sources, assertion_used, cfg, vol, workspace)
        if also:
            record["siblings"] = also
    return record


RECORDED = "T0_recorded"


def _absent_from_capture(stored: dict, field: str, decision, cfg: dict) -> str:
    """Why the failure capture cannot hold this element, or "" when it could.

    Two facts, and it takes both. The last good run found nothing for this
    locator as the page loaded and used it later, so the element exists only once
    something is done to the page. And nothing captured at failure resembles what
    that run recorded. A confirmation that shows and clears itself is gone again
    by the time a wait for it gives up: the capture is of a page it has left.

    Recorded because every check that asks "does the replacement match the
    capture" has only one answer for such an element, and it is the wrong one.
    """
    if not baseline_store.appears_after_action(stored, field):
        return ""
    if decision.top is not None and decision.top.score >= cfg["thresholds"]["accept"]:
        return ""
    return (f"{field} exists only after an action (it matched nothing as the page "
            f"loaded on the last good run, and was used later in that run), and "
            f"nothing in the failure capture resembles it")


def _restored(stored: dict, field: str, raw: str, in_frame: bool, soup, prints: dict,
              absent: str) -> dict | None:
    """The selector the last passing run used, when the source now declares another.

    A locator can break because the page changed or because the code did. The
    fingerprint search answers the first. For the second the answer was written
    down: the baseline holds the selector each field was found with, and a source
    that declares something else has been edited since. Putting it back needs no
    search and cannot pick a different element.

    Offered only on proof, like every other answer here: the recorded selector
    matches exactly one visible element in the failure capture, or the element is
    one the capture cannot hold and the test run is left to prove it.
    """
    recorded = failure_identity.bare_selector(
        str((stored.get("selectors") or {}).get(field) or ""))
    # A chain of hops prints as one string and is declared as several calls;
    # writing it back as a single selector would not be the same locator.
    if not recorded or in_frame or ">>" in recorded or "internal:" in recorded:
        return None
    if (page_identity.normalize_selector(recorded) or recorded) == \
            (page_identity.normalize_selector(raw) or raw):
        return None
    if dom_snapshot.selector_visibility(recorded, soup, prints) == (1, 1):
        verification = "unique in the failure capture"
    elif absent:
        verification = ("not checked against the failure capture, which the element "
                        "is not in — the test run is the proof")
    else:
        return None
    return {
        "verdict": HEALED,
        "classification": "LOCATOR_STALE",
        "reason": (f"the source declares {raw!r}; the last passing run found this "
                   f"element with {recorded!r}"),
        "tier": RECORDED,
        "verification": verification,
        "new_locator": recorded,
        "new_expression": emit_mod.code_for(recorded)["java"],
        "strategy": "recorded",
    }


_SIBLING_KEYS = ("locator_id", "page_object", "field", "failed_selector", "new_locator",
                 "new_expression", "score", "margin", "tier", "verification", "strategy")


def _siblings(issue: dict, class_name: str, field: str, raw: str, stored: dict,
              declared: list, details: dict, sources: dict, assertion_used: dict,
              cfg: dict, vol: Volatility, workspace: Path) -> list:
    """The page's other locators that went missing with this one, resolved too.

    One broken locator hides the next, and each costs a whole test run to reach.
    When two of them are on one page that run is spent learning something this
    capture already shows: a locator that matched as the page loaded on the last
    good run and matches nothing now.

    Held to what the failing locator is held to: the same baseline, the same
    capture, the same proof. One that does not reach it waits for its own
    failure, and one an assertion reads is never repaired without having failed.
    """
    source = sources.get(class_name, "")
    by_name = {d["name"]: d["raw"] for d in declared if d.get("name")}
    wanted = []
    for name, count in (stored.get("coverage") or {}).items():
        if name == field or name in assertion_used or not isinstance(count, int) or count < 1:
            continue
        if name in by_name:
            if details.get(name) == 0:
                wanted.append((name, by_name[name]))
        elif raw in ((locator_patch.declaration_edit(source, name, "")[0] or {})
                     .get("old_string") or ""):
            # Declared with the very selector that failed. A selector is listed
            # once however many fields use it, so this one is found by its name.
            wanted.append((name, raw))

    found = []
    for name, sibling_raw in wanted:
        sibling = {"test_name": issue.get("test_name", ""), "failed_selector": sibling_raw,
                   "verdict": "SKIPPED", "reason": "", "locator_id": ""}
        try:
            sibling = _resolve_field(issue, sibling, class_name, name, sibling_raw,
                                     sources, assertion_used, cfg, vol, workspace)
        except Exception:                              # noqa: BLE001 - never costs the main answer
            continue
        if sibling.get("verdict") == HEALED:
            found.append({key: sibling[key] for key in _SIBLING_KEYS if key in sibling})
    return found


def main() -> int:
    started = time.time()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    vol = Volatility(cfg)

    handoff = json.loads(HANDOFF_FILE.read_text())
    previous_fix, previous_locate = _previous_attempt()
    issues = _issues(handoff, previous_fix)
    tried = _already_tried(previous_fix, previous_locate)

    workspace = _workspace()
    if workspace is None:
        log("WORKSPACE_DIR/GITHUB_REPO_AUTOMATION not set — cannot read page objects")
        _write({"mode": HEALING_LOCATE_MODE, "resolutions": previous_locate,
                "reason": "no workspace"}, started)
        return 0

    sources = _page_object_sources(workspace)
    fields = {declared["name"]
              for source in sources.values()
              for declared in page_identity.extract_locators(source)
              if declared.get("name")}
    assertion_used = locator_assertions.assertion_sites_by_field(sources, fields)
    assertion_mode = locator_assertions.heal_mode(cfg)

    # One broken locator fails every test that walks past it. Fix already clusters
    # on that; Locate did not, and ran the identical search five times over for one
    # login button. The failure names the locator, so the grouping needs nothing
    # the resolution itself produces.
    groups: dict = {}
    for issue in issues:
        groups.setdefault(
            ((issue.get("failed_selector") or ""), _owner_hint(issue)), []).append(issue)
    source = ("still failing after attempt %d" % (FIX_ATTEMPT - 1)
              if FIX_ATTEMPT > 1 else "from the handoff")
    log(f"{len(issues)} issue(s) {source}, {len(groups)} distinct locator(s); "
        f"mode={HEALING_LOCATE_MODE}")
    if assertion_used and assertion_mode != locator_assertions.ALWAYS:
        log(f"{len(assertion_used)} locator(s) are read by an assertion — "
            + ("reported, never repaired" if assertion_mode == locator_assertions.NEVER
               else "repaired only on proof that it is the same element"))

    resolutions = []
    for members in groups.values():
        # The member with a capture behind it. They share a locator, so any of them
        # answers the question, but only one of them may have shipped the DOM.
        issue = next((m for m in members if _snapshot_path(m)), members[0])
        failed = issue.get("failed_selector") or ""
        if failed in tried:
            # Whose locator it was and what was written for it, from the answer
            # this replaces — a refusal that names neither reads as if nothing
            # had been found.
            earlier = next((r for r in previous_locate
                            if r.get("failed_selector") == failed), {})
            answer = (earlier.get("tried") or earlier.get("new_expression")
                      or earlier.get("new_locator") or "")
            resolution = {
                "test_name": issue.get("test_name", ""), "failed_selector": failed,
                "verdict": NO_HEAL, "classification": "REVERTED",
                "locator_id": earlier.get("locator_id", ""), "tried": answer,
                "reason": ((f"{answer} was" if answer else "this answer was")
                           + " applied on an earlier attempt, the test still failed "
                             "on the same element and the edit was reverted — the "
                             "retry needs a different approach, not the same "
                             "selector again"),
            }
            resolution.update({key: earlier[key] for key in ("page_object", "field")
                               if earlier.get(key)})
            # The proved answer did not hold, so there is no proof left — and a
            # locator an assertion reads is not repaired without one.
            site = next((r.get("assertion_site") for r in previous_locate
                         if r.get("failed_selector") == failed and r.get("assertion_site")), "")
            if site and assertion_mode == locator_assertions.PROVEN:
                resolution["assertion_site"] = site
                resolution["classification"] = "ASSERTION_LOCATOR"
                resolution["reason"] = locator_assertions.unproven(
                    site, "the answer proved from the fingerprint was applied and "
                          "the test still failed on this element")
            _log_resolution(resolution)
            for member in members:
                resolutions.append({**resolution, "test_name": member.get("test_name", "")})
            continue
        try:
            resolution = locate_one(issue, sources, assertion_used, cfg, vol, workspace)
        except Exception as exc:                       # noqa: BLE001 - per-locator isolation
            # The whole message, not its first 160 characters. Truncating cut off
            # exactly the half that says why — "waiting for locator(...)" — and
            # left a line nobody could act on.
            resolution = {
                "test_name": issue.get("test_name", ""),
                "failed_selector": issue.get("failed_selector", ""),
                "verdict": "SKIPPED",
                "reason": f"locate raised {type(exc).__name__}: {' '.join(str(exc).split())[:400]}",
                "locator_id": "",
            }
        _log_resolution(resolution)
        if len(members) > 1:
            log(f"  same locator in {len(members)} failing tests — resolved once")
        for member in members:
            resolutions.append({**resolution, "test_name": member.get("test_name", "")})

    located = [r for r in resolutions if r["verdict"] == HEALED]
    also = len({s.get("locator_id") for r in located for s in r.get("siblings") or []})
    log(f"located {len(located)} of {len(resolutions)} deterministically"
        + (f", and {also} more missing from the same page" if also else "")
        + f" ({'applied by fix' if HEALING_LOCATE_MODE == 'enforce' else 'shadow — fix unchanged'})")

    # Every link of the chain, not just this attempt's. Fix matches a resolution
    # by its failing selector and each link has a different one, so carrying the
    # earlier answers costs nothing and keeps the report honest about the work.
    all_resolutions = _merge(previous_locate, resolutions)
    _write({
        "mode": HEALING_LOCATE_MODE,
        "attempt": FIX_ATTEMPT,
        "attempted": len(all_resolutions),
        # Across every attempt, not just this one: a chain repaired link by link
        # would otherwise report the last link as the whole run's work.
        "distinct_locators": len({r.get("locator_id") or r.get("failed_selector")
                                  for r in all_resolutions}),
        "located": len([r for r in all_resolutions if r["verdict"] == HEALED]),
        "refused": len([r for r in all_resolutions
                        if r["verdict"] not in (HEALED, "SKIPPED")]),
        "verdicts": _counts(all_resolutions),
        "resolutions": all_resolutions,
    }, started)
    return 0


def _log_resolution(r: dict) -> None:
    """One console line per decision. The Live Run panel reads stdout, and a
    reviewer looking for *why* looks there rather than at a badge."""
    name = r.get("locator_id") or r.get("failed_selector", "")[:40]
    if r["verdict"] == HEALED:
        log(f"{name}  {r.get('classification')}  {_basis(r)} — {r.get('verification')}")
        log(f"  -> {r.get('new_expression', '')[:110]}")
        for sibling in r.get("siblings") or []:
            log(f"  also missing on this page: {sibling.get('locator_id')}  "
                f"{_basis(sibling)} — {sibling.get('verification')}")
            log(f"    -> {sibling.get('new_expression', '')[:110]}")
        for attribute, why in (r.get("anchors_passed_over") or {}).items():
            log(f"  not anchored on {attribute}: {why} — the page changes it")
        for rejected in (r.get("rejected") or [])[:2]:
            log(f"  rejected: <{rejected['tag']}> {str(rejected['name'])[:28]!r} "
                f"score {rejected['score']}")
        if r.get("fragile"):
            log(f"  fragile: {r['fragile'][:100]}")
    else:
        # Refusals carry the reason a human acts on — "no recorded good run",
        # "the feature was removed". The console scrolls; the advice should survive.
        log(f"{name}  {r.get('classification') or r['verdict']} — {r.get('reason','')[:220]}")


def _basis(r: dict, report: bool = False) -> str:
    """What an answer rests on, as the console says it or as the report does."""
    if r.get("tier") == RECORDED:
        return "the selector the last passing run used"
    if report:
        return f"score {r.get('score')} (margin {r.get('margin'):+}), {r.get('tier')}"
    return f"score {r.get('score')} margin {r.get('margin'):+} tier {r.get('tier')}"


def _counts(resolutions: list) -> dict:
    counts: dict = {}
    for r in resolutions:
        key = r.get("classification") or r["verdict"]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _write(payload: dict, started: float) -> None:
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload["duration_s"] = round(time.time() - started, 1)
    (AUDIT_DIR / "01-locate.json").write_text(json.dumps(payload, indent=2))
    (AUDIT_DIR / "01-locate.md").write_text(_markdown(payload))
    # No record_stage() here: run_step in shared/session.sh already records one
    # per step, with the right index. Recording our own would double-count the
    # stage and inflate the time/cost table the UI renders.


def _markdown(payload: dict) -> str:
    """One section per locator, not per test — five tests on one broken login
    button are one piece of work and reading it five times says otherwise."""
    resolutions = payload.get("resolutions", [])
    by_locator = {r.get("locator_id") or r.get("failed_selector", "?"): r
                  for r in resolutions}
    located = [r for r in by_locator.values() if r.get("verdict") == HEALED]
    lines = [f"# Locate ({payload.get('mode')})", "",
             f"- {len(by_locator)} distinct locator(s) across "
             f"{len({r.get('test_name') for r in resolutions})} failing test(s)",
             f"- located deterministically: {len(located)}",
             f"- refused: {len(by_locator) - len(located)}", ""]
    for key, r in by_locator.items():
        lines.append(f"## {key}")
        lines.append(f"- verdict: **{r.get('classification') or r['verdict']}** — {r.get('reason','')}")
        if r.get("new_expression"):
            lines += [f"- was: `{r.get('failed_selector')}`",
                      f"- now: `{r['new_expression']}`",
                      f"- {_basis(r, report=True)}, {r.get('verification')}"]
        for sibling in r.get("siblings") or []:
            lines.append(f"- also missing on this page: `{sibling.get('locator_id')}` "
                         f"→ `{sibling.get('new_expression')}` "
                         f"({_basis(sibling, report=True)}, {sibling.get('verification')})")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    # This step is an optimisation: it saves a model call when it works and costs
    # nothing when it does not. run.sh runs under `set -e` with an ERR trap, so a
    # traceback escaping here would abort the whole run before Fix ever ran —
    # turning a missing baseline into a failed heal. Same principle as
    # Baseline.java: never allowed to fail the thing it is helping.
    try:
        sys.exit(main())
    except Exception as exc:                       # noqa: BLE001 - deliberate catch-all
        import traceback
        log(f"locate failed ({type(exc).__name__}: {exc}) — continuing to Fix")
        traceback.print_exc()
        try:
            # Keep the links already resolved: Fix reads this file to decide what
            # it still has to ask the model, and an empty one would send it back
            # to the model for answers this session already proved.
            _write({"mode": HEALING_LOCATE_MODE, "attempt": FIX_ATTEMPT,
                    "resolutions": _previous_attempt()[1],
                    "error": f"{type(exc).__name__}: {exc}"}, time.time())
        except Exception:                          # noqa: BLE001
            pass
        sys.exit(0)
