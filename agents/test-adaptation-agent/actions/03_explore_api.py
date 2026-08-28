#!/usr/bin/env python3
"""
Step 03 (API half) — Explore the live API

The web half needs a model driving a browser for thirty minutes. This half needs
neither: an HTTP GET against a real endpoint answers "did the response shape
change?" in seconds and with no ambiguity. It runs first for exactly that reason —
it proves the flow-map schema and the diff end-to-end before anything expensive
starts.

Its conservatism is inherited deliberately from the authoring agent's
`02_validate_api.py`, and it matters more here because this runs against a live
product rather than a scratch environment:

  * **GET only.** POST/PUT/DELETE are never invoked. There is no generically safe
    way to undo one, and "explore" must not mean "mutate".
  * Path parameters are substituted only from literal values already present in
    the automation repo's own test data. A templated path with no known literal is
    reported as unexplored rather than guessed at.

Endpoints come from the repo's own `{Feature}Api` enums — the ones step 02 named
as edit candidates — so what gets probed is what the tests actually call.

Reads:   $AUDIT_DIR/01-parse-change.json, $AUDIT_DIR/02-scope.json
Writes:  $AUDIT_DIR/03-explore-api.json + .md
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.log import log as _log
def log(msg): _log("explore-api", msg)

from shared.baseline import url_shape
from shared.code_analyzer import read_source

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
TIMEOUT_S = int(os.environ.get("ADAPT_API_TIMEOUT_S", "15"))

# Only GET is ever issued. Kept as a constant so the rule is greppable and a
# future change to it is a visible decision rather than an accident.
SAFE_METHODS = ("GET",)

_ENUM_ENTRY = re.compile(
    r"(\w+)\s*\(\s*\"([^\"]+)\"\s*,\s*(?:Method\.)?(GET|POST|PUT|DELETE|PATCH)",
    re.IGNORECASE)
_ALT_ENTRY = re.compile(
    r"(\w+)\s*\(\s*(?:Method\.)?(GET|POST|PUT|DELETE|PATCH)\s*,\s*\"([^\"]+)\"",
    re.IGNORECASE)
_PATH_PARAM = re.compile(r"\{(\w+)\}")


def endpoints_from_repo(workspace: Path, candidates: list) -> list:
    """GET endpoints declared in the repo's own Api enums."""
    found = []
    for candidate in candidates:
        if candidate.get("role") != "api":
            continue
        content = read_source(workspace / candidate["path"])
        if not content:
            continue
        for match in _ENUM_ENTRY.finditer(content):
            found.append({"name": match.group(1), "path": match.group(2),
                          "method": match.group(3).upper(),
                          "source": candidate["path"]})
        for match in _ALT_ENTRY.finditer(content):
            found.append({"name": match.group(1), "path": match.group(3),
                          "method": match.group(2).upper(),
                          "source": candidate["path"]})
    unique, seen = [], set()
    for entry in found:
        key = (entry["method"], entry["path"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def literal_for(param: str, note: str) -> str:
    """A concrete value for a path parameter, taken from the change note only."""
    match = re.search(rf"{re.escape(param)}\s*[=:]\s*([\w.-]+)", note, re.IGNORECASE)
    return match.group(1) if match else ""


def probe(base_url: str, entry: dict, note: str, index: int) -> dict:
    """One GET, recorded as a flow step. Never raises."""
    path = entry["path"]
    missing = []
    for param in _PATH_PARAM.findall(path):
        value = literal_for(param, note)
        if not value:
            missing.append(param)
        else:
            path = path.replace("{" + param + "}", value)

    step = {
        "index": index,
        "page": {"id": f"api:{url_shape(path)}", "url": path,
                 "url_shape": url_shape(path), "title": entry["name"],
                 "headings": [], "identity_digest": f"api:{entry['name']}"},
        "action": {"verb": "request", "target": {"name": entry["name"],
                                                 "selector": path,
                                                 "accessible_name": entry["name"],
                                                 "control_kind": "endpoint"},
                   "value": entry["method"], "value_source": "literal"},
        "selector_check": {"candidate": path, "normalized": path,
                           "match_count": None, "counted_against": "none",
                           "claimed_by_model": None, "unique": None},
        "result": {"outcome": "skipped", "category": "skipped", "navigated": False,
                   "resulting_url": "", "detail": ""},
        "maps_to_test": {"kind": "existing"},
    }

    if entry["method"] not in SAFE_METHODS:
        step["result"].update({
            "outcome": "refused", "category": "destructive_refused",
            "detail": f"{entry['method']} is never invoked during exploration — "
                      f"there is no generically safe way to undo it"})
        return step
    if missing:
        step["result"]["detail"] = (
            f"path parameter(s) {', '.join(missing)} have no literal value in the "
            f"change note — reported unexplored rather than guessed")
        return step
    if not base_url:
        step["result"]["detail"] = "no API URL given in the change note"
        return step

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    request = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
    # Headers are captured on BOTH paths. A 401 carrying a new
    # `WWW-Authenticate`, or a 410 carrying `Sunset`, is exactly the contract
    # change worth knowing about — and that is the path where a response object
    # scoped to the success branch would already be gone.
    raw_headers = {}
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read(200_000)
            status = response.status
            raw_headers = dict(response.headers or {})
    except urllib.error.HTTPError as exc:
        body, status = exc.read(20_000) or b"", exc.code
        raw_headers = dict(getattr(exc, "headers", None) or {})
    except Exception as exc:
        step["result"].update({"outcome": "failed", "category": "network_error",
                               "detail": str(exc)[:200]})
        return step

    # A contract change often shows up in a header rather than the body — a new
    # required auth scheme, a changed content type, a version bump.
    interesting = ("content-type", "www-authenticate", "x-api-version",
                   "deprecation", "sunset", "location")
    headers_of_interest = {k.lower(): v for k, v in raw_headers.items()
                           if k.lower() in interesting}

    keys = []
    try:
        parsed = json.loads(body.decode("utf-8", errors="ignore"))
        if isinstance(parsed, dict):
            keys = sorted(parsed.keys())
        elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            keys = sorted(parsed[0].keys())
    except (ValueError, AttributeError):
        pass

    step["result"].update({
        "outcome": "ok" if status < 400 else "failed",
        "category": "" if status < 400 else "unexpected_content",
        "resulting_url": url, "status": status,
    })
    step["response"] = {"status": status, "keys": keys,
                        "headers_of_interest": headers_of_interest,
                        "content_type": "json" if keys else "other"}
    return step


def main():
    plan = json.loads((AUDIT_DIR / "01-parse-change.json").read_text())
    scope_path = AUDIT_DIR / "02-scope.json"
    scope = json.loads(scope_path.read_text()) if scope_path.exists() else {}

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ran": False, "status": "skipped", "steps": [], "reason": "",
        "unexplained_failures": [],
    }

    if plan.get("type") not in ("api", "both"):
        result["reason"] = f"Type: {plan.get('type')} — no API half to explore"
        log(result["reason"])
    elif scope.get("skipped"):
        result["reason"] = "scope was skipped"
    else:
        workspace = Path(scope.get("workspace", ""))
        endpoints = endpoints_from_repo(workspace, scope.get("edit_candidates") or [])
        log(f"{len(endpoints)} endpoint(s) declared in the repo's Api enums")
        base = plan.get("api_base_url", "")
        note = plan.get("note_masked", "")
        steps = [probe(base, entry, note, i) for i, entry in enumerate(endpoints)]
        result["steps"] = steps
        result["ran"] = True
        failed = [s for s in steps if s["result"]["outcome"] == "failed"]
        result["status"] = "ok" if not failed else "partial"
        # A failure nothing in the change note accounts for is the "change or
        # bug?" gate: a human asserted change A, which says nothing about B.
        described = " ".join(i["text"].lower() for i in plan.get("items", []))
        for step in failed:
            name = step["action"]["target"]["name"].lower()
            if name not in described:
                result["unexplained_failures"].append({
                    "index": step["index"], "endpoint": name,
                    "category": step["result"].get("category", ""),
                    "detail": step["result"].get("detail")
                              or f"HTTP {step['result'].get('status')}"})
        for step in steps:
            log(f"  [{step['result']['outcome']:8}] {step['action']['value']:6} "
                f"{step['action']['target']['selector']}"
                + (f" → {step['result'].get('status')}"
                   if step["result"].get("status") else ""))
        if result["unexplained_failures"]:
            log(f"⚠️  {len(result['unexplained_failures'])} failure(s) the change "
                f"note does not account for — this is the change-vs-bug gate")

    (AUDIT_DIR / "03-explore-api.json").write_text(json.dumps(result, indent=2))
    md = [f"# Explore — API", "", f"Status: **{result['status']}**"]
    if result["reason"]:
        md.append(f"\n{result['reason']}")
    if result["steps"]:
        md += ["", "| # | Method | Path | Status | Response keys |",
               "|---|---|---|---|---|"]
        md += [f"| {s['index']} | {s['action']['value']} | "
               f"`{s['action']['target']['selector']}` | "
               f"{s['result'].get('status', s['result']['outcome'])} | "
               f"{', '.join((s.get('response') or {}).get('keys', [])[:6])} |"
               for s in result["steps"]]
    (AUDIT_DIR / "03-explore-api.md").write_text("\n".join(md) + "\n")
    log(f"Wrote {AUDIT_DIR / '03-explore-api.json'}")


if __name__ == "__main__":
    main()
