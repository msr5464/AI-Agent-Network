#!/usr/bin/env python3
"""
Step 04 — Run and Fix
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
from datetime import datetime
from pathlib import Path

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

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("04-run-and-fix", msg)

from shared.claude import call_claude as _call_claude
def call_claude(prompt: str) -> str:
    output = _call_claude(prompt, MODEL, str(REPO_ROOT))
    if not output:
        log("ERROR: Claude CLI returned empty response")
    return output


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
        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()

    reader.join(timeout=5)

    if timed_out:
        log("ERROR: mvn test timed out (300s)")
        return False, "\n".join(all_lines) + "\nERROR: Maven test timed out after 300 seconds."

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


def _log_failure_summary(output: str) -> None:
    """Extract and log the most actionable failure lines from Maven/TestNG output."""
    summary = []
    capture_call_log = False
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
            capture_call_log = False
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
        log("Failure summary:")
        for ln in summary[:10]:
            log(f"  {ln}")
    else:
        # Fallback: show last few [ERROR] lines
        error_lines = [l.strip() for l in output.splitlines() if "[ERROR]" in l][-5:]
        if error_lines:
            log("Last errors:")
            for ln in error_lines:
                log(f"  {ln}")


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
        passed, test_output = run_maven_test(test_class, test_method)
        if not passed:
            _log_failure_summary(test_output)
            log("Initial test FAILED — fix attempt 1 will apply a Claude fix and re-run")
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
        }, files_written, 0)
        return

    # ── FIX_ATTEMPT >= 1 — classify previous failure → fix → run ─────────────
    # Load the failure output written by the previous attempt
    prev_output = ""
    prev_fix_path = AUDIT_DIR / "04-run-and-fix.json"
    if prev_fix_path.exists():
        try:
            prev = json.loads(prev_fix_path.read_text())
            prev_output = prev.get("test_output", "")[-3000:]
            log(f"Fix attempt {FIX_ATTEMPT}/{MAX_ATTEMPTS} — loaded previous failure ({len(prev_output)} chars)")
        except Exception:
            pass

    failure_class = classify_failure(prev_output)
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
            failure_class = classify_failure(test_output)

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
            failure_class = classify_failure(test_output)

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

    retry_section = ""
    if FIX_ATTEMPT > 1:
        retry_section = f"""
## ⚠️ RETRY — Fix attempt {FIX_ATTEMPT}
Previous fix did not resolve the test. Previous failure:
```
{prev_output}
```
Try a DIFFERENT approach — do NOT repeat what was tried before.
"""

    prompt = f"""You are a Java test automation debugging agent for the Jarvis framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<generated_files>
{files_context}
</generated_files>

<test_failure>
```
{prev_output}
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
- Auth not set — ensure setAuthToken(token) is called on the helper, or doLogin() for web tests

CRITICAL: Preserve ALL existing JavaDoc comments, inline comments, and annotations exactly as written.
Only change the minimum code required to fix the failure. Do NOT remove, shorten, or reword any comments.

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
    fix_contents: dict = {}
    if fix_map:
        fixes_applied, fix_contents = apply_fix(fix_map)
        log(f"Applied fixes to {len(fixes_applied)} file(s) — running test")
    else:
        log("WARNING: Claude did not return a valid fix map — running test without fix")

    # Run the test with the fix applied
    passed, test_output = run_maven_test(test_class, test_method)
    if not passed:
        _log_failure_summary(test_output)

    if passed:
        log(f"Test PASSED after fix attempt {FIX_ATTEMPT}")
        _write_gate("true")
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
