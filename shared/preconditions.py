"""Check the artifacts a test depends on before blaming its locators.

A stored browser session, a fixture file, a properties file — these go stale
silently. The test still runs, the browser still opens, the page still loads;
it is simply the wrong page, and the first locator to be looked for on it becomes
the accused. This module answers "was the input to this test still valid?", which
is cheap, deterministic, and needs no model.

Only artifacts the run actually referenced are examined. The path is taken from
the execution log the framework already prints, never from a scan of whatever
happens to be lying in the repo — a stale file some other test owns is not
evidence about this one, and treating it as such would preempt genuine locator
failures.

The trap this is written around: an expired session usually still contains
plenty of *valid* cookies. In the case that prompted it, `logged_in=yes` and
`dotcom_user=<name>` were good for another year while the two cookies that
actually authenticate had died four months earlier. So a count of valid cookies
proves nothing, and every finding here names the specific cookie and its date.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

# What the framework prints when it loads or fails to find a stored session.
_SESSION_LOADED = re.compile(r"Loaded stored session:\s*(?P<path>\S+)")
_SESSION_MISSING = re.compile(r"Session file not found[^:]*:\s*(?P<path>\S+)")

# The framework now refuses to start a run on a dead session, and says so. Its own
# statement is stronger evidence than anything inferred afterwards: it checked the
# file it was about to use, at the moment it was about to use it. Reading it here
# means the diagnosis keeps working once the framework stops the run so early that
# no page object is ever reached to be named.
_FRAMEWORK_VERDICTS = (
    (re.compile(r"StoredSessionExpired:\s*(?P<artifact>\S+)\s*[—-]\s*(?P<detail>[^\n]+)"),
     "session_expired"),
    (re.compile(r"StoredSessionMissing:\s*(?P<detail>[^\n]+)"), "session_missing"),
)

# Cookies whose absence or expiry breaks authentication tend to be named like
# this. Used only to rank findings, never to decide one — any expired persistent
# cookie is reported regardless.
_AUTH_HINTS = ("session", "auth", "token", "sso", "jwt", "sid", "csrf")


def session_files_from_log(execution_log: str, workspace) -> List[Dict]:
    """Storage-state files this run actually referenced, resolved to real paths."""
    found: List[Dict] = []
    seen = set()
    workspace = Path(workspace) if workspace else None

    for pattern, status in ((_SESSION_LOADED, "loaded"),
                            (_SESSION_MISSING, "missing")):
        for match in pattern.finditer(execution_log or ""):
            raw = match.group("path").rstrip(".,;")
            if raw in seen:
                continue
            seen.add(raw)
            path = Path(raw)
            if not path.is_absolute() and workspace:
                path = workspace / raw
            found.append({"declared": raw, "path": path, "status": status})
    return found


def audit_storage_state(path) -> Dict:
    """Expiry report for one Playwright storage-state file.

    `valid` is None when the file could not be read at all — unknown is not the
    same as fine, and the caller has to be able to tell them apart.
    """
    report: Dict = {
        "path": str(path), "exists": False, "valid": None, "error": "",
        "cookies": 0, "session_cookies": 0, "expired": [], "origins": 0,
    }
    path = Path(path)
    if not path.exists():
        report["error"] = "file does not exist"
        return report
    report["exists"] = True

    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError) as exc:
        report["error"] = f"could not read storage state: {exc}"
        return report
    if not isinstance(data, dict):
        report["error"] = "storage state is not a JSON object"
        return report

    now = time.time()
    cookies = data.get("cookies") or []
    report["cookies"] = len(cookies)
    report["origins"] = len(data.get("origins") or [])

    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        expires = cookie.get("expires")
        # -1 (and absent) mean a session cookie: it dies with the browser and
        # carries no expiry to check.
        if expires is None or expires < 0:
            report["session_cookies"] += 1
            continue
        if expires < now:
            report["expired"].append({
                "name": cookie.get("name", "?"),
                "domain": cookie.get("domain", ""),
                "expired_at": _iso(expires),
                "auth_like": _looks_like_auth(cookie.get("name", "")),
            })

    report["expired"].sort(key=lambda c: (not c["auth_like"], c["expired_at"]))
    report["valid"] = not report["expired"]
    return report


def _iso(epoch_seconds: float) -> str:
    try:
        return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "?"


def _looks_like_auth(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


def framework_findings(text: str) -> List[Dict]:
    """Precondition problems the framework itself reported."""
    findings: List[Dict] = []
    for pattern, kind in _FRAMEWORK_VERDICTS:
        match = pattern.search(text or "")
        if not match:
            continue
        groups = match.groupdict()
        detail = (groups.get("detail") or "").strip()
        # The framework appends the remediation to its own message; keep them
        # apart so the UI can lead with the action rather than the diagnosis.
        remediation = ""
        for marker in ("Regenerate it by", "Generate it by"):
            if marker in detail:
                detail, _, remediation = detail.partition(marker)
                remediation = (marker + remediation).strip()
                break
        findings.append({
            "kind": kind,
            "artifact": (groups.get("artifact") or "the stored session").rstrip(":,"),
            "detail": detail.strip().rstrip(".") or "the stored session is not usable",
            "remediation": remediation or "regenerate the stored session by "
                                          "re-running the login test that produces it",
        })
    return findings


def check(execution_log: str, workspace) -> Dict:
    """Every precondition artifact this run touched, and whether it still holds.

    Returns `problems` only for artifacts the run genuinely referenced, so an
    empty list means "nothing to report", not "nothing was checked" — that
    distinction is carried by `checked`.
    """
    result: Dict = {"checked": 0, "problems": [], "reports": []}

    for finding in framework_findings(execution_log):
        result["checked"] += 1
        result["problems"].append(finding)
    if result["problems"]:
        return result

    for entry in session_files_from_log(execution_log, workspace):
        if entry["status"] == "missing":
            result["checked"] += 1
            result["problems"].append({
                "kind": "session_missing",
                "artifact": entry["declared"],
                "detail": "the test asked for a stored session that does not exist, "
                          "so it ran unauthenticated",
                "remediation": "regenerate the stored session",
            })
            continue

        report = audit_storage_state(entry["path"])
        result["checked"] += 1
        result["reports"].append(report)
        if report["valid"] is False:
            named = ", ".join(f"{c['name']} expired {c['expired_at']}"
                              for c in report["expired"][:4])
            result["problems"].append({
                "kind": "session_expired",
                "artifact": entry["declared"],
                "detail": f"{len(report['expired'])} of {report['cookies']} cookies "
                          f"have expired ({named})",
                "remediation": "regenerate the stored session by re-running the "
                               "login test that produces it",
            })
        elif report["valid"] is None and report["error"]:
            result["problems"].append({
                "kind": "session_unreadable",
                "artifact": entry["declared"],
                "detail": report["error"],
                "remediation": "regenerate the stored session",
            })
    return result
