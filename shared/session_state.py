"""Reuse a login session the framework already saved, instead of a password.

`BrowserHelper.storeSession()` writes a Playwright storage-state file per module.
Handing that to the browser means it starts already logged in, and no credential
ever enters a prompt or a log — which matters because `shared/claude.py` writes
the full command line, `-p <prompt>` included, into its debug log.

The expiry check is not politeness. An expired session still *looks* valid: the
file is there, the browser accepts it, and the flow simply lands on a login page.
An explorer in that state does not fail — it succeeds at describing the login
page, and reports that the entire flow changed. That is the highest-probability
silent-wrong-answer in the adaptation design, so an expired session is a hard stop
rather than a warning or a fallback to signing in.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

from shared.preconditions import audit_storage_state

# How much life a session must have LEFT to be worth starting with. Checking
# only "has it expired" was not enough: SauceDemo issues a ten-minute cookie, a
# run began twenty-three seconds before it lapsed, and exploration crossed the
# expiry mid-flow. The browser was then bounced to a login gate and the agent
# reported that it could not reach the page it was asked about — a correct
# refusal produced by a stale premise, which is the most expensive kind.
DEFAULT_MIN_REMAINING_S = int(os.environ.get("ADAPT_SESSION_MIN_TTL_S", "300"))


def find(workspace, module: str) -> Optional[Path]:
    """The newest saved session for a module, if one exists."""
    if not module:
        return None
    folder = (Path(workspace) / "src" / "test" / "resources"
              / module.lower() / "loginStorage")
    if not folder.exists():
        return None
    sessions = sorted(folder.glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


def usable(workspace, module: str,
           min_remaining_s: int = DEFAULT_MIN_REMAINING_S) -> Dict:
    """Whether exploration may proceed, and why not when it may not.

    Returns {ok, path, reason, report}. `ok` is False for both "no session" and
    "expired session" — deliberately the same answer, because signing in from
    scratch is exactly the fallback that produces a confident wrong result.
    """
    path = find(workspace, module)
    if path is None:
        # No remediation advice here on purpose. This used to say "run a passing
        # test for this module first so the framework writes one", which is true
        # for exactly one module: storeSession() is keyed on the ProjectName enum
        # — GitHub, SauceDemo, FullSuite — and has a single caller. Every other
        # module could run green forever and never produce this file. What to do
        # about it depends on the entry path the test declares, so the caller
        # that read that says it.
        return {"ok": False, "path": None, "report": {},
                "reason": (f"no saved login session for module {module!r} "
                           f"(src/test/resources/{module.lower()}/loginStorage/)")}
    report = audit_storage_state(path)
    soon = _expiring_within(path, min_remaining_s)
    if report.get("valid", False) and soon:
        return {"ok": False, "path": path, "report": report, "expiring": True,
                "reason": (f"the saved session at {path.name} expires in {soon}s, "
                           f"less than the {min_remaining_s}s a run needs. Starting "
                           f"with it would drop the browser onto a login page "
                           f"partway through.")}
    if not report.get("valid", False):
        expired = report.get("expired") or []
        named = ", ".join(f"{c['name']} (expired {c['expired_at']})"
                          for c in expired[:3])
        return {"ok": False, "path": path, "report": report,
                "reason": (f"the saved session at {path.name} has expired: {named}. "
                           f"Exploring with it would land on a login page and "
                           f"report that the whole flow changed.")}
    return {"ok": True, "path": path, "report": report, "reason": ""}


def _expiring_within(path: Path, seconds: int) -> Optional[int]:
    """Seconds until the soonest cookie expiry, if that is under `seconds`."""
    if seconds <= 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    now = time.time()
    expiries = [c.get("expires") for c in (data.get("cookies") or [])
                if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
    if not expiries:
        return None                      # session cookies carry no expiry to check
    remaining = int(min(expiries) - now)
    return remaining if remaining < seconds else None
