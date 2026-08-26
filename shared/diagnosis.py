"""Answer "why wasn't the element there?" before anything edits a locator.

The healing agent used to hold one hypothesis — the locator is stale — and had no
way to represent another. Every failure that surfaced as a missing element was
therefore laundered into a confident selector edit, including the ones no selector
edit could ever fix: a page that never loaded, a request that failed, a session
that had expired, an overlay in the way, a wait that was two seconds short.

This module gathers the evidence that already exists on disk and returns a verdict
with the reasons that produced it. Three rules govern it:

  * Structural signals beat text ones. How many of a page object's own locators
    match the captured DOM is a fact about this run; a search for the words
    "Sign in" is a guess about somebody else's application. Text markers only ever
    break ties.
  * Unevaluable is not absent. A selector we could not test, a trace we could not
    read and an artifact that was never referenced all contribute nothing, rather
    than contributing zero.
  * Abstaining is a valid answer. INSUFFICIENT_EVIDENCE hands control back to the
    behaviour that shipped before this existed. A weak signal must never block a
    genuine fix.

`collect()` reads the artifacts; `diagnose()` applies the rules. They are split so
the evidence can be logged, tested and shown to a model even when the verdict is
an abstention.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from shared import (baseline, failure_context, page_identity, preconditions,
                    step_provenance, trace_network)

# Verdicts the agent can act on, and what it is allowed to do about them.
ACTIONS = {
    "LOCATOR_STALE": "edit the selector",
    "NOT_READY": "add an explicit readiness wait",
    "TOO_SLOW": "raise the wait budget",
    "BLOCKED": "dismiss what is covering the element, then interact",
}
# Verdicts that stop the run instead. Nothing here is fixable by editing code.
STOP = ("WRONG_PAGE", "PRIOR_STEP_FAILED", "ERROR_STATE", "ENV_UNREACHABLE",
        "DATA_PRECONDITION", "FLAKY_TRANSIENT", "ELEMENT_GONE")
ABSTAIN = "INSUFFICIENT_EVIDENCE"

# Whether a stop verdict should leave a queued handoff in place for the next run.
# An environment that was down will recover; a page the test never reaches will
# produce this identical diagnosis every night until a human changes something.
QUEUE_RETAINING = ("ENV_UNREACHABLE", "ERROR_STATE")


def skip_reason(verdict: str) -> str:
    """The `.skip-reason` value a verdict maps to.

    `infra` tells run.sh to leave the handoff queued; `no-work` consumes it.
    """
    if verdict in QUEUE_RETAINING:
        return "infra"
    return "diagnosed" if verdict in STOP else "no-work"


# "Failed to load Element <locator> in DashboardPage" / "DashboardPage.java:19"
_PAGE_OBJECT_IN_MESSAGE = re.compile(r"\bin\s+([A-Z]\w*(?:Page|Screen|Component|View))\b")
_PAGE_OBJECT_IN_TRACE = re.compile(r"\b([A-Z]\w*(?:Page|Screen|Component|View))\.(?:java|kt|ts)\b")

# Enough of a page object has to be testable before its coverage means anything.
_MIN_EVALUABLE = 2
# A rival page object only corroborates when essentially all of it matches.
_RIVAL_RATIO = 0.99


def expected_page_object(issue: Dict) -> str:
    """The page object the test believed it was on, per the failure itself."""
    for text, pattern in ((issue.get("error_message"), _PAGE_OBJECT_IN_MESSAGE),
                          (issue.get("root_cause"), _PAGE_OBJECT_IN_MESSAGE),
                          (issue.get("stack_trace"), _PAGE_OBJECT_IN_TRACE),
                          (issue.get("execution_log"), _PAGE_OBJECT_IN_MESSAGE)):
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return ""


def _load_page_object_sources(workspace, names: List[str]) -> List[Dict]:
    """Read named page objects straight from the repo. One targeted glob each."""
    sources: List[Dict] = []
    if not workspace or not names:
        return sources
    workspace = Path(workspace)
    for name in names:
        for suffix in ("java", "kt", "ts", "tsx"):
            hits = list(workspace.rglob(f"{name}.{suffix}"))
            hits = [h for h in hits if "target" not in h.parts and "build" not in h.parts]
            if hits:
                try:
                    sources.append({"path": str(hits[0]),
                                    "snippet": hits[0].read_text(encoding="utf-8",
                                                                 errors="ignore")})
                except OSError:
                    pass
                break
    return sources


def _sibling_page_objects(workspace, expected: str, limit: int = 12) -> List[Dict]:
    """Page objects living beside the expected one — the plausible alternatives.

    Scoped to the same directory rather than the whole repo: matching every page
    object in a large codebase against every failure DOM is a quadratic walk for
    a signal that only needs to name the neighbour we actually landed on.
    """
    found: List[Dict] = []
    if not workspace or not expected:
        return found
    anchor = _load_page_object_sources(workspace, [expected])
    if not anchor:
        return found
    for path in sorted(Path(anchor[0]["path"]).parent.glob("*.*")):
        if path.suffix.lstrip(".") not in ("java", "kt", "ts", "tsx"):
            continue
        if path.stem == expected:
            continue
        try:
            found.append({"path": str(path),
                          "snippet": path.read_text(encoding="utf-8", errors="ignore")})
        except OSError:
            continue
        if len(found) >= limit:
            break
    return found


def collect(issue: Dict, workspace=None, page_objects: Optional[List[Dict]] = None,
            budget_s: Optional[int] = None) -> Dict:
    """Gather every channel available for one failure. Never raises."""
    evidence: Dict = {
        "test_name": issue.get("test_name", ""),
        "expected_page_object": expected_page_object(issue),
        "failed_selector": issue.get("failed_selector", ""),
        "failed_selector_inferred": bool(issue.get("failed_selector_inferred")),
        "snapshot_available": False,
        "facts": {}, "markers": [], "coverage": [], "expected_coverage": None,
        "best_rival": None, "failing_selector_matches": None,
        "network": {"available": False}, "steps": {"available": False},
        "preconditions": {"checked": 0, "problems": []},
        "context": {"available": False},
        "baseline": {"available": False}, "baseline_diff": {"available": False},
        "notes": [],
    }

    snapshot_text = ""
    path = issue.get("dom_snapshot") or ""
    if path and Path(path).exists():
        try:
            snapshot_text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            evidence["notes"].append(f"DOM snapshot unreadable: {exc}")
    elif path:
        evidence["notes"].append("DOM snapshot referenced but missing on disk")

    # The framework's own record of the moment, when it wrote one. Everything in
    # it was measured in the live page, so it outranks anything reconstructed from
    # a saved snapshot afterwards.
    evidence["context"] = failure_context.load(
        failure_context.beside_snapshot(issue.get("dom_snapshot") or ""))

    soup = page_identity.parse(snapshot_text) if snapshot_text else None
    if soup is not None:
        evidence["snapshot_available"] = True
        evidence["facts"] = page_identity.page_facts(snapshot_text, soup)
        evidence["markers"] = page_identity.state_markers(evidence["facts"], soup)

        expected = evidence["expected_page_object"]
        candidates = list(page_objects or [])
        if expected and not any(expected in (po.get("path") or "") for po in candidates):
            candidates += _load_page_object_sources(workspace, [expected])
        candidates += _sibling_page_objects(workspace, expected)

        seen_paths, unique = set(), []
        for candidate in candidates:
            key = candidate.get("path", "")
            if key in seen_paths:
                continue
            seen_paths.add(key)
            unique.append(candidate)

        evidence["coverage"] = page_identity.page_object_coverage(unique, soup)
        for report in evidence["coverage"]:
            if expected and report["name"] == expected:
                evidence["expected_coverage"] = report

        live = failure_context.self_coverage(evidence["context"])
        if live and live["evaluable"]:
            live.setdefault("total", live["evaluable"])
            live.setdefault("ratio", live["matched"] / live["evaluable"])
            evidence["expected_coverage"] = live
        rivals = [c for c in evidence["coverage"]
                  if c["name"] != expected and c["ratio"] is not None]
        if rivals:
            evidence["best_rival"] = rivals[0]

        live_matches = failure_context.anchor_matches(evidence["context"])
        if live_matches is not None:
            evidence["failing_selector_matches"] = live_matches

        selector = page_identity.normalize_selector(evidence["failed_selector"])
        if evidence["failing_selector_matches"] is None and selector:
            try:
                evidence["failing_selector_matches"] = len(
                    soup.select(selector, limit=25))
            except Exception:
                evidence["notes"].append("failing selector could not be evaluated")

    # What this page looked like the last time a test reached it successfully.
    # Absent for a page never yet seen passing, which only lowers confidence.
    expected_name = evidence["expected_page_object"]
    evidence["baseline"] = baseline.load(expected_name, workspace)
    if evidence["baseline"]["available"] and evidence["facts"]:
        facts = dict(evidence["facts"])
        facts["body_class"] = evidence["facts"].get("body_class") or \
            evidence["context"].get("body_class", "")
        evidence["baseline_diff"] = baseline.diff(
            evidence["baseline"], facts,
            failure_context.self_coverage(evidence["context"]))

    evidence["network"] = trace_network.summarize(issue.get("trace_path"),
                                                  issue.get("failure_url", ""))
    evidence["steps"] = step_provenance.summarize(issue.get("execution_log", ""),
                                                  budget_s)
    if workspace:
        evidence["preconditions"] = preconditions.check(
            issue.get("execution_log", ""), workspace)
    return evidence


# ── Rules ─────────────────────────────────────────────────────────────────────
#
# Ordered, first match wins, so the early ones must be tight. Each returns
# (verdict, confidence, reasons, remediation) or None.

def _rule_env_unreachable(ev: Dict):
    net = ev["network"]
    if not net.get("available"):
        return None
    # Third-party beacons fail constantly and mean nothing about the application.
    failed, total = len(net.get("first_party_failed") or []), net.get("total") or 0
    document_dead = net.get("document_status") in (None, 0, -1) and net.get("document_url")
    # A single failed telemetry beacon is not an outage. Require the document
    # itself to have died, or the failures to dominate the whole page load.
    widespread = total >= 5 and failed / total > 0.5
    if document_dead or widespread:
        return ("ENV_UNREACHABLE", "HIGH",
                [f"{failed} of {total} requests failed"
                 + ("; the document request never completed" if document_dead else "")],
                "check the environment is reachable before re-running")
    return None


def _rule_error_state(ev: Dict):
    net = ev["network"]
    if not net.get("available"):
        return None
    status = net.get("document_status")
    if isinstance(status, int) and status >= 400:
        return ("ERROR_STATE", "HIGH",
                [f"the page itself returned HTTP {status}"],
                "this is an application or environment error, not a locator")
    if net.get("first_party_server_errors"):
        first = net["first_party_server_errors"][0]
        return ("ERROR_STATE", "HIGH",
                [f"HTTP {first['status']} from {first['url'][:80]}"],
                "a backend call failed; the page could not render what the test expected")
    if net.get("first_party_auth_rejections"):
        first = net["first_party_auth_rejections"][0]
        return ("ERROR_STATE", "HIGH",
                [f"HTTP {first['status']} from {first['url'][:80]} — the session was rejected"],
                "re-establish authentication before re-running")
    return None


def _rule_precondition(ev: Dict):
    problems = ev["preconditions"].get("problems") or []
    if not problems:
        return None
    # A stale artifact only explains the failure if the test really did end up
    # somewhere unexpected. If the expected page is plainly there, the artifact
    # is a separate piece of housekeeping and must not preempt a real fix.
    expected = ev.get("expected_coverage")
    if expected and expected["evaluable"] >= _MIN_EVALUABLE and expected["matched"] > 0:
        ev["notes"].append(f"{problems[0]['kind']} found, but the expected page is "
                           f"present — not treating it as the cause")
        return None
    first = problems[0]
    reasons = [f"{first['artifact']}: {first['detail']}"]

    # Corroboration matters as much as the finding. A stale artifact plus a page
    # that plainly is not the expected one is a complete causal chain; the stale
    # artifact on its own is only a suspicion.
    confidence = "MEDIUM"
    if expected and expected["evaluable"] >= _MIN_EVALUABLE:
        reasons.append(f"consequence: {expected['name']} has 0 of "
                       f"{expected['evaluable']} of its own locators on this page")
        confidence = "HIGH"
    rival = ev.get("best_rival")
    if rival and rival["ratio"] is not None and rival["ratio"] >= _RIVAL_RATIO:
        reasons.append(f"the page reached is {rival['name']} "
                       f"({rival['matched']}/{rival['evaluable']} matched)")
    if ev.get("markers"):
        reasons.append("page state markers: " + ", ".join(
            f"{m['marker']} ({m['where']})" for m in ev["markers"][:3]))

    return ("DATA_PRECONDITION", confidence, reasons, first["remediation"])


def _rule_not_ready(ev: Dict):
    context = ev["context"]
    if not context.get("available"):
        return None
    still_loading = str(context.get("ready_state") or "").lower() in ("loading", "interactive")
    busy = str(context.get("aria_busy") or "").lower() == "true"
    changing = context.get("dom_changed_during_wait") is True
    if not (changing and (still_loading or busy)):
        return None
    reasons = ["the DOM was still changing when the wait gave up"]
    if still_loading:
        reasons.append(f"document.readyState was '{context['ready_state']}', not 'complete'")
    if busy:
        reasons.append("the page was marked aria-busy")
    return ("NOT_READY", "HIGH", reasons,
            "wait for the page to settle before asserting on it, rather than "
            "changing the selector")


def _rule_too_slow(ev: Dict):
    """The element arrived, but only after the budget had already expired.

    Distinguished from BLOCKED by the DOM still changing: something was still being
    rendered, so more time was the missing ingredient rather than an obstruction.
    """
    context = ev["context"]
    if not context.get("available"):
        return None
    if not ev.get("failing_selector_matches"):
        return None
    if context.get("dom_changed_during_wait") is not True:
        return None
    elapsed, budget = context.get("elapsed_ms"), context.get("budget_ms")
    if not elapsed or not budget or elapsed < budget - 1000:
        return None
    return ("TOO_SLOW", "MEDIUM",
            [f"the anchor matched {ev['failing_selector_matches']} element(s) but only "
             f"after the {round(budget / 1000)}s budget had run out",
             "the DOM was still changing throughout, so the page was still rendering"],
            "raise the wait budget for this element — a probe with a larger budget "
            "confirms it before anything is edited")


def _rule_present_but_not_visible(ev: Dict):
    matches = ev.get("failing_selector_matches")
    if not matches:
        return None
    reasons = [f"the failing selector matches {matches} element(s) in the captured DOM, "
               f"so it is present but never became visible"]
    confidence = "MEDIUM"
    # A settled page rules out "it was still rendering", which is what separates
    # something covering the element from something not having drawn it yet.
    if ev["context"].get("dom_changed_during_wait") is False:
        reasons.append("the DOM had stopped changing, so it was obstructed rather "
                       "than still rendering")
        confidence = "HIGH"
    # The element the test looked for is in the DOM. Whatever went wrong, the
    # selector is not stale — replacing it can only make the test weaker.
    return ("BLOCKED", confidence, reasons,
            "the element is covered, collapsed or off-screen — dismiss what is over "
            "it rather than changing the selector")


def _rule_wrong_page(ev: Dict):
    expected = ev.get("expected_coverage")
    if not expected or expected["evaluable"] < _MIN_EVALUABLE or expected["matched"]:
        return None

    reasons = [f"{expected['name']}: 0 of {expected['evaluable']} of its own "
               f"locators match the captured DOM"]
    confidence = "MEDIUM"

    comparison = ev.get("baseline_diff") or {}
    if baseline.is_different_page(comparison):
        for mismatch in comparison["mismatches"][:3]:
            reasons.append(f"vs. the last good run: {mismatch}")
        confidence = "HIGH"

    rival = ev.get("best_rival")
    if rival and rival["ratio"] is not None and rival["ratio"] >= _RIVAL_RATIO \
            and rival["evaluable"] >= 1:
        reasons.append(f"{rival['name']} matches {rival['matched']} of "
                       f"{rival['evaluable']} — that is the page we are actually on")
        confidence = "HIGH"
    if ev.get("markers"):
        shown = ", ".join(f"{m['marker']} ({m['where']})" for m in ev["markers"][:3])
        reasons.append(f"page state markers: {shown}")
        confidence = "HIGH"
    if ev["facts"].get("title"):
        reasons.append(f"title: {ev['facts']['title'][:80]}")

    return ("WRONG_PAGE", confidence, reasons,
            "the test never reached this page — fix what happens before it, "
            "not the locator")


def _rule_locator_stale(ev: Dict):
    expected = ev.get("expected_coverage")
    if not expected or expected["evaluable"] < _MIN_EVALUABLE:
        return None
    if expected["matched"] == 0:
        return None
    if ev.get("failing_selector_matches"):
        return None
    reasons = [f"{expected['name']}: {expected['matched']} of {expected['evaluable']} "
               f"locators still match, so this is the right page",
               "the failing selector is the one that no longer matches"]
    comparison = ev.get("baseline_diff") or {}
    if comparison.get("available"):
        vanished = comparison.get("vanished") or []
        if vanished:
            reasons.append(f"vs. the last good run, only {', '.join(vanished[:3])} "
                           f"went missing")
        elif comparison.get("matches"):
            reasons.append("the page still matches its last good run in "
                           + ", ".join(comparison["matches"][:3]))
    return ("LOCATOR_STALE", "HIGH", reasons, "update the selector")


_RULES = (
    _rule_env_unreachable,
    _rule_error_state,
    _rule_precondition,
    _rule_not_ready,
    _rule_too_slow,
    _rule_present_but_not_visible,
    _rule_wrong_page,
    _rule_locator_stale,
)


def diagnose(evidence: Dict) -> Dict:
    """Apply the rules to gathered evidence. Always returns a verdict."""
    for rule in _RULES:
        try:
            outcome = rule(evidence)
        except Exception as exc:
            evidence["notes"].append(f"{rule.__name__} failed: {exc}")
            continue
        if outcome:
            verdict, confidence, reasons, remediation = outcome
            return {"verdict": verdict, "confidence": confidence,
                    "reasons": reasons, "remediation": remediation,
                    "action": ACTIONS.get(verdict, ""),
                    "actionable": verdict in ACTIONS,
                    "rule": rule.__name__, "notes": evidence.get("notes", [])}

    return {"verdict": ABSTAIN, "confidence": "LOW",
            "reasons": [_why_abstained(evidence)], "remediation": "",
            "action": "", "actionable": False, "rule": "",
            "notes": evidence.get("notes", [])}


def _why_abstained(evidence: Dict) -> str:
    if not evidence.get("snapshot_available"):
        return "no DOM snapshot was captured, so the page could not be identified"
    expected = evidence.get("expected_coverage")
    if not expected:
        return ("the page object named in the failure could not be located, "
                "so its coverage could not be measured")
    if expected["evaluable"] < _MIN_EVALUABLE:
        return (f"only {expected['evaluable']} of {expected['name']}'s "
                f"{expected['total']} locators could be evaluated — too few to judge")
    return "no rule matched with enough confidence to act on"


# ── Confirmation probes ───────────────────────────────────────────────────────
#
# The agent already has the one capability that turns a guess into a measurement:
# it can run the test again. Re-runs were only ever used to verify a fix, which
# is the least informative moment to use them — by then a model has been called
# and a file has been edited on the strength of an unverified hypothesis.
#
# A probe is one targeted re-run that either confirms a verdict or refutes it,
# and it costs less than the model call plus speculative edit plus verification
# run plus revert that it replaces. For TOO_SLOW and NOT_READY it is also the
# experiment itself: a run with a larger budget that passes has proved the fix
# before a line is changed.
PROBES = {
    "FLAKY_TRANSIENT":   {"kind": "rerun", "confirms_when": "passed"},
    "ENV_UNREACHABLE":   {"kind": "rerun", "confirms_when": "failed"},
    "ERROR_STATE":       {"kind": "rerun", "confirms_when": "failed"},
    "WRONG_PAGE":        {"kind": "rerun_compare_dom", "confirms_when": "same_dom"},
    "PRIOR_STEP_FAILED": {"kind": "rerun_compare_dom", "confirms_when": "same_dom"},
    "TOO_SLOW":          {"kind": "rerun_extended_budget", "confirms_when": "passed"},
    "NOT_READY":         {"kind": "rerun_extended_budget", "confirms_when": "passed"},
}


def needs_probe(diagnosis: dict) -> bool:
    """Whether this verdict should be measured before it is acted on.

    HIGH-confidence verdicts already rest on several agreeing channels, so a probe
    would cost a test run to re-learn what is known. Everything below that is
    worth one.
    """
    return (diagnosis.get("confidence") != "HIGH"
            and diagnosis.get("verdict") in PROBES)


def apply_probe(diagnosis: dict, outcome: str) -> dict:
    """Fold a probe result back into the verdict.

    `outcome` is what the probe observed: "passed", "failed", "same_dom",
    "different_dom", or "inconclusive". A confirmed verdict is promoted to HIGH;
    a refuted one is demoted to an abstention rather than inverted, because a
    probe that disagrees tells us the reasoning was wrong, not what is right.
    """
    probe = PROBES.get(diagnosis.get("verdict") or "")
    if not probe or outcome == "inconclusive":
        diagnosis.setdefault("probe", {})["result"] = outcome
        return diagnosis

    confirmed = outcome == probe["confirms_when"]
    diagnosis["probe"] = {"kind": probe["kind"], "result": outcome,
                          "confirmed": confirmed}

    if confirmed:
        diagnosis["confidence"] = "HIGH"
        diagnosis["reasons"] = list(diagnosis.get("reasons") or []) + [
            f"probe ({probe['kind']}) confirmed this: {outcome}"]
        # A passing re-run at a larger budget is the fix, demonstrated.
        if diagnosis["verdict"] in ("TOO_SLOW", "NOT_READY"):
            diagnosis["proven"] = True
    else:
        diagnosis["reasons"] = list(diagnosis.get("reasons") or []) + [
            f"probe ({probe['kind']}) did not confirm this: {outcome}"]
        diagnosis["verdict"] = ABSTAIN
        diagnosis["confidence"] = "LOW"
        diagnosis["actionable"] = False
        diagnosis["action"] = ""
    return diagnosis


def describe(diagnosis: Dict, evidence: Dict) -> List[str]:
    """The diagnosis as log lines. Rendered by the caller with its own prefix."""
    headline = f"DIAGNOSIS: {diagnosis['verdict']} ({diagnosis['confidence']})"
    if diagnosis["verdict"] in STOP:
        headline += " — not a locator problem"
    lines = [headline]
    lines += [f"  {reason}" for reason in diagnosis.get("reasons", [])]

    steps = step_provenance.describe(evidence.get("steps") or {})
    if steps:
        lines += [f"  {line}" for line in steps.splitlines()]
    network = trace_network.describe(evidence.get("network") or {}, max_lines=3)
    if network:
        lines += [f"  network: {line}" for line in network.splitlines()]
    if diagnosis.get("remediation"):
        lines.append(f"REMEDIATION: {diagnosis['remediation']}")
    return lines
