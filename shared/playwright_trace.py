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

from shared.frameworks import get_active_plugin

# Actions that say nothing about locators; noise in a timeline.
_UNINTERESTING = {"BrowserContext.newPage", "Frame.content", "BrowserContext.close",
                  "Browser.close", "Page.close", "Tracing.start", "Tracing.stop"}


def read_actions(trace_path: Path) -> List[Dict]:
    """Return the ordered actions in a trace, each with its selector and error.

    Returns [] for anything unreadable — a missing or malformed artefact must
    never break a fix run.
    """
    return get_active_plugin().telemetry.read_actions(trace_path)


def failing_action(actions: List[Dict]) -> Optional[Dict]:
    """The action whose locator broke."""
    return get_active_plugin().telemetry.failing_action(actions)


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
