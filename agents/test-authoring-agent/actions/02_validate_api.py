#!/usr/bin/env python3
"""
Step 02 (API half) — Validate API
Confirms the real API is reachable and auth actually works BEFORE codegen —
the same motivation as Validate Web (02_validate_web.py), but architecturally
much simpler: hitting a REST endpoint doesn't need an LLM driving a browser,
plain HTTP calls are enough. This step runs in seconds, not minutes, and makes
no Claude call at all.

Runs alongside 02_validate_web.py under the same pipeline step (run.sh calls
this first, then validate_web, when test_type == "both") — see CLAUDE.md.

Scope (deliberately conservative):
  - Confirms api_base_url is reachable.
  - Performs the real auth recipe from plan["api_auth"] and confirms it
    actually succeeds (this is the single highest-value, zero-side-effect
    check — most real API test failures are auth-related).
  - For every GET endpoint: makes the real call and records the actual status
    code and top-level response JSON keys, so codegen can see real shape
    instead of guessing from prose. Path params are safe to resolve and call
    for real too, AS LONG AS a real literal value for them is actually known
    at parse time (plan["api_endpoints"][i]["sample_path_params"], set by
    01_parse.py only when the input text gave a concrete example value, e.g.
    "GET /users/octocat"). A GET is read-only regardless of whether its path
    is templated — the path shape was never the actual risk.
  - A path param with NO known literal value (its real value only exists at
    test-run time — e.g. an id returned by an earlier create call) is NOT
    invoked here; there's nothing safe to substitute. Deferred to step 04's
    real test run, same as before this step existed.
  - POST/PUT/DELETE endpoints are NEVER invoked here — firing those against a
    real backend risks creating/mutating real data with no generically-safe
    way to clean up, regardless of whether a literal value is known. Those
    are exercised for real by step 04's actual `mvn test` run.
    KNOWN LIMITATION: full CRUD-chain validation (create → capture id → use
    it in a follow-up call) is not attempted — a reasonable v2, not built here.

Reads:  $AUDIT_DIR/01-parse.json
Writes: $AUDIT_DIR/02-validate-api.json
        $AUDIT_DIR/02-validate-api.md
"""

import json
import os
import sys
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REQUEST_TIMEOUT_S = int(os.environ.get("VALIDATE_API_REQUEST_TIMEOUT_S", "15"))
# A transient network blip is worth one retry; a genuine 401/wrong-credentials
# is not — retrying with the same wrong password can't fix it, mirroring the
# same "don't retry the pointless case" rule step 04's fix loop follows.
RETRY_ON_CONNECTION_ERROR = os.environ.get("VALIDATE_API_RETRY_ON_ERROR", "true").lower() != "false"

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → shared.*
from shared.log import log as _log


def log(msg: str) -> None:
    _log("02-validate-api", msg)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _dot_get(obj, path: str):
    """Resolve a dot-path like 'data.access_token' into a nested dict/list."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _request_with_retry(method: str, url: str, **kwargs):
    """One retry, ONLY for connection-level failures (DNS, refused, timeout) —
    never for a response that came back with a real status code, since that's
    not a transient condition a retry can fix."""
    attempts = 2 if RETRY_ON_CONNECTION_ERROR else 1
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return requests.request(method, url, timeout=REQUEST_TIMEOUT_S, **kwargs), None
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < attempts:
                log(f"  transient error on {method} {url} ({e.__class__.__name__}) — retrying once")
    return None, last_exc


def _persistable_auth(auth_result: dict) -> dict:
    """Strip auth_result down to what's safe to write into the audit JSON.

    `headers` carries the real bearer token / API key value, and `auth` is a
    live requests.auth.AuthBase object (not JSON-serializable at all) — both
    are purely for this process's own subsequent HTTP calls, not for an audit
    trail that could end up committed, screenshotted, or shared.
    """
    return {"status": auth_result.get("status"), "detail": auth_result.get("detail")}


def perform_auth(base_url: str, api_auth: dict, demo_creds: dict) -> dict:
    """Execute the auth recipe from plan["api_auth"]. Returns a result dict:
    {status: "ok"|"skipped"|"unreachable"|"auth_failed"|"auth_misconfigured"|
             "token_extraction_failed", detail: str, headers: dict, auth: requests.auth.AuthBase|None}
    `headers`/`auth` are what subsequent endpoint checks should send.
    """
    auth_type = (api_auth or {}).get("type", "none")

    if auth_type == "none":
        return {"status": "skipped", "detail": "api_auth.type is 'none' — no auth configured", "headers": {}, "auth": None}

    if auth_type == "basic":
        username, password = demo_creds.get("username"), demo_creds.get("password")
        if not username or not password:
            return {"status": "auth_misconfigured",
                    "detail": "api_auth.type is 'basic' but demo_credentials has no username/password",
                    "headers": {}, "auth": None}
        return {"status": "ok", "detail": "basic auth configured (not independently verified against a real endpoint)",
                "headers": {}, "auth": requests.auth.HTTPBasicAuth(username, password)}

    if auth_type == "api_key":
        header_name = api_auth.get("header_name", "X-API-Key")
        api_key = demo_creds.get("api_key", "")
        if not api_key:
            return {"status": "auth_misconfigured",
                    "detail": f"api_auth.type is 'api_key' but demo_credentials.api_key is not set",
                    "headers": {}, "auth": None}
        return {"status": "ok", "detail": f"api_key configured on header '{header_name}'",
                "headers": {header_name: api_key}, "auth": None}

    if auth_type == "bearer_token":
        login = api_auth.get("login_endpoint") or {}
        method = login.get("method", "POST")
        path = login.get("path", "")
        body_fields = login.get("body_fields", {})
        token_path = api_auth.get("token_json_path", "")
        if not path or not body_fields or not token_path:
            return {"status": "auth_misconfigured",
                    "detail": "api_auth.type is 'bearer_token' but login_endpoint/token_json_path is incomplete",
                    "headers": {}, "auth": None}

        body = {}
        missing_creds = []
        for body_key, cred_field in body_fields.items():
            value = demo_creds.get(cred_field)
            if value is None:
                missing_creds.append(cred_field)
            body[body_key] = value
        if missing_creds:
            return {"status": "auth_misconfigured",
                    "detail": f"login_endpoint needs demo_credentials {missing_creds}, not present in the plan",
                    "headers": {}, "auth": None}

        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        resp, exc = _request_with_retry(method, url, json=body)
        if exc is not None:
            return {"status": "unreachable", "detail": f"{method} {path} — {exc.__class__.__name__}: {exc}",
                    "headers": {}, "auth": None}
        if not (200 <= resp.status_code < 300):
            return {"status": "auth_failed",
                    "detail": f"{method} {path} returned {resp.status_code} — {resp.text[:300]}",
                    "headers": {}, "auth": None}
        try:
            resp_json = resp.json()
        except ValueError:
            return {"status": "token_extraction_failed",
                    "detail": f"{method} {path} returned {resp.status_code} but the body isn't JSON",
                    "headers": {}, "auth": None}
        token = _dot_get(resp_json, token_path)
        if not token:
            return {"status": "token_extraction_failed",
                    "detail": f"login succeeded ({resp.status_code}) but token_json_path '{token_path}' "
                              f"did not resolve — response keys were {list(resp_json.keys())}",
                    "headers": {}, "auth": None}
        header_name = api_auth.get("header_name", "Authorization")
        header_prefix = api_auth.get("header_prefix", "Bearer ")
        return {"status": "ok", "detail": f"authenticated via {method} {path}",
                "headers": {header_name: header_prefix + str(token)}, "auth": None}

    return {"status": "auth_misconfigured", "detail": f"unrecognized api_auth.type: {auth_type!r}",
            "headers": {}, "auth": None}


# ── Endpoint checks ──────────────────────────────────────────────────────────

def _resolve_path(path: str, path_params: list, sample_values: dict) -> tuple:
    """Substitute known literal values into a templated path.

    Returns (resolved_path, missing_params). missing_params is the subset of
    path_params with no known literal value — the caller must skip (not
    invoke) an endpoint that still has any of these, since there's nothing
    safe to substitute.
    """
    resolved = path
    missing = []
    for p in path_params:
        value = sample_values.get(p)
        if value is None or value == "":
            missing.append(p)
            continue
        resolved = resolved.replace("{" + p + "}", str(value))
    return resolved, missing


def check_safe_endpoints(base_url: str, endpoints: list, headers: dict, auth) -> tuple:
    """Returns (checked: list, skipped: list).

    Every GET endpoint is a candidate for a real call — a GET is read-only
    regardless of whether its path is templated. Only two things disqualify
    an endpoint from actually being invoked: (1) it's a mutating method
    (POST/PUT/DELETE — never invoked here, see module docstring), or (2) it's
    a GET with a path param whose real value isn't known until test-run time
    (no entry in sample_path_params) — nothing safe to substitute for that.
    """
    checked, skipped = [], []
    for ep in endpoints:
        method = ep.get("method", "GET").upper()
        path = ep.get("path", "")
        path_params = ep.get("path_params") or []
        sample_values = ep.get("sample_path_params") or {}
        enum_name = ep.get("enum_name", path)

        if method != "GET":
            skipped.append({
                "enum_name": enum_name, "method": method, "path": path,
                "reason": "mutating method — not safely invokable without side effects; "
                          "validated by step 04's test run instead",
            })
            continue

        resolved_path, missing_params = _resolve_path(path, path_params, sample_values)
        if missing_params:
            skipped.append({
                "enum_name": enum_name, "method": method, "path": path,
                "reason": f"no known value for path param(s) {missing_params} — its real value "
                          "only exists at test-run time; validated by step 04's test run instead",
            })
            continue

        url = base_url.rstrip("/") + "/" + resolved_path.lstrip("/")
        resp, exc = _request_with_retry("GET", url, headers=headers, auth=auth)
        if exc is not None:
            checked.append({
                "enum_name": enum_name, "method": method, "path": path,
                "resolved_path": resolved_path,
                "expected_status": ep.get("expected_status"), "actual_status": None,
                "matched_expected": False, "response_keys": [],
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            continue

        expected = ep.get("expected_status")
        response_keys = []
        try:
            body = resp.json()
            if isinstance(body, dict):
                response_keys = list(body.keys())
        except ValueError:
            pass
        checked.append({
            "enum_name": enum_name, "method": method, "path": path,
            "resolved_path": resolved_path,
            "expected_status": expected, "actual_status": resp.status_code,
            "matched_expected": (expected is not None and resp.status_code == expected),
            "response_keys": response_keys,
            "error": None,
        })
    return checked, skipped


# ── Result writers ───────────────────────────────────────────────────────────

def _write_result(data: dict) -> None:
    (AUDIT_DIR / "02-validate-api.json").write_text(json.dumps(data, indent=2))

    lines = ["# Validate API Results", ""]
    if data.get("skipped"):
        lines.append(f"Skipped: {data.get('reason')}")
    else:
        lines.append(f"Outcome: {data.get('status')}")
        auth = data.get("auth", {})
        lines.append(f"Auth:    {auth.get('status')} — {auth.get('detail')}")
        checked = data.get("endpoints_checked", [])
        skipped_eps = data.get("endpoints_not_checked", [])
        if checked:
            lines += ["", "## Endpoints Checked (real calls made)"]
            for ep in checked:
                mark = "✓" if ep.get("matched_expected") else ("✗" if ep.get("error") is None else "⚠")
                resolved = ep.get("resolved_path", "")
                endpoint_desc = f"{ep['method']} {ep['path']}"
                if resolved and resolved != ep["path"]:
                    endpoint_desc += f" (called as {resolved})"
                lines.append(
                    f"- {mark} `{endpoint_desc}` → expected {ep.get('expected_status')}, "
                    f"got {ep.get('actual_status') if ep.get('error') is None else ep['error']}"
                    + (f" (keys: {ep['response_keys']})" if ep.get("response_keys") else "")
                )
        if skipped_eps:
            lines += ["", "## Endpoints Not Independently Checked"]
            for ep in skipped_eps:
                lines.append(f"- `{ep['method']} {ep['path']}` — {ep['reason']}")
    (AUDIT_DIR / "02-validate-api.md").write_text("\n".join(lines))


def _write_empty(reason: str) -> None:
    _write_result({"skipped": True, "reason": reason, "status": "skipped",
                   "auth": {}, "endpoints_checked": [], "endpoints_not_checked": []})


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    plan = json.loads((AUDIT_DIR / "01-parse.json").read_text())

    test_type = plan.get("test_type", "")
    base_url = plan.get("api_base_url", "")
    endpoints = plan.get("api_endpoints", [])

    if test_type not in ("api", "both"):
        log(f"Skipped — test_type={test_type!r}")
        _write_empty(reason=f"test_type={test_type!r}, not an API test")
        return
    if not base_url:
        log("No api_base_url in plan — nothing to validate")
        _write_empty(reason="no api_base_url in plan")
        return
    if not endpoints:
        log("No api_endpoints in plan — nothing to validate")
        _write_empty(reason="no api_endpoints in plan")
        return

    api_auth = plan.get("api_auth") or {"type": "none"}
    demo_creds = plan.get("demo_credentials", {})

    log(f"Authenticating against {base_url} (type={api_auth.get('type')})...")
    auth_result = perform_auth(base_url, api_auth, demo_creds)
    log(f"  {auth_result['status']}: {auth_result['detail']}")

    if auth_result["status"] not in ("ok", "skipped"):
        log("  → FIX: " + {
            "unreachable": "check api_base_url and network/VPN access to the target environment",
            "auth_failed": "verify demo_credentials against the real login endpoint",
            "auth_misconfigured": "fix api_auth in the queue input file or re-run step 01",
            "token_extraction_failed": "check api_auth.token_json_path against the real login response shape",
        }.get(auth_result["status"], "check the detail above"))
        unchecked = [{
            "enum_name": ep.get("enum_name", ep.get("path", "")),
            "method": ep.get("method", "GET"),
            "path": ep.get("path", ""),
            "reason": "not checked — authentication did not succeed",
        } for ep in endpoints]
        _write_result({
            "skipped": False, "reason": None, "status": auth_result["status"],
            "auth": _persistable_auth(auth_result), "endpoints_checked": [], "endpoints_not_checked": unchecked,
        })
        return

    log(f"Checking {len(endpoints)} endpoint(s) — every GET is called for real "
        f"(with known path-param values substituted); POST/PUT/DELETE and GETs "
        f"with an unresolvable path param are deferred to step 04...")
    checked, skipped_eps = check_safe_endpoints(
        base_url, endpoints, auth_result["headers"], auth_result["auth"]
    )
    for ep in checked:
        called = ep.get("resolved_path") or ep["path"]
        if ep.get("error"):
            log(f"  ✗ {ep['method']} {called}: {ep['error']}")
        else:
            mark = "✓" if ep["matched_expected"] else "⚠"
            log(f"  {mark} {ep['method']} {called} → {ep['actual_status']} "
                f"(expected {ep['expected_status']}), keys={ep['response_keys']}")
    for ep in skipped_eps:
        log(f"  – {ep['method']} {ep['path']}: {ep['reason']}")

    any_mismatch = any((not e["matched_expected"]) for e in checked if not e.get("error"))
    any_error = any(e.get("error") for e in checked)
    overall = "ok" if not (any_mismatch or any_error) else "endpoint_mismatch"

    _write_result({
        "skipped": False, "reason": None, "status": overall,
        "auth": _persistable_auth(auth_result), "endpoints_checked": checked, "endpoints_not_checked": skipped_eps,
    })


if __name__ == "__main__":
    main()
