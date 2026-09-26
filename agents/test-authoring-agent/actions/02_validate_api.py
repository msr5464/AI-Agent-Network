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

Scope:
  - Confirms api_base_url is reachable.
  - Performs the real auth recipe from plan["api_auth"] and confirms it
    actually succeeds (this is the single highest-value, zero-side-effect
    check — most real API test failures are auth-related).
  - For every endpoint: makes the real call and records the actual status
    code and top-level response JSON keys, so codegen can see real shape
    instead of guessing from prose. Path params are resolved from a real
    literal value known at parse time (plan["api_endpoints"][i]["sample_path_params"],
    set by 01_parse.py only when the input text gave a concrete example value,
    e.g. "GET /users/octocat").
  - An endpoint the input gave a `curl` for (plan["api_endpoints"][i]["curl"])
    is run exactly as written — it carries the body, headers and ids the author
    meant — and never through a shell (see _run_curl).
  - POST/PUT/DELETE without a curl are called too, with no body. That proves the
    route is reachable and nothing about how it answers a real request, so the
    result is marked `body_sent: false` and neither this step's verdict nor step
    03's codegen hint treats its status as the endpoint's. These calls DO create
    or change data on the target backend — the accepted cost of calling them.
  - A path param with NO known literal value and no curl (its real value only
    exists at test-run time — e.g. an id returned by an earlier create call) is
    NOT invoked; there's nothing safe to substitute. Deferred to step 04's real
    test run.
    KNOWN LIMITATION: full CRUD-chain validation (create → capture id → use
    it in a follow-up call) is not attempted — a reasonable v2, not built here.

Reads:  $AUDIT_DIR/01-parse.json
Writes: $AUDIT_DIR/02-validate-api.json
        $AUDIT_DIR/02-validate-api.md
"""

import json
import os
import shlex
import subprocess
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
from shared.credential_extraction import credentials_from_plan


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


def _run_curl(curl: str) -> tuple:
    """Run the author's curl command. Returns (status, response_keys, error).

    Never through a shell: the command comes from a user-written queue file, and a
    shell would also run whatever follows a `;` or sits inside `$(...)`. Parsed into
    argv instead — which also means `$VARS` are sent literally, not expanded.
    """
    # ponytail: curl's own file options (-o, -K, -T, -d @file) are not filtered; the
    # queue author can already run arbitrary code through step 04's mvn test.
    try:
        argv = shlex.split(curl.replace("\\\n", " "))
    except ValueError as exc:                       # unbalanced quotes
        return None, [], f"could not parse the curl command: {exc}"
    if not argv or Path(argv[0]).name not in ("curl", "curl.exe"):
        return None, [], "the endpoint's curl does not start with curl — not run"
    try:
        proc = subprocess.run(argv + ["-s", "-w", "\n%{http_code}"], capture_output=True,
                              text=True, timeout=REQUEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, [], f"curl timed out after {REQUEST_TIMEOUT_S}s"
    except OSError as exc:                          # curl not installed
        return None, [], f"could not run curl: {exc}"
    if proc.returncode != 0:
        return None, [], f"curl exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    body, _, code = proc.stdout.rpartition("\n")
    if not code.strip().isdigit():
        return None, [], f"no HTTP status in the curl output: {proc.stdout[-200:]!r}"
    try:
        parsed = json.loads(body) if body.strip() else None
    except ValueError:
        parsed = None
    return int(code), (list(parsed.keys()) if isinstance(parsed, dict) else []), None


def check_safe_endpoints(base_url: str, endpoints: list, headers: dict, auth) -> tuple:
    """Returns (checked: list, skipped: list).

    Every endpoint is called for real. One the input gave a `curl` for is run
    exactly as written; the rest go through requests. A mutating call made that
    way has no body, so it proves reachability and nothing about the status a real
    request would get — recorded as `body_sent: False`, so neither this step's
    verdict nor step 03's hint mistakes a 400 for the endpoint's real status. The
    only endpoint not called is one with a path param whose real value isn't known
    until test-run time and no curl to supply it — nothing safe to substitute.
    """
    checked, skipped = [], []
    for ep in endpoints:
        method = ep.get("method", "GET").upper()
        path = ep.get("path", "")
        path_params = ep.get("path_params") or []
        sample_values = ep.get("sample_path_params") or {}
        enum_name = ep.get("enum_name", path)
        curl_cmd = (ep.get("curl") or "").strip()

        resolved_path, missing_params = _resolve_path(path, path_params, sample_values)
        if missing_params and not curl_cmd:
            skipped.append({
                "enum_name": enum_name, "method": method, "path": path,
                "reason": f"no known value for path param(s) {missing_params} — its real value "
                          "only exists at test-run time; validated by step 04's test run instead",
            })
            continue

        expected = ep.get("expected_status")
        if curl_cmd:
            status, response_keys, error = _run_curl(curl_cmd)
        else:
            url = base_url.rstrip("/") + "/" + resolved_path.lstrip("/")
            resp, exc = _request_with_retry(method, url, headers=headers, auth=auth)
            status, response_keys, error = None, [], None
            if exc is not None:
                error = f"{exc.__class__.__name__}: {exc}"
            else:
                status = resp.status_code
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        response_keys = list(body.keys())
                except ValueError:
                    pass
        checked.append({
            "enum_name": enum_name, "method": method, "path": path,
            "resolved_path": resolved_path,
            "expected_status": expected, "actual_status": status,
            "matched_expected": error is None and expected is not None and status == expected,
            "response_keys": response_keys,
            "error": error,
            "via": "curl" if curl_cmd else "requests",
            # A mutating call with no body proves the route exists, not its status.
            "body_sent": bool(curl_cmd) or method == "GET",
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
                if ep.get("via") == "curl":
                    endpoint_desc += " (the input's curl)"
                elif resolved and resolved != ep["path"]:
                    endpoint_desc += f" (called as {resolved})"
                lines.append(
                    f"- {mark} `{endpoint_desc}` → expected {ep.get('expected_status')}, "
                    f"got {ep.get('actual_status') if ep.get('error') is None else ep['error']}"
                    + (f" (keys: {ep['response_keys']})" if ep.get("response_keys") else "")
                    + ("" if ep.get("body_sent", True) else " — sent without a body, reachability only")
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
    demo_creds = credentials_from_plan(plan)

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

    log(f"Checking {len(endpoints)} endpoint(s) — every method is called for real "
        f"(the input's curl where it gave one; POST/PUT/DELETE without one go out with "
        f"no body and prove reachability only); an unresolvable path param with no curl "
        f"is deferred to step 04...")
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
                f"(expected {ep['expected_status']}), keys={ep['response_keys']}"
                + ("" if ep["body_sent"] else " — sent without a body, reachability only"))
    for ep in skipped_eps:
        log(f"  – {ep['method']} {ep['path']}: {ep['reason']}")

    # A body-less POST answering 400 is the expected result of sending nothing, not
    # a disagreement with the endpoint's documented status.
    any_mismatch = any((not e["matched_expected"]) for e in checked
                       if not e.get("error") and e.get("body_sent", True))
    any_error = any(e.get("error") for e in checked)
    overall = "ok" if not (any_mismatch or any_error) else "endpoint_mismatch"

    _write_result({
        "skipped": False, "reason": None, "status": overall,
        "auth": _persistable_auth(auth_result), "endpoints_checked": checked, "endpoints_not_checked": skipped_eps,
    })


if __name__ == "__main__":
    main()
