"""Read the structured failure context the framework writes beside the DOM snapshot.

`automation.core.FailureContext` records, at the moment an element fails — a
page-load assertion giving up (`kind` PAGE_NOT_LOADED) or an interaction timing out
(`kind` ELEMENT_INTERACTION) — the things that cannot be reconstructed afterwards:
how many elements each anchor actually matched, whether the page was still loading,
whether the DOM was still changing, how much of the page object's own locator set was
present, and what the network and JavaScript did on the way there. One per test, for
the first failure: the ones after it are its consequences.

Older runs, other frameworks and CI layouts that lost the file all produce nothing
here, and every consumer has to keep working without it — the diagnosis is weaker
without these channels, never blocked on them. `available` is the field that carries
that distinction.

`available` is necessary but not sufficient. A file is only evidence about *this*
failure, and the fallback that locates one matches on test-method name, which every
previous run of that test also wrote. `for_failure()` is the entry point that checks;
`load()` and `beside_snapshot()` do not.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Written next to the snapshot as <method>_<HHmmss>.context.json.
_SUFFIX = ".context.json"

# How far apart the context's `failedAt` and the snapshot's `capturedAt` may be and
# still describe the same failure. They are written seconds apart by the same
# handler; two minutes is slack for a slow capture, not for a different run.
_TOLERANCE_S = 120

_TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def _parse_timestamp(value: str) -> Optional[datetime]:
    """One of the framework's timestamps, or None when it is not one."""
    text = (value or "").strip().rstrip("Z")
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def find(report_dir, method_name: str, not_before=None) -> Optional[Path]:
    """The newest failure context for this test method, if one was written.

    `not_before` is an epoch seconds floor — normally the moment the run started.
    Without it the newest match may be from any previous run, which reads as this
    run's evidence and is indistinguishable from it once loaded.
    """
    for path in _candidates(report_dir, method_name, not_before):
        return path
    return None


def _candidates(report_dir, method_name: str, not_before=None) -> List[Path]:
    """Every context for this method, newest first, no older than `not_before`."""
    if not method_name or not report_dir or not Path(report_dir).exists():
        return []
    matches = [p for p in Path(report_dir).rglob(f"{method_name}_*{_SUFFIX}") if p.is_file()]
    if not_before is not None:
        matches = [p for p in matches if p.stat().st_mtime >= not_before]
    return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)


def beside_snapshot(dom_snapshot: str, not_before=None) -> Optional[Path]:
    """The context written for the same failure as this DOM snapshot.

    The two are named from the same test and timestamp, so the snapshot's own path
    locates it without a directory scan. Falls back to the newest context for the
    same method when the timestamps differ by a second.

    Prefer `for_failure()`: this locates a file by name alone, and a name says
    nothing about which run wrote it.
    """
    if not dom_snapshot:
        return None
    snapshot = Path(dom_snapshot)
    candidate = snapshot.parent / (snapshot.stem + _SUFFIX)
    if candidate.exists():
        return candidate
    # The two files are stamped a second apart when a capture straddles the clock.
    method = snapshot.stem.rsplit("_", 1)[0]
    return find(snapshot.parent, method, not_before)


def _simple_method(test_name: str) -> str:
    """`a.b.LoginTest.signIn` and `signIn` both reduce to `signIn`.

    The context records a fully qualified name and the snapshot header a bare
    method, so they can only be compared at their narrowest end.
    """
    return (test_name or "").strip().rsplit(".", 1)[-1]


def _belongs(context: Dict, test_name: str, captured_at: str,
             tolerance_s: int, vouched_by_run: bool = False) -> Optional[str]:
    """Why this context does not describe that failure. None when it does.

    Only recorded facts count. A file named after the right method proves
    nothing: every run of that test writes one under the same name.
    """
    recorded_test = _simple_method(context.get("test") or "")
    wanted_test = _simple_method(test_name)
    if recorded_test and wanted_test and recorded_test != wanted_test:
        return f"it was written for {recorded_test}, not {wanted_test}"

    # The caller knows when the run started and the file survived that floor, so
    # it was written by this run. That is the fact the timestamp comparison below
    # is trying to approximate — and a better one, because a context is written
    # the moment an element fails while the DOM snapshot is captured at the end
    # of execution, which on a cascading failure is minutes later.
    if vouched_by_run:
        return None

    failed_at = _parse_timestamp(context.get("failed_at") or "")
    captured = _parse_timestamp(captured_at or "")
    if failed_at and captured:
        drift = abs((failed_at - captured).total_seconds())
        if drift > tolerance_s:
            return (f"it was written {round(drift / 60)} minute(s) from this failure "
                    f"({context.get('failed_at')} vs {captured_at})")
        return None

    # Nothing lined the two up. A context we cannot tie to this failure is worse
    # than no context: it is read with the same authority as a measured one.
    if recorded_test and wanted_test:
        return None
    return "nothing ties it to this failure"


def for_failure(dom_snapshot: str, test_name: str = "", captured_at: str = "",
                tolerance_s: int = _TOLERANCE_S, not_before=None) -> Dict:
    """The context describing *this* failure, or an empty one.

    The sibling named for the snapshot is trusted outright — the framework writes
    the pair together. Anything else has to prove it belongs, because the fallback
    search matches on method name, and every previous run of the test left a file
    with that name. Adopting one silently reports another run's page, coverage,
    anchors and navigation as this run's measurements.

    `rejected` names the reason when a candidate was found and declined, so the
    caller can say why it is working without a context.
    """
    if not dom_snapshot:
        return load(None)

    snapshot = Path(dom_snapshot)
    sibling = snapshot.parent / (snapshot.stem + _SUFFIX)
    if sibling.exists():
        return load(sibling)

    method = snapshot.stem.rsplit("_", 1)[0]
    first_reason = ""
    for path in _candidates(snapshot.parent, method, not_before):
        context = load(path)
        if not context["available"]:
            continue
        reason = _belongs(context, test_name, captured_at, tolerance_s,
                          vouched_by_run=not_before is not None)
        if reason is None:
            return context
        first_reason = first_reason or f"{path.name}: {reason}"

    empty = load(None)
    if first_reason:
        empty["rejected"] = first_reason
    return empty


def load(path) -> Dict:
    """Parse one context file into the shape the diagnosis engine consumes."""
    result: Dict = {
        "available": False, "path": "", "test": "", "failed_at": "", "kind": "",
        "page_object": "", "anchors": [],
        "elapsed_ms": None, "budget_ms": None, "wait_error": "",
        "url": "", "title": "", "body_class": "", "h1": "",
        "ready_state": "", "aria_busy": "",
        "coverage": {}, "dom_changed_during_wait": None,
        "navigation": [], "http_errors": [], "js_errors": [],
        "rejected": "",
    }
    if not path or not Path(path).exists():
        return result
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return result
    if not isinstance(data, dict):
        return result

    result["available"] = True
    result["path"] = str(path)
    failure = data.get("failure") or {}
    page = data.get("page") or {}
    # Which failure this file describes. Without these two the file can only be
    # matched on its name, and every run of the test writes the same name.
    result["test"] = data.get("test", "")
    result["failed_at"] = data.get("failedAt", "")
    result["kind"] = failure.get("kind", "")
    result["page_object"] = failure.get("pageObject", "")
    result["anchors"] = failure.get("anchors") or []
    result["elapsed_ms"] = failure.get("elapsedMs")
    result["budget_ms"] = failure.get("budgetMs")
    # Which exception ended the wait. WaitHelper catches them all and used to
    # report every one as a timeout, so without this a strict-mode violation —
    # an ambiguous selector — is indistinguishable from an element that never
    # appeared. Absent from contexts written before the framework recorded it.
    result["wait_error"] = failure.get("waitError", "")
    result["url"] = page.get("url", "")
    result["title"] = page.get("title", "")
    result["body_class"] = page.get("bodyClass", "")
    result["h1"] = page.get("h1", "")
    result["ready_state"] = page.get("readyState", "")
    result["aria_busy"] = page.get("ariaBusy", "")
    result["coverage"] = data.get("pageObjectCoverage") or {}
    result["dom_changed_during_wait"] = (
        (data.get("domVolatility") or {}).get("changedDuringWait"))
    result["navigation"] = data.get("navigation") or []
    result["http_errors"] = data.get("httpErrors") or []
    result["js_errors"] = data.get("jsErrors") or []
    return result


def wait_error(context: Dict) -> str:
    """The exception that ended the wait, as the framework recorded it.

    Empty for a context written before the framework kept it, which is not the
    same as a wait that ended cleanly — no caller may read it as one.
    """
    if not context.get("available"):
        return ""
    return context.get("wait_error") or ""


def wait_was_a_timeout(context: Dict) -> Optional[bool]:
    """Whether the wait actually spent its budget. None when it cannot be told.

    The distinction the elapsed/budget pair exists to draw. A wait that ended in a
    fraction of its budget did not run out of time; something threw. Any verdict
    of the form "it never became visible in time" is false in that case, however
    well the rest of the evidence fits.
    """
    elapsed, budget = context.get("elapsed_ms"), context.get("budget_ms")
    if elapsed is None or not budget:
        return None
    return elapsed >= budget / 10


def anchor_matches(context: Dict) -> Optional[int]:
    """Total elements the waited-for anchors matched. None when unknown.

    Zero means the element is not in the document; anything else means it is there
    and something stopped it becoming visible. That single number separates a
    renamed element from a covered one.
    """
    if not context.get("available"):
        return None
    counts = [a.get("count") for a in context.get("anchors") or []
              if isinstance(a.get("count"), int)]
    return sum(counts) if counts else None


def anchor_state(context: Dict) -> Optional[str]:
    """What the waited-for anchors were: absent, hidden, or visible. None if unknown.

    `anchor_matches` answers "how many", which cannot separate the two ways a
    click times out. An element that matched nothing was renamed or removed. An
    element that matched and was still not visible is a different failure with a
    different remedy — it is present, so replacing the selector fixes nothing, and
    a "fix" that swaps in another hidden element times out identically.
    """
    if not context.get("available"):
        return None
    anchors = [a for a in context.get("anchors") or []
               if isinstance(a.get("count"), int)]
    if not anchors:
        return None
    if sum(a["count"] for a in anchors) == 0:
        return "absent"
    # `visible` is only trustworthy where the capture recorded it; a missing flag
    # is unknown, not hidden.
    flags = [a.get("visible") for a in anchors if a.get("count")]
    if flags and all(f is False for f in flags):
        return "hidden"
    if any(f is True for f in flags):
        return "visible"
    return None


def self_coverage(context: Dict) -> Optional[Dict]:
    """The page object's own matched/evaluable counts, measured in the live page.

    Strictly better than evaluating selectors against a saved snapshot: it ran in
    the browser, so `getByRole`, XPath and every other strategy the framework
    supports were all genuinely evaluated rather than approximated.
    """
    coverage = context.get("coverage") or {}
    page_object = context.get("page_object") or ""
    report = coverage.get(page_object) or (
        next(iter(coverage.values())) if len(coverage) == 1 else None)
    if not isinstance(report, dict) or "evaluable" not in report:
        return None
    return {"name": page_object, "matched": report.get("matched", 0),
            "evaluable": report.get("evaluable", 0),
            "details": report.get("details") or {}, "source": "live"}


def describe(context: Dict) -> str:
    """A short block for a log line or a prompt. Empty when nothing was recorded."""
    if not context.get("available"):
        return ""
    lines = []
    if context.get("ready_state"):
        busy = f", aria-busy={context['aria_busy']}" if context.get("aria_busy") else ""
        lines.append(f"readyState={context['ready_state']}{busy}")
    changed = context.get("dom_changed_during_wait")
    if changed is not None:
        lines.append("the DOM kept changing during the wait" if changed
                     else "the DOM did not change during the wait")
    matches = anchor_matches(context)
    if matches is not None:
        lines.append(f"the waited-for anchor(s) matched {matches} element(s)")
    if context.get("js_errors"):
        lines.append(f"{len(context['js_errors'])} JavaScript error(s): "
                     f"{context['js_errors'][0][:100]}")
    return "\n".join(lines)
