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

from shared import (baseline, dom_snapshot, failure_context, history,
                    page_identity, preconditions, step_provenance,
                    trace_network)

# Verdicts the agent can act on, and what it is allowed to do about them.
# The only verdict that authorises the agent to edit code.
#
# Timing and obstruction used to be here too. They were removed on a constraint
# that only surfaced during implementation: the framework has no per-element wait
# budget — WaitHelper.getTimeout reads the global ObjectWaitTime — so "give this
# element more time" means slowing down every test in the suite. A fix like that
# also hides the thing most worth knowing, which is that the page got slower. They
# now report precisely and stop, each naming the change a human would make.
ACTIONS = {
    "LOCATOR_STALE": "edit the selector",
    # Ambiguity is a locator defect with a locator fix, so it belongs here rather
    # than among the stop verdicts. It is also the one case a match count above
    # zero used to be read as proof that the selector was fine.
    "AMBIGUOUS_LOCATOR": "narrow the selector so it matches exactly one element",
}
# Verdicts that stop the run instead. Nothing here is fixable by editing a locator.
STOP = ("WRONG_PAGE", "PRIOR_STEP_FAILED", "ERROR_STATE", "ENV_UNREACHABLE",
        "DATA_PRECONDITION", "FLAKY_TRANSIENT", "ELEMENT_GONE",
        "NOT_READY", "TOO_SLOW", "BLOCKED")
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
_PAGE_OBJECT_NAME = re.compile(r"^[A-Z]\w*(?:Page|Screen|Component|View)$")

# Enough of a page object has to be testable before its coverage means anything.
_MIN_EVALUABLE = 2
# A rival page object only corroborates when essentially all of it matches.
_RIVAL_RATIO = 0.99

# Actions whose whole purpose is to move the flow on. If one of these was the last
# thing that happened and nothing navigated, it did not work.
_INTERACTIONS = ("click", "submit", "select", "press", "tap", "choose", "login",
                 "sign in", "continue", "next", "save", "search")


def expected_page_object(issue: Dict, workspace=None) -> str:
    """The page object the test believed it was on, per the failure itself."""
    for text, pattern in ((issue.get("error_message"), _PAGE_OBJECT_IN_MESSAGE),
                          (issue.get("root_cause"), _PAGE_OBJECT_IN_MESSAGE),
                          (issue.get("stack_trace"), _PAGE_OBJECT_IN_TRACE),
                          (issue.get("execution_log"), _PAGE_OBJECT_IN_MESSAGE)):
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return owner_of_failing_element(issue, workspace)


def owner_of_failing_element(issue: Dict, workspace=None) -> str:
    """The page object that declares the element the failure names.

    Only the page-load assertion says "in DashboardPage". Every interaction
    failure — the majority of them — reports an element name and a selector and
    nothing about who owns them, so the patterns above return nothing and the
    whole coverage signal switches off for exactly the failures it was built for.

    The declaration is the missing link and it is in the repo: whichever page
    object holds that selector is the page the test believed it was on.
    """
    if not workspace:
        return ""
    try:
        from shared.code_analyzer import CodeAnalyzer
    except Exception:
        return ""
    try:
        analyzer = CodeAnalyzer()
        names = analyzer.extract_element_names(
            root_cause=issue.get("error_message") or issue.get("root_cause") or "",
            execution_log=issue.get("execution_log", ""))
        selector = (issue.get("failed_selector") or "").strip()
        if selector and selector not in names:
            names.insert(0, selector)
        if not names:
            return ""
        found = analyzer.find_page_objects_for_locators(
            repo_path=str(workspace), element_names=names, max_files=3,
            max_chars_per_file=1)
    except Exception:
        return ""

    # Scored best-first, but a test class quoting the same element name scores
    # too. Only something named like a page object can be one.
    for candidate in found:
        stem = Path(candidate.get("path", "")).stem
        if _PAGE_OBJECT_NAME.match(stem):
            return stem
    return ""


def _anchors_cover(context: Dict, failed_selector: str) -> bool:
    """Whether the context's recorded anchors are the selector that failed.

    A context written for a page-load assertion counts that assertion's anchors.
    Reading those as the failing element's match count answers a question about
    one element with a measurement of another — and zero is the answer that
    silently turns a stale locator into a page that was never reached.
    """
    if not context.get("available"):
        return False
    anchors = context.get("anchors") or []
    if not anchors:
        return False
    selector = page_identity.normalize_selector(failed_selector or "")
    if not selector:
        # Nothing to compare against: the anchors are the only account there is.
        return True
    for anchor in anchors:
        recorded = str(anchor.get("selector") or "")
        recorded = recorded.split("Locator@", 1)[-1].strip()
        if page_identity.normalize_selector(recorded) == selector:
            return True
    return False


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
            budget_s: Optional[int] = None, audit_dir=None,
            not_before: Optional[float] = None) -> Dict:
    """Gather every channel available for one failure. Never raises."""
    evidence: Dict = {
        "test_name": issue.get("test_name", ""),
        "expected_page_object": expected_page_object(issue, workspace),
        "failed_selector": issue.get("failed_selector", ""),
        "failed_selector_inferred": bool(issue.get("failed_selector_inferred")),
        "snapshot_available": False,
        "facts": {}, "markers": [], "coverage": [], "expected_coverage": None,
        "best_rival": None, "failing_selector_matches": None,
        # A count measured on the live locator and a count evaluated against saved
        # markup answer different questions — see `_rule_ambiguous_locator`.
        "matches_source": "", "wait_error": "",
        "network": {"available": False}, "steps": {"available": False},
        "preconditions": {"checked": 0, "problems": []},
        "context": {"available": False},
        "baseline": {"available": False}, "baseline_diff": {"available": False},
        "history": {"available": False},
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
    # a saved snapshot afterwards — which is exactly why it has to be shown to
    # belong to this failure before any of it is believed.
    header = dom_snapshot.parse_header(snapshot_text) if snapshot_text else {}
    preserved = issue.get("failure_context") or ""
    if preserved and Path(preserved).exists():
        # Already paired with this failure by whoever collected the artifacts,
        # and copied into the session so it survives CI clearing the report dir.
        evidence["context"] = failure_context.load(preserved)
    else:
        evidence["context"] = failure_context.for_failure(
            issue.get("dom_snapshot") or "",
            test_name=header.get("test") or issue.get("test_name", ""),
            captured_at=header.get("capturedAt", ""),
            not_before=not_before)
    if evidence["context"].get("rejected"):
        evidence["notes"].append(
            "ignored a failure context from another run — "
            + evidence["context"]["rejected"])
    evidence["wait_error"] = failure_context.wait_error(evidence["context"])

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

        # Measured in the browser, so it outranks the snapshot approximation — but
        # only for the page object it actually measured. A report for some other
        # page installed here answers "how much of the expected page is present?"
        # with a number about a page nobody asked about.
        live = failure_context.self_coverage(evidence["context"])
        if live and live["evaluable"]:
            live.setdefault("total", live["evaluable"])
            live.setdefault("ratio", live["matched"] / live["evaluable"])
            if not expected or live["name"] == expected:
                evidence["expected_coverage"] = live
            else:
                evidence["coverage"] = [live] + list(evidence["coverage"])
                evidence["notes"].append(
                    f"the failure context measured {live['name']}, not {expected} — "
                    f"kept as a rival rather than as the expected page")
        rivals = [c for c in evidence["coverage"]
                  if c["name"] != expected and c["ratio"] is not None]
        if rivals:
            evidence["best_rival"] = rivals[0]

        # Same rule for the anchor counts: they answer "is the failing element in
        # the document?" only when the anchors are the failing element. Anchors
        # from a different wait report a different element's absence.
        if _anchors_cover(evidence["context"], evidence["failed_selector"]):
            live_matches = failure_context.anchor_matches(evidence["context"])
            if live_matches is not None:
                evidence["failing_selector_matches"] = live_matches
                evidence["matches_source"] = "live"
            # Present-but-hidden is not a stale locator, and the count alone
            # cannot say which of the two happened.
            evidence["anchor_state"] = failure_context.anchor_state(evidence["context"])

        selector = page_identity.normalize_selector(evidence["failed_selector"])
        if evidence["failing_selector_matches"] is None and selector:
            try:
                evidence["failing_selector_matches"] = len(
                    soup.select(selector, limit=25))
                evidence["matches_source"] = "snapshot"
            except Exception:
                evidence["notes"].append("failing selector could not be evaluated")

    # What this page looked like the last time a test reached it successfully.
    # Absent for a page never yet seen passing, which only lowers confidence.
    expected_name = evidence["expected_page_object"]
    evidence["baseline"] = baseline.load(expected_name, workspace,
                                         issue.get("healing_baseline_dir"),
                                         not_after=header.get("capturedAt", ""))
    if evidence["baseline"].get("rejected"):
        evidence["notes"].append(
            "ignored a baseline that is not older than the failure — "
            + evidence["baseline"]["rejected"])
    if evidence["baseline"]["available"] and evidence["facts"]:
        facts = dict(evidence["facts"])
        facts["body_class"] = evidence["facts"].get("body_class") or \
            evidence["context"].get("body_class", "")
        # Per-locator counts are what make the diff name individual elements.
        # Without them `vanished` is never computed, and a rule that reads its
        # absence as "nothing vanished" concludes from no evidence at all.
        evidence["baseline_diff"] = baseline.diff(
            evidence["baseline"], facts,
            _live_or_snapshot_coverage(evidence))

    evidence["network"] = trace_network.summarize(issue.get("trace_path"),
                                                  issue.get("failure_url", ""))
    evidence["steps"] = step_provenance.summarize(issue.get("execution_log", ""),
                                                  budget_s)
    evidence["history"] = history.load(issue.get("test_name", ""),
                                       issue.get("flaky_tests"), audit_dir)
    if workspace:
        evidence["preconditions"] = preconditions.check(
            issue.get("execution_log", ""), workspace)
    return evidence


def _live_or_snapshot_coverage(evidence: Dict) -> Optional[Dict]:
    """Per-locator counts for the expected page, measured or approximated.

    The live report is better and is preferred. The snapshot-derived one is
    weaker — `getByRole` and XPath cannot be evaluated against it — but it names
    the same fields, which is all the baseline diff needs.
    """
    live = failure_context.self_coverage(evidence.get("context") or {})
    expected = evidence.get("expected_coverage") or {}
    if live and live["evaluable"] and live["name"] == expected.get("name"):
        return live
    details = expected.get("details")
    if isinstance(details, dict):
        return expected
    if isinstance(details, list):
        return {"name": expected.get("name", ""),
                "details": {d["name"]: d["count"] for d in details
                            if d.get("name") and isinstance(d.get("count"), int)}}
    return None


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
            "add WaitHelper.waitForNetworkIdle(config), or "
            "waitForLoadingComplete(config, loadingBar), before this assertion — "
            "the selector is not the problem")


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
            "the element does appear, just late. ObjectWaitTime is global, so "
            "raising it slows every test — decide that deliberately, or find out "
            "why this page got slower")


_STRICT_MODE = re.compile(r"strict[ _-]?mode violation", re.I)


def _rule_ambiguous_locator(ev: Dict):
    """The selector matched several elements, so no action could run on it.

    Playwright resolves a locator strictly: two matches is an error thrown the
    instant the wait starts, not a wait that ran out of time. The remedy is to
    narrow the selector to the one element the test means — which makes this a
    locator defect, and the exact case a match count above zero was previously
    read as proof that the locator was fine.

    Placed ahead of the timing and visibility rules because it is not a competing
    explanation for the same evidence: when the locator cannot resolve, nothing
    downstream of it ever got the chance to be slow, hidden or obstructed.
    """
    matches = ev.get("failing_selector_matches") or 0
    if matches < 2:
        return None
    strict = bool(_STRICT_MODE.search(ev.get("wait_error") or ""))
    # A snapshot count is an approximation of a different question. The real
    # locator may be chained off a parent or scoped to a frame, in which case
    # document-wide matches say nothing about whether it was ambiguous. Only a
    # count taken on the locator itself settles that.
    if ev.get("matches_source") != "live" and not strict:
        return None

    reasons = [f"the failing selector matches {matches} elements, so Playwright "
               f"cannot decide which one the test means"]
    if strict:
        reasons.append("the framework recorded a strict mode violation, which is "
                       "this failure by name")
    else:
        reasons.append("counted on the live locator at the moment it failed, so it "
                       "holds for a chained or scoped locator too")
    context = ev["context"]
    if failure_context.wait_was_a_timeout(context) is False:
        reasons.append(f"the wait ended after {context['elapsed_ms']}ms of a "
                       f"{context['budget_ms']}ms budget, so it threw rather than "
                       f"timed out")
    return ("AMBIGUOUS_LOCATOR", "HIGH", reasons,
            "narrow the selector to the single element the test means — a second "
            "match is the failure, not a symptom of one")


def _rule_present_but_not_visible(ev: Dict):
    matches = ev.get("failing_selector_matches")
    if not matches:
        return None
    context = ev["context"]
    # The framework measured this element as visible at the moment it failed, so
    # "it never became visible" is contradicted by the only channel that looked.
    # An element that is merely covered still reports visible and still passes the
    # visibility wait, so this rule never depended on that case: what reaches here
    # with a `visible` anchor failed for some other reason entirely.
    if ev.get("anchor_state") == "visible":
        ev["notes"].append("declined BLOCKED: the anchor was measured visible at "
                           "failure time, so it did not fail to become visible")
        return None
    # A wait that gave up in a fraction of its budget did not run out of time.
    # Whatever ended it, it was not the element being slow to appear — and a
    # verdict of "never became visible" reads as though 30 seconds were spent.
    if failure_context.wait_was_a_timeout(context) is False:
        ev["notes"].append(
            f"declined BLOCKED: the wait ended after {context['elapsed_ms']}ms of a "
            f"{context['budget_ms']}ms budget, so it threw rather than timed out")
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
            "it before interacting, rather than changing the selector")


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


def _rule_prior_step_failed(ev: Dict):
    """An earlier action silently did nothing, so the flow never advanced.

    Ranked above WRONG_PAGE, which is the same observation with less to say about
    it. The distinguishing evidence is the navigation history: a click that was
    supposed to move the flow, and no new URL after it.
    """
    expected = ev.get("expected_coverage")
    if not expected or expected["evaluable"] < _MIN_EVALUABLE or expected["matched"]:
        return None
    context = ev["context"]
    if not context.get("available"):
        return None

    steps = ev["steps"]
    last_action = (steps.get("last_action") or "").lower()
    if not last_action or not any(word in last_action for word in _INTERACTIONS):
        return None

    # One entry means the only navigation was the initial load: nothing the test
    # did afterwards moved the page.
    navigation = context.get("navigation") or []
    distinct = list(dict.fromkeys(navigation))
    if len(distinct) > 1:
        return None

    return ("PRIOR_STEP_FAILED", "HIGH" if navigation else "MEDIUM",
            [f"the last action was {steps['last_action'][:80]!r}, and the page never "
             f"navigated afterwards",
             f"{expected['name']} has 0 of {expected['evaluable']} of its locators here, "
             f"so the flow stopped before this page"],
            "the previous step did not do what it was supposed to — fix that, not "
            "the locator on the page it never reached")


def _rule_element_gone(ev: Dict):
    """Right page, and this element was never here even when the test passed.

    The mirror of LOCATOR_STALE, and only separable from it with a baseline: from
    a single run, "the feature was removed" and "the selector was always wrong"
    look identical. Without one, abstain rather than guess.
    """
    expected = ev.get("expected_coverage")
    if not expected or expected["evaluable"] < _MIN_EVALUABLE or not expected["matched"]:
        return None
    if ev.get("failing_selector_matches"):
        return None
    comparison = ev.get("baseline_diff") or {}
    if not comparison.get("available"):
        return None
    # Nothing that used to be present has gone missing, yet the element is absent:
    # it was never part of a passing run.
    if comparison.get("vanished"):
        return None
    # "Absent on the last good run too" has to be read off that run, not inferred
    # from `vanished` being empty — it is also empty when no per-locator counts
    # were available to compare, which is the common case. Without the recorded
    # zero this rule fires on nothing and outranks the stale locator it hides.
    field = _baseline_field_for(ev)
    if not field:
        return None
    return ("ELEMENT_GONE", "MEDIUM",
            [f"{expected['name']} matches its last good run, so this is the right page",
             f"{field} was absent on that run too, so it was never here to be renamed"],
            "the element appears to have been removed from the product — confirm "
            "with the team before changing the test")


def _baseline_field_for(ev: Dict) -> str:
    """The failing element's field name, when the baseline recorded it as absent.

    Empty when the baseline never measured it, which is not the same as having
    measured it and found nothing. The name comes from the page object source,
    the only place that ties a selector to the field the baseline counts under —
    a live coverage report is keyed by field name and carries no selectors.
    """
    recorded = (ev.get("baseline") or {}).get("coverage") or {}
    selector = page_identity.normalize_selector(ev.get("failed_selector") or "")
    if not recorded or not selector:
        return ""
    for report in ev.get("coverage") or []:
        for detail in report.get("details") or []:
            if not isinstance(detail, dict):
                continue
            if page_identity.normalize_selector(detail.get("selector") or "") != selector:
                continue
            name = detail.get("name") or ""
            if name in recorded:
                return name if recorded[name] == 0 else ""
    return ""


def _rule_flaky_transient(ev: Dict):
    """Nothing structural explains it, and this test has recovered before.

    Deliberately last. "It works sometimes" is a conclusion to reach after the
    deterministic rules have all declined, never before.
    """
    record = ev.get("history") or {}
    if not record.get("available") or not record.get("intermittent"):
        return None
    return ("FLAKY_TRANSIENT", "MEDIUM",
            [history.describe(record),
             "no structural cause was found, and this test has recovered before "
             "without a code change"],
            "re-run to confirm; if it passes, this is flakiness to investigate "
            "rather than a locator to fix")


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
    _rule_ambiguous_locator,
    _rule_not_ready,
    _rule_too_slow,
    _rule_present_but_not_visible,
    _rule_prior_step_failed,
    _rule_wrong_page,
    _rule_element_gone,
    _rule_locator_stale,
    _rule_flaky_transient,
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
                    "anchor_state": evidence.get("anchor_state"),
                    "rule": rule.__name__, "notes": evidence.get("notes", [])}

    return {"verdict": ABSTAIN, "confidence": "LOW",
            "reasons": [_why_abstained(evidence)], "remediation": "",
            "action": "", "actionable": False, "rule": "",
            "anchor_state": evidence.get("anchor_state"),
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
    # Measured in the live page, and the one distinction the reasons above cannot
    # draw: a selector that matched nothing versus one that matched and was hidden.
    if diagnosis.get("anchor_state") == "hidden":
        lines.append("  the failing selector DID match — the element was present "
                     "but not visible, so this is not a renamed locator")
    elif diagnosis.get("anchor_state") == "absent":
        lines.append("  the failing selector matched nothing in the live page")
    elif diagnosis.get("anchor_state") == "visible":
        # Said out loud because it rules out a whole family of verdicts, and
        # silence here previously let "never became visible" stand unchallenged.
        lines.append("  the failing selector DID match, and the element was "
                     "visible — so it neither went missing nor stayed hidden")

    steps = step_provenance.describe(evidence.get("steps") or {})
    if steps:
        lines += [f"  {line}" for line in steps.splitlines()]
    network = trace_network.describe(evidence.get("network") or {}, max_lines=3)
    if network:
        lines += [f"  network: {line}" for line in network.splitlines()]
    if diagnosis.get("remediation"):
        lines.append(f"REMEDIATION: {diagnosis['remediation']}")
    return lines
