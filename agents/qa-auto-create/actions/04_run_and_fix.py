#!/usr/bin/env python3
"""
Step 04 — Run and Fix
Runs the generated test via mvn test, captures output, and if the test fails
calls Claude with the error context to generate a fix.

run.sh re-invokes this script on each retry (FIX_ATTEMPT env var increments).
On retry, the previous test failure output is injected into the Claude prompt.

Reads:  $AUDIT_DIR/03-generate.json
        $AUDIT_DIR/04-run-and-fix.json  (on retries — previous attempt context)
Writes: $AUDIT_DIR/04-run-and-fix.json
        $AUDIT_DIR/04-run-and-fix.md
        $AUDIT_DIR/.fix-passed          gate: true / false / skipped
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR    = Path(os.environ["AUDIT_DIR"])
AGENT_DIR    = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT    = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
THANOS_PW_DIR    = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Thanos-pw")

CLAUDE_CLI   = os.environ.get("CLAUDE_CLI_PATH", "claude")
MODEL        = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
ENVIRONMENT  = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging")
COUNTRY      = os.environ.get("AUTOCREATE_COUNTRY", "SG")
FIX_ATTEMPT  = int(os.environ.get("FIX_ATTEMPT", "1"))
MAX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [04-run-and-fix] {msg}", flush=True)


def call_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt, "--model", MODEL],
            capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT)
        )
    except subprocess.TimeoutExpired:
        log("ERROR: Claude CLI timed out (300s)")
        return ""
    if result.returncode != 0:
        log(f"Claude error: {result.stderr[:400]}")
        return ""
    return result.stdout


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
    """Run a single test via mvn. Returns (passed: bool, output: str)."""
    if test_method:
        test_arg = f"{test_class}#{test_method}"
    else:
        test_arg = test_class

    cmd = [
        "mvn", "test",
        f"-Dtest={test_arg}",
        f"-Denvironment={ENVIRONMENT}",
        f"-Dcountry={COUNTRY}",
        "-Dheadless=true",
        "--no-transfer-progress",
    ]
    log(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=300,
            cwd=str(THANOS_PW_DIR)
        )
        output = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0
        log(f"Test exit code: {result.returncode} ({'PASS' if passed else 'FAIL'})")
        return passed, output[-4000:]
    except subprocess.TimeoutExpired:
        log("ERROR: mvn test timed out (300s)")
        return False, "ERROR: Maven test timed out after 300 seconds."
    except FileNotFoundError:
        log("ERROR: mvn not found in PATH")
        return False, "ERROR: mvn command not found. Is Maven installed and in PATH?"


def read_generated_files(files_written: list) -> dict:
    """Read the content of all generated Java files."""
    contents = {}
    for rel_path in files_written:
        full = THANOS_PW_DIR / rel_path
        if full.exists():
            try:
                contents[rel_path] = full.read_text()
            except Exception:
                pass
    return contents


def apply_fix(files_map: dict) -> list:
    """Write Claude's fixed file contents back to Thanos-pw. Returns list of patched files."""
    patched = []
    for rel_path, content in files_map.items():
        if not content or not content.strip():
            continue
        full = THANOS_PW_DIR / rel_path
        # Safety: only write inside Thanos-pw
        try:
            full.resolve().relative_to(THANOS_PW_DIR.resolve())
        except ValueError:
            log(f"  BLOCKED: {rel_path} escapes repo root")
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        patched.append(rel_path)
        log(f"  Fixed: {rel_path}")
    return patched


# ── Infrastructure helpers ────────────────────────────────────────────────────

def classify_failure(output: str) -> str:
    """Returns 'INFRA_BUILD' | 'INFRA_DB' | 'INFRA_USER' | 'CODE_ERROR'."""
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
    for signal in infra_build_signals:
        if signal in output:
            return "INFRA_BUILD"
    for signal in infra_db_signals:
        if signal in output:
            return "INFRA_DB"
    for signal in infra_user_signals:
        if signal in output:
            return "INFRA_USER"
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

    sys_props = THANOS_PW_DIR / "parameters" / "system.properties"
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
        "# Auto-configured by qa-auto-create",
        "db.thanos.url=jdbc:mysql://localhost:3306/thanos",
        "db.thanos.username=root",
        "db.thanos.password=",
    ]
    sys_props.write_text("\n".join(lines) + "\n")
    log("DB auto-repair: wrote local MySQL config to system.properties")
    return True


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
    username     = creds["username"].replace("'", "\\'")
    password     = creds["password"].replace("'", "\\'")
    otp          = creds.get("otp", "").replace("'", "\\'")

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    gen_data = json.loads((AUDIT_DIR / "03-generate.json").read_text())
    files_written = gen_data.get("files_written", [])
    test_class    = gen_data.get("test_class", "")
    test_method   = gen_data.get("test_method", "")
    plan_data     = json.loads((AUDIT_DIR / "01-parse.json").read_text())

    # Load previous attempt context if retrying
    prev_output = ""
    prev_fix_path = AUDIT_DIR / "04-run-and-fix.json"
    if FIX_ATTEMPT > 1 and prev_fix_path.exists():
        try:
            prev = json.loads(prev_fix_path.read_text())
            prev_output = prev.get("test_output", "")[-3000:]
            log(f"Retry attempt {FIX_ATTEMPT} — loaded previous failure output")
        except Exception:
            pass

    if not test_class:
        log("No test class found in generate output — skipping test run")
        _write_gate("skipped")
        _write_result({"skipped": True, "reason": "no test class"}, [], FIX_ATTEMPT)
        return

    if not THANOS_PW_DIR.exists():
        log(f"ERROR: Thanos-pw directory not found: {THANOS_PW_DIR}")
        _write_gate("skipped")
        _write_result({"skipped": True, "reason": f"Thanos-pw not found at {THANOS_PW_DIR}"}, [], FIX_ATTEMPT)
        return

    log(f"Attempt {FIX_ATTEMPT}/{MAX_ATTEMPTS}: Running {test_class}#{test_method}")
    passed, test_output = run_maven_test(test_class, test_method)

    if passed:
        log("Test PASSED")
        _write_gate("true")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": True,
            "test_output": test_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    # Classify the failure before spending Claude API quota on code fixes
    failure_class = classify_failure(test_output)
    log(f"Failure classified as: {failure_class}")

    if failure_class == "INFRA_DB":
        log("Detected DB infrastructure error — attempting auto-repair")
        if try_fix_infra_db():
            log("DB auto-configured — retrying test once")
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
            failure_class = classify_failure(test_output)

    if failure_class == "INFRA_USER":
        log("Detected user pool error — attempting demo user auto-insert")
        if try_fix_infra_user(plan_data):
            log("Demo user inserted/reset — retrying test once")
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
            failure_class = classify_failure(test_output)

    if failure_class == "INFRA_BUILD":
        log(f"ERROR: Maven project not found — check WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")
        log(f"Expected pom.xml at: {THANOS_PW_DIR}")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Maven project not found at {THANOS_PW_DIR} — ensure WORKSPACE_DIR and GITHUB_REPO_AUTOMATION point to a valid Maven project",
            "test_output": test_output,
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
            "test_output": test_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    # CODE_ERROR — call Claude for a fix
    log("Test FAILED — calling Claude for fix...")
    generated_files = read_generated_files(files_written)
    claude_md = (AGENT_DIR / "CLAUDE.md").read_text() if (AGENT_DIR / "CLAUDE.md").exists() else ""

    files_context = "\n".join(
        f"\n--- {path} ---\n{content}\n" for path, content in generated_files.items()
    )

    retry_section = ""
    if prev_output:
        retry_section = f"""
## ⚠️ RETRY — Attempt {FIX_ATTEMPT}
Previous fix did not resolve the test. Previous failure:
```
{prev_output}
```
Try a DIFFERENT approach — do NOT repeat what was tried before.
"""

    prompt = f"""You are a Java test automation debugging agent for the Thanos-pw framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<generated_files>
{files_context}
</generated_files>

<test_failure>
```
{test_output}
```
</test_failure>
{retry_section}

The test failed. Analyze the failure and return a JSON object with fixed file contents.
Only include files that need to change.

Common failure causes:
- Import statements missing or wrong package names
- Locator not found — fix the selector or add a fallback
- Method not found — check the framework API (use BasePage/Element/WaitHelper wrappers)
- Compilation error — fix the Java syntax
- User not allocated — check allocateUser() call matches feature enum
- Auth not set — ensure loginAndSetAuth() or doLogin() is called before API calls

Return ONLY a JSON object:
{{
  "src/test/java/automation/{plan_data.get('feature_name', 'feature')}/{{}}.java": "...fixed content...",
  "src/main/java/automation/modules/...": "...fixed content..."
}}

Include the COMPLETE file content (not just the changed lines). Output ONLY valid JSON.
"""

    fix_response = call_claude(prompt)
    fix_map = extract_json(fix_response)

    fixes_applied = []
    if fix_map:
        fixes_applied = apply_fix(fix_map)
        log(f"Applied fixes to {len(fixes_applied)} file(s)")
    else:
        log("WARNING: Claude did not return a valid fix map")

    _write_gate("false")  # Signal to run.sh to retry (if attempts remain)
    _write_result({
        "attempt": FIX_ATTEMPT,
        "test_class": test_class,
        "test_method": test_method,
        "passed": False,
        "test_output": test_output,
        "fixes_applied": fixes_applied,
        "fix_response_length": len(fix_response),
    }, files_written, FIX_ATTEMPT)


def _write_gate(value: str) -> None:
    (AUDIT_DIR / ".fix-passed").write_text(value)


def _write_result(data: dict, files_written: list, attempt: int) -> None:
    (AUDIT_DIR / "04-run-and-fix.json").write_text(json.dumps(data, indent=2))

    passed = data.get("passed", False)
    skipped = data.get("skipped", False)
    fixes = data.get("fixes_applied", [])

    lines = [
        "# Run and Fix Results",
        "",
        f"Attempt:  {attempt}",
        f"Result:   {'PASSED' if passed else ('SKIPPED' if skipped else 'FAILED')}",
    ]
    if not skipped:
        lines += [
            f"Class:    {data.get('test_class')}",
            f"Method:   {data.get('test_method')}",
        ]
    if fixes:
        lines += ["", "## Files Fixed", ] + [f"- `{f}`" for f in fixes]
    (AUDIT_DIR / "04-run-and-fix.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
