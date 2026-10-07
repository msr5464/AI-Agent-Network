"""Read the action timeline a test run left behind, whatever framework wrote it.

The timeline is what turns "a locator broke somewhere" into "THIS selector
stopped matching, after these ones worked" — the single most useful piece of
evidence a fix step gets.

Where it comes from is the framework's business, and this module knows none of
it. Playwright records a trace zip per failed test (`BrowserHelper.startTracing`
/ `stopTracing`) and the parser reads its event stream; a Selenium repo has no
native trace format, so its listener writes a JSONL action log instead (see
docs/FRAMEWORK_INTEGRATION.md). Both arrive here as the same normalised action
dicts — see TelemetryParser.ACTION_KEYS for the schema.

This was `shared/playwright_trace.py`, and the name was the tell: it delegated
parsing to the active plugin while still assuming Playwright's action shape and
its zip layout, so a non-Playwright parser could neither be found nor formatted.

Only the action timeline is parsed. A Playwright trace also contains per-step DOM
snapshots, but those use an internal incremental format with back-references that
changes between releases; depending on it would be fragile for little gain, since
the framework already writes the failure-time DOM as plain HTML alongside. Humans
get the full picture by opening the zip in Playwright Trace Viewer.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from shared.frameworks import get_active_plugin

def discover(results_dir: Path, method_name: str) -> List[Path]:
    """Every telemetry artifact the active framework wrote for one test method."""
    return get_active_plugin().telemetry.discover(results_dir, method_name)


def read_actions(trace_path: Path) -> List[Dict]:
    """The ordered actions in a trace, each with its selector and error.

    Returns [] for anything unreadable — a missing or malformed artefact must
    never break a fix run.
    """
    return get_active_plugin().telemetry.read_actions(trace_path)


def failing_action(actions: List[Dict]) -> Optional[Dict]:
    """The action whose locator broke."""
    return get_active_plugin().telemetry.failing_action(actions)


_SECRET_FIELD = re.compile(r"pass(word|wd)?\b|pwd|otp|token|secret", re.IGNORECASE)


def _typed_values(actions: List[Dict], limit: int = 25) -> List[tuple]:
    """[(selector, value)] for each non-empty value typed into a field, last value
    per field. A field that looks like a secret shows its length, not its value."""
    last: Dict[str, str] = {}
    for action in actions:
        selector, value = action.get("selector") or "", action.get("value") or ""
        if selector and value:
            last.pop(selector, None)
            last[selector] = (f"({len(value)} characters, not shown)"
                              if _SECRET_FIELD.search(selector) else repr(value))
    return list(last.items())[-limit:]


def format_for_prompt(actions: List[Dict], max_actions: int = 40) -> str:
    """Render the timeline as the prompt section a fixer reads.

    Every field is read with .get(). These used to be direct subscripts, which
    assumed Playwright's exact dict shape — so any other parser's records raised
    KeyError here, inside prompt construction, at the point where the evidence
    was about to be used.
    """
    noise = get_active_plugin().telemetry.NOISE_ACTIONS
    interesting = [a for a in actions if a.get("action", "") not in noise]
    if not interesting:
        return ""

    failed = failing_action(interesting)
    lines: List[str] = []
    if failed and failed.get("selector"):
        how = ("inferred from the trace — it was re-checked until the test gave up"
               if failed.get("inferred") else "recorded as the failing call")
        lines.append(f"The selector that failed at runtime: {failed['selector']}")
        lines.append(f"  ({failed.get('action', '?')} — {failed.get('error', '')}; {how})")
        lines.append("")

    # Every value the test typed, however far back. The timeline below keeps only the
    # last actions and shows a selector, never what went into it, so a test whose
    # data had shifted one column (an address split at its comma, "India" typed as
    # the card number) failed three fix attempts later on a promo that only renders
    # for a valid card, and no fixer saw why.
    typed = _typed_values(interesting)
    if typed:
        lines.append("Values the test typed, in order:")
        lines += [f"  {selector}  <- {value}" for selector, value in typed]
        lines.append("")

    lines.append("Full action timeline (selectors that worked, then the one that did not):")
    shown = interesting[-max_actions:]
    if len(interesting) > max_actions:
        lines.append(f"  … {len(interesting) - max_actions} earlier action(s) omitted")
    for action in shown:
        target = action.get("selector") or action.get("url") or action.get("value") or ""
        error = action.get("error") or ""
        marker = f"   <-- FAILED: {error}" if error else ""
        lines.append(f"  {action.get('action', ''):<24} {target}{marker}")
    return "\n".join(lines)
