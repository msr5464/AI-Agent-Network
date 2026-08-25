"""Read the action timeline out of a Playwright trace zip.

The automation framework records a trace per failed test
(`BrowserHelper.startTracing` / `stopTracing`). The trace holds the whole flow —
every action, the selector it used, and which one failed — which is what turns
"a locator broke somewhere" into "THIS selector stopped matching, after these
ones worked".

Only the action timeline is parsed here. A trace also contains per-step DOM
snapshots, but those use an internal incremental format with back-references
between snapshots that changes between Playwright releases; depending on it
would make this fragile for little gain, since the framework already writes the
failure-time DOM as plain HTML alongside the trace. Humans get the full picture
by opening the zip in Playwright Trace Viewer.
"""

import json
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

# Actions that say nothing about locators; noise in a timeline.
_UNINTERESTING = {"BrowserContext.newPage", "Frame.content", "BrowserContext.close",
                  "Browser.close", "Page.close", "Tracing.start", "Tracing.stop"}


def read_actions(trace_path: Path) -> List[Dict]:
    """Return the ordered actions in a trace, each with its selector and error.

    Returns [] for anything unreadable — a missing or malformed artefact must
    never break a fix run.
    """
    trace_path = Path(trace_path)
    if not trace_path.exists():
        return []

    try:
        with zipfile.ZipFile(trace_path) as archive:
            names = [n for n in archive.namelist() if n.endswith("trace.trace")]
            if not names:
                return []
            raw = archive.read(names[0]).decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, OSError, KeyError):
        return []

    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue

    # "after" events carry the outcome and are matched to "before" by callId.
    outcomes: Dict[str, Dict] = {}
    for event in events:
        if event.get("type") == "after" and event.get("callId"):
            outcomes[event["callId"]] = event

    actions: List[Dict] = []
    for event in events:
        if event.get("type") != "before" or not event.get("class"):
            continue
        name = f"{event['class']}.{event['method']}"
        params = event.get("params") or {}
        after = outcomes.get(event.get("callId"), {})
        error = (after.get("error") or {}).get("message", "")
        duration = ""
        if after.get("endTime") and event.get("startTime"):
            duration = f"{after['endTime'] - event['startTime']:.0f}ms"
        actions.append({
            "action": name,
            "selector": params.get("selector", ""),
            "url": params.get("url", ""),
            "value": str(params.get("value", ""))[:60],
            "error": error.splitlines()[0] if error else "",
            "duration": duration,
        })
    return actions


def failing_action(actions: List[Dict]) -> Optional[Dict]:
    """The action whose locator broke.

    Usually the first one that errored. But a framework that polls — the
    Playwright framework's waitForAnyElementToBeDisplayed calls isVisible() in a
    retry loop and gives up by returning false — never produces an errored
    action at all, even though the trace plainly shows one selector being
    checked over and over right up to the end. That repetition is the signal, so
    fall back to it rather than reporting no failing action for what is
    obviously a locator problem.
    """
    errored = next((a for a in actions if a.get("error")), None)
    if errored:
        return errored
    return _polled_to_death(actions)


def _polled_to_death(actions: List[Dict], min_repeats: int = 3) -> Optional[Dict]:
    """The selector the run kept re-checking as it ran out of patience."""
    tail = [a for a in actions if a.get("selector")]
    if not tail:
        return None
    last_selector = tail[-1]["selector"]
    repeats = 0
    for action in reversed(tail):
        if action["selector"] != last_selector:
            break
        repeats += 1
    if repeats < min_repeats:
        return None
    return {**tail[-1],
            "error": f"polled {repeats}x without ever becoming visible",
            "inferred": True}


def format_for_prompt(actions: List[Dict], max_actions: int = 40) -> str:
    """Render the timeline as the prompt section a fixer reads."""
    interesting = [a for a in actions if a["action"] not in _UNINTERESTING]
    if not interesting:
        return ""

    failed = failing_action(interesting)
    lines: List[str] = []
    if failed and failed.get("selector"):
        how = ("inferred from the trace — it was re-checked until the test gave up"
               if failed.get("inferred") else "recorded as the failing call")
        lines.append(f"The selector that failed at runtime: {failed['selector']}")
        lines.append(f"  ({failed['action']} — {failed['error']}; {how})")
        lines.append("")

    lines.append("Full action timeline (selectors that worked, then the one that did not):")
    shown = interesting[-max_actions:]
    if len(interesting) > max_actions:
        lines.append(f"  … {len(interesting) - max_actions} earlier action(s) omitted")
    for action in shown:
        target = action["selector"] or action["url"] or action["value"]
        marker = f"   <-- FAILED: {action['error']}" if action["error"] else ""
        lines.append(f"  {action['action']:<24} {target}{marker}")
    return "\n".join(lines)
