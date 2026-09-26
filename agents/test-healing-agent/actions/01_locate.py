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
from shared import dom_snapshot, failure_context, locator_assertions, page_identity
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


def _emit_offline(el: dict, vol: Volatility, soup, prints: dict) -> dict | None:
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
    for cand in emit_mod.candidates_for(el, vol):
        if emit_mod.VOLATILE_SELECTOR.search(cand["sel"]):
            continue
        if dom_snapshot.selector_visibility(cand["sel"], soup, prints) == (1, 1):
            return emit_mod._flag(cand)
    return None


def _resolve_offline(baseline: dict, prints: dict, soup, cfg: dict, vol: Volatility):
    """Rank every captured element against the baseline, then write a locator.

    Returns (emitted, candidate, decision). `emitted` is None when the decision
    refused, or when no candidate could be expressed as a unique selector — two
    outcomes the caller must keep apart, which is why the decision comes back too.
    """
    base_el = baseline["element"]
    cands = []
    for el in capture.scorable(prints.get("elements") or []):
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
        emitted = _emit_offline(cand.el, vol, soup, prints)
        if emitted:
            return emitted, cand, decision
    return None, None, decision


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
    """
    healed = {r.get("failed_selector") for r in previous_locate
              if r.get("verdict") == HEALED}
    return {f.get("failed_selector") for f in (previous_fix.get("failed_fixes") or [])
            if f.get("status") == "test_failed" and f.get("failed_selector") in healed}


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


def locate_one(issue: dict, sources: dict, assertion_used: set, cfg: dict,
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
        record["reason"] = "no failing selector recorded — not a locator failure"
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
    record["locator_id"] = f"{class_name}#{field}"
    record["page_object"], record["field"] = class_name, field

    # Heal how a test FINDS an element; never what it VERIFIES. A healed assertion
    # locator turns a caught regression into a green build, which is the exact
    # failure this whole system exists to avoid.
    if field in assertion_used and not cfg["classify"].get("heal_assertions", False):
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
        record["verdict"] = "NO_BASELINE"
        record["reason"] = (f"{class_name} has a baseline but no fingerprint for "
                            f"{field} — it did not resolve on the last good run")
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
    coverage = page_identity.locator_coverage(
        page_identity.extract_locators(sources.get(class_name, "")), soup)
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

    started = time.time()
    emitted, cand, decision = _resolve_offline(baseline, prints, soup, cfg, vol)
    record["elapsed_ms"] = int((time.time() - started) * 1000)
    record["rejected"] = [
        {"tag": r.el["tag"], "name": r.el.get("accessible_name"),
         "score": round(r.score, 3), "tier": r.best_tier}
        for r in (decision.runners or [])]

    if emitted is None:
        record["verdict"] = NO_HEAL
        if decision.proceed:
            record["classification"] = "NO_STABLE_LOCATOR"
            record["reason"] = ("found the element but could not express it as a stable "
                                "unique locator — it needs a test id")
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
    if emitted["sel"] == raw:
        record["verdict"], record["classification"] = NO_HEAL, "ALREADY_CURRENT"
        record["reason"] = (f"{class_name}#{field} already declares {raw!r} — this "
                            f"failure predates the file and has nothing left to fix")
        return record

    record.update({
        "verdict": HEALED,
        "classification": "LOCATOR_STALE",
        "reason": decision.reason,
        "score": round(cand.score, 3),
        "margin": round(decision.margin, 3),
        "tier": cand.best_tier,
        # What stands behind the answer. Not "executed": Fix applies the edit and
        # runs the real test, and claiming a proof this step did not perform is
        # how an unverified fix gets reported as a verified one.
        "verification": "unique in the failure capture",
        "new_locator": emitted["sel"],
        "new_expression": emitted.get("java") or emit_mod.code_for(emitted["sel"])["java"],
        "strategy": emitted.get("strategy"),
        "fragile": emitted.get("fragile"),
    })
    return record


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
    assertion_used = locator_assertions.assertion_fields(sources, fields)

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
    if assertion_used:
        log(f"{len(assertion_used)} locator(s) are read by assertions and will not be healed")

    resolutions = []
    for members in groups.values():
        # The member with a capture behind it. They share a locator, so any of them
        # answers the question, but only one of them may have shipped the DOM.
        issue = next((m for m in members if _snapshot_path(m)), members[0])
        failed = issue.get("failed_selector") or ""
        if failed in tried:
            resolution = {
                "test_name": issue.get("test_name", ""), "failed_selector": failed,
                "verdict": NO_HEAL, "classification": "ALREADY_TRIED", "locator_id": "",
                "reason": ("this answer was applied on an earlier attempt and the test "
                           "still failed on the same element — the retry needs a "
                           "different approach, not the same selector again"),
            }
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
    log(f"located {len(located)} of {len(resolutions)} deterministically "
        f"({'applied by fix' if HEALING_LOCATE_MODE == 'enforce' else 'shadow — fix unchanged'})")

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
        log(f"{name}  {r.get('classification')}  score {r.get('score')} "
            f"margin {r.get('margin'):+} tier {r.get('tier')} — {r.get('verification')}")
        log(f"  -> {r.get('new_expression', '')[:110]}")
        for rejected in (r.get("rejected") or [])[:2]:
            log(f"  rejected: <{rejected['tag']}> {str(rejected['name'])[:28]!r} "
                f"score {rejected['score']}")
        if r.get("fragile"):
            log(f"  fragile: {r['fragile'][:100]}")
    else:
        # Refusals carry the reason a human acts on — "no recorded good run",
        # "the feature was removed". The console scrolls; the advice should survive.
        log(f"{name}  {r.get('classification') or r['verdict']} — {r.get('reason','')[:220]}")


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
                      f"- score {r.get('score')} (margin {r.get('margin'):+}), "
                      f"{r.get('tier')}, {r.get('verification')}"]
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
