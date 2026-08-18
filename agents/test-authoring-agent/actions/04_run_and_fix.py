#!/usr/bin/env python3
"""
Step 04 — Run & Fix
Two-phase design driven by FIX_ATTEMPT (set by run.sh):

  FIX_ATTEMPT=0  (initial run)
    Runs the generated test as-is. No Claude call. Writes gate=true/false.
    If the test passes, the fix loop in run.sh is skipped entirely.

  FIX_ATTEMPT>=1  (fix attempt N)
    Loads the previous failure output, calls Claude for a fix, applies it,
    THEN runs the test. Each fix attempt is an atomic (fix + verify) unit.
    run.sh counts only these attempts against MAX_FIX_ATTEMPTS.

Reads:  $AUDIT_DIR/03-generate.json
        $AUDIT_DIR/04-run-and-fix.json  (previous attempt's test output)
Writes: $AUDIT_DIR/04-run-and-fix.json
        $AUDIT_DIR/04-run-and-fix.md
        $AUDIT_DIR/04-run-and-fix-attempt-{N}.json  (fix attempts only, for per-step commits)
        $AUDIT_DIR/.fix-passed          gate: true / false / skipped
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR    = Path(os.environ["AUDIT_DIR"])
AGENT_DIR    = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT    = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
AUTOMATION_FRAMEWORK_DIR    = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Jarvis")

MODEL        = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
ENVIRONMENT  = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging")
COUNTRY      = os.environ.get("AUTOCREATE_COUNTRY", "SG")
HEADLESS     = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
FIX_ATTEMPT  = int(os.environ.get("FIX_ATTEMPT", "1"))
MAX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))
MAVEN_TEST_TIMEOUT_S = int(os.environ.get("MAVEN_TEST_TIMEOUT_S", "300"))

# Where the Java framework's TestListener/JsonTestReporter write machine-readable
# results — built specifically "for AI agents to read... without parsing HTML
# reports" per JsonTestReporter's own docstring, but nothing here read it before.
# Defaults match Config.resultsDirectory's own default (user.dir/test-output),
# and user.dir for the mvn subprocess below is AUTOMATION_FRAMEWORK_DIR.
TEST_RESULTS_DIR = AUTOMATION_FRAMEWORK_DIR / os.environ.get("TEST_RESULTS_DIR_NAME", "test-output")

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("04-run-and-fix", msg)

from shared.claude import call_claude as _call_claude
def call_claude(prompt: str) -> str:
    output = _call_claude(prompt, MODEL, str(REPO_ROOT))
    if not output:
        log("ERROR: Claude CLI returned empty response")
    return output

from shared.credential_properties import write_credential_property


def extract_json(text: str):
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def run_maven_test(test_class: str, test_method: str) -> tuple:
    """Run mvn test with real-time line-by-line streaming. Returns (passed, output)."""
    test_arg = f"{test_class}#{test_method}" if test_method else test_class

    cmd = [
        "mvn", "test",
        f"-Dtest={test_arg}",
        f"-Denvironment={ENVIRONMENT}",
        f"-Dcountry={COUNTRY}",
        f"-Dheadless={'true' if HEADLESS else 'false'}",
        "--no-transfer-progress",
    ]
    log(f"Running: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into the same stream
            text=True,
            cwd=str(AUTOMATION_FRAMEWORK_DIR),
        )
    except FileNotFoundError:
        log("ERROR: mvn not found in PATH")
        return False, "ERROR: mvn command not found. Is Maven installed and in PATH?"

    all_lines: list = []
    timed_out = False

    def _stream() -> None:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            all_lines.append(line)
            if line.strip():            # skip blank separator lines
                log(f"  {line}")

    reader = threading.Thread(target=_stream, daemon=True)
    reader.start()

    try:
        proc.wait(timeout=MAVEN_TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()

    reader.join(timeout=5)

    if timed_out:
        log(f"ERROR: mvn test timed out ({MAVEN_TEST_TIMEOUT_S}s)")
        return False, "\n".join(all_lines) + f"\nERROR: Maven test timed out after {MAVEN_TEST_TIMEOUT_S} seconds."

    passed = proc.returncode == 0
    log(f"Test exit code: {proc.returncode} ({'PASS' if passed else 'FAIL'})")
    # Return the full captured output (last 6000 chars keeps tail for Claude context)
    return passed, "\n".join(all_lines)[-6000:]


def read_generated_files(files_written: list) -> dict:
    """Read the content of all generated Java files."""
    contents = {}
    for rel_path in files_written:
        full = AUTOMATION_FRAMEWORK_DIR / rel_path
        if full.exists():
            try:
                contents[rel_path] = full.read_text()
            except Exception:
                pass
    return contents


def extract_fix_response(fix_map) -> tuple:
    """Unpack the fix response into (root_cause, confidence, files_map).

    Accepts the new {"root_cause":..., "confidence":..., "files": {...}} shape
    the prompt now asks for, but falls back to treating the whole object as a
    flat {file: content} map if "files" is absent — an LLM doesn't always
    follow a structure change on the first try, and a fix that still applies
    correctly shouldn't be discarded just because the diagnosis fields are
    missing.
    """
    if not isinstance(fix_map, dict):
        return "", "", {}
    if "files" in fix_map and isinstance(fix_map["files"], dict):
        return (
            str(fix_map.get("root_cause", "")),
            str(fix_map.get("confidence", "")),
            fix_map["files"],
        )
    return "", "", fix_map


def apply_fix(files_map: dict) -> tuple:
    """Write Claude's fixed file contents back to Thanos-pw.
    Returns (patched_paths: list, patched_contents: dict)."""
    patched = []
    patched_contents: dict = {}
    for rel_path, content in files_map.items():
        if not content or not content.strip():
            continue
        full = AUTOMATION_FRAMEWORK_DIR / rel_path
        # Safety: only write inside Thanos-pw
        try:
            full.resolve().relative_to(AUTOMATION_FRAMEWORK_DIR.resolve())
        except ValueError:
            log(f"  BLOCKED: {rel_path} escapes repo root")
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        patched.append(rel_path)
        patched_contents[rel_path] = content
        log(f"  Fixed: {rel_path}")
    return patched, patched_contents


def _extract_failure_summary(output: str) -> list:
    """Extract the most actionable failure lines from Maven/TestNG output.

    Returns the list rather than only logging it — this used to be a log-only
    side effect, so the curated signal it computes (vs. the raw last-N-chars
    tail truncation) never reached the Claude fix prompt, only a human reading
    the console.
    """
    summary = []
    for line in output.splitlines():
        s = line.strip()
        # TestNG failure header: [ERROR]   ClassName.method:N » ErrorType
        if s.startswith("[ERROR]") and ("»" in s or ("Failures" in s and s != "[ERROR] Failures:")):
            summary.append(s)
        # Timeout exceeded line
        elif "exceeded" in s and ("Timeout" in s or "timeout" in s):
            summary.append(s)
        # "waiting for locator(...)" — what the test got stuck on
        elif "waiting for locator" in s:
            summary.append(f"  stuck waiting for: {s.split('waiting for locator')[-1].strip()}")
        # Assertion mismatch
        elif ("Expected" in s and ("but got" in s or "was" in s)) or "AssertionError" in s:
            summary.append(s)
        # Java compile errors
        elif "cannot find symbol" in s or ("error:" in s and ".java" in s):
            summary.append(s)
        # Maven timeout (from our subprocess)
        elif "Maven test timed out" in s:
            summary.append(s)

    if summary:
        return summary[:10]
    # Fallback: last few [ERROR] lines
    return [l.strip() for l in output.splitlines() if "[ERROR]" in l][-5:]


def _log_failure_summary(output: str) -> list:
    """Log the curated failure summary and return it (see _extract_failure_summary)."""
    summary = _extract_failure_summary(output)
    if summary:
        log("Failure summary:")
        for ln in summary:
            log(f"  {ln}")
    return summary


def read_json_test_report(test_class: str, test_method: str) -> dict:
    """Read the structured failure entry from the Java framework's own
    test-output/report.json, written by JsonTestReporter specifically "for AI
    agents to read... without parsing HTML reports" (its own docstring) — a
    precise failureLocation (file:line) instead of hunting through a raw
    stack trace, that nothing here consulted before this change.

    Returns {} if the report doesn't exist, isn't valid JSON, or has no entry
    for this class/method (e.g. a compile error prevented the suite from
    running at all, so TestNG never invoked the listener).
    """
    report_path = TEST_RESULTS_DIR / "report.json"
    if not report_path.exists():
        return {}
    try:
        entries = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(entries, list):
        return {}

    candidates = [e for e in entries if isinstance(e, dict) and e.get("className") == test_class]
    if test_method:
        method_matches = [e for e in candidates if e.get("testName") == test_method]
        if method_matches:
            candidates = method_matches
    if not candidates:
        return {}
    # Prefer the most recent non-passed entry (retries can produce multiple
    # entries for the same test); fall back to the last entry of any status.
    failed = [e for e in candidates if e.get("status") != "PASSED"]
    return (failed or candidates)[-1]


def find_latest_screenshot(test_method: str, newer_than: float = 0.0) -> str:
    """Best-effort screenshot lookup via glob, NOT via report.json's own
    screenshotPath field — that field is always written as an empty string
    by JsonTestReporter (verified against the framework source: the comment
    even says "populated by TestListener" but nothing actually cross-fills
    it). TestListener.onTestFailure DOES call BrowserHelper.takeScreenshot,
    which writes to test-output/screenshots/{testcaseName}_{HHmmss}.png —
    config.testcaseName is set to the TestNG method name, matching test_method.

    newer_than (epoch seconds) filters to screenshots from THIS run, so a
    stale screenshot left over from a previous run of the same test method
    isn't mistaken for current evidence.
    """
    if not test_method:
        return ""
    screenshots_dir = TEST_RESULTS_DIR / "screenshots"
    if not screenshots_dir.exists():
        return ""
    matches = sorted(
        (p for p in screenshots_dir.glob(f"{test_method}_*.png") if p.stat().st_mtime >= newer_than),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return ""
    try:
        return str(matches[0].resolve().relative_to(AUTOMATION_FRAMEWORK_DIR.resolve()))
    except ValueError:
        return str(matches[0])


def build_failure_context(test_class: str, test_method: str, test_output: str,
                          run_started_at: float) -> dict:
    """Consolidate every failure signal available after a test run: the
    structured JsonTestReporter entry, a screenshot (best-effort glob), and
    the curated Maven/TestNG summary — one place both the human log and the
    Claude fix prompt draw from, instead of two independently-computed views
    of "what failed" that only the human-facing one was ever complete.
    """
    report_entry = read_json_test_report(test_class, test_method)
    screenshot = find_latest_screenshot(test_method, newer_than=run_started_at)
    summary_lines = _log_failure_summary(test_output)

    if report_entry:
        log(f"Structured report: {report_entry.get('failureLocation') or '(no location)'} "
            f"— {(report_entry.get('failureMessage') or '')[:200]}")
    if screenshot:
        log(f"Screenshot: {screenshot}")

    return {
        "failure_location":  report_entry.get("failureLocation", ""),
        "failure_message":   report_entry.get("failureMessage", ""),
        "retry_count":       report_entry.get("retryCount"),
        "screenshot_path":   screenshot,
        "summary_lines":     summary_lines,
    }


# ── Infrastructure helpers ────────────────────────────────────────────────────

def _extract_host(url: str) -> str:
    """Return 'host' or 'host:port' from a URL, or '' if unparseable/empty."""
    if not url:
        return ""
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""


def classify_failure(output: str, api_base_url: str = "") -> str:
    """Returns 'INFRA_BUILD' | 'INFRA_DB' | 'INFRA_USER' | 'INFRA_CREDENTIALS' |
    'INFRA_API_CONNECTION' | 'INFRA_API_AUTH' | 'CODE_ERROR'."""
    infra_build_signals = [
        "There is no POM in this directory",
        "requires a project to execute",
        "Could not find artifact",
    ]
    infra_db_signals = [
        "Communications link failure",
        "Connection refused",
        "No suitable driver found",
        "Unable to connect",
        "db.thanos.url",
        "Could not connect to database",
        "<host>",
        "<dbname>",
        "jdbc:mysql://<",
    ]
    infra_user_signals = [
        "Failed to get free user after",
        "getUserWithRetry",
        "UserQuery[",
        "No free user available",
    ]
    # A missing {feature}.username/.password property (see try_fix_infra_credentials)
    # surfaces as a null being handed to a Playwright fill() call — this exact
    # Playwright-Java error text was confirmed against a real failure caused by
    # exactly that gap, which two separate Claude fix attempts misdiagnosed as a
    # locator/timing bug because nothing connected the null value back to its source.
    infra_credentials_signals = [
        "value: expected string, got undefined",
    ]
    # Common REST-client phrasings for an auth rejection. Kept multi-word/prefixed
    # (never a bare "401"/"403") so this can't collide with an unrelated line
    # number or byte offset elsewhere in Maven output.
    infra_api_auth_signals = [
        "401 Unauthorized", "HTTP/1.1 401", "statusCode=401", "\"status\":401",
        "status code: 401", "status code 401", "Response status:401",
        "403 Forbidden", "HTTP/1.1 403", "statusCode=403", "\"status\":403",
        "status code: 403", "status code 403", "Response status:403",
    ]

    # "Connection refused" is ambiguous — it fires for BOTH a dead local DB and
    # an unreachable REST API. Disambiguate using api_base_url's own host so an
    # unreachable API is never misrouted into try_fix_infra_db() (which would
    # attempt to auto-configure a local MySQL — never the right fix here).
    # Checked before the generic infra_db_signals loop below.
    api_host = _extract_host(api_base_url)
    if api_host and "Connection refused" in output and api_host in output:
        return "INFRA_API_CONNECTION"

    for signal in infra_api_auth_signals:
        if signal in output:
            return "INFRA_API_AUTH"
    for signal in infra_build_signals:
        if signal in output:
            return "INFRA_BUILD"
    for signal in infra_db_signals:
        if signal in output:
            return "INFRA_DB"
    for signal in infra_user_signals:
        if signal in output:
            return "INFRA_USER"
    for signal in infra_credentials_signals:
        if signal in output:
            return "INFRA_CREDENTIALS"
    return "CODE_ERROR"


def _find_mysql() -> str:
    """Return path to mysql binary, or empty string if not found."""
    for candidate in ["/usr/local/mysql/bin/mysql", "/opt/homebrew/bin/mysql"]:
        if Path(candidate).exists():
            return candidate
    return shutil.which("mysql") or ""


def try_fix_infra_db() -> bool:
    """Auto-configure local MySQL in system.properties if not already set. Returns True if fixed."""
    mysql_bin = _find_mysql()
    if not mysql_bin:
        log("DB auto-repair: mysql binary not found")
        return False

    result = subprocess.run([mysql_bin, "-u", "root", "-e", "SELECT 1;"],
                            capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        log("DB auto-repair: could not connect to local MySQL as root (no password)")
        return False

    sys_props = AUTOMATION_FRAMEWORK_DIR / "parameters" / "system.properties"
    if not sys_props.exists():
        sys_props.parent.mkdir(parents=True, exist_ok=True)
        sys_props.write_text("")

    content = sys_props.read_text()
    if "<host>" not in content and "db.thanos.url" in content:
        log("DB auto-repair: system.properties already has a real DB URL — skipping")
        return False

    # Remove any placeholder lines and append real values
    lines = [ln for ln in content.splitlines()
             if not ln.strip().startswith("db.thanos.") or "<host>" not in ln]
    lines += [
        "",
        "# Auto-configured by test-authoring-agent",
        "db.thanos.url=jdbc:mysql://localhost:3306/thanos",
        "db.thanos.username=root",
        "db.thanos.password=",
    ]
    sys_props.write_text("\n".join(lines) + "\n")
    log("DB auto-repair: wrote local MySQL config to system.properties")
    return True


def _mysql_escape(value: str) -> str:
    """Minimal correct MySQL string escaping for values embedded via the
    `mysql -e` CLI (no parameterized-query API is available at this layer —
    the mysql binary itself doesn't support bound parameters).

    Order matters: backslashes MUST be escaped before quotes. The previous
    .replace("'", "\\'") escaped quotes only — a value ending in a literal
    backslash (e.g. "foo\\") turned that lone \\' into \\\\' , which MySQL
    reads as an escaped backslash followed by an UNESCAPED quote, breaking
    out of the string.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def try_fix_infra_user(plan: dict) -> bool:
    """Insert demo user into the user pool table if missing. Returns True if action taken."""
    creds = plan.get("demo_credentials", {})
    if not creds.get("username") or not creds.get("password"):
        log("User auto-repair: no demo_credentials in plan")
        return False

    mysql_bin = _find_mysql()
    if not mysql_bin:
        log("User auto-repair: mysql binary not found")
        return False

    environment  = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging")
    table        = f"users_{environment}"
    country      = plan.get("country", "SG")
    feature_enum = plan.get("feature_enum", "CARD")
    username     = _mysql_escape(creds["username"])
    password     = _mysql_escape(creds["password"])
    otp          = _mysql_escape(creds.get("otp", ""))

    def run_mysql(sql: str):
        return subprocess.run(
            [mysql_bin, "-u", "root", "thanos", "-e", sql],
            capture_output=True, text=True, timeout=10
        )

    # Check if user already exists
    check = run_mysql(f"SELECT id FROM `{table}` WHERE username='{username}' LIMIT 1;")
    if check.returncode != 0:
        log(f"User auto-repair: could not query {table}: {check.stderr[:200]}")
        return False

    if username in check.stdout:
        run_mysql(f"UPDATE `{table}` SET usageStatus='FREE', testcaseName=NULL "
                  f"WHERE username='{username}';")
        log(f"User auto-repair: reset existing user to FREE: {username}")
        return True

    sql = (
        f"INSERT INTO `{table}` "
        f"(isActive, userType, poolUser, feature, usageStatus, username, password, otp, country) "
        f"VALUES (1, 'Admin', 'YES', '{feature_enum}', 'FREE', "
        f"'{username}', '{password}', '{otp}', '{country}');"
    )
    result = run_mysql(sql)
    if result.returncode == 0:
        log(f"User auto-repair: inserted demo user into {table}: {username}")
        return True
    log(f"User auto-repair: INSERT failed: {result.stderr[:200]}")
    return False


def try_fix_infra_credentials(plan: dict) -> bool:
    """Ensure {feature}.username/.password exist in the environment+country
    properties file. Returns True only if the property was genuinely missing
    and just got written — "already present" means this genuinely isn't why
    the test is failing, so the caller should NOT re-run the test expecting
    it to be fixed and should fall through to the normal CODE_ERROR path.

    03_generate.py already writes this once, right after generating a new web
    module — this is a defensive re-check for cases that path doesn't cover:
    an existing module (03 only writes for brand-new modules), a session run
    before this existed, or a properties file edited/reverted since generation.
    """
    feature = plan.get("feature_name", "")
    if not feature:
        log("Credential auto-repair: no feature_name in plan")
        return False
    status = write_credential_property(
        AUTOMATION_FRAMEWORK_DIR, feature.lower(), plan.get("demo_credentials", {}), log=log
    )
    if status == "no credentials to write":
        log("Credential auto-repair: no demo_credentials in plan")
    return status == "written"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    gen_data = json.loads((AUDIT_DIR / "03-generate.json").read_text())
    files_written = gen_data.get("files_written", [])
    test_class    = gen_data.get("test_class", "")
    test_method   = gen_data.get("test_method", "")
    plan_data     = json.loads((AUDIT_DIR / "01-parse.json").read_text())
    api_val_path  = AUDIT_DIR / "02-validate-api.json"
    api_validation = json.loads(api_val_path.read_text()) if api_val_path.exists() else {}

    if not test_class:
        log("No test class found in generate output — skipping test run")
        _write_gate("skipped")
        _write_result({"skipped": True, "reason": "no test class"}, [], FIX_ATTEMPT)
        return

    if not AUTOMATION_FRAMEWORK_DIR.exists():
        log(f"ERROR: Automation framework repo not found: {AUTOMATION_FRAMEWORK_DIR}")
        _write_gate("skipped")
        _write_result({"skipped": True, "reason": f"Automation framework repo not found at {AUTOMATION_FRAMEWORK_DIR}"}, [], FIX_ATTEMPT)
        return

    # ── FIX_ATTEMPT == 0 — initial run, no fix ────────────────────────────────
    if FIX_ATTEMPT == 0:
        log(f"Initial test run: {test_class}#{test_method}")
        run_started_at = time.time()
        passed, test_output = run_maven_test(test_class, test_method)
        failure_ctx = {}
        if not passed:
            failure_ctx = build_failure_context(test_class, test_method, test_output, run_started_at)
            log("Initial test FAILED — re-running once (no code change) to rule out "
                "flakiness before spending a fix attempt")
            run_started_at = time.time()
            passed_retry, test_output_retry = run_maven_test(test_class, test_method)
            if passed_retry:
                log("Re-run PASSED — treating the first failure as a flake, no fix needed")
                passed = True
                test_output = test_output_retry
                failure_ctx = {}
            else:
                log("Re-run also FAILED — reproducible, fix attempt 1 will apply a Claude fix")
                test_output = test_output_retry
                failure_ctx = build_failure_context(test_class, test_method, test_output, run_started_at)
        else:
            log("Initial test PASSED")
        _write_gate("true" if passed else "false")
        _write_result({
            "attempt": 0,
            "test_class": test_class,
            "test_method": test_method,
            "passed": passed,
            "test_output": test_output,
            "fixes_applied": [],
            **failure_ctx,
        }, files_written, 0)
        return

    # ── FIX_ATTEMPT >= 1 — classify previous failure → fix → run ─────────────
    # Load the failure output (and structured failure context) written by the
    # previous attempt — this is the failure THIS attempt is being asked to fix.
    prev_output = ""
    prev_failure_location = ""
    prev_root_cause = ""
    prev_fix_path = AUDIT_DIR / "04-run-and-fix.json"
    if prev_fix_path.exists():
        try:
            prev = json.loads(prev_fix_path.read_text())
            prev_output = prev.get("test_output", "")[-3000:]
            prev_failure_location = prev.get("failure_location", "")
            prev_failure_message  = prev.get("failure_message", "")
            prev_screenshot       = prev.get("screenshot_path", "")
            prev_summary_lines    = prev.get("summary_lines", [])
            prev_root_cause       = prev.get("root_cause", "")
            log(f"Fix attempt {FIX_ATTEMPT}/{MAX_ATTEMPTS} — loaded previous failure ({len(prev_output)} chars)"
                + (f", location={prev_failure_location}" if prev_failure_location else ""))
        except Exception:
            prev_failure_message, prev_screenshot, prev_summary_lines = "", "", []
    else:
        prev_failure_message, prev_screenshot, prev_summary_lines = "", "", []

    failure_class = classify_failure(prev_output, plan_data.get("api_base_url", ""))
    log(f"Failure classified as: {failure_class}")

    if failure_class == "INFRA_DB":
        log("Detected DB infrastructure error — attempting auto-repair")
        if try_fix_infra_db():
            log("DB auto-configured — running test")
            passed, test_output = run_maven_test(test_class, test_method)
            if passed:
                log("Test PASSED after DB auto-repair")
                _write_gate("true")
                _write_result({
                    "attempt": FIX_ATTEMPT,
                    "test_class": test_class,
                    "test_method": test_method,
                    "passed": True,
                    "test_output": test_output,
                    "fixes_applied": ["auto:db_config"],
                    "infra_repair": "INFRA_DB",
                }, files_written, FIX_ATTEMPT)
                return
            failure_class = classify_failure(test_output, plan_data.get("api_base_url", ""))

    if failure_class == "INFRA_USER":
        log("Detected user pool error — attempting demo user auto-insert")
        if try_fix_infra_user(plan_data):
            log("Demo user inserted/reset — running test")
            passed, test_output = run_maven_test(test_class, test_method)
            if passed:
                log("Test PASSED after user auto-repair")
                _write_gate("true")
                _write_result({
                    "attempt": FIX_ATTEMPT,
                    "test_class": test_class,
                    "test_method": test_method,
                    "passed": True,
                    "test_output": test_output,
                    "fixes_applied": ["auto:user_insert"],
                    "infra_repair": "INFRA_USER",
                }, files_written, FIX_ATTEMPT)
                return
            failure_class = classify_failure(test_output, plan_data.get("api_base_url", ""))

    if failure_class == "INFRA_CREDENTIALS":
        log("Detected a null-credential error signature — checking the demo-credential property")
        if try_fix_infra_credentials(plan_data):
            log("Credential property written — running test")
            passed, test_output = run_maven_test(test_class, test_method)
            if passed:
                log("Test PASSED after credential auto-repair")
                _write_gate("true")
                _write_result({
                    "attempt": FIX_ATTEMPT,
                    "test_class": test_class,
                    "test_method": test_method,
                    "passed": True,
                    "test_output": test_output,
                    "fixes_applied": ["auto:credential_property"],
                    "infra_repair": "INFRA_CREDENTIALS",
                }, files_written, FIX_ATTEMPT)
                return
            failure_class = classify_failure(test_output, plan_data.get("api_base_url", ""))
        else:
            # The property was already correct (or there were no demo_credentials
            # to write at all) — this signature match wasn't actually a missing-
            # credential issue after all. Unlike INFRA_DB/INFRA_USER, this is NOT
            # "unresolvable infra" — fall through to the normal CODE_ERROR path
            # rather than skipping the Claude fix loop for what's likely a real bug.
            log("Credential property was already correct — not a credentials issue; "
                "treating as a normal code failure")
            failure_class = "CODE_ERROR"

    api_auth_prevalidated_ok = api_validation.get("auth", {}).get("status") == "ok"
    api_auth_code_bug_hint = False  # surfaced to the Claude fix prompt below
    if failure_class == "INFRA_API_AUTH":
        if api_auth_prevalidated_ok:
            # Step 02 already proved these exact credentials work via a direct
            # HTTP call before codegen — a 401/403 now, with nothing else
            # changed, points at the GENERATED code (wrong header name/prefix,
            # token not attached, etc.), not the credentials or the API itself.
            # Fall through to the normal CODE_ERROR/Claude-fix path; the extra
            # context injected into the prompt below tells Claude as much.
            log("Detected an API 401/403, but step 02 pre-validated this exact "
                "auth recipe as working — treating as a code bug, not an infra issue")
            failure_class = "CODE_ERROR"
            api_auth_code_bug_hint = True
        else:
            log(f"Detected an API 401/403 — and step 02's auth pre-check did not "
                f"confirm it working either ({api_validation.get('auth', {}).get('status', 'not run')}: "
                f"{api_validation.get('auth', {}).get('detail', 'n/a')}) — not auto-resolvable")

    if failure_class in ("INFRA_API_AUTH", "INFRA_API_CONNECTION"):
        reason = (f"API auth not resolvable: {api_validation.get('auth', {}).get('detail', 'see test output')}"
                  if failure_class == "INFRA_API_AUTH" else
                  f"API at {plan_data.get('api_base_url', '?')} is unreachable — check network/VPN access")
        log(f"Infrastructure error not auto-resolvable ({failure_class}) — skipping Claude fix loop")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Infrastructure error: {failure_class} — {reason}",
            "test_output": prev_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    if failure_class == "INFRA_BUILD":
        log(f"ERROR: Maven project not found — check WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")
        log(f"Expected pom.xml at: {AUTOMATION_FRAMEWORK_DIR}")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Maven project not found at {AUTOMATION_FRAMEWORK_DIR} — ensure WORKSPACE_DIR and GITHUB_REPO_AUTOMATION point to a valid Maven project",
            "test_output": prev_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    if failure_class in ("INFRA_DB", "INFRA_USER"):
        log(f"Infrastructure error not auto-resolvable ({failure_class}) — skipping Claude fix loop")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Infrastructure error: {failure_class} — manual setup required",
            "test_output": prev_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    # CODE_ERROR — call Claude for a fix, apply it, then run the test
    log(f"Fix attempt {FIX_ATTEMPT}/{MAX_ATTEMPTS}: calling Claude for a fix...")
    generated_files = read_generated_files(files_written)
    # Read Jarvis/CLAUDE.md — single source of truth for framework conventions.
    fw_claude_md_path = AUTOMATION_FRAMEWORK_DIR / "CLAUDE.md"
    claude_md = fw_claude_md_path.read_text() if fw_claude_md_path.exists() else ""
    if not claude_md:
        log("WARNING: Jarvis/CLAUDE.md not found — check WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")

    files_context = "\n".join(
        f"\n--- {path} ---\n{content}\n" for path, content in generated_files.items()
    )

    # Structured evidence section — precise failureLocation from the framework's
    # own JsonTestReporter (built "for AI agents to read", never consulted
    # before) plus the curated summary this file already computed but only
    # used to print to the console, not to inform the fix itself.
    structured_section = ""
    if prev_failure_location or prev_summary_lines:
        structured_section = "\n<structured_failure_report>\n"
        if prev_failure_location:
            structured_section += f"Failure location: {prev_failure_location}\n"
            if files_written and not any(prev_failure_location.startswith(Path(f).name)
                                         for f in files_written):
                structured_section += (
                    "NOTE: this location is NOT one of the files listed in "
                    "<generated_files> below — it may be a bug in shared framework "
                    "code you cannot see or edit here. If so, say so plainly in "
                    "root_cause instead of inventing a workaround in a file you can "
                    "edit; a workaround around a framework bug tends to make the "
                    "next failure harder to diagnose, not fix this one.\n"
                )
        if prev_failure_message:
            structured_section += f"Failure message: {prev_failure_message}\n"
        if prev_screenshot:
            structured_section += f"Screenshot (browser-driven web test only): {prev_screenshot}\n"
        if prev_summary_lines:
            structured_section += "Curated Maven/TestNG summary:\n" + "\n".join(
                f"  {ln}" for ln in prev_summary_lines
            ) + "\n"
        structured_section += "</structured_failure_report>\n"

    if api_auth_code_bug_hint:
        api_auth = plan_data.get("api_auth", {})
        structured_section += (
            "\n<api_auth_note>\n"
            f"This test is failing with a 401/403, but step 02 (Validate API) already "
            f"confirmed THIS EXACT auth recipe works via a direct HTTP call: "
            f"{api_validation.get('auth', {}).get('detail', '')}\n"
            f"api_auth from the plan: {json.dumps(api_auth)}\n"
            "Since the credentials and endpoint are already proven to work outside the "
            "generated code, look for a bug in how the generated Helper/ApiHelper code "
            "builds and attaches the auth header (wrong header name, missing 'Bearer ' "
            "prefix, token not actually set before the call, wrong request going out) "
            "rather than treating this as a credentials or environment problem.\n"
            "</api_auth_note>\n"
        )

    retry_section = ""
    if FIX_ATTEMPT > 1:
        stuck_note = ""
        if prev_root_cause:
            stuck_note = (
                f"\nThe PREVIOUS fix attempt's stated root cause was:\n  {prev_root_cause}\n"
                "That attempt's fix did not resolve the test (see the failure above, "
                "captured AFTER that fix was applied and the test re-run) — so either "
                "that diagnosis was wrong, or the fix for it was incomplete. Do not "
                "repeat the same diagnosis unless you have a specific reason the fix "
                "for it was incomplete rather than misdiagnosed.\n"
            )
        retry_section = f"""
## ⚠️ RETRY — Fix attempt {FIX_ATTEMPT}
Previous fix did not resolve the test. Previous failure:
```
{prev_output}
```
{stuck_note}
Try a DIFFERENT approach — do NOT repeat what was tried before.
"""

    prompt = f"""You are a Java test automation debugging agent for the Jarvis framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<generated_files>
{files_context}
</generated_files>
{structured_section}
<test_failure>
```
{prev_output}
```
</test_failure>
{retry_section}

The test failed. Analyze the failure and return a JSON object with your diagnosis and
fixed file contents. Only include files that need to change.

Common failure causes:
- Import statements missing or wrong package names
- Locator not found — fix the selector or add a fallback
- Method not found — check the framework API (use BasePage/Element/WaitHelper wrappers)
- Compilation error — fix the Java syntax
- User not allocated — check allocateUser() call matches feature enum
- Auth not set — ensure setAuthToken(token) is called on the helper, or doLogin() for web tests

CRITICAL: Preserve ALL existing JavaDoc comments, inline comments, and annotations exactly as written.
Only change the minimum code required to fix the failure. Do NOT remove, shorten, or reword any comments.

Return ONLY a JSON object of this exact shape:
{{
  "root_cause": "one or two sentences: what actually broke and why, not just what error appeared",
  "confidence": "high | medium | low",
  "files": {{
    "src/test/java/automation/{plan_data.get('feature_name', 'feature')}/{{}}.java": "...fixed content...",
    "src/main/java/automation/modules/...": "...fixed content..."
  }}
}}

Include the COMPLETE file content (not just the changed lines) for every file in "files".
If you believe this is a framework-level issue you cannot fix from the files you can see,
set "files" to an empty object {{}} and explain that clearly in root_cause instead of
guessing at a workaround. Output ONLY valid JSON.
"""

    fix_response = call_claude(prompt)
    fix_map = extract_json(fix_response)
    root_cause, confidence, files_map = extract_fix_response(fix_map)

    fixes_applied = []
    fix_contents: dict = {}
    if files_map:
        fixes_applied, fix_contents = apply_fix(files_map)
        log(f"Applied fixes to {len(fixes_applied)} file(s) — running test")
    elif root_cause:
        log(f"Claude diagnosed the failure but proposed no file changes: {root_cause}")
        log("  → Likely a framework-level issue outside the generated files — running "
            "test anyway in case it was already resolved, but expect this to still fail")
    else:
        log("WARNING: Claude did not return a valid fix map — running test without fix")
    if root_cause:
        log(f"Root cause ({confidence or 'unknown confidence'}): {root_cause}")

    # Run the test with the fix applied
    run_started_at = time.time()
    passed, test_output = run_maven_test(test_class, test_method)
    failure_ctx = {}
    if not passed:
        failure_ctx = build_failure_context(test_class, test_method, test_output, run_started_at)

    stuck_on_same_failure = bool(
        not passed and fixes_applied and prev_failure_location
        and failure_ctx.get("failure_location") == prev_failure_location
    )

    if passed:
        log(f"Test PASSED after fix attempt {FIX_ATTEMPT}")
        _write_gate("true")
    elif stuck_on_same_failure:
        log(f"Test still FAILED after fix attempt {FIX_ATTEMPT} — at the EXACT SAME location "
            f"as before the fix ({prev_failure_location}). The applied fix had no effect on "
            f"the actual failure point — stopping the fix loop rather than burning the "
            f"remaining attempts on a diagnosis that isn't converging.")
        # Distinct gate value from "skipped" — this test genuinely ran and genuinely
        # failed (unlike a real infra skip, where it never got a fair shot), so it
        # must NOT be treated as APPROVED/"not run" downstream in 05_ship.py the way
        # "skipped" is. run.sh stops the retry loop on "stuck" exactly like "skipped".
        _write_gate("stuck")
    else:
        log(f"Test still FAILED after fix attempt {FIX_ATTEMPT}")
        _write_gate("false")

    result_data = {
        "attempt": FIX_ATTEMPT,
        "test_class": test_class,
        "test_method": test_method,
        "passed": passed,
        "test_output": test_output,
        "fixes_applied": fixes_applied,
        "fix_response_length": len(fix_response),
        "root_cause": root_cause,
        "confidence": confidence,
        **({"stuck": True, "reason": "stuck on identical failure across fix attempts — "
            "see root_cause history in the per-attempt audit files"} if stuck_on_same_failure else {}),
        **failure_ctx,
    }

    _write_result(result_data, files_written, FIX_ATTEMPT)

    # Per-attempt audit file — includes fix file contents for per-step git commits in ship step
    attempt_data = {**result_data, "fix_file_contents": fix_contents}
    (AUDIT_DIR / f"04-run-and-fix-attempt-{FIX_ATTEMPT}.json").write_text(
        json.dumps(attempt_data, indent=2)
    )


def _write_gate(value: str) -> None:
    (AUDIT_DIR / ".fix-passed").write_text(value)


def _write_result(data: dict, files_written: list, attempt: int) -> None:
    (AUDIT_DIR / "04-run-and-fix.json").write_text(json.dumps(data, indent=2))

    passed = data.get("passed", False)
    skipped = data.get("skipped", False)
    stuck = data.get("stuck", False)
    fixes = data.get("fixes_applied", [])

    result_label = "PASSED" if passed else "SKIPPED" if skipped else "STUCK" if stuck else "FAILED"
    lines = [
        "# Run & Fix Results",
        "",
        f"Attempt:  {attempt}",
        f"Result:   {result_label}",
    ]
    if data.get("reason"):
        lines.append(f"Reason:   {data['reason']}")
    if not skipped:
        lines += [
            f"Class:    {data.get('test_class')}",
            f"Method:   {data.get('test_method')}",
        ]
    if data.get("failure_location"):
        lines.append(f"Failure:  {data['failure_location']}")
    if data.get("screenshot_path"):
        lines.append(f"Screenshot: {data['screenshot_path']}")
    if data.get("root_cause"):
        lines += ["", "## Root Cause", f"({data.get('confidence', 'unknown')} confidence) {data['root_cause']}"]
    if fixes:
        lines += ["", "## Files Fixed", ] + [f"- `{f}`" for f in fixes]
    (AUDIT_DIR / "04-run-and-fix.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
