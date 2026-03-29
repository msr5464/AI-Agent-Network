#!/usr/bin/env python3
"""
Step 01 — Fix
For each AUTOMATION_ISSUE (HIGH confidence, ELEMENT_NOT_FOUND) in the handoff file:
  1. Build rich context  — extract method, element names, page object files, likely location
  2. Generate fix prompt — targeted context: method + page objects + element names + conventions
  3. Call Claude CLI     — get corrected file content
  4. Apply + verify      — write file, run test, restore on failure
  5. Commit all successes to a branch

Reads: agents/qa-auto-fix/queue/<build_tag>.json  (written by qa-auto-analyse/05_ship.py)
Outputs: audit/<session>/01-fix.json + 01-fix.md + .fix-passed

Gate file: .fix-passed
  - "true"    — all targeted fixes applied and tests pass
  - "false"   — one or more fixes failed tests (triggers retry loop in run.sh)
  - "skipped" — no eligible candidates or infrastructure not configured
"""

import os, sys, json, subprocess, re, difflib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from log_utils import log as _log
def log(msg): _log("fix", msg)

# CodeAnalyzer import — graceful fallback if not available
try:
    from src.auto_fix.code_analyzer import CodeAnalyzer as _CodeAnalyzer
    _HAS_CODE_ANALYZER = True
except ImportError:
    _HAS_CODE_ANALYZER = False
    log("Warning: CodeAnalyzer not available — falling back to glob-only file search")

import warnings, urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
import logging
logging.basicConfig(level=logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR   = Path(os.environ["AUDIT_DIR"])
AGENT_DIR   = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT   = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))
FIX_ATTEMPT = int(os.environ.get("FIX_ATTEMPT", "1"))

# Handoff file written by qa-auto-analyse/05_ship.py
HANDOFF_FILE = Path(os.environ["HANDOFF_FILE"])

CLAUDE_CLI   = os.environ.get("CLAUDE_CLI_PATH", "claude")
AUTOFIX_MODEL = os.environ.get("AUTOFIX_MODEL",
                    os.environ.get("CLASSIFIER_MODEL", "claude-opus-4-6"))

GITHUB_TOKEN           = os.environ.get("GITHUB_TOKEN", "")
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
AUTOFIX_BRANCH_PREFIX  = os.environ.get("AUTOFIX_BRANCH_PREFIX", "chore/qa-autofix")

KNOWN_ISSUES_FILE = AGENT_DIR / "feedback" / "known-issues.json"
REPO_CONTEXT_FILE = os.environ.get("REPO_CONTEXT_FILE", "")
MAX_FIXES         = int(os.environ.get("AUTO_FIX_MAX_FIXES_PER_RUN", "5"))
MAX_LOG_CHARS     = 3000
MAX_METHOD_CHARS  = 4000
MAX_PAGE_OBJ_CHARS   = 2000
MAX_BASE_CLASS_CHARS = 3000

# ── I/O helpers ───────────────────────────────────────────────────────────────

def write_gate(value: str):
    (AUDIT_DIR / ".fix-passed").write_text(value)


def load_known_issues() -> list:
    if not KNOWN_ISSUES_FILE.exists():
        return []
    try:
        return json.loads(KNOWN_ISSUES_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return []


def is_known_issue(test_name: str, known_issues: list) -> bool:
    for entry in known_issues:
        pattern = entry.get("pattern", "")
        if pattern and re.search(pattern, test_name, re.IGNORECASE):
            return True
    return False


def call_claude(prompt: str, cwd: Path) -> str:
    result = subprocess.run(
        [CLAUDE_CLI, "-p", prompt, "--model", AUTOFIX_MODEL],
        capture_output=True, text=True, timeout=300, cwd=str(cwd),
    )
    if result.returncode != 0:
        log(f"Claude CLI error (exit {result.returncode}): {result.stderr[:400]}")
        return ""
    return result.stdout


def run_git(args: list, cwd: Path):
    result = subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, timeout=60
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def clone_automation_repo(workspace: Path) -> Path | None:
    """Clone the automation repo into workspace/ if GITHUB_ORG + GITHUB_REPO_AUTOMATION are set."""
    if not GITHUB_ORG or not GITHUB_REPO_AUTOMATION:
        log("Cannot clone: GITHUB_ORG or GITHUB_REPO_AUTOMATION not set")
        return None
    if not GITHUB_TOKEN:
        log("Cannot clone: GITHUB_TOKEN not set")
        return None

    dest = workspace / GITHUB_REPO_AUTOMATION
    clone_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}.git"
    log(f"Cloning {GITHUB_ORG}/{GITHUB_REPO_AUTOMATION} into {workspace}...")
    workspace.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", GITHUB_DEFAULT_BRANCH, clone_url, str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        # Redact token from error output before logging
        err = result.stderr.replace(GITHUB_TOKEN, "***")
        log(f"Clone failed: {err[:400]}")
        return None
    log(f"Cloned successfully → {dest}")
    return dest


def get_workspace() -> Path | None:
    workspace_dir = os.environ.get("WORKSPACE_DIR", "")
    if not workspace_dir:
        # Fallback: parent of QA-AI-Agent repo (automation repo should be a sibling)
        workspace_dir = str(REPO_ROOT.parent)
        log(f"Warning: WORKSPACE_DIR not set — defaulting to {workspace_dir}")
    workspace = Path(workspace_dir)

    if GITHUB_REPO_AUTOMATION:
        p = workspace / GITHUB_REPO_AUTOMATION
        if p.exists():
            return p
        # Repo not present — clone it
        log(f"{GITHUB_REPO_AUTOMATION} not found in {workspace} — attempting clone")
        return clone_automation_repo(workspace)

    # No repo name set — scan for any sibling repo with a src/ directory
    for candidate in workspace.iterdir():
        # Skip QA-AI-Agent itself — the automation repo must be separate
        if not candidate.is_dir() or candidate.resolve() == REPO_ROOT.resolve():
            continue
        if (candidate / "src").exists():
            log(f"Warning: auto-detected workspace {candidate} — set WORKSPACE_DIR + GITHUB_REPO_AUTOMATION to avoid this")
            return candidate
    return None

# ── Likely location extractor ─────────────────────────────────────────────────

def extract_likely_location(stack_trace: str, execution_log: str) -> str:
    combined = f"{stack_trace or ''}\n{execution_log or ''}"
    matches = re.findall(r'([\w$]+\.java):(\d+)', combined)
    if matches:
        for fname, line in matches:
            if any(k in fname.lower() for k in ["test", "page", "automation", "spec"]):
                return f"{fname}:{line}"
        return f"{matches[0][0]}:{matches[0][1]}"
    fq_match = re.search(r'at\s+([\w.]+)\((\w+\.java):(\d+)\)', combined)
    if fq_match:
        return f"{fq_match.group(2)}:{fq_match.group(3)}"
    return ""

# ── Base class extractor ──────────────────────────────────────────────────────

def extract_base_class_api(file_path: str, workspace: Path) -> dict:
    if not file_path or not Path(file_path).exists():
        return {}
    try:
        content = Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return {}

    extends_match = re.search(r'\bextends\s+([\w]+)', content)
    if not extends_match:
        return {}

    base_name = extends_match.group(1)
    skip_bases = {"Object", "Thread", "Enum", "AbstractTest", "TestCase", "Assert"}
    if base_name in skip_bases:
        return {}

    base_file = None
    for candidate in workspace.rglob(f"{base_name}.java"):
        base_file = candidate
        break

    if not base_file or not base_file.exists():
        return {}

    try:
        base_content = base_file.read_text(encoding="utf-8")
    except Exception:
        return {}

    sig_pattern = re.compile(
        r'public\s+(?:static\s+)?(?:final\s+)?(?:[\w<>\[\],\s]+?)\s+(\w+)\s*\(([^)]*)\)',
        re.MULTILINE,
    )
    sigs = []
    for m in sig_pattern.finditer(base_content):
        return_and_name = m.group(0).split("(")[0].strip()
        params = m.group(2).strip()
        sigs.append(f"{return_and_name}({params})")

    sigs_text = "\n".join(sigs)
    if len(sigs_text) > MAX_BASE_CLASS_CHARS:
        sigs_text = sigs_text[:MAX_BASE_CLASS_CHARS] + "\n... (truncated)"

    return {
        "base_class_name": base_name,
        "base_class_file": str(base_file.relative_to(workspace)),
        "public_methods":  sigs_text,
    }


def load_repo_conventions(workspace: Path) -> str:
    candidates = []
    if REPO_CONTEXT_FILE:
        p = Path(REPO_CONTEXT_FILE)
        if not p.is_absolute():
            p = workspace / REPO_CONTEXT_FILE
        candidates.append(p)
    candidates += [
        workspace / "CONVENTIONS.md",          # conventions in the automation repo itself
        workspace / "docs" / "TESTING.md",
        workspace / "TESTING.md",
        workspace / "CONTRIBUTING.md",
        AGENT_DIR / "CONVENTIONS.md",          # fallback: bundled conventions inside this agent
    ]
    for path in candidates:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                log(f"Loaded repo conventions from {path} ({len(content)} chars)")
                return content[:16000]
            except Exception:
                continue
    log("Warning: no conventions file found — fixes will use Claude's defaults")
    return ""

# ── Context builder ───────────────────────────────────────────────────────────

def build_candidate_context(issue: dict, workspace: Path, prev_test_output: str,
                             repo_conventions: str = "") -> dict:
    test_name  = issue["test_name"]
    parts      = test_name.split(".")
    class_name = parts[-2] if len(parts) >= 2 else test_name
    method_name = parts[-1] if len(parts) >= 2 else ""

    error_type    = issue.get("error_type", "")
    error_message = issue.get("error_message") or ""
    stack_trace   = issue.get("stack_trace") or ""
    execution_log = issue.get("execution_log") or ""

    likely_location = extract_likely_location(stack_trace, execution_log)

    # Find test file
    test_file = None
    if _HAS_CODE_ANALYZER:
        try:
            test_file = _CodeAnalyzer().find_test_file(test_name, str(workspace))
            if test_file:
                abs_path = workspace / test_file
                test_file = str(abs_path) if abs_path.exists() else test_file
        except Exception as e:
            log(f"  CodeAnalyzer.find_test_file failed ({e}) — falling back to glob")
    if not test_file or not Path(test_file).exists():
        for ext in ("java", "kt", "ts", "tsx"):
            for found in workspace.rglob(f"{class_name}.{ext}"):
                test_file = str(found)
                break
            if test_file:
                break

    # Extract test method code
    test_method_code = ""
    if test_file and Path(test_file).exists() and _HAS_CODE_ANALYZER:
        try:
            method_code = _CodeAnalyzer().extract_test_method(test_file, method_name)
            if method_code:
                test_method_code = method_code[:MAX_METHOD_CHARS]
        except Exception as e:
            log(f"  extract_test_method failed: {e}")

    # Extract element names
    element_names = []
    if _HAS_CODE_ANALYZER:
        try:
            element_names = _CodeAnalyzer().extract_element_names(
                root_cause=issue.get("root_cause", ""),
                execution_log=execution_log[:MAX_LOG_CHARS],
                category=issue.get("root_cause_category", ""),
            )
        except Exception as e:
            log(f"  extract_element_names failed: {e}")

    # Find page object files
    page_objects = []
    if element_names and _HAS_CODE_ANALYZER:
        try:
            page_objects = _CodeAnalyzer().find_page_objects_for_locators(
                repo_path=str(workspace),
                element_names=element_names,
                max_files=3,
                max_chars_per_file=MAX_PAGE_OBJ_CHARS,
            )
        except Exception as e:
            log(f"  find_page_objects_for_locators failed: {e}")

    # Related files from imports
    related_files = []
    if test_file and Path(test_file).exists() and _HAS_CODE_ANALYZER:
        try:
            related_files = _CodeAnalyzer().get_related_files(
                repo_path=str(workspace),
                file_path=test_file,
                max_files=2,
                max_chars=1200,
            )
        except Exception as e:
            log(f"  get_related_files failed: {e}")

    # Base class API — check page objects first (they extend BasePage)
    base_class_info: dict = {}
    files_to_check = [po["path"] for po in page_objects] + ([test_file] if test_file else [])
    for f in files_to_check:
        if not f:
            continue
        resolved = Path(f) if Path(f).is_absolute() else workspace / f
        info = extract_base_class_api(str(resolved), workspace)
        if info:
            base_class_info = info
            log(f"  Base class: {info['base_class_name']} ({info['base_class_file']})")
            break

    return {
        "test_name":     test_name,
        "class_name":    class_name,
        "method_name":   method_name,
        "classification": issue.get("classification", ""),
        "confidence":    issue.get("confidence", ""),
        "root_cause_category": issue.get("root_cause_category", ""),
        "root_cause":    issue.get("root_cause", ""),
        "failure_signature": issue.get("failure_signature", ""),
        "recommended_action": issue.get("recommended_action", ""),
        "error_type":    error_type,
        "error_message": error_message[:500],
        "stack_trace":   stack_trace[:800],
        "execution_log": execution_log[:MAX_LOG_CHARS],
        "likely_location": likely_location,
        "test_file":     test_file or "",
        "test_method_code": test_method_code,
        "element_names": element_names,
        "page_objects":  page_objects,
        "related_files": related_files,
        "base_class_info": base_class_info,
        "repo_conventions": repo_conventions,
        "prev_test_output": prev_test_output[:1500] if prev_test_output else "",
        "fix_attempt":   FIX_ATTEMPT,
    }

# ── Prompt builder ────────────────────────────────────────────────────────────

def build_fix_prompt(ctx: dict) -> str:
    page_obj_text = ""
    for po in ctx["page_objects"]:
        page_obj_text += f"\n### Page Object: {po['path']}\n"
        page_obj_text += f"Elements matched: {', '.join(po['element_matches'])}\n"
        page_obj_text += f"```java\n{po['snippet']}\n```\n"

    related_text = ""
    for rf in ctx["related_files"]:
        related_text += f"\n### Related: {rf['path']}\n```java\n{rf['snippet']}\n```\n"

    base_class_text = ""
    bc = ctx.get("base_class_info", {})
    if bc:
        base_class_text = f"""
## Project Base Class: {bc['base_class_name']} ({bc['base_class_file']})
These are the PUBLIC wrapper methods available from the base class.
**Use these wrappers instead of raw Selenium/RestAssured calls.**
```
{bc['public_methods']}
```
"""

    conventions_text = ""
    if ctx.get("repo_conventions"):
        conventions_text = f"""
---
## PROJECT CONVENTIONS — read before writing any code
These rules apply to every line you write or modify.

{ctx['repo_conventions']}
---
"""

    code_section = ""
    if ctx["test_method_code"]:
        code_section = f"### Failing Test Method ({ctx['method_name']})\n```java\n{ctx['test_method_code']}\n```"
    elif ctx["test_file"]:
        try:
            content = Path(ctx["test_file"]).read_text(encoding="utf-8")
            code_section = f"### Full Test File (method not extracted)\n```java\n{content[:5000]}\n```"
        except Exception:
            code_section = "### Test File\n(could not read)"

    retry_text = ""
    if ctx["prev_test_output"]:
        retry_text = f"""
## ⚠️ RETRY — Attempt {ctx['fix_attempt']}
Previous fix did not resolve the test. Different test output:
```
{ctx['prev_test_output']}
```
Try a different locator strategy — do NOT repeat the previous approach.
"""

    return f"""You are fixing a broken locator in a Selenium/RestAssured test automation file.
Work independently on this test case only.
{conventions_text}{base_class_text}
## Test Case
- **Full Name:** {ctx['test_name']}
- **Class:** {ctx['class_name']}
- **Method:** {ctx['method_name']}
- **File:** {ctx['test_file']}

## Failure Information
- **Classification:** {ctx['classification']} ({ctx['confidence']} confidence)
- **Root Cause Category:** {ctx['root_cause_category']}
- **Root Cause:** {ctx['root_cause']}
- **Failure Signature:** {ctx['failure_signature']}
- **Likely Location:** {ctx['likely_location']}
- **Error Type:** {ctx['error_type']}
- **Error Message:** {ctx['error_message']}
- **Recommended Action:** {ctx['recommended_action']}

## Extracted Element Names
{chr(10).join(f"- {e}" for e in ctx['element_names']) if ctx['element_names'] else "- (none extracted)"}

## Execution Log (truncated)
```
{ctx['execution_log']}
```

## Stack Trace (truncated)
```
{ctx['stack_trace']}
```
{retry_text}
## Code to Fix

{code_section}
{page_obj_text}
{related_text}

## Instructions
1. Identify the EXACT broken locator (CSS selector, XPath, @FindBy, etc.)
2. The broken element is most likely one of the extracted element names above
3. Look in the page object files above for the @FindBy annotation that needs updating
4. If the fix is in a page object file (not the test file), target the page object
5. **IMPORTANT**: Use the wrapper methods from the base class — do NOT use raw Selenium/RestAssured
6. **IMPORTANT**: Follow the project conventions shown above
7. Do not refactor, rename, or change anything unrelated to the broken locator

## Output Format (strict)
Respond with a JSON object ONLY. No prose, no markdown fences around it.

{{
  "fixable": true | false,
  "unfixable_reason": "<reason if fixable=false, else null>",
  "fix_description": "<1-2 sentences: what was broken and what you changed>",
  "target_file": "<absolute path of the file to modify>",
  "fixed_content": "<complete corrected file content, or null if fixable=false>"
}}
"""


def extract_fix_json(response: str) -> dict | None:
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass
    m = re.search(r"```json\s*([\s\S]*?)\s*```", response)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\{[\s\S]*\})", response)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None

# ── Test runner ───────────────────────────────────────────────────────────────

def run_single_test(test_name: str, workspace: Path) -> tuple:
    parts = test_name.split(".")
    class_part  = parts[-2] if len(parts) >= 2 else test_name
    method_part = parts[-1] if len(parts) >= 2 else ""

    test_runner_cmd = os.environ.get("TEST_RUNNER_CMD", "")
    if test_runner_cmd:
        full_class = ".".join(parts[:-1]) if len(parts) >= 2 else test_name
        expanded = (test_runner_cmd
                    .replace("{test_name}", test_name)
                    .replace("{class}", full_class)
                    .replace("{class_simple}", class_part)
                    .replace("{method}", method_part))
        cmd = expanded.split()
    elif (workspace / "gradlew").exists():
        cmd = ["./gradlew", "test", "--tests", f"*.{class_part}.{method_part}", "-q",
               "--rerun-tasks"]
    elif (workspace / "build.gradle").exists() or (workspace / "build.gradle.kts").exists():
        cmd = ["gradle", "test", "--tests", f"*.{class_part}.{method_part}", "-q"]
    elif (workspace / "pom.xml").exists():
        cmd = ["mvn", "test", f"-Dtest={class_part}#{method_part}", "-q",
               "--no-transfer-progress"]
    elif (workspace / "package.json").exists():
        cmd = ["npx", "playwright", "test", "--grep", method_part, "-x"]
    else:
        return True, "No test runner detected — fix applied but not auto-verified"

    try:
        r = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=300)
        output = (r.stdout + r.stderr)[-3000:]
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Test timed out after 300s"
    except Exception as e:
        return False, f"Test runner error: {e}"


def compute_diff(original: str, fixed: str, filename: str) -> str:
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=3,
    ))
    return "".join(diff_lines[:100])

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load handoff
    if not HANDOFF_FILE.exists():
        log(f"ERROR: Handoff file not found: {HANDOFF_FILE}")
        sys.exit(1)

    handoff = json.loads(HANDOFF_FILE.read_text())
    build_tag = handoff.get("build_tag", "unknown")
    issues    = handoff.get("automation_issues", [])

    log(f"Build tag: {build_tag}")
    log(f"Issues in handoff: {len(issues)}")

    # Filter out known issues
    known_issues = load_known_issues()
    if known_issues:
        log(f"Loaded {len(known_issues)} known-issue patterns to skip")

    eligible = [i for i in issues if not is_known_issue(i["test_name"], known_issues)]
    if len(eligible) != len(issues):
        log(f"Skipped {len(issues) - len(eligible)} known issues")

    def write_skipped(reason: str):
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "timestamp": ts, "build_tag": build_tag, "fix_attempt": FIX_ATTEMPT,
            "eligible_count": len(eligible), "skipped_reason": reason,
            "attempted": 0, "succeeded": 0, "failed": 0,
            "candidates": [], "fixes": [], "failed_fixes": [],
        }
        (AUDIT_DIR / "01-fix.json").write_text(json.dumps(result, indent=2))
        (AUDIT_DIR / "01-fix.md").write_text(f"# Fix\n\nSkipped — {reason}.\n")
        write_gate("skipped")
        log(f"Gate: .fix-passed = skipped ({reason})")

    if not eligible:
        write_skipped("no eligible issues in handoff")
        return

    eligible = eligible[:MAX_FIXES]
    log(f"{len(eligible)} eligible issues (capped at MAX_FIXES={MAX_FIXES})")

    if not GITHUB_TOKEN or not GITHUB_REPO_AUTOMATION:
        write_skipped("GitHub config not set (GITHUB_TOKEN / GITHUB_REPO_AUTOMATION)")
        return

    workspace = get_workspace()
    if not workspace:
        write_skipped("automation repo workspace not found")
        return

    log(f"Workspace: {workspace}")

    # Load repo conventions once
    repo_conventions = load_repo_conventions(workspace)

    # Load previous fix failures for retry context
    prev_test_outputs: dict = {}
    if FIX_ATTEMPT > 1:
        prev_path = AUDIT_DIR / "01-fix.json"
        if prev_path.exists():
            prev_data = json.loads(prev_path.read_text())
            for fix in prev_data.get("failed_fixes", []):
                prev_test_outputs[fix["test_name"]] = fix.get("test_output", "")

    # Create / checkout fix branch
    # Branch name: <AUTOFIX_BRANCH_PREFIX>/<safe-build-tag>
    # On retry (FIX_ATTEMPT > 1), reuse the same branch so commits stack
    safe_tag    = re.sub(r"[^a-zA-Z0-9_-]", "-", build_tag).lower()
    branch_name = f"{AUTOFIX_BRANCH_PREFIX}/{safe_tag}"
    ok, _, err  = run_git(["fetch", "origin"], workspace)
    if ok:
        # FIX_ATTEMPT 1: reset to origin base so we always start clean
        # FIX_ATTEMPT > 1: reuse the existing branch (fixes accumulate across retries)
        if FIX_ATTEMPT <= 1:
            run_git(["checkout", "-B", branch_name, f"origin/{GITHUB_DEFAULT_BRANCH}"], workspace)
        else:
            # Branch should already exist from attempt 1; just check it out
            ret, _, _ = run_git(["checkout", branch_name], workspace)
            if not ret:
                # Branch doesn't exist yet (e.g. first attempt committed nothing) — create it
                run_git(["checkout", "-B", branch_name, f"origin/{GITHUB_DEFAULT_BRANCH}"], workspace)
        log(f"Branch: {branch_name} (attempt {FIX_ATTEMPT}, base: {GITHUB_DEFAULT_BRANCH})")
    else:
        log(f"Warning: git fetch failed ({err}) — proceeding on current branch")

    candidates_json = []
    fixes        = []
    failed_fixes = []

    for issue in eligible:
        test_name = issue["test_name"]
        log(f"Processing: {test_name}")

        prev_output = prev_test_outputs.get(test_name, "")
        ctx = build_candidate_context(issue, workspace, prev_output, repo_conventions)
        ctx_slim = {k: v for k, v in ctx.items() if k != "repo_conventions"}
        candidates_json.append(ctx_slim)

        if not ctx["test_file"] or not Path(ctx["test_file"]).exists():
            log(f"  No test file found — skipping {test_name}")
            failed_fixes.append({**ctx_slim, "status": "no_file", "fix_diff": "",
                                  "test_passed": False, "test_output": ""})
            continue

        log(f"  File: {ctx['test_file']}")
        if ctx["element_names"]:
            log(f"  Elements: {ctx['element_names'][:5]}")
        if ctx["page_objects"]:
            log(f"  Page objects: {[po['path'] for po in ctx['page_objects']]}")
        if ctx["likely_location"]:
            log(f"  Likely location: {ctx['likely_location']}")

        try:
            Path(ctx["test_file"]).read_text(encoding="utf-8")
        except Exception as e:
            log(f"  Cannot read file: {e} — skipping")
            failed_fixes.append({**ctx_slim, "status": "no_file", "fix_diff": "",
                                  "test_passed": False, "test_output": str(e)})
            continue

        # Call Claude
        prompt = build_fix_prompt(ctx)
        log(f"  Calling Claude for fix...")
        response = call_claude(prompt, workspace)

        if not response:
            log(f"  Empty Claude response — skipping")
            failed_fixes.append({**ctx_slim, "status": "no_response", "fix_diff": "",
                                  "test_passed": False, "test_output": ""})
            continue

        fix_json = extract_fix_json(response)
        if not fix_json:
            log(f"  Could not parse fix JSON — skipping")
            failed_fixes.append({**ctx_slim, "status": "parse_error", "fix_diff": "",
                                  "test_passed": False, "test_output": response[:500]})
            continue

        if not fix_json.get("fixable", False):
            reason = fix_json.get("unfixable_reason", "Claude declared unfixable")
            log(f"  Unfixable: {reason}")
            failed_fixes.append({**ctx_slim, "status": "unfixable", "unfixable_reason": reason,
                                  "fix_description": fix_json.get("fix_description", ""),
                                  "fix_diff": "", "test_passed": False, "test_output": ""})
            continue

        target_file_str = fix_json.get("target_file") or ctx["test_file"]
        target_file = Path(target_file_str)
        if not target_file.is_absolute():
            target_file = workspace / target_file_str

        if not target_file.exists():
            log(f"  Target file not found: {target_file} — skipping")
            failed_fixes.append({**ctx_slim, "status": "target_not_found", "fix_diff": "",
                                  "fix_description": fix_json.get("fix_description", ""),
                                  "test_passed": False, "test_output": ""})
            continue

        fixed_content = fix_json.get("fixed_content") or ""
        if not fixed_content.strip():
            log(f"  Empty fixed_content — skipping")
            failed_fixes.append({**ctx_slim, "status": "empty_fix", "fix_diff": "",
                                  "test_passed": False, "test_output": ""})
            continue

        try:
            target_original = target_file.read_text(encoding="utf-8")
        except Exception:
            target_original = ""

        fix_diff = compute_diff(target_original, fixed_content, target_file.name)
        fix_description = fix_json.get("fix_description", "")
        log(f"  Fix: {fix_description}")

        # Apply fix
        try:
            target_file.write_text(fixed_content, encoding="utf-8")
        except Exception as e:
            log(f"  Cannot write fix: {e}")
            failed_fixes.append({**ctx_slim, "status": "write_error", "fix_diff": "",
                                  "test_passed": False, "test_output": str(e)})
            continue

        # Run test to verify
        log(f"  Running test to verify...")
        passed, test_output = run_single_test(test_name, workspace)

        if passed:
            log(f"  ✅ Fix verified: {test_name}")
            fixes.append({
                **ctx_slim,
                "status": "success",
                "target_file": str(target_file),
                "fix_description": fix_description,
                "fix_diff": fix_diff,
                "test_passed": True,
                "test_output": test_output[-500:],
            })
        else:
            log(f"  ❌ Fix failed test: {test_name}")
            try:
                target_file.write_text(target_original, encoding="utf-8")
            except Exception:
                pass
            failed_fixes.append({
                **ctx_slim,
                "status": "test_failed",
                "target_file": str(target_file),
                "fix_description": fix_description,
                "fix_diff": fix_diff,
                "test_passed": False,
                "test_output": test_output[-2000:],
            })

    # Commit successful fixes
    pr_branch = None
    if fixes:
        for fix in fixes:
            ok, _, err = run_git(["add", fix["target_file"]], workspace)
            if not ok:
                log(f"Warning: git add failed for {fix['target_file']}: {err}")

        fixed_names = ", ".join(f['test_name'].split(".")[-1] for f in fixes[:5])
        commit_msg = (
            f"fix(automation): update locators for {len(fixes)} test(s)\n\n"
            f"Build tag: {build_tag}\n"
            f"Fixed: {fixed_names}"
        )
        ok, _, err = run_git(["commit", "-m", commit_msg], workspace)
        if ok:
            log(f"Committed {len(fixes)} fix(es) to {branch_name}")
            pr_branch = branch_name
        else:
            log(f"Warning: commit failed: {err}")

    # Gate
    if not fixes and not failed_fixes:
        gate = "skipped"
    elif failed_fixes and not fixes:
        gate = "false"
    elif failed_fixes:
        gate = "false"
    else:
        gate = "true"

    write_gate(gate)
    log(f"Gate: .fix-passed = {gate} ({len(fixes)} succeeded, {len(failed_fixes)} failed)")

    # Write JSON
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp":      ts,
        "build_tag":      build_tag,
        "fix_attempt":    FIX_ATTEMPT,
        "eligible_count": len(eligible),
        "attempted":      len(fixes) + len(failed_fixes),
        "succeeded":      len(fixes),
        "failed":         len(failed_fixes),
        "pr_branch":      pr_branch,
        "candidates":     candidates_json,
        "fixes":          fixes,
        "failed_fixes":   failed_fixes,
    }
    json_path = AUDIT_DIR / "01-fix.json"
    json_path.write_text(json.dumps(result, indent=2, default=str))
    log(f"Wrote 01-fix.json ({json_path.stat().st_size // 1024}KB)")

    # Write Markdown
    md_lines = [
        "# Fix Results",
        "",
        f"**Build Tag:** {build_tag}  ",
        f"**Attempt:** {FIX_ATTEMPT}  ",
        f"**Eligible:** {len(eligible)} | **Succeeded:** {len(fixes)} | **Failed:** {len(failed_fixes)}  ",
        f"**Gate:** `{gate}`  ",
        f"**PR Branch:** `{pr_branch or 'none'}`",
        "",
    ]
    if fixes:
        md_lines += ["## Successful Fixes", ""]
        for f in fixes:
            md_lines += [
                f"### ✅ {f['test_name']}",
                f"- **File:** `{f.get('target_file', f['test_file'])}`",
                f"- **Root Cause:** {f['root_cause']}",
                f"- **Fix:** {f['fix_description']}",
                "",
                "```diff",
                f["fix_diff"][:2000] or "(no diff)",
                "```",
                "",
            ]
    if failed_fixes:
        md_lines += ["## Failed Fixes", ""]
        for f in failed_fixes:
            status = f.get("status", "unknown")
            md_lines += [
                f"### ❌ {f['test_name']} (`{status}`)",
                f"- **Root Cause:** {f['root_cause']}",
            ]
            if status == "unfixable":
                md_lines.append(f"- **Reason:** {f.get('unfixable_reason', '')}")
            elif status == "test_failed":
                md_lines.append("- **Fix applied but test still failing**")
                md_lines += ["```", f.get("test_output", "")[-400:], "```"]
            md_lines.append("")

    (AUDIT_DIR / "01-fix.md").write_text("\n".join(md_lines) + "\n")
    log(f"Done — {len(fixes)} fixed, {len(failed_fixes)} failed")


if __name__ == "__main__":
    main()
