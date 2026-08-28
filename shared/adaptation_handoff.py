"""When healing meets a rebuilt page, hand it to adaptation instead of guessing.

This closes the loop between the reactive agent and the proactive one, and it
fixes a misdiagnosis that ships today.

A page rebuilt in place — same route, same purpose, entirely new markup — makes
every one of its page object's locators stop matching. `_rule_wrong_page` sees
`0 of N matched`, `baseline.is_different_page` returns True on
"everything vanished, nothing survived", and the verdict is `WRONG_PAGE` at HIGH
confidence with the remediation *"the test never reached this page — fix what
happens before it"*.

The test reached exactly the page it meant to. A human is sent to investigate
navigation that is not broken.

The discriminator was already in the evidence and never consulted: if the URL
shape still matches the last good run, this is the right page rebuilt, not the
wrong page reached. That is not something healing may fix — regenerating a page
object is well outside a locator edit — but it is exactly what the adaptation
agent exists for. So healing writes a *draft* change note into that agent's queue
and says so.

Draft, deliberately. Nobody runs it automatically. A human reads what the machine
observed, confirms the product really did change rather than break, and edits the
note before running it — which is the same "is this a change or a bug?" judgement
the whole design refuses to make on its own.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from shared import baseline

# Written into the note so a reader knows it was machine-drafted.
_BANNER = "# DRAFT — written by test-healing-agent, not yet reviewed."


def looks_restructured(evidence: Dict, diagnosis: Dict) -> bool:
    """Whether a WRONG_PAGE verdict is really a rebuilt page.

    Requires the URL shape to have matched the last good run: without a baseline
    there is nothing to compare and the honest answer is "cannot tell", which
    leaves healing's existing behaviour alone.
    """
    if (diagnosis or {}).get("verdict") != "WRONG_PAGE":
        return False
    comparison = evidence.get("baseline_diff") or {}
    if not comparison.get("available"):
        return False
    # Every locator gone and none surviving is the shape of a rebuild...
    if comparison.get("still_present"):
        return False
    if not comparison.get("vanished"):
        return False
    # ...but only if we are still on the same route. A different route with
    # everything vanished is a genuine WRONG_PAGE and must stay one.
    return not any(m.startswith("url shape") for m in comparison.get("mismatches", []))


def _module_for(test_name: str) -> str:
    parts = [p for p in (test_name or "").split(".") if p]
    if len(parts) >= 3 and parts[0] == "automation":
        return parts[1].lower()
    return re.sub(r"[^a-z0-9]+", "-", (parts[0] if parts else "unknown").lower())


def draft_note(issue: Dict, evidence: Dict, diagnosis: Dict) -> str:
    """The change note a human would have written, as far as we can tell."""
    page = evidence.get("expected_page_object") or "the page"
    facts = evidence.get("facts") or {}
    comparison = evidence.get("baseline_diff") or {}
    vanished = comparison.get("vanished") or []
    url = issue.get("failure_url") or facts.get("url", "")

    return f"""{_BANNER}
# {page} appears to have been rebuilt: its route is unchanged but none of its
# locators survive. Confirm this was an intended redesign and not an outage,
# then fill in what actually changed and run:
#     make run AGENT=test-adaptation-agent MODULE={_module_for(issue.get('test_name',''))}

Module: {_module_for(issue.get('test_name', ''))}
Type: web
URL: {url}
Affects: {issue.get('test_name', '')}

What changed:
1. The {page} screen was rebuilt. Its route ({baseline.url_shape(url) or 'unchanged'})
   and title ({facts.get('title', 'unknown')}) are the same, but {len(vanished)}
   element(s) the tests rely on are gone: {', '.join(vanished[:6]) or 'unknown'}.
   <describe what the page looks like now>

Expected outcome unchanged: <what this flow is still supposed to achieve>

# Observed by {issue.get('test_name', '')} on
# {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
"""


def write_draft(queue_dir, issue: Dict, evidence: Dict, diagnosis: Dict) -> Optional[Path]:
    """Write the draft unless one is already waiting. Returns the path, or None.

    Never overwrites: a human may already be editing it, and a nightly run that
    clobbered yesterday's edits every night would be worse than not writing one.
    """
    folder = Path(queue_dir)
    module = _module_for(issue.get("test_name", ""))
    path = folder / f"{module}-rebuilt.txt"
    if path.exists():
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(draft_note(issue, evidence, diagnosis), encoding="utf-8")
    except OSError:
        return None
    return path
