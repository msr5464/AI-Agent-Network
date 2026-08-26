"""Read the structured failure context the framework writes beside the DOM snapshot.

`automation.core.FailureContext` records, at the moment a page-load assertion gives
up, the things that cannot be reconstructed afterwards: how many elements each
anchor actually matched, whether the page was still loading, whether the DOM was
still changing, how much of the page object's own locator set was present, and what
the network and JavaScript did on the way there.

Older runs, other frameworks and CI layouts that lost the file all produce nothing
here, and every consumer has to keep working without it — the diagnosis is weaker
without these channels, never blocked on them. `available` is the field that carries
that distinction.
"""

import json
from pathlib import Path
from typing import Dict, Optional

# Written next to the snapshot as <method>_<HHmmss>.context.json.
_SUFFIX = ".context.json"


def find(report_dir, method_name: str) -> Optional[Path]:
    """The newest failure context for this test method, if one was written."""
    if not method_name or not report_dir or not Path(report_dir).exists():
        return None
    matches = [p for p in Path(report_dir).rglob(f"{method_name}_*{_SUFFIX}") if p.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def beside_snapshot(dom_snapshot: str) -> Optional[Path]:
    """The context written for the same failure as this DOM snapshot.

    The two are named from the same test and timestamp, so the snapshot's own path
    locates it without a directory scan. Falls back to the newest context for the
    same method when the timestamps differ by a second.
    """
    if not dom_snapshot:
        return None
    snapshot = Path(dom_snapshot)
    candidate = snapshot.parent / (snapshot.stem + _SUFFIX)
    if candidate.exists():
        return candidate
    # The two files are stamped a second apart when a capture straddles the clock.
    method = snapshot.stem.rsplit("_", 1)[0]
    return find(snapshot.parent, method)


def load(path) -> Dict:
    """Parse one context file into the shape the diagnosis engine consumes."""
    result: Dict = {
        "available": False, "page_object": "", "anchors": [],
        "elapsed_ms": None, "budget_ms": None,
        "url": "", "title": "", "body_class": "", "h1": "",
        "ready_state": "", "aria_busy": "",
        "coverage": {}, "dom_changed_during_wait": None,
        "navigation": [], "http_errors": [], "js_errors": [],
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
    failure = data.get("failure") or {}
    page = data.get("page") or {}
    result["page_object"] = failure.get("pageObject", "")
    result["anchors"] = failure.get("anchors") or []
    result["elapsed_ms"] = failure.get("elapsedMs")
    result["budget_ms"] = failure.get("budgetMs")
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
