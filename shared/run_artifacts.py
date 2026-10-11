"""Point a failure record at the artifacts one test run just produced.

Written for the reproduce step and now shared, because the fix step needs the
same thing for a different reason: after a verification run, the failure on the
screen may be a *different* element from the one that was just repaired, and
deciding that requires this run's DOM snapshot, failure context and trace — not
the ones collected before the edit.

Every lookup takes the newest match for the test method, and every previous run
left files under that same name, so `not_before` (when the run started) is what
keeps a run that died before writing a snapshot from silently attaching evidence
from days ago.
"""

from pathlib import Path
from typing import Callable, Optional

from shared import failure_context as _failure_context
from shared import failure_identity
from shared.dom_snapshot import find_snapshot, parse_header
from shared.telemetry import failing_action, read_actions


def from_this_run(paths: list, not_before: Optional[float]) -> list:
    """Drop artifacts written before the run started."""
    if not_before is None:
        return paths
    return [p for p in paths if p.stat().st_mtime >= not_before]


def attach(issue: dict, results_dir: Path, method_name: str,
           not_before: Optional[float] = None,
           log: Optional[Callable[[str], None]] = None) -> str:
    """Fill in dom_snapshot / screenshot / failure_context / trace_path.

    Returns the failing selector read from the trace, or "" when the trace does
    not name one — `failed_selector` is then filled from the failure's own
    account, which is evidence of a different kind and is not returned as this. `log` is optional: the fix step calls this to refresh an issue
    quietly, where the reproduce step narrates it.
    """
    say = log or (lambda _msg: None)
    trace_selector = ""

    snapshot = find_snapshot(results_dir, method_name, not_before)
    if snapshot:
        try:
            text = snapshot.read_text(encoding="utf-8", errors="ignore")
            issue["dom_snapshot"] = str(snapshot)
            issue["failure_url"] = parse_header(text).get("url", "")
            say(f"  DOM snapshot: {snapshot.name} ({len(text) // 1024}KB)")
        except OSError as e:
            say(f"  Could not read DOM snapshot: {e}")

    # Located by convention: JsonTestReporter leaves screenshotPath empty by design.
    shots = [p for p in results_dir.rglob(f"screenshots/{method_name}_*.png") if p.is_file()]
    shots = from_this_run(shots, not_before)
    if shots:
        issue["screenshot"] = str(max(shots, key=lambda p: p.stat().st_mtime))
        say(f"  Screenshot: {Path(issue['screenshot']).name}")

    # Located against the run rather than picked independently: a context is
    # written the moment an element fails, the snapshot at the end of execution,
    # so on a cascading failure the two are minutes apart and only the run
    # boundary can say they belong together.
    context = _failure_context.for_failure(
        issue.get("dom_snapshot", ""), test_name=method_name,
        not_before=not_before)
    if context.get("path"):
        issue["failure_context"] = context["path"]
        say(f"  Failure context: {Path(context['path']).name} "
            f"({context.get('kind') or 'unknown'} in {context.get('page_object') or '?'})")
    elif context.get("rejected"):
        say(f"  Ignoring a failure context from another run — {context['rejected']}")

    # Discovery is the framework's own business: Playwright writes
    # traces/<method>_*.zip, another framework writes a JSONL action log
    # somewhere else. Globbing for *.zip here meant a non-Playwright parser
    # could never be handed a path it accepted.
    from shared.frameworks import get_active_plugin
    traces = from_this_run(
        get_active_plugin().telemetry.discover(results_dir, method_name), not_before)
    if traces:
        trace = max(traces, key=lambda p: p.stat().st_mtime)
        issue["trace_path"] = str(trace)
        failed = failing_action(read_actions(trace))
        if failed and failed.get("selector"):
            trace_selector = failed["selector"]
            issue["failed_selector"] = trace_selector
            issue["failed_selector_inferred"] = bool(failed.get("inferred"))
            how = " (inferred from repeated polling)" if failed.get("inferred") else ""
            say(f"  Trace: {trace.name} — failing selector {trace_selector}{how}")
        else:
            say(f"  Trace: {trace.name}")

    name_failing_selector(issue, context, say)
    return trace_selector


def name_failing_selector(issue: dict, context: Optional[dict] = None,
                          log: Optional[Callable[[str], None]] = None) -> str:
    """Fill `failed_selector` from the failure itself when the trace named none.

    A trace names the selector only when an action on it threw. A wait that polls
    until its budget runs out throws nothing, so for that whole class of failure
    the field stayed empty — and every step that looks a failure up by its
    selector (the offline locate, the clustering, the edit guards) had nothing to
    look up. The framework says which locator it was waiting for twice over: in
    the message it fails with, and in the context it writes beside the snapshot.
    """
    if issue.get("failed_selector"):
        return issue["failed_selector"]
    if context is None:
        path = issue.get("failure_context") or ""
        context = _failure_context.load(path) if isinstance(path, str) else path
    text = issue.get("error_message") or issue.get("root_cause") or ""
    selector = failure_identity.failing_selector(text, context)
    if selector:
        issue["failed_selector"] = selector
        source = ("the failure message" if failure_identity.identify(text).get("selector")
                  else "the failure context")
        (log or (lambda _msg: None))(f"  Failing selector: {selector} (from {source})")
    return selector
