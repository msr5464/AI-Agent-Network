"""Read the network log out of a Playwright trace zip.

A trace carries two logs. `trace.trace` holds the action timeline and is already
parsed by `shared/playwright_trace.py`; `trace.network` holds every HTTP request
the page made, in HAR-shaped records, and until now was never opened at all.

That file is the generic evidence channel for a whole family of failures that
reach the fixer disguised as a missing element: the host was unreachable, the
document came back 500, an API call was rejected 401, a redirect landed somewhere
other than the page the test expected, or a request was simply still in flight
when the wait gave up. None of those are fixable by editing a selector, and none
of them are distinguishable from a stale locator by looking at the error text.

Deliberately conservative: anything unreadable yields empty findings, never a
guess. A trace that cannot be parsed must not manufacture a diagnosis.
"""

import json
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

# A request Playwright could not complete is recorded with status 0 or -1.
_FAILED_STATUSES = (0, -1)

# Requests slower than this are worth mentioning when a wait timed out.
_SLOW_MS = 3000


def read_entries(trace_path) -> List[Dict]:
    """Every network record in the trace, flattened. [] for anything unreadable."""
    if not trace_path:
        return []
    path = Path(trace_path)
    if not path.exists():
        return []
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith("trace.network")]
            if not names:
                return []
            raw = archive.read(names[0]).decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, OSError, KeyError):
        return []

    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        snapshot = event.get("snapshot") if isinstance(event, dict) else None
        if not isinstance(snapshot, dict):
            continue
        request = snapshot.get("request") or {}
        response = snapshot.get("response") or {}
        entries.append({
            "url": request.get("url", ""),
            "method": request.get("method", ""),
            "status": response.get("status"),
            "status_text": response.get("statusText", ""),
            "time_ms": snapshot.get("time"),
            "started": snapshot.get("startedDateTime", ""),
        })
    return entries


def _registrable(host: str) -> str:
    """An approximation of the registrable domain: the last two labels.

    Good enough to tell `api.github.com` (the application) from
    `events.backtrace.io` (somebody's telemetry), which is the only distinction
    made with it. It is wrong for multi-label suffixes like `co.uk`, and that
    only ever merges two hosts that were already going to be compared as one.
    """
    labels = (host or "").lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else (host or "").lower()


def _is_first_party(url: str, page_url: str) -> bool:
    """Whether a request belongs to the application under test.

    Pages are full of third-party analytics, error collectors and ad beacons that
    fail, 401 and time out as a matter of routine. Letting those decide a verdict
    means a healthy application gets diagnosed as broken — and a genuine locator
    fix gets blocked by somebody else's telemetry.
    """
    if not page_url:
        return True  # nothing to compare against; do not silently discard evidence
    return _registrable(urlsplit(url).netloc) == _registrable(urlsplit(page_url).netloc)


def _is_document(entry: Dict, page_url: str) -> bool:
    """Whether this record is the navigation that produced the failing page."""
    if not page_url or not entry.get("url"):
        return False
    if entry["url"] == page_url:
        return True
    # A trailing slash and a query string are not a different document.
    left, right = urlsplit(entry["url"]), urlsplit(page_url)
    return (left.netloc == right.netloc
            and left.path.rstrip("/") == right.path.rstrip("/"))


def summarize(trace_path, page_url: str = "") -> Dict:
    """What the network did around the failure, in the terms a diagnosis needs.

    `available` is the field that matters most: False means this channel has
    nothing to say, which must never be read as "the network was fine".
    """
    result: Dict = {
        "available": False, "total": 0,
        "document_status": None, "document_url": "",
        "failed": [], "server_errors": [], "client_errors": [],
        "auth_rejections": [], "redirects": [], "slow": [],
        "first_party_failed": [], "first_party_server_errors": [],
        "first_party_auth_rejections": [],
    }
    entries = read_entries(trace_path)
    if not entries:
        return result

    result["available"] = True
    result["total"] = len(entries)

    for entry in entries:
        status = entry.get("status")
        brief = {"url": entry["url"][:200], "method": entry.get("method", ""),
                 "status": status, "time_ms": entry.get("time_ms"),
                 "first_party": _is_first_party(entry["url"], page_url)}

        if status is None or status in _FAILED_STATUSES:
            result["failed"].append(brief)
        elif 500 <= status < 600:
            result["server_errors"].append(brief)
        elif status in (401, 403):
            result["auth_rejections"].append(brief)
        elif 400 <= status < 500:
            result["client_errors"].append(brief)
        elif 300 <= status < 400:
            result["redirects"].append(brief)

        time_ms = entry.get("time_ms")
        if isinstance(time_ms, (int, float)) and time_ms >= _SLOW_MS:
            result["slow"].append(brief)

        if result["document_status"] is None and _is_document(entry, page_url):
            result["document_status"] = status
            result["document_url"] = entry["url"][:200]

    # First-party first, so the entries a rule reads and a human sees are the ones
    # that belong to the application rather than to its analytics vendors.
    for key in ("failed", "server_errors", "client_errors",
                "auth_rejections", "redirects", "slow"):
        result[key].sort(key=lambda item: not item.get("first_party"))
        result[key] = result[key][:10]
    result["first_party_failed"] = [i for i in result["failed"] if i.get("first_party")]
    result["first_party_server_errors"] = [i for i in result["server_errors"]
                                           if i.get("first_party")]
    result["first_party_auth_rejections"] = [i for i in result["auth_rejections"]
                                             if i.get("first_party")]
    return result


def describe(summary: Dict, max_lines: int = 8) -> str:
    """One short human/prompt-readable block. Empty when there is nothing to say."""
    if not summary.get("available"):
        return ""
    lines: List[str] = []
    if summary.get("document_status") is not None:
        lines.append(f"document request -> HTTP {summary['document_status']}")
    # First party first, and third party last. An ad or analytics beacon fails on
    # most pages and says nothing about the test; listing those in arrival order
    # pushed the requests the application actually made past `max_lines` and out
    # of the report entirely.
    for label, key in (("failed request", "failed"),
                       ("server error", "server_errors"),
                       ("auth rejected", "auth_rejections"),
                       ("client error", "client_errors")):
        entries = list(summary.get(key) or [])
        for item in sorted(entries, key=lambda i: not i.get("first_party")):
            where = "" if item.get("first_party") else " [third-party]"
            lines.append(f"{label}: {item['method']} {item['url'][:90]} "
                         f"({item['status']}){where}")
    for item in summary.get("slow") or []:
        lines.append(f"slow: {round(item['time_ms'])}ms {item['url'][:90]}")
    if not lines:
        lines.append(f"{summary['total']} requests, none failed or errored")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… {len(lines) - max_lines} more"]
    return "\n".join(lines)
