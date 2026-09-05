#!/usr/bin/env python3
"""
Step 01 — Fix
For each AUTOMATION_ISSUE (HIGH confidence, ELEMENT_NOT_FOUND) in the handoff file:
  1. Build rich context  — extract method, element names, page object files, likely location
  2. Inspect the live page (optional) — drive a real browser via Playwright MCP to
     read the element's ACTUAL selector instead of guessing from stale source
  3. Generate fix prompt — targeted context: method + page objects + element names + conventions
  4. Call Claude CLI     — get a minimal set of search/replace edits
  5. Apply + verify      — edit file, run test, restore on failure
  6. Commit all successes to a branch

Reads: agents/test-healing-agent/queue/<build_tag>.json  (written by test-triaging-agent/05_ship.py)
Outputs: audit/<session>/01-fix.json + 01-fix.md + .fix-passed

Gate file: .fix-passed
  - "true"    — every attempted fix was applied (and passed, where a runner exists)
  - "false"   — one or more fixes failed tests (triggers retry loop in run.sh)
  - "skipped" — no eligible candidates or infrastructure not configured
"""

import os, sys, json, subprocess, re, signal, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → shared.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
from shared.log import blocked
def log(msg): _log("fix", msg)

from shared import browser_mode

# CodeAnalyzer import — graceful fallback if not available
try:
    from shared.code_analyzer import (CodeAnalyzer as _CodeAnalyzer, split_class_members,
                                   invalidate_file, reset_caches)
    from shared.failure_clusters import build_clusters
    from shared.test_runner import run_test
    _HAS_CODE_ANALYZER = True
except ImportError:
    _HAS_CODE_ANALYZER = False
    split_class_members = None
    build_clusters = None
    def invalidate_file(_path): pass
    def reset_caches(): pass
    from shared.test_runner import run_test  # required: verification cannot be skipped
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
SESSION_ID  = os.environ.get("SESSION_ID", AUDIT_DIR.name)
FIX_ATTEMPT = int(os.environ.get("FIX_ATTEMPT", "1"))

# Handoff file written by test-triaging-agent/05_ship.py
HANDOFF_FILE = Path(os.environ["HANDOFF_FILE"])

HEALING_MODEL = os.environ.get("HEALING_MODEL", "claude-opus-5")

GITHUB_TOKEN           = os.environ.get("GITHUB_TOKEN", "")
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
HEALING_BRANCH_PREFIX  = os.environ.get("HEALING_BRANCH_PREFIX", "healing")

# Step 01 (Locate) resolves locators deterministically. In enforce mode a
# verified resolution replaces the model call entirely; in shadow it is recorded
# and ignored, so the fix step behaves exactly as it did before.
HEALING_LOCATE_MODE = os.environ.get("HEALING_LOCATE_MODE", "shadow").strip().lower()

# AUTO_PUSH=false means "let me look at this first". Branching and committing
# would defeat that: the change disappears from `git status`, and reviewing it
# means knowing which branch to look on. So a dry run leaves the edits in the
# working tree, on whatever branch the user is already on.
# Parsed exactly as 02_ship.py does. An empty value must not mean "commit" here
# while meaning "do not push" there — the two halves of one agent disagreeing
# about the same flag is how a fix ends up committed to a branch nobody pushes.
AUTO_PUSH = os.environ.get("AUTO_PUSH", "true").lower() == "true"

KNOWN_ISSUES_FILE = AGENT_DIR / "feedback" / "known-issues.json"
REPO_CONTEXT_FILE = os.environ.get("REPO_CONTEXT_FILE", "")
MAX_FIXES         = int(os.environ.get("HEALING_MAX_FIXES_PER_RUN", "5"))
MAX_LOG_CHARS     = 3000
MAX_METHOD_CHARS  = 4000
# Page objects hold the locators being fixed — give them room. The extractor
# keeps every field/constructor regardless and only drops methods to fit.
MAX_PAGE_OBJ_CHARS   = int(os.environ.get("AUTOFIX_PAGE_OBJECT_CHARS", "8000"))
MAX_BASE_CLASS_CHARS = 3000

# Persistent domain context for the model, passed as --system-prompt-file.
SYSTEM_PROMPT_FILE  = REPO_ROOT / "config" / "skills" / "automation-repo.md"
# Static half of the fix prompt (instructions, output contract, checklist).
FIX_RULES_FILE      = REPO_ROOT / "config" / "prompts" / "fix.md"

# ── Live DOM inspection ───────────────────────────────────────────────────────
# A locator breaks because the DOM changed, which means the correct new value is
# not present anywhere in the source. Reading the real page is the only way to
# find it rather than guess it.
INSPECT_DOM        = os.environ.get("HEALING_INSPECT_DOM", "true").lower() == "true"
HEALING_BASE_URL   = os.environ.get("HEALING_BASE_URL", "")
DOM_TIMEOUT_S      = int(os.environ.get("AUTOFIX_DOM_TIMEOUT_S", "600"))
PW_HEADLESS        = browser_mode.headless()
REPAIR_SESSION_FILE = os.environ.get("AUTOFIX_REPAIR_SESSION", "")
LOGIN_USERNAME     = os.environ.get("AUTOFIX_LOGIN_USERNAME", "")
LOGIN_PASSWORD     = os.environ.get("AUTOFIX_LOGIN_PASSWORD", "")

# Reject a "fix" that rewrites far more than a locator.
MAX_FIX_DIFF_LINES = int(os.environ.get("AUTOFIX_MAX_DIFF_LINES", "40"))
# shadow: diagnose and log only. enforce: a stop verdict skips the cluster.
DIAGNOSIS_MODE = os.environ.get("DIAGNOSIS_MODE", "shadow").strip().lower()
# The operator's override. A diagnosis is evidence, not an authority — when
# someone has looked at it and still wants a fix attempted, they get one.
FORCE = os.environ.get("FORCE", "false").strip().lower() == "true"

TEST_TIMEOUT_S     = int(os.environ.get("HEALING_TEST_TIMEOUT_S", "300"))

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


from shared.claude import call_claude as _call_claude
from shared.git import run_git

try:
    from shared.mcp_config import write_playwright_mcp_config
    _HAS_MCP_CONFIG = True
except ImportError:
    _HAS_MCP_CONFIG = False

from shared.dom_snapshot import (distill as distill_dom,
                                format_for_prompt as format_dom,
                                load_fingerprints,
                                candidates_from_fingerprints)
from shared import (adaptation_handoff, baseline, diagnosis, failure_identity,
                    locator_patch, narration, run_artifacts, verdict_feedback,
                    workspace as workspace_helper)
from shared.playwright_trace import read_actions, format_for_prompt as format_trace

# Edit application and the fix-integrity guards now live in shared/edit_guards.py
# so test-adaptation-agent runs the same checks. Re-exported at module level:
# tests/unit/test_fix_guards.py loads THIS file by path and calls them as
# attributes of it, and the fix step's own call sites are unchanged.
from shared.edit_guards import (            # noqa: F401
    _condense, _is_broader, _IDENTITY_CALL, _line_of, _QUOTED, _selectors_in,
    apply_edits, compute_diff, log_edits, logstep_present, matches_negative,
    no_new_swallowing, validate_diagnosis_fit, validate_fix, wrapper_compliance,
)


def call_claude(prompt: str, cwd: Path, use_system_prompt: bool = True,
                artifact_dir: str = "", allowed_tools: list | None = None,
                add_dir: str = "", **kwargs) -> str:
    """Call the Claude CLI for this agent.

    use_system_prompt=False for the browser-inspection call: that task is about
    reading a live DOM, and the Java framework context would only be noise.

    allowed_tools and add_dir are named parameters rather than **kwargs
    passthrough: artifact_dir implies a tool grant of its own, so a caller
    supplying its own list — the browser inspections pass ["mcp__playwright__*"]
    — collided with it inside the call and raised TypeError. The two are merged
    instead, so a browser call that also has an artifact dir keeps both.
    """
    system_prompt = (SYSTEM_PROMPT_FILE
                     if use_system_prompt and SYSTEM_PROMPT_FILE.exists() else None)
    tools = list(allowed_tools or [])
    if artifact_dir and "Read" not in tools:
        # Reading an image needs a tool, and granting Read grants it broadly:
        # --add-dir was measured and is additive, not a sandbox. Accepted because
        # this call is already handed the test and page-object source in the
        # prompt, so Read is not new reach — but it is not the confinement an
        # earlier comment here claimed.
        tools.append("Read")
    output = _call_claude(prompt, HEALING_MODEL, str(cwd),
                          system_prompt_file=system_prompt,
                          allowed_tools=(tools or None),
                          add_dir=(add_dir or artifact_dir or None),
                          log_dir=str(AUDIT_DIR),
                          **kwargs)
    if not output:
        log("Claude CLI returned empty response")
    return output


def _repo_https_url() -> str:
    return f"https://github.com/{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}.git"


def _authenticated_url() -> str:
    """Token-bearing remote URL, for one command only — never written to disk.

    The username is a fixed non-secret placeholder; git needs both halves present
    or it tries to negotiate credentials interactively and fails in a headless
    subprocess. See shared/git.py for the full rationale.
    """
    return (f"https://x-access-token:{GITHUB_TOKEN}@github.com/"
            f"{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}.git")


def clone_automation_repo(workspace: Path) -> Path | None:
    """Clone the automation repo. Delegates to shared/workspace.py.

    Kept as a named function because this file is loaded by path in tests and
    callers reference it by name; the implementation moved so three agents stop
    carrying three answers to the same question.
    """
    return workspace_helper.clone(
        workspace, GITHUB_ORG, GITHUB_REPO_AUTOMATION, GITHUB_TOKEN,
        GITHUB_DEFAULT_BRANCH, log=log)


def get_workspace() -> Path | None:
    """The automation checkout, cloned if it is genuinely absent.

    FRAMEWORK_DIR names the path outright; otherwise it is
    WORKSPACE_DIR/GITHUB_REPO_AUTOMATION, and failing both, a sibling checkout
    matched by shape — which works but is a guess, so it says so.
    """
    explicit = workspace_helper.configured()
    workspace_dir = os.environ.get("WORKSPACE_DIR", "")
    if not workspace_dir and explicit is None:
        # Fallback: parent of QA-Agent-Network repo (automation repo should be a sibling)
        workspace_dir = str(REPO_ROOT.parent)
        log(f"Warning: neither FRAMEWORK_DIR nor WORKSPACE_DIR set — "
            f"defaulting to {workspace_dir}")

    found = workspace_helper.find(workspace_dir, GITHUB_REPO_AUTOMATION, exclude=REPO_ROOT)
    if found is not None:
        if explicit is None and not GITHUB_REPO_AUTOMATION:
            log(f"Warning: auto-detected workspace {found} — set FRAMEWORK_DIR to avoid this")
        return found

    target = workspace_helper.expected(workspace_dir, GITHUB_REPO_AUTOMATION)
    if target is None:
        return None
    # Nothing on disk at the configured path — clone into it.
    log(f"{target} not found — attempting clone")
    return clone_automation_repo(Path(workspace_dir or REPO_ROOT.parent))

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


def extract_page_url(issue: dict) -> str:
    """Best-effort recovery of the URL the failing step was on.

    Checked in order: an explicit HEALING_BASE_URL override, a `url=` marker
    (the shape test-authoring-agent's STEP_FAILED protocol emits), then any
    http(s) URL in the log or error text.
    """
    # The URL the framework recorded at the moment of failure is exact; prefer it
    # over an operator-supplied base URL or anything scraped out of the log.
    if issue.get("failure_url"):
        return issue["failure_url"]
    if HEALING_BASE_URL:
        return HEALING_BASE_URL

    combined = "\n".join(str(issue.get(k) or "") for k in
                         ("execution_log", "error_message", "stack_trace", "root_cause"))

    marker = re.search(r'\burl\s*=\s*["\']?(https?://[^\s"\'\],)]+)', combined, re.IGNORECASE)
    if marker:
        return marker.group(1)

    urls = re.findall(r'https?://[^\s"\'\],)]+', combined)
    for url in urls:
        # Skip infrastructure URLs that are never the page under test.
        if any(skip in url for skip in ("selenium", "grid", "hub:", "localhost:4444",
                                        "maven", "gradle", "schemas.")):
            continue
        return url.rstrip('.,;')
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
        workspace / "CLAUDE.md",               # framework rules, if the repo keeps them there
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

# ── Live DOM inspection ───────────────────────────────────────────────────────

def repair_possible(workspace: Path) -> tuple:
    """Whether parking a browser is viable here. Returns (ok, why_not)."""
    if os.environ.get("REPAIR", "").lower() == "false":
        return False, "REPAIR=false"
    if not os.environ.get("TEST_NAME"):
        # A pipeline handoff was produced on another machine hours ago; there is
        # no test run of ours to park.
        return False, "not a standalone run"
    if os.environ.get("CI"):
        return False, "running under CI (no display, and the browser would strand)"
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False, "no DISPLAY"
    port = os.environ.get("AUTOFIX_REPAIR_PORT", "9222")
    if _cdp_alive(f"http://localhost:{port}"):
        return False, f"port {port} already has a browser on it"
    return True, ""


def _same_page(left: str, right: str) -> bool:
    """Whether two URLs are the same page, ignoring ids and query strings."""
    if not left or not right:
        return True          # nothing to contradict; the caller keeps what it has
    return baseline.url_shape(left) == baseline.url_shape(right)


def park_browser_for_repair(workspace: Path, test_name: str,
                            expected_url: str = "") -> dict:
    """Re-run the failing test with the browser parked, then attach to it.

    Only worth the extra test run once the cheap path has already failed: the
    first attempt works from the failure-time DOM snapshot, which is enough for
    most broken locators. When that attempt produced no working fix, a live
    browser is the one thing that adds something the snapshot cannot — the fixer
    can count how many elements a candidate selector matches, and try the
    corrected locator for real before any code is edited.
    """
    ok, why_not = repair_possible(workspace)
    if not ok:
        log(f"  Repair mode not used: {why_not}")
        return {}

    log("  Re-running the test with the browser parked, for a live inspection...")
    status, _ = run_test(
        test_name, workspace,
        extra_properties={"repairMode": "true", "traceMode": "on"},
        timeout_s=int(os.environ.get("AUTOFIX_REPRODUCE_TIMEOUT_S", "900")),
        log=log,
    )
    if status == "passed":
        # It passed this time — flaky, not a broken locator.
        log("  The test passed on the re-run; nothing to inspect")
        return {}
    return find_repair_session(workspace, test_name, expected_url)


def find_repair_session(workspace: Path, test_name: str,
                        expected_url: str = "") -> dict:
    """A browser parked on this test's failing page, if one is live right now.

    `repairMode` in the automation framework leaves the browser open at the point
    of failure and publishes a CDP endpoint. Attaching to it is the strongest
    evidence available: the page is genuinely in the failed state, so a candidate
    selector can be counted for uniqueness and the corrected locator tried for
    real before any Java is edited.

    Only applies when the test run and this run share a machine — normally a
    developer reproducing locally, not the CI pipeline.
    """
    candidates = []
    if REPAIR_SESSION_FILE:
        candidates.append(Path(REPAIR_SESSION_FILE))
    candidates += [
        workspace / "test-output" / ".repair-session.json",
        workspace / os.environ.get("TEST_RESULTS_DIR_NAME", "test-output") / ".repair-session.json",
    ]
    seen = set()
    for path in candidates:
        # TEST_RESULTS_DIR_NAME often IS "test-output", so the candidates overlap.
        resolved = str(path.resolve())
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        try:
            session = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # A stale file from an earlier run is worse than none: it would point the
        # fixer at a different test's page, or at nothing.
        if session.get("test") and session["test"].split(".")[-1] != test_name.split(".")[-1]:
            log(f"  Repair session is for {session['test']} — not this test, ignoring")
            continue
        endpoint = session.get("cdpEndpoint", "")
        if not endpoint or not _cdp_alive(endpoint):
            log(f"  Repair session found but its browser is gone ({endpoint}) — ignoring")
            try:
                path.unlink()   # stale: would otherwise mislead every later run
            except OSError:
                pass
            continue
        # Parked, alive, and for the right test — none of which says it stopped
        # where the failure was. A re-run that dies at an earlier step parks on a
        # page the failing locator was never on, and answers questions about that
        # page instead. Being on the wrong page is not a weaker version of being
        # on the right one; it is a different page.
        parked_url = session.get("url", "")
        if not _same_page(parked_url, expected_url):
            log(f"  Repair session parked on {parked_url or 'an unknown page'}, but the "
                f"failure was on {expected_url} — the re-run stopped somewhere else, "
                f"ignoring it")
            session["_path"] = str(path)
            _reap_parked_browser(session)   # frees the CDP port for the next attempt
            continue

        log(f"  Live repair session: {endpoint} parked on {parked_url or 'unknown URL'}")
        session["_path"] = str(path)
        return session
    return {}


def _cdp_alive(endpoint: str, timeout: float = 2.0) -> bool:
    """True when a browser is actually listening on this CDP endpoint."""
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def load_framework_properties(workspace: Path) -> dict:
    """Read the automation framework's own properties files.

    The tests get their URLs and logins from
    `parameters/{environment}-{country}.properties` via
    `config.getRunTimeProperty(...)`, so that file — already present in the
    workspace — is the correct source for both. Asking an operator to re-enter
    the same secrets as env vars would just be a second place to keep in sync.
    """
    environment = os.environ.get("AUTOFIX_ENVIRONMENT",
                                 os.environ.get("AUTHORING_ENVIRONMENT", "staging")).lower()
    country = os.environ.get("AUTOFIX_COUNTRY",
                             os.environ.get("AUTHORING_COUNTRY", "SG")).lower()

    props: dict = {}
    for name in ("config.properties", f"{environment}-{country}.properties"):
        path = workspace / "parameters" / name
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip()
        except Exception as e:
            log(f"  Could not read {path.name}: {e}")
    if props:
        log(f"  Framework properties loaded: {len(props)} keys from parameters/")
    return props


def guess_module(ctx: dict) -> str:
    """Best guess at the framework module a test belongs to.

    Used to look up `{module}.username` / `{module}.url` and the module's saved
    session. Derived from the page object's path first (most reliable), then the
    test's own package.
    """
    for po in ctx.get("page_objects", []):
        parts = Path(po["path"]).parts
        if "modules" in parts:
            idx = parts.index("modules")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    parts = ctx.get("test_name", "").split(".")
    if len(parts) >= 3:
        return parts[-3]
    return ""


def resolve_credentials(props: dict, module: str) -> dict:
    """Credentials for the failing page: explicit env vars win, else properties."""
    if LOGIN_USERNAME and LOGIN_PASSWORD:
        return {"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD,
                "source": "AUTOFIX_LOGIN_* env vars"}
    for key in ([f"{module}.username"] if module else []):
        if props.get(key) and props.get(key.replace(".username", ".password")):
            return {"username": props[key],
                    "password": props[key.replace(".username", ".password")],
                    "source": f"parameters/*.properties ({key})"}
    return {}


def find_storage_state(workspace: Path, module: str) -> Path | None:
    """A session the framework already saved for this module, if one exists.

    BrowserHelper.storeSession() writes these; reusing one means the inspection
    browser starts logged in and no credential ever reaches the prompt.
    """
    if not module:
        return None
    session_dir = workspace / "src" / "test" / "resources" / module.lower() / "loginStorage"
    if not session_dir.exists():
        return None
    sessions = sorted(session_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


_PARKED_PROMPT = """You are a QA automation agent. A test just failed because an element could not
be found, and the browser has been LEFT OPEN on the exact page where it failed.

Use the Playwright browser MCP tools to inspect the page that is already loaded.
DO NOT navigate away, reload, or log in — you would destroy the state you are here
to inspect. The current page IS the failure.

ELEMENT(S) THE TEST COULD NOT FIND:
{elements}

FAILURE CONTEXT:
{failure}
{failed_selector}
══════════════════════════════════════════════════════════════
WHAT TO DO
══════════════════════════════════════════════════════════════
1. Snapshot the page as it currently stands.
2. Find the element the test wanted, matching on visible text, role, placeholder
   or nearby labels — NOT on the old selector, which no longer matches.
3. Build a selector in this priority order:
     a) [data-cy=...] / [data-testid=...] / [data-test=...]
     b) [id=...]   c) [name=...]   d) [aria-label=...]   e) role   f) text
4. UNIQUENESS CHECK — mandatory, and the reason a live browser is worth having:
   count how many elements your candidate matches. Report it ONLY when the count
   is exactly 1; otherwise narrow it and count again.
5. If you can, confirm the element is actually usable (visible, enabled).

══════════════════════════════════════════════════════════════
OUTPUT — emit these markers on their own lines
══════════════════════════════════════════════════════════════
• SELECTOR_FOUND: <elementName>=<selector>
• ELEMENT_ABSENT: <elementName>|<why it is genuinely not on this page>
• PAGE_DUMP: <json array>
  Up to 15 visible interactive elements, valid JSON on ONE line. Each object has
  "tag" plus whichever of these it has: "data-testid", "id", "name",
  "aria-label", "placeholder", "type", "text".

Report only what you observed. Never invent a selector.
"""


def _pid_listening_on(endpoint: str) -> int:
    """The pid holding the CDP port, when the session file does not name one.

    Older framework builds wrote no browserPid, and the session file is deleted
    on the way out either way — so without this the stray Chromium becomes
    unreachable the moment we drop the file, and it holds the port against every
    later repair run.
    """
    try:
        port = int(endpoint.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return 0
    try:
        out = subprocess.run(["lsof", "-tnP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.split():
        if line.strip().isdigit():
            return int(line.strip())
    return 0


def _reap_parked_browser(session: dict) -> None:
    """Shut down the browser a repair session left running, and clear the file.

    The framework launches it detached precisely so it outlives the test JVM, so
    nothing else will ever close it — leaving it behind means a stray Chromium
    (and a held CDP port) after every repair run.
    """
    try:
        pid = int(session.get("browserPid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        pid = _pid_listening_on(session.get("cdpEndpoint", ""))

    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid = 0  # already gone
        except OSError as e:
            log(f"  Could not close the parked browser (pid {pid}): {e}")
            pid = 0

    if pid:
        # SIGTERM is asynchronous, and the file is about to be deleted: report
        # what actually happened rather than assuming the browser took the hint.
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.25)
        else:
            log(f"  Parked browser (pid {pid}) ignored SIGTERM — still holding "
                f"{session.get('cdpEndpoint', 'the CDP port')}; kill it before the next repair run")
            pid = 0
        if pid:
            log(f"  Parked browser closed (pid {pid})")

    path = session.get("_path")
    if path:
        try:
            Path(path).unlink()
        except OSError:
            pass


def _inspect_parked_browser(ctx: dict, session: dict) -> dict:
    """Inspect the live page a repair session parked for us.

    The browser is reaped on the way out whatever happens — a failed inspection
    must not strand it either.
    """
    result = {"status": "skipped", "selectors": {}, "page_dump": "", "absent": [], "raw": ""}
    endpoint = session.get("cdpEndpoint", "")

    elements = "\n".join(f"  - {e}" for e in ctx["element_names"][:6]) or "  - (none extracted)"
    failure = (f"{ctx['error_type']}: {ctx['error_message']}\n"
               f"Root cause: {ctx['root_cause']}")[:800]
    failed_selector = ""
    if ctx.get("failed_selector"):
        failed_selector = (f"\nThe selector that failed at runtime was: "
                           f"{ctx['failed_selector']}\nIt no longer matches — find what replaced it.\n")

    mcp_path = write_playwright_mcp_config(AUDIT_DIR, cdp_endpoint=endpoint)
    prompt = _PARKED_PROMPT.format(elements=elements, failure=failure,
                                   failed_selector=failed_selector)

    log(f"  Attaching to the parked browser at {endpoint}...")
    try:
        raw = call_claude(
            prompt, AUDIT_DIR,
            use_system_prompt=False,
            timeout=DOM_TIMEOUT_S,
            allowed_tools=["mcp__playwright__*"],
            mcp_config=str(mcp_path),
            strict_mcp_config=True,
            stream_json=True,
            partial_on_timeout=True,
        )
        result["raw"] = raw[-4000:] if raw else ""
        if not raw:
            result["status"] = "attached to the parked browser but it produced no output"
            return result

        _parse_browser_markers(raw, result)
        if result["selectors"]:
            result["status"] = "ok (live, parked on the failing page)"
            log(f"  Live selectors confirmed against the failing page: {result['selectors']}")
        elif result["status"] == "skipped":
            result["status"] = "inspected the parked page but confirmed no selector"
        return result
    finally:
        _reap_parked_browser(session)


def _parse_browser_markers(raw: str, result: dict) -> None:
    """Read the SELECTOR_FOUND / PAGE_DUMP protocol out of a browser run."""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("SELECTOR_FOUND:"):
            payload = line.split(":", 1)[1].strip()
            if "=" in payload:
                name, selector = payload.split("=", 1)
                result["selectors"][name.strip()] = selector.strip()
        elif line.startswith("ELEMENT_ABSENT:"):
            result["absent"].append(line.split(":", 1)[1].strip())
        elif line.startswith("PAGE_DUMP:"):
            result["page_dump"] = line.split(":", 1)[1].strip()[:2000]
        elif line.startswith("NAVIGATION_FAILED:"):
            result["status"] = f"navigation failed: {line.split(':', 1)[1].strip()[:200]}"
        elif line.startswith("UNREACHABLE_STATE:"):
            result["status"] = ("page reached but the failing step's state could not be "
                                f"reproduced from a cold start: {line.split(':', 1)[1].strip()[:200]}")


_DOM_PROMPT = """You are a QA automation agent. A test failed because an element could not be found.
Use the Playwright browser MCP tools to open the page and find what that element ACTUALLY looks like now.

TARGET URL: {url}
ELEMENT(S) THE TEST COULD NOT FIND:
{elements}

FAILURE CONTEXT:
{failure}
{credentials}
══════════════════════════════════════════════════════════════
WHAT TO DO
══════════════════════════════════════════════════════════════
1. Navigate to the target URL. If a login form appears and credentials are given
   above, log in first. Dismiss any cookie banner or modal covering the page.
2. Take a snapshot of the page and look for the element(s) listed above — match on
   visible text, role, placeholder or nearby labels, not on the old selector.
3. For each element you find, work out a selector using this priority order:
     a) [data-cy=...] / [data-testid=...] / [data-test=...]
     b) [id=...]      c) [name=...]      d) [aria-label=...]
     e) role-based    f) text-based
4. UNIQUENESS CHECK — mandatory. Before reporting a selector, use the browser
   tools to count how many elements it matches. Only report it when the count is
   exactly 1; otherwise narrow it (add a parent scope, combine attributes) and
   count again.

══════════════════════════════════════════════════════════════
OUTPUT — emit these markers on their own lines, as you find them
══════════════════════════════════════════════════════════════
• SELECTOR_FOUND: <elementName>=<selector>
• ELEMENT_ABSENT: <elementName>|<why it is genuinely not on the page>
• PAGE_DUMP: <json array>
  Up to 15 visible interactive elements, valid JSON on ONE line. Each object has
  "tag" plus whichever of these it actually has (omit keys it lacks):
  "data-testid", "id", "name", "aria-label", "placeholder", "type", "text".

Report only what you actually observed in the browser. Never invent a selector.
If you cannot reach the page at all, emit: NAVIGATION_FAILED: <reason>

IMPORTANT — this failure may have happened partway through a longer journey. If
the target URL loads but the element's surrounding state does not exist (the
modal was never opened, the record was never created, the wizard step is not
reachable from a cold start), do NOT guess at a selector from a similar-looking
element. Emit:
    UNREACHABLE_STATE: <what you could reach>|<what was missing>
so the fix is made from source with that limitation known, rather than from a
confident-looking but wrong observation.
"""


def inspect_live_dom(ctx: dict, url: str, workspace: Path, props: dict,
                     repair_session: dict | None = None) -> dict:
    """Open the real page and report what the missing element looks like now.

    This is the fallback for when the framework captured no DOM on failure. It
    can only reach pages addressable by URL — a locator that breaks midway
    through a journey (after creating a record, opening a modal, paging through
    a wizard) is not reachable this way, and the run will honestly report the
    element as absent rather than invent a selector.

    Returns {"status", "selectors", "page_dump", "absent", "raw"}. Any failure is
    non-fatal — the fix step continues with static context only, and the audit
    trail records that the DOM was never read.
    """
    result = {"status": "skipped", "selectors": {}, "page_dump": "", "absent": [], "raw": ""}

    if not _HAS_MCP_CONFIG:
        result["status"] = "unavailable: shared.mcp_config not importable"
        return result

    repair_session = repair_session or {}
    if repair_session:
        return _inspect_parked_browser(ctx, repair_session)

    module = guess_module(ctx)
    if not url and module:
        # The framework keeps each module's entry point in the same properties
        # file the tests read it from.
        url = props.get(f"{module}.url") or props.get(f"{module}Url") or ""
    if not url:
        result["status"] = "no page URL in the handoff, properties file, or HEALING_BASE_URL"
        return result

    elements = "\n".join(f"  - {e}" for e in ctx["element_names"][:6]) or "  - (none extracted)"
    failure = (f"{ctx['error_type']}: {ctx['error_message']}\n"
               f"Root cause: {ctx['root_cause']}")[:800]

    # A saved session beats handing the model a password: the browser simply
    # starts logged in, and the credential never enters the prompt at all.
    storage_state = find_storage_state(workspace, module)
    credentials = ""
    if storage_state:
        log(f"  Reusing saved session: {storage_state.name} (no credentials needed)")
        credentials = ("\nThe browser starts from a saved logged-in session, so skip any "
                       "login step and navigate straight to the target URL.\n")
    else:
        creds = resolve_credentials(props, module)
        if creds:
            log(f"  Credentials from {creds['source']}")
            credentials = (f"\nCREDENTIALS (use exactly these):\n"
                           f"  username: {creds['username']}\n  password: {creds['password']}\n")

    mcp_path = write_playwright_mcp_config(AUDIT_DIR, headless=PW_HEADLESS,
                                           storage_state=storage_state)
    prompt = _DOM_PROMPT.format(url=url, elements=elements, failure=failure,
                                credentials=credentials)

    log(f"  Inspecting live DOM at {url} ({browser_mode.label(PW_HEADLESS)})...")
    raw = call_claude(
        prompt, AUDIT_DIR,
        use_system_prompt=False,
        timeout=DOM_TIMEOUT_S,
        allowed_tools=["mcp__playwright__*"],
        mcp_config=str(mcp_path),
        strict_mcp_config=True,
        stream_json=True,
        partial_on_timeout=True,
    )
    result["raw"] = raw[-4000:] if raw else ""

    if not raw:
        result["status"] = "browser run produced no output"
        log("  DOM inspection returned nothing — continuing with static context only")
        return result

    _parse_browser_markers(raw, result)

    if result["selectors"]:
        result["status"] = "ok"
        log(f"  Live selectors found: {result['selectors']}")
    elif result["status"] == "skipped":
        result["status"] = "page reached but no selector confirmed"
        log("  DOM inspected but no selector confirmed")
    return result

def load_dom_snapshot(issue: dict, element_names: list) -> dict:
    """Distil the DOM the framework captured when the test failed.

    This is strictly better than re-opening the page: it is the real state at the
    point of failure — correct session, correct test data, correct step of the
    flow — so it works for locators that break deep inside a journey no direct
    URL can reach. Returns {} when no snapshot was shipped in the handoff.
    """
    path = issue.get("dom_snapshot", "")
    if not path:
        return {}
    snapshot = Path(path)
    if not snapshot.exists():
        log(f"  DOM snapshot referenced but missing on disk: {path}")
        return {}
    try:
        text = snapshot.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        log(f"  Could not read DOM snapshot: {e}")
        return {}

    # Prefer the capture over the markup. Parsing the HTML cannot tell what was
    # visible, and describes elements only by the attributes it thinks to look
    # for — on a page whose edit control is an <img> carrying nothing but an
    # `alt`, it offered three candidates, all wrong, and could not express the
    # right one at all. The sidecar has every element with computed visibility.
    prints = load_fingerprints(path)
    if prints.get("elements"):
        distilled = candidates_from_fingerprints(
            prints, element_names, issue.get("failed_selector") or "")
        if not distilled.get("error"):
            log(f"  DOM candidates from the failure capture: "
                f"{distilled['total_elements']} visible element(s), "
                f"{len(distilled['likely_matches'])} in the failing scope "
                f"at {distilled.get('url') or 'unknown URL'}")
            return distilled
        log(f"  Capture unusable ({distilled['error']}) — parsing the saved HTML")

    distilled = distill_dom(text, element_names)
    if distilled.get("error"):
        log(f"  DOM snapshot unusable: {distilled['error']}")
        return {}
    log(f"  DOM snapshot loaded: {distilled['total_elements']} interactive elements, "
        f"{len(distilled['likely_matches'])} likely match(es) at {distilled.get('url') or 'unknown URL'}")
    return distilled


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
    # Ranked by locator similarity, which a generic selector makes meaningless:
    # `button[type='submit']` is declared by a login page, an OTP page and a
    # checkout page alike, and the model was handed all three with nothing saying
    # which one the test was actually on. The failure names it — put it first.
    owner = failure_identity.identify(f"{error_message}\n{stack_trace}").get("page_object")
    if owner and page_objects:
        page_objects.sort(key=lambda po: Path(po.get("path", "")).stem != owner)

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
        "cause_group_key": issue.get("cause_group_key", ""),
        "recommended_action": issue.get("recommended_action", ""),
        "error_type":    error_type,
        "error_message": error_message[:500],
        "stack_trace":   stack_trace[:800],
        "execution_log": execution_log[:MAX_LOG_CHARS],
        "likely_location": likely_location,
        "page_url":      extract_page_url(issue),
        "dom_snapshot_path": issue.get("dom_snapshot", ""),
        "screenshot":    issue.get("screenshot", ""),
        "dom_snapshot":  {},
        "trace_path":    issue.get("trace_path", ""),
        "failed_selector": issue.get("failed_selector", ""),
        "trace_timeline": "",
        "test_file":     test_file or "",
        "test_method_code": test_method_code,
        "element_names": element_names,
        "page_objects":  page_objects,
        "related_files": related_files,
        "base_class_info": base_class_info,
        "repo_conventions": repo_conventions,
        "dom_findings":  {},
        "prev_test_output": prev_test_output[:1500] if prev_test_output else "",
        "fix_attempt":   FIX_ATTEMPT,
    }

# ── Prompt builder ────────────────────────────────────────────────────────────

_DEFAULT_FIX_RULES = """## Instructions
1. Identify the EXACT broken locator (CSS selector, XPath, @FindBy, etc.)
2. The broken element is most likely one of the extracted element names above
3. Look in the page object files above for the declaration that needs updating —
   an @FindBy annotation, a `By` constant, or a locator assigned in a constructor
4. If the fix is in a page object file (not the test file), target the page object
5. **IMPORTANT**: Use the wrapper methods from the base class — do NOT use raw Selenium/RestAssured
6. **IMPORTANT**: Follow the project conventions shown above
7. Do not refactor, rename, or change anything unrelated to the broken locator

## Output Format (strict)
Respond with a JSON object ONLY. No prose, no markdown fences around it.

{
  "fixable": true | false,
  "unfixable_reason": "<reason if fixable=false, else null>",
  "fix_description": "<1-2 sentences: what was broken and what you changed>",
  "target_file": "<absolute path of the file to modify>",
  "edits": [
    {
      "old_string": "<exact text to replace — must appear EXACTLY ONCE in the file>",
      "new_string": "<replacement text>"
    }
  ]
}

Rules for `edits`:
- Keep each edit as small as possible — ideally the single locator line.
- `old_string` must match the file byte-for-byte, including indentation, and must
  be unique in the file. Include a line of surrounding context if that is what it
  takes to make it unique.
- Do NOT return the whole file. Do NOT reformat untouched lines.

## Self-Resolving Checklist (before declaring unfixable)
Before setting `fixable: false`, you MUST exhaustively try:
1. Re-read the full execution log and stack trace for the exact failing selector
2. Check all page object files listed above for the declaration matching the element name
3. Try alternative locator strategies in priority order: `id` > `name` > `css [data-cy]` > `css` > `xpath`
4. Check related files for alternative element declarations (inner classes, static strings)
5. Look for similar working locators in the same page object as a pattern reference

Only declare `fixable: false` after all 5 checks are exhausted and you have a specific blocker.
"""


def load_fix_rules() -> str:
    """Static half of the prompt — kept in config/prompts/fix.md so it can be
    tuned without a code change. Falls back to the bundled default."""
    if FIX_RULES_FILE.exists():
        try:
            text = FIX_RULES_FILE.read_text(encoding="utf-8")
            # Drop the file's own documentation header (everything before the
            # first "## Instructions" section) so only prompt content is sent.
            # Anchored to line start so a mention of the heading inside the
            # header prose does not match ahead of the real heading.
            marker = re.search(r"^## Instructions\s*$", text, re.MULTILINE)
            if marker:
                return text[marker.start():]
            return text
        except Exception:
            pass
    return _DEFAULT_FIX_RULES


_LOCATE_CACHE = None


def load_locate_resolutions() -> list:
    """What step 01 concluded, or an empty list if it did not run."""
    global _LOCATE_CACHE
    if _LOCATE_CACHE is None:
        path = AUDIT_DIR / "01-locate.json"
        try:
            _LOCATE_CACHE = (json.loads(path.read_text()) or {}).get("resolutions") or []
        except (OSError, ValueError):
            _LOCATE_CACHE = []
    return _LOCATE_CACHE


# What Locate concluded, and whether it leaves Fix anything to do. Two of these
# are deliberate refusals that Fix must honour rather than override: healing a
# locator an assertion reads turns a caught regression into a green build, and a
# locator that has already been healed repeatedly needs a stable test id, not a
# fourth heal. The rest are the cases Locate cannot serve — no recorded
# fingerprint, a page its replay cannot reach, an element it found but could not
# express uniquely — which is precisely where Fix earns its place.
_LOCATE_DEFER = {
    "ASSERTION_LOCATOR": "the locator is read by an assertion — healing it would "
                         "turn a caught regression into a green build",
    "UNSTABLE_LOCATOR": "this locator has been healed repeatedly — it needs a "
                        "stable test id, not another selector",
    "MISBOUND": "the selector resolves to the wrong element; rebinding it is a "
                "review decision, not a search",
}


def locate_outcome(ctx: dict) -> dict:
    """What Locate concluded for this cluster's selector, in every mode.

    Distinct from locate_resolution(), which only speaks in enforce mode because
    it *applies* an answer. This only reports one, so shadow mode has no reason to
    withhold it: knowing that Locate already tried literal repair, identity
    attributes, role+name and anchoring is what stops Fix spending a model call
    re-treading the same ground.
    """
    failed = ctx.get("failed_selector") or ""
    if not failed:
        return {}
    for resolution in load_locate_resolutions():
        if resolution.get("failed_selector") == failed:
            return resolution
    return {}


def locate_route(outcome: dict) -> tuple:
    """(route, note) for what Fix should do about Locate's verdict.

    route is one of: "consume" (Locate proved an answer), "defer" (Locate refused
    on purpose and so must Fix), or "own" (Locate could not answer; Fix searches).
    """
    if not outcome:
        return "own", "Locate did not run for this selector"
    verdict = outcome.get("verdict") or ""
    classification = outcome.get("classification") or ""
    if verdict == "HEALED":
        return "consume", (f"Locate proved {outcome.get('new_locator')!r} "
                           f"({outcome.get('strategy')}, score {outcome.get('score')}, "
                           f"verification {outcome.get('verification')})")
    for name, why in _LOCATE_DEFER.items():
        if name in (verdict, classification):
            return "defer", why
    return "own", (outcome.get("reason")
                   or f"Locate returned {verdict or classification or 'nothing'}")


def locate_resolution(ctx: dict):
    """A verified resolution for this cluster's failing selector, if there is one.

    Shadow mode returns nothing on purpose: the resolution is still written to
    01-locate.json for comparison, but the model keeps making the decision until
    the numbers say it should not.
    """
    if HEALING_LOCATE_MODE != "enforce":
        return None
    # Only on the first attempt. Reaching attempt 2 means the located locator was
    # applied and the test still failed, so re-applying the identical edit would
    # burn the retry on a known-failing answer. Hand the retry to the model, which
    # is what the retry loop is for.
    if FIX_ATTEMPT > 1:
        log(f"  attempt {FIX_ATTEMPT}: the located fix did not verify — asking the model")
        return None
    failed = ctx.get("failed_selector") or ""
    if not failed:
        return None
    for resolution in load_locate_resolutions():
        if (resolution.get("verdict") == "HEALED"
                and resolution.get("failed_selector") == failed
                and resolution.get("new_expression")):
            return resolution
    return None


def build_located_fix(resolution: dict, ctx: dict, workspace: Path):
    """Shape a resolution like the model's output. Returns (fix_json, error).

    Deliberately produces the identical structure `extract_fix_json` yields, so
    the edit guards, the diff cap, the test verification and the audit record all
    run exactly as they do for a model-written fix. A deterministic fix earns no
    exemptions.
    """
    page_object, field = resolution.get("page_object"), resolution.get("field")
    if not page_object or not field:
        return None, "resolution names no page object field"

    target = None
    for candidate in ctx.get("page_objects") or []:
        path = Path(candidate["path"])
        if path.stem == page_object:
            target = path if path.is_absolute() else workspace / path
            break
    if target is None or not target.exists():
        return None, f"page object {page_object}.java not found in this workspace"

    try:
        source = target.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {target}: {exc}"

    edit, error = locator_patch.declaration_edit(source, field,
                                                 resolution["new_expression"])
    if edit is None:
        return None, error

    return {
        "fixable": True,
        "target_file": str(target),
        "edits": [edit],
        "fix_description": (
            f"Located deterministically: {resolution['failed_selector']} no longer "
            f"matches. Scored {resolution.get('score')} against the fingerprint "
            f"recorded on the last good run (margin {resolution.get('margin'):+}, "
            f"{resolution.get('tier')}), then verified by performing the step "
            f"({resolution.get('verification')})."),
    }, ""


def _refresh_baseline_after_heal(ctx: dict, workspace: Path) -> None:
    """Re-record the healed locator's fingerprint, if this was a located fix.

    Advisory: a stale fingerprint costs a later diagnosis some confidence, so
    failing the run over one would be trading a real fix for a bookkeeping error.
    """
    located = ctx.get("located")
    if not located or not located.get("page_object"):
        return
    try:
        directory = baseline.directory(workspace)
        if not directory:
            return
        path = Path(directory) / f"{located['page_object']}.json"
        locator_patch.update_baseline(
            path, located["field"], located.get("new_locator", ""),
            fingerprint={}, score=located.get("score", 0.0),
            source_expression=located.get("new_expression", ""))
        log(f"  Baseline history updated for {located['page_object']}#{located['field']}")
    except Exception as exc:                       # noqa: BLE001 - advisory only
        log(f"  Could not update the baseline ({type(exc).__name__}) — "
            f"the next green run will re-record it")


def build_fix_prompt(ctx: dict, fix_rules: str) -> str:
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

    # What the evidence says went wrong, worked out before this prompt was built.
    # Without it the model is asked "what should the new locator be?" and has no
    # way to answer "the locator was never the problem".
    diagnosis_text = ""
    verdict = ctx.get("diagnosis") or {}
    if verdict.get("verdict") and verdict["verdict"] != "INSUFFICIENT_EVIDENCE":
        reasons = "\n".join(f"- {reason}" for reason in verdict.get("reasons") or [])
        diagnosis_text = f"""
## 🔬 DIAGNOSIS — {verdict['verdict']} ({verdict.get('confidence', '')} confidence)
Worked out from the DOM captured at failure, the page object's own locators, the
network log and the step timeline — before you were called.

{reasons}

**What you are being asked to do: {verdict.get('action') or 'assess whether a fix is possible'}.**
"""
        if verdict["verdict"] == "LOCATOR_STALE":
            diagnosis_text += (
                "\nThe page is confirmed correct and its other locators still match, "
                "so a replacement for the failing one does exist on the page below.\n")
        if verdict["verdict"] == "AMBIGUOUS_LOCATOR":
            diagnosis_text += (
                "\n**The element is present — the selector just matches more than one "
                "of them.** Playwright refuses to act on an ambiguous locator, so the "
                "action never ran. Narrow the existing selector to the one element "
                "this step means, using something that distinguishes it from its "
                "siblings below (a distinguishing class, an accessible name, the "
                "enclosing form or section). Do NOT reach for `.first()` or `.nth()` "
                "to silence it: position is not identity, and the next sibling added "
                "to that page moves it. Do NOT widen the selector.\n")
        # Measured in the live page at the moment of the timeout, so it outranks
        # anything inferred from the saved markup.
        if verdict.get("anchor_state") == "hidden":
            diagnosis_text += (
                "\n**The failing selector DID match an element — it was simply not "
                "visible.** So this is not a renamed locator, and swapping in another "
                "selector for the same hidden element will time out in exactly the "
                "same way. Either the element must be revealed first (an expand, a "
                "scroll, a parent click the flow is missing), or the test is "
                "targeting the wrong element entirely. Only propose a new selector "
                "if you can point to a *visible* element below that does the job; "
                "otherwise return `fixable: false` and say the element was present "
                "but hidden.\n")
        elif verdict.get("anchor_state") == "absent":
            diagnosis_text += (
                "\nThe failing selector matched nothing at all in the live page, so "
                "the element really was renamed, moved or removed.\n")
        elif not verdict.get("actionable"):
            diagnosis_text += (
                "\nThis is **not** a stale locator. Do not propose a new selector: "
                "return `fixable: false` with this cause as the reason.\n")

    # What the deterministic search already established. Without this the model
    # re-derives — worse — an answer that was computed and, when HEALED, proved by
    # performing the step in a live browser twelve seconds earlier.
    locate_text = ""
    outcome = ctx.get("locate_outcome") or {}
    if outcome and ctx.get("locate_route") == "consume":
        locate_text = f"""
## \u2705 ALREADY LOCATED AND PROVED
A deterministic search matched the element against the fingerprint recorded on
the last good run, then **performed the action on it in a live browser**:

- replacement: `{outcome.get('new_locator')}`
- expression: `{outcome.get('new_expression')}`
- score {outcome.get('score')} (margin {outcome.get('margin')}, {outcome.get('tier')}),
  verification {outcome.get('verification')}

Use this unless the source below makes it impossible. It is evidence, not a
suggestion — it was checked against the running page.
"""
    elif outcome:
        tried = "\n".join(f"- {a.get('loop')}: {a.get('detail')}"
                           for a in (outcome.get("attempts") or []))
        locate_text = f"""
## \u26a0\ufe0f THE DETERMINISTIC SEARCH ALREADY FAILED HERE
It could not propose a locator: {ctx.get('locate_note') or outcome.get('reason')}

What it already tried, so you do not repeat it:
{tried or '- (no attempt log recorded)'}

Literal repair of the old selector, identity attributes, role + accessible name
and anchoring to a surviving ancestor have all been ruled out. A different
strategy is needed — the evidence below, not another variation on the string.
"""

    # Observed DOM outranks everything else in this prompt: the source below is
    # by definition the version that was already failing.
    dom_text = ""
    snapshot = ctx.get("dom_snapshot") or {}
    dom = ctx.get("dom_findings") or {}

    if snapshot:
        dom_text = f"""
## ✅ ACTUAL PAGE AT THE MOMENT OF FAILURE
This is the real DOM, captured by the test framework the instant the test failed —
same session, same test data, same step of the flow. It is the ground truth for
what the element looks like now. **Base the fix on this, not on the source below.**

```
{format_dom(snapshot)}
```

If none of these elements is the one the test wanted, do not settle for the
closest-looking one. Either the element was removed (a product bug) or this is not
the page the test was supposed to reach — both mean `fixable: false`, with which
one it is stated as the reason.
"""

    # A selector confirmed in a live browser outranks the snapshot, and used to be
    # unreachable: the branches were exclusive, a snapshot exists on every real
    # failure, so the confirmed selector was found, logged, and then never shown
    # to the model — which answered with a guess of its own instead.
    if dom.get("selectors"):
        found = "\n".join(f"- `{name}` → `{sel}`" for name, sel in dom["selectors"].items())
        dom_text += f"""
## ✅ LIVE DOM — CONFIRMED SELECTORS (observed in a real browser just now)
These were read from the actual page and each was verified to match exactly one
element. **This is the strongest evidence in this prompt — prefer it over the
snapshot above and over anything you infer from the source below. Use one of
these unless you can say why it is wrong.**

{found}
"""
        if dom.get("page_dump"):
            dom_text += f"\nVisible interactive elements on the page:\n```json\n{dom['page_dump']}\n```\n"
    elif dom.get("absent"):
        # Appended, not assigned: the snapshot block above may already be here,
        # and overwriting it would drop the strongest evidence in the prompt.
        dom_text += f"""
## ⚠️ LIVE DOM — ELEMENT GENUINELY ABSENT
A real browser was opened on the failing page and these elements were not present:
{chr(10).join('- ' + a for a in dom['absent'])}

This may be a PRODUCT bug rather than a broken locator. If the element is simply
gone rather than renamed, set `fixable: false` and say so.
"""
    elif dom.get("status") and dom["status"] != "skipped" and not dom_text:
        dom_text = f"""
## ⚠️ LIVE DOM — NOT AVAILABLE
The page could not be inspected ({dom['status']}). Everything below is static
source that predates the failure — you are inferring the new selector, not
observing it. Prefer a resilient strategy (id / data-* / name) over a brittle one
(deep CSS path, positional XPath).
"""

    # An image settles "what is covering this element" and "is this an error page"
    # in a glance. The framework has always taken one; nothing has ever looked at it.
    screenshot_text = ""
    if ctx.get("screenshot"):
        screenshot_text = f"""
## 📸 SCREENSHOT AT FAILURE
`{ctx['screenshot']}`

Read this file before deciding what went wrong. It shows the page exactly as the
test left it — an overlay, an error page or an empty state is visible here even
when the DOM below looks unremarkable.
"""

    trace_text = ""
    if ctx.get("trace_timeline"):
        trace_text = f"""
## 🔎 WHAT THE TEST ACTUALLY DID (Playwright trace)
Recorded at runtime, so these are the selector strings the framework really used —
not what the source appears to say.

```
{ctx['trace_timeline']}
```
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
"""
        # Telling the model to "try something different" unconditionally is what
        # walks it down the ladder from a precise selector to a broad one. A
        # second failure on the same element is evidence about the diagnosis, not
        # an invitation to guess wider.
        if verdict.get("verdict") == "LOCATOR_STALE":
            retry_text += ("Try a different locator strategy — do NOT repeat the "
                           "previous approach, and do NOT widen the selector to "
                           "make it match.\n")
        else:
            retry_text += ("Two attempts have now failed on the same element. That "
                           "is evidence the original diagnosis was wrong rather "
                           "than a reason to guess again. If you cannot identify a "
                           "specific, verified replacement, return `fixable: false` "
                           "and say what you would need to decide.\n")

    return f"""You are fixing a broken locator in a Selenium/RestAssured test automation file.
Work independently on this test case only.
{conventions_text}{base_class_text}
## Test Case
- **Full Name:** {ctx['test_name']}
- **Class:** {ctx['class_name']}
- **Method:** {ctx['method_name']}
- **File:** {ctx['test_file']}

{diagnosis_text}{screenshot_text}
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
{locate_text}
{dom_text}
## Execution Log (truncated)
```
{ctx['execution_log']}
```

## Stack Trace (truncated)
```
{ctx['stack_trace']}
```
{trace_text}{retry_text}
## Code to Fix

{code_section}
{page_obj_text}
{related_text}

{fix_rules}
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

# ── Attempt history ───────────────────────────────────────────────────────────

def _attempt_history(result: dict) -> list:
    """Append this attempt's outcome to whatever earlier attempts recorded.

    Reads the previous 01-fix.json before it is overwritten. Each entry stays
    small — the description, the file, the diff and the verdict — so the history
    is readable even after several retries.
    """
    history = []
    prev_path = AUDIT_DIR / "01-fix.json"
    if prev_path.exists():
        try:
            history = (json.loads(prev_path.read_text()) or {}).get("attempts") or []
        except (json.JSONDecodeError, OSError):
            history = []

    entries = []
    for bucket, outcome in (("fixes", "verified"),
                            ("unverified_fixes", "applied but unverified"),
                            ("failed_fixes", "failed")):
        for f in result.get(bucket) or []:
            if f.get("fix_attempt") not in (None, FIX_ATTEMPT):
                continue        # carried forward from an earlier attempt
            # An "advanced" entry sits in failed_fixes because the test is not
            # green yet, but its edit repaired what it targeted and is still on
            # disk. Reporting it as "failed, reverted" describes the opposite of
            # what happened, and is what the console summary said while the fix
            # it was describing was sitting in the working tree.
            kept = f.get("status") == "advanced"
            entries.append({
                "test_name": f.get("test_name"),
                "target_file": f.get("target_file"),
                "fix_description": f.get("fix_description") or "",
                "unfixable_reason": f.get("unfixable_reason") or "",
                "fix_diff": (f.get("fix_diff") or "")[:4000],
                "status": f.get("status"),
                "outcome": "kept — the test now stops at a later locator" if kept
                           else outcome,
                "reverted": (not kept and outcome == "failed"
                             and bool(f.get("fix_diff"))),
            })

    history = [h for h in history if h.get("attempt") != FIX_ATTEMPT]
    history.append({"attempt": FIX_ATTEMPT, "timestamp": result.get("timestamp"),
                    "entries": entries})
    return sorted(history, key=lambda h: h.get("attempt", 0))


def should_gate(verdict: dict, mode: str, force: bool) -> tuple:
    """Whether this verdict may stop the pipeline path. Returns (gate, note).

    Extracted so the asymmetry between the two entry points is testable rather
    than implied. Probes run on the standalone path only, so a verdict reached
    here has never been measured — it rests on inference. This path therefore
    gates at HIGH alone, where several independent channels agreed; standalone
    gates at MEDIUM because a probe stands behind it. The property that holds in
    both: nothing blocks work unless it was measured or corroborated.
    """
    name = (verdict or {}).get("verdict")
    if name not in diagnosis.STOP:
        return False, ""
    if force:
        return False, "FORCE=true — attempting a fix anyway"
    if (verdict or {}).get("confidence") != "HIGH":
        return False, (f"{name} at {verdict.get('confidence')} confidence and unprobed "
                       f"on this path — reporting, not gating")
    if mode != "enforce":
        return False, (f"shadow mode: would have stopped here — {name}. "
                       f"Set DIAGNOSIS_MODE=enforce to act on it.")
    return True, ""


def _snapshot_soup(issue: dict):
    """Parse the failure DOM once, for the guards. None when there is none."""
    path = issue.get("dom_snapshot") or ""
    if not path or not Path(path).exists():
        return None
    try:
        from shared.page_identity import parse as _parse_dom
        return _parse_dom(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


# ── Test runner ───────────────────────────────────────────────────────────────


def evaluate_shadow_guards(original: str, updated: str, target_file, ctx: dict) -> list:
    """Run the adaptation-agent guards without letting them decide anything.

    Returns one row per guard: {guard, would_reject, reason}. Failures inside a
    guard are recorded as errors rather than raised — a guard that crashes must
    not take down a fix it was only observing.
    """
    is_test = target_file.name.endswith(("Test.java", "Test.kt", "Tests.java"))
    negatives = []
    snapshot = ctx.get("dom_snapshot_path") or ""
    checks = [
        ("no_new_swallowing", lambda: no_new_swallowing(original, updated)),
        ("wrapper_compliance", lambda: wrapper_compliance(original, updated)),
        ("logstep_present", lambda: logstep_present(original, updated, is_test)),
        ("matches_negative",
         lambda: matches_negative(_selectors_in(updated), negatives)),
    ]
    rows = []
    for name, run in checks:
        try:
            ok, reason = run()
        except Exception as exc:                      # pragma: no cover - defensive
            rows.append({"guard": name, "would_reject": False,
                         "reason": "", "error": str(exc)})
            continue
        rows.append({"guard": name, "would_reject": not ok, "reason": reason})
    if snapshot:
        rows.append({"guard": "_snapshot", "would_reject": False, "reason": snapshot})
    return rows


def split_by_progress(still_failing: list) -> tuple:
    """Split still-failing tests into those that moved on and those that did not.

    A run that still fails is not proof the edit was wrong. If the flow now stops
    at a *different* element, the repaired one works and the test has simply
    reached the next broken locator — which is progress, and reverting there
    throws away a good fix and guarantees the next attempt spends itself
    re-fixing the same thing.

    Returns (advanced, unchanged). `advanced` entries carry the before/after
    failure so the caller can name what moved.
    """
    advanced, unchanged = [], []
    for member_name, member_output, member, member_started in still_failing:
        before = failure_identity.identify(
            f"{member.get('error_message') or ''}\n{member.get('stack_trace') or ''}")
        after = failure_identity.identify(member_output)
        if failure_identity.same_locator(before, after):
            unchanged.append((member_name, member_output, member))
        else:
            advanced.append((member_name, member_output, member,
                             member_started, before, after))
    return advanced, unchanged


def _refresh_issue(issue: dict, output: str, workspace: Path,
                   started: float) -> dict:
    """The same test's issue, rebuilt around the element that fails NOW.

    After an edit lands, the failure on the screen can be a different element
    entirely. Carrying the original issue into the next attempt hands it a
    repaired selector, a stale DOM snapshot and a diagnosis about a problem that
    no longer exists — which is how a retry spends itself re-fixing what it just
    fixed. Everything here is read from the artifacts the verification run wrote.
    """
    refreshed = dict(issue)
    failure = failure_identity.identify(output)
    refreshed.update({
        "error_message": output[-2000:],
        "root_cause": (output or "")[:400],
        "execution_log": narration.for_handoff(output),
        # Cleared before re-attaching: a stale path that survives is worse than
        # an absent one, because it reads as this failure's evidence.
        "dom_snapshot": "", "failure_url": "", "trace_path": "",
        "failed_selector": "", "screenshot": "", "failure_context": "",
        "diagnosis": {},
    })
    if failure.get("selector"):
        refreshed["failed_selector"] = failure["selector"]
    if failure.get("page_object"):
        refreshed["stack_trace"] = f"{failure['page_object']}.java"

    results_dir = workspace / os.environ.get("TEST_RESULTS_DIR_NAME", "test-output")
    method = (issue.get("test_name") or "").rsplit(".", 1)[-1]
    try:
        from_trace = run_artifacts.attach(refreshed, results_dir, method,
                                          not_before=started)
        if from_trace:
            refreshed["failed_selector"] = from_trace
    except Exception as exc:                        # noqa: BLE001 - advisory only
        log(f"  could not attach fresh artifacts ({type(exc).__name__}) — the next "
            f"attempt will work from the failure text alone")
    return refreshed


def run_single_test(test_name: str, workspace: Path) -> tuple:
    """Verify one test. Returns (status, output) — passed / failed / unverified.

    Delegates to shared.test_runner so the fix step and the reproduce step invoke
    tests identically; a fix "verified" by a different command than the one that
    produced the failure would prove nothing.
    """
    return run_test(test_name, workspace, timeout_s=TEST_TIMEOUT_S, log=log)


# ── Main ──────────────────────────────────────────────────────────────────────

def _commit_baselines(workspace, build_tag: str) -> list:
    """Commit the locator fingerprints the verification re-run recorded.

    A heal is precisely the moment the repo's baseline goes stale: the locator
    that broke has just been replaced, so the committed fingerprint describes an
    element the page no longer has. Leaving it means the NEXT drift in that page
    is diagnosed against a record of the page before this fix — which is how a
    healed page keeps looking broken to the comparison that is supposed to
    explain it.

    Path-scoped like the fix commit above: only the baselines directory is ever
    staged, never `git add -A`.
    """
    changed = baseline.changed(workspace)
    if not changed:
        return []
    paths = sorted(changed)
    ok, _, err = run_git(["add", "--"] + paths, workspace)
    if not ok:
        log(f"Warning: could not stage baselines: {err.strip()[:120]}")
        return []
    ok, out, err = run_git(
        ["commit", "-m",
         f"healing: refresh {len(paths)} locator baseline(s) for {build_tag}\n\n"
         f"Element fingerprints recorded while the healed tests were verified,\n"
         f"so the next drift is diagnosed against the page as it is now.\n\n"
         f"Session: {SESSION_ID}"],
        workspace)
    if not ok and "nothing to commit" not in f"{out}{err}".lower():
        log(f"Warning: baseline commit failed: {(err or out).strip()[:120]}")
        return []
    log(f"Committed {len(paths)} locator baseline(s)")
    return paths


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

    def write_skipped(reason: str, infra: bool = False):
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "timestamp": ts, "build_tag": build_tag, "fix_attempt": FIX_ATTEMPT,
            "eligible_count": len(eligible), "skipped_reason": reason,
            "skipped_for_infra": infra,
            "attempted": 0, "succeeded": 0, "unverified": 0, "failed": 0,
            "candidates": [], "fixes": [], "unverified_fixes": [], "failed_fixes": [],
        }
        (AUDIT_DIR / "01-fix.json").write_text(json.dumps(result, indent=2))
        (AUDIT_DIR / "01-fix.md").write_text(f"# Fix\n\nSkipped — {reason}.\n")
        write_gate("skipped")
        # run.sh reads this to decide whether the handoff may be consumed. An
        # infra skip means nothing was even attempted, so the work must stay queued.
        (AUDIT_DIR / ".skip-reason").write_text("infra" if infra else "no-work")
        log(f"Gate: .fix-passed = skipped ({reason})")

    if not eligible:
        write_skipped("no eligible issues in handoff")
        return

    if not GITHUB_TOKEN or not GITHUB_REPO_AUTOMATION:
        write_skipped("GitHub config not set (GITHUB_TOKEN / GITHUB_REPO_AUTOMATION)", infra=True)
        return

    workspace = get_workspace()
    if not workspace:
        write_skipped("automation repo workspace not found", infra=True)
        return

    log(f"Workspace: {workspace}")

    # Load repo conventions and prompt rules once
    repo_conventions = load_repo_conventions(workspace)
    framework_props = load_framework_properties(workspace)
    fix_rules = load_fix_rules()
    if SYSTEM_PROMPT_FILE.exists():
        log(f"System prompt: {SYSTEM_PROMPT_FILE.relative_to(REPO_ROOT)}")

    # Retry bookkeeping: only re-attempt what actually failed, and carry forward
    # the fixes already committed by earlier attempts so they stay in the report.
    prev_test_outputs: dict = {}
    carried_fixes: list = []
    carried_unverified: list = []
    if FIX_ATTEMPT > 1:
        prev_path = AUDIT_DIR / "01-fix.json"
        if prev_path.exists():
            prev_data = json.loads(prev_path.read_text())
            failed_names = set()
            # An attempt that repaired its element and uncovered the next one
            # wrote the new failure down. Without it this retry re-investigates
            # the selector it just fixed: a repaired locator, a DOM snapshot from
            # before the edit, and a diagnosis of a problem that no longer exists.
            refreshed = {}
            for fix in prev_data.get("failed_fixes", []):
                prev_test_outputs[fix["test_name"]] = fix.get("test_output", "")
                failed_names.add(fix["test_name"])
                if fix.get("next_issue"):
                    refreshed[fix["test_name"]] = fix["next_issue"]
            carried_fixes = prev_data.get("fixes", [])
            carried_unverified = prev_data.get("unverified_fixes", [])
            if failed_names:
                before = len(eligible)
                eligible = [refreshed.get(i["test_name"], i) for i in eligible
                            if i["test_name"] in failed_names]
                log(f"Retry attempt {FIX_ATTEMPT}: re-attempting {len(eligible)} of "
                    f"{before} issue(s) — {len(carried_fixes)} already fixed and committed")
                for name, issue in refreshed.items():
                    log(f"  {name.rsplit('.', 1)[-1]}: the previous fix landed — now "
                        f"working on {issue.get('failed_selector') or 'the next failure'}")

    log(f"{len(eligible)} eligible failing test(s) to analyse")

    # Create / checkout fix branch
    # Branch name: <HEALING_BRANCH_PREFIX>/<safe-build-tag>
    # On retry (FIX_ATTEMPT > 1), reuse the same branch so commits stack
    safe_tag    = re.sub(r"[^a-zA-Z0-9_-]", "-", build_tag).lower()
    branch_name = f"{HEALING_BRANCH_PREFIX}/{safe_tag}"
    on_branch = False
    if not AUTO_PUSH:
        # Dry run: no branch, no commit. The whole block below is skipped rather
        # than routed through the offline fallback — that path creates a local
        # branch too, and it would drag the user's working tree onto it.
        _, current, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
        log(f"AUTO_PUSH=false — staying on {current.strip() or 'the current branch'}; "
            f"edits will be left uncommitted for review")
    else:
        # Attempt 1 establishes the base and records it. Later attempts reuse
        # that recorded SHA and deliberately do NOT re-fetch: origin/<base> may
        # have advanced in the meantime, and re-cutting the branch from a moved
        # base would silently drop the commits attempt 1 already made.
        if FIX_ATTEMPT <= 1:
            prepared = workspace_helper.prepare_base(
                workspace, GITHUB_ORG, GITHUB_REPO_AUTOMATION, GITHUB_TOKEN,
                GITHUB_DEFAULT_BRANCH, log=log)
            ok, err = prepared["ok"], prepared["reason"]
            base_sha = prepared["sha"]
        else:
            _recorded, base_sha = workspace_helper.read_base_marker(AUDIT_DIR)
            ok, err = bool(base_sha), "attempt 1 recorded no base"
        if ok:
            # FIX_ATTEMPT 1: reset to the recorded base so we always start clean
            # FIX_ATTEMPT > 1: reuse the existing branch (fixes accumulate across retries)
            if FIX_ATTEMPT <= 1:
                on_branch, _, switch_err = run_git(
                    ["checkout", "-B", branch_name, base_sha], workspace)
            else:
                # Branch should already exist from attempt 1; just check it out
                on_branch, _, switch_err = run_git(["checkout", branch_name], workspace)
                if not on_branch:
                    # Branch doesn't exist yet (first attempt committed nothing) — create it
                    on_branch, _, switch_err = run_git(
                        ["checkout", "-B", branch_name, base_sha], workspace)
            # Checked, deliberately. This used to be fire-and-forget: a switch
            # blocked by local modifications failed silently, the log announced a
            # branch we were never on, and the commit landed on the user's working
            # branch — precisely what the comment below says must never happen.
            if on_branch:
                log(f"Branch: {branch_name} (attempt {FIX_ATTEMPT}, "
                    f"base: {GITHUB_DEFAULT_BRANCH} @ {base_sha[:8]})")
            else:
                log(blocked(
                    f"could not switch to {branch_name} ({switch_err.strip()[:120]})",
                    "no PR will be raised; edits are left uncommitted on the "
                    "current branch",
                    f"git -C {workspace} status"))
        else:
            # Offline, or a bad token. Branch off whatever is checked out rather
            # than committing onto it: "proceeding on current branch" quietly meant
            # fixes landed as commits on main, which is not something an autofix
            # should ever do to someone's working checkout.
            log(f"Warning: could not establish the base branch ({err})")
            on_branch, _, local_err = run_git(["checkout", "-B", branch_name], workspace)
            if on_branch:
                log(f"Branch: {branch_name} (created from the current HEAD — no remote base)")
            else:
                log(blocked(
                    f"could not create {branch_name} ({local_err})",
                    "no PR will be raised; refusing to commit onto the "
                    "checked-out branch",
                    f"git -C {workspace} status"))
                write_skipped("could not create a fix branch; refusing to commit to the "
                              "current branch", infra=True)
                return

    candidates_json  = []
    fixes            = []
    unverified_fixes = []
    # Edits that fixed the element they targeted but left the test failing on a
    # later one. Kept on disk and committed like any other fix — the run is not
    # green yet, so they are reported separately from the ones that are.
    advanced_fixes = []
    failed_fixes     = []

    # ── Phase A — understand every failure before fixing any of them ──────────
    # No model calls here. Building all the contexts first is what makes it
    # possible to see that several tests are dying on the SAME locator.
    contexts, live_issues = [], []
    for issue in eligible:
        test_name = issue["test_name"]
        prev_output = prev_test_outputs.get(test_name, "")
        ctx = build_candidate_context(issue, workspace, prev_output, repo_conventions)

        if ctx["trace_path"]:
            ctx["trace_timeline"] = format_trace(read_actions(Path(ctx["trace_path"])))

        ctx_slim = {k: v for k, v in ctx.items() if k != "repo_conventions"}
        candidates_json.append(ctx_slim)

        if not ctx["test_file"] or not Path(ctx["test_file"]).exists():
            log(f"  No test file found — skipping {test_name}")
            failed_fixes.append({**ctx_slim, "status": "no_file", "fix_diff": "",
                                  "test_passed": False, "test_output": ""})
            continue

        contexts.append(ctx)
        live_issues.append(issue)

    # ── Phase B — group failures by the locator that actually broke ───────────
    clusters = build_clusters(contexts, live_issues)
    if clusters:
        log(f"{len(contexts)} failing test(s) → {len(clusters)} distinct broken locator(s)")
        for cluster in clusters:
            if cluster.size > 1:
                log(f"  ×{cluster.size}  {cluster.describe()}")
                for name in cluster.test_names:
                    log(f"         {name}")

    # ── Phase C — budget is per FIX, not per test ─────────────────────────────
    # One edit can green several tests, so capping on tests would throw away work
    # that costs nothing extra. Largest clusters first, so a capped run fixes
    # whatever unblocks the most tests.
    attempted, deferred = clusters[:MAX_FIXES], clusters[MAX_FIXES:]
    if deferred:
        deferred_tests = sum(c.size for c in deferred)
        log(f"Attempting {len(attempted)} of {len(clusters)} locator fixes "
            f"(MAX_FIXES={MAX_FIXES}) — {len(deferred)} deferred, "
            f"covering {deferred_tests} test(s)")
        for cluster in deferred:
            for ctx_d in cluster.contexts:
                failed_fixes.append({
                    **{k: v for k, v in ctx_d.items() if k != "repo_conventions"},
                    "status": "deferred", "fix_diff": "",
                    "unfixable_reason": (f"not attempted this run — {len(clusters)} distinct "
                                         f"locators found, MAX_FIXES={MAX_FIXES}"),
                    "test_passed": False, "test_output": "",
                })

    # ── Phase D — one investigation, one edit, per broken locator ─────────────
    for cluster in attempted:
        ctx = cluster.representative
        test_name = ctx["test_name"]
        # The model should reason about the element, not one test's view of it.
        ctx["element_names"] = cluster.merged_element_names()
        ctx["affected_tests"] = cluster.test_names
        issue = cluster.issues[cluster.contexts.index(ctx)]

        if cluster.size > 1:
            log(f"Fixing: {cluster.describe()} — affects {cluster.size} tests")
        else:
            log(f"Fixing: {test_name}")
        log(f"  File: {ctx['test_file']}")
        if ctx["element_names"]:
            log(f"  Elements: {ctx['element_names'][:5]}")
        if ctx["page_objects"]:
            log(f"  Page objects: {[po['path'] for po in ctx['page_objects']]}")
        else:
            log("  WARNING: no page object matched — Claude will have no locator "
                "declarations to work from")

        def fail_cluster(status: str, reason: str = "", output: str = "", diff: str = ""):
            """Record every test in this cluster as unfixed for the same reason."""
            for member in cluster.contexts:
                slim = {k: v for k, v in member.items() if k != "repo_conventions"}
                failed_fixes.append({**slim, "status": status, "fix_diff": diff,
                                     "unfixable_reason": reason,
                                     "test_passed": False, "test_output": output})

        # Ask why the element was missing before assuming the locator is at
        # fault. A handoff from triaging never runs step 00, so this is the only
        # place the pipeline path gets asked the question at all.
        ctx["diagnosis"] = {}
        snapshot_soup, snapshot_prints = None, {}
        try:
            evidence = diagnosis.collect(issue, workspace=workspace,
                                         page_objects=ctx.get("page_objects"),
                                         audit_dir=AUDIT_DIR.parent)
            verdict = diagnosis.diagnose(evidence)
            ctx["diagnosis"] = verdict
            snapshot_soup = _snapshot_soup(issue)
            # Visibility lives in the sidecar, never in the saved markup: a
            # selector can match a real element that no user could have
            # clicked, and only the capture knows which.
            snapshot_prints = load_fingerprints(issue.get("dom_snapshot") or "")
            for line in diagnosis.describe(verdict, evidence):
                log(f"  {line}")
            # Probes run on the standalone path only, so a verdict reached here has
            # never been measured — it rests on inference alone. Part 1 also turned
            # more verdicts into stops, which means more chances to block a fix on a
            # guess. So this path gates only on HIGH confidence, where several
            # independent channels agreed; MEDIUM says what it would have done and
            # lets the existing behaviour proceed. The property that holds in both
            # paths: nothing blocks work unless it was measured or corroborated.
            # A page rebuilt in place reads as WRONG_PAGE at HIGH confidence, and
            # the remediation sends a human to investigate navigation that is not
            # broken. Regenerating a page object is far outside a locator edit, so
            # this does not attempt it — it drafts a change note for the agent
            # whose job it is, and says so.
            if adaptation_handoff.looks_restructured(evidence, verdict):
                queue = (REPO_ROOT / "agents" / "test-adaptation-agent" / "queue")
                drafted = adaptation_handoff.write_draft(queue, issue, evidence, verdict)
                verdict["restructured"] = True
                if drafted:
                    log(f"  {ctx.get('diagnosis', {}).get('verdict')} here means the "
                        f"page was REBUILT, not un-reached: the route still matches "
                        f"the last good run.")
                    log(f"  Drafted a change note for test-adaptation-agent: "
                        f"{drafted.name} — review it before running that agent.")
                else:
                    log("  Page looks rebuilt; a draft change note is already queued.")

            gate, note = should_gate(verdict, DIAGNOSIS_MODE, FORCE)
            if note:
                log(f"  ({note})")
            if gate:
                fail_cluster(verdict["verdict"].lower(),
                             reason="; ".join(verdict.get("reasons") or []),
                             output=verdict.get("remediation", ""))
                continue
        except Exception as exc:
            log(f"  Diagnosis failed ({exc}) — continuing with the locator fix")

        # What Locate already concluded, before anything expensive happens: the
        # park below is a whole Maven run, and a refusal must not pay for it.
        # A deliberate refusal there is a refusal here; anything else is Fix's to
        # own, and either way the model should be told rather than left to
        # re-derive it.
        outcome = locate_outcome(ctx)
        route, note = locate_route(outcome)
        ctx["locate_route"], ctx["locate_note"] = route, note
        ctx["locate_outcome"] = outcome
        log(f"  Locate: {route} — {note}")
        if route == "defer":
            fail_cluster("locate_refused", reason=note)
            continue

        # Ground the fix in the real DOM rather than in stale source. Four tiers,
        # best first:
        #   1. a browser still parked on the failing page (repairMode) — live and
        #      interactive, so a candidate selector can be counted for uniqueness;
        #   2. the DOM captured at the moment of failure — correct mid-flow state;
        #   3. re-opening the page in a browser — URL-addressable pages only;
        #   4. none of the above: the prompt says so and Claude infers.
        # A browser someone already parked (a developer ran with -DrepairMode=true)
        # always wins. Otherwise the agent parks one itself, but only from the
        # second attempt on — see park_browser_for_repair for why.
        failure_url = issue.get("failure_url", "") or ctx.get("page_url", "")
        repair_session = (find_repair_session(workspace, test_name, failure_url)
                          if INSPECT_DOM else {})
        if not repair_session and INSPECT_DOM:
            explicit = os.environ.get("REPAIR", "").lower() == "true"
            if explicit or FIX_ATTEMPT > 1:
                repair_session = park_browser_for_repair(workspace, test_name,
                                                         failure_url)

        ctx["dom_snapshot"] = load_dom_snapshot(issue, ctx["element_names"])

        if repair_session:
            ctx["dom_findings"] = inspect_live_dom(ctx, ctx["page_url"], workspace,
                                                   framework_props, repair_session)
        elif ctx["dom_snapshot"]:
            ctx["dom_findings"] = {
                "status": "not needed — failure-time DOM snapshot available",
                "selectors": {}, "page_dump": "", "absent": [], "raw": "",
            }
        elif INSPECT_DOM:
            ctx["dom_findings"] = inspect_live_dom(ctx, ctx["page_url"], workspace,
                                                   framework_props)
        else:
            ctx["dom_findings"] = {"status": "disabled (HEALING_INSPECT_DOM=false)",
                                   "selectors": {}, "page_dump": "", "absent": [], "raw": ""}

        ctx_slim = {k: v for k, v in ctx.items() if k != "repo_conventions"}

        # A locator the Locate step already found and proved needs no model.
        fix_json = None
        located = locate_resolution(ctx)
        if located:
            fix_json, why = build_located_fix(located, ctx, workspace)
            if fix_json:
                ctx["located"] = located
                log(f"  Located deterministically: {located.get('strategy')} "
                    f"score {located.get('score')} — no model call")
            else:
                log(f"  Located, but not applicable here ({why}) — asking the model")

        if fix_json is None:
            prompt = build_fix_prompt(ctx, fix_rules)
            log("  Calling Claude for fix...")
            response = call_claude(prompt, workspace,
                                   artifact_dir=(str(Path(ctx["screenshot"]).parent)
                                                 if ctx.get("screenshot") else ""))
            if not response:
                log("  Empty Claude response — skipping")
                fail_cluster("no_response")
                continue

            fix_json = extract_fix_json(response)
            if not fix_json:
                log("  Could not parse fix JSON — skipping")
                fail_cluster("parse_error", output=response[:500])
                continue

            if not fix_json.get("fixable", False):
                reason = fix_json.get("unfixable_reason", "Claude declared unfixable")
                log(f"  Unfixable: {reason}")
                fail_cluster("unfixable", reason=reason)
                continue

        target_file = Path(fix_json.get("target_file") or ctx["test_file"])
        if not target_file.is_absolute():
            target_file = workspace / target_file

        if not target_file.exists():
            log(f"  Target file not found: {target_file} — skipping")
            fail_cluster("target_not_found", reason=str(target_file))
            continue

        try:
            target_original = target_file.read_text(encoding="utf-8")
        except Exception as e:
            log(f"  Cannot read target file: {e}")
            fail_cluster("no_file", output=str(e))
            continue

        if fix_json.get("edits"):
            fixed_content, edit_err = apply_edits(target_original, fix_json["edits"])
        elif fix_json.get("fixed_content"):
            fixed_content, edit_err = fix_json["fixed_content"], ""
        else:
            fixed_content, edit_err = None, "response contained neither edits nor fixed_content"

        if not fixed_content:
            log(f"  Cannot apply fix: {edit_err}")
            fail_cluster("edit_failed", reason=edit_err, output=edit_err)
            continue

        valid, invalid_reason = validate_fix(target_original, fixed_content,
                                             target_file.name, MAX_FIX_DIFF_LINES)
        if FORCE and (ctx.get("diagnosis") or {}).get("verdict") in diagnosis.STOP:
            # Remember that this run overrode a stop verdict, so that a fix which
            # then verifies can be recorded as evidence the verdict was wrong.
            ctx["_forced_over_verdict"] = ctx["diagnosis"].get("verdict", "")

        if valid and not FORCE:
            # The re-run cannot catch a fix built on a wrong diagnosis, because
            # the easiest way to make an assertion pass is to weaken it. This
            # also applies when the diagnosis abstained: the asymmetry is that a
            # blocked fix is visible and retryable, while a weakened assertion
            # ships a permanently green broken test. FORCE is the way past it.
            valid, invalid_reason = validate_diagnosis_fit(
                target_original, fixed_content,
                (ctx.get("diagnosis") or {}).get("verdict", ""), snapshot_soup,
                snapshot_prints)
        # Guards built for test-adaptation-agent, evaluated here but never acting.
        # They are about to become load-bearing for edits far larger than a
        # locator, and the cheapest place to find out that one of them is wrong is
        # against real locator fixes that are known to be good. If any of these
        # ever reports would_reject on a fix that then verifies, the guard is
        # wrong — not the fix.
        ctx["shadow_guards"] = evaluate_shadow_guards(
            target_original, fixed_content, target_file, ctx)
        for entry in ctx["shadow_guards"]:
            if entry["would_reject"]:
                log(f"  [shadow] {entry['guard']} would have rejected: {entry['reason']}")

        if not valid:
            log(f"  Rejected by safety guard: {invalid_reason}")
            fail_cluster("rejected_unsafe", reason=invalid_reason, output=invalid_reason,
                         diff=compute_diff(target_original, fixed_content, target_file.name))
            continue

        fix_diff = compute_diff(target_original, fixed_content, target_file.name)
        fix_description = fix_json.get("fix_description", "")
        log(f"  Fix: {fix_description}")
        log_edits(target_file, target_original, fix_json.get("edits") or [], log)

        try:
            target_file.write_text(fixed_content, encoding="utf-8")
            invalidate_file(target_file)   # later clusters must see the edited file
        except Exception as e:
            log(f"  Cannot write fix: {e}")
            fail_cluster("write_error", output=str(e))
            continue

        # ── Phase E — one edit, but every affected test must prove it ─────────
        # A fix claiming to green 5 tests is only credited for the ones that
        # actually pass. Members that still fail keep their own failure record
        # so the next attempt re-investigates them separately.
        passed, still_failing, unverified = [], [], []
        for member in cluster.contexts:
            member_name = member["test_name"]
            log(f"  Verifying {member_name}...")
            member_started = time.time() - 1
            member_status, member_output = run_single_test(member_name, workspace)
            if member_status == "passed":
                passed.append(member_name)
            elif member_status == "unverified":
                unverified.append((member_name, member_output))
            else:
                still_failing.append((member_name, member_output, member,
                                      member_started))

        advanced, unchanged = split_by_progress(still_failing)

        record = {
            **ctx_slim,
            # ctx_slim was snapshotted before the model was called, so anything
            # computed after that has to be named here or it never reaches the
            # audit file — which for a shadow guard is the entire point of it.
            "shadow_guards": ctx.get("shadow_guards") or [],
            "target_file": str(target_file),
            "fix_description": fix_description,
            "fix_diff": fix_diff,
            "cluster_size": cluster.size,
            "cluster_description": cluster.describe(),
            "dom_verified": bool((ctx.get("dom_findings") or {}).get("selectors")
                                 or ctx.get("dom_snapshot")),
            "dom_source": (
                "live-parked-browser" if "parked" in (ctx.get("dom_findings") or {}).get("status", "")
                else "failure-snapshot" if ctx.get("dom_snapshot")
                else "live-browser" if (ctx.get("dom_findings") or {}).get("selectors")
                else "none"),
        }

        if not passed and not unverified and advanced and not unchanged:
            # Every test that still fails has moved on to a different element, so
            # the edit did its job. Keep it, and hand the next attempt the NEW
            # failure — refreshed from the artifacts this run just wrote — rather
            # than the one that is now repaired.
            log(f"  ➜  Fix kept — the repaired element no longer fails; "
                f"{len(advanced)} test(s) now stop at a different locator")
            for member_name, member_output, member, member_started, before, after in advanced:
                log(f"     {member_name.rsplit('.', 1)[-1]}: "
                    f"{failure_identity.describe(before)} → "
                    f"{failure_identity.describe(after)}")
                slim = {k: v for k, v in member.items() if k != "repo_conventions"}
                failed_fixes.append({
                    **slim, "status": "advanced", "verified": False,
                    "target_file": str(target_file),
                    "fix_description": fix_description, "fix_diff": fix_diff,
                    "test_passed": False, "test_output": member_output[-2000:],
                    # Kept so `02_ship` and the retry can tell a fix that worked
                    # and uncovered the next problem from one that did nothing.
                    "progressed_from": before, "progressed_to": after,
                    "next_issue": _refresh_issue(
                        cluster.issues[cluster.contexts.index(member)],
                        member_output, workspace, member_started),
                })
            advanced_fixes.append({**record, "status": "advanced",
                                   "verified": False, "test_passed": False,
                                   "test_names": [m[0] for m in advanced]})
            continue

        if not passed and not unverified:
            # The edit helped nobody — put the file back exactly as it was.
            log(f"  ❌ Fix failed every test in the cluster ({cluster.size})")
            for member_name, member_output, member in unchanged:
                log(f"     {member_name.rsplit('.', 1)[-1]} still fails on the same "
                    f"element: {failure_identity.describe(failure_identity.identify(member_output))}")
            try:
                target_file.write_text(target_original, encoding="utf-8")
                invalidate_file(target_file)
                # Otherwise the next attempt looks like it mysteriously went back
                # to the original selector, with nothing saying the edit was undone.
                log(f"  ↩︎  Reverted {target_file.name} — the file is back to its "
                    f"pre-fix state, so attempt {FIX_ATTEMPT + 1} starts clean")
            except Exception as e:
                log(f"  WARNING: could not revert {target_file.name}: {e}")
            for member_name, member_output, member, _started in still_failing:
                slim = {k: v for k, v in member.items() if k != "repo_conventions"}
                failed_fixes.append({**slim, "status": "test_failed", "verified": False,
                                     "target_file": str(target_file),
                                     "fix_description": fix_description, "fix_diff": fix_diff,
                                     "test_passed": False, "test_output": member_output[-2000:]})
            continue

        if passed:
            log(f"  ✅ Fix verified — {len(passed)}/{cluster.size} test(s) now passing")
            overridden = ctx.get("_forced_over_verdict")
            if overridden:
                # Someone read the diagnosis, overrode it, and the fix worked. That
                # is the only direct evidence available that a stop verdict was
                # wrong, and nothing else in the system would have noticed.
                verdict_feedback.record(
                    KNOWN_ISSUES_FILE, "false_stop", ctx["test_name"], overridden,
                    detail="a forced locator fix verified, so this verdict should "
                           "not have stopped the run",
                    session=os.environ.get("SESSION_ID", ""))
                log(f"  Recorded a false stop for {overridden} in "
                    f"feedback/known-issues.json")
            # The locator moved, so the fingerprint describing it is now stale.
            # The next green run would re-record it anyway, but leaving it behind
            # means the PR carries a baseline that describes the element the test
            # no longer uses — and any heal attempted before that next green run
            # compares against the pre-drift element.
            _refresh_baseline_after_heal(ctx, workspace)
            fixes.append({**record, "status": "success", "verified": True,
                          "test_name": passed[0], "test_names": passed,
                          "test_passed": True,
                          "test_output": f"{len(passed)} test(s) passed"})
        if unverified:
            log(f"  ⚠️  {len(unverified)} test(s) applied but NOT verified")
            unverified_fixes.append({**record, "status": "applied_unverified", "verified": False,
                                     "test_name": unverified[0][0],
                                     "test_names": [n for n, _ in unverified],
                                     "test_passed": False, "test_output": unverified[0][1][-500:]})
        for member_name, member_output, member, _started in still_failing:
            # The fix worked for its cluster but not this test — a different
            # root cause hiding behind the same symptom. Keep it separate.
            log(f"  ❌ Still failing after the cluster fix: {member_name}")
            slim = {k: v for k, v in member.items() if k != "repo_conventions"}
            failed_fixes.append({**slim, "status": "test_failed", "verified": False,
                                 "target_file": str(target_file),
                                 "fix_description": fix_description, "fix_diff": fix_diff,
                                 "test_passed": False, "test_output": member_output[-2000:]})

    # Merge in anything earlier attempts already committed, so a later attempt
    # never reports a landed fix as missing.
    def merge(current: list, carried: list) -> list:
        seen = {f["test_name"] for f in current}
        return current + [f for f in carried if f["test_name"] not in seen]

    fixes            = merge(fixes, carried_fixes)
    unverified_fixes = merge(unverified_fixes, carried_unverified)

    # Commit everything that was applied this attempt (carried entries are
    # already committed, and git add on an unchanged file is a no-op anyway).
    pr_branch = None
    applied = [f for f in fixes + unverified_fixes + advanced_fixes
               if f.get("target_file")]
    if applied and not on_branch:
        # Both a dry run and a failed branch switch land here, and both mean "do
        # not commit" — but only one of them is news. AUTO_PUSH tells them apart:
        # with it off the user asked for exactly this; with it on they asked for a
        # PR and are not getting one.
        review = f"{len(applied)} edit(s) left uncommitted in the working tree — " \
                 f"review with: git -C {workspace} diff"
        log(review if not AUTO_PUSH else blocked(
            "the fix branch was never checked out",
            f"no PR will be raised; {len(applied)} edit(s) left uncommitted",
            f"git -C {workspace} diff"))
    elif applied:
        for fix in applied:
            ok, _, err = run_git(["add", fix["target_file"]], workspace)
            if not ok:
                log(f"Warning: git add failed for {fix['target_file']}: {err}")

        fixed_names = ", ".join(f['test_name'].split(".")[-1] for f in applied[:5])
        commit_msg = (
            f"healing: repair locators for {len(applied)} test(s) ({build_tag})\n\n"
            f"Fixed: {fixed_names}\n\n"
            f"Session: {SESSION_ID}"
        )
        ok, out, err = run_git(["commit", "-m", commit_msg], workspace)
        if ok:
            log(f"Committed {len(applied)} fix(es) to {branch_name}")
            pr_branch = branch_name
            _commit_baselines(workspace, build_tag)
        elif "nothing to commit" in f"{out}{err}".lower():
            # Everything applied this attempt was already committed by an earlier
            # one. git reports this on stdout, not stderr.
            log("Nothing new to commit — reusing existing branch")
            pr_branch = branch_name
        else:
            log(blocked(f"commit failed ({(err or out).strip()[:120]})",
                        "no PR will be raised; edits remain in the working tree",
                        f"git -C {workspace} status"))

    # Gate
    _stop_statuses = {v.lower() for v in diagnosis.STOP}
    _diagnosed = [f for f in failed_fixes if f.get("status") in _stop_statuses]
    _real_failures = [f for f in failed_fixes if f.get("status") not in _stop_statuses]

    if not fixes and not unverified_fixes and not failed_fixes:
        gate = "skipped"
    elif _real_failures:
        gate = "false"
    elif _diagnosed and not fixes and not unverified_fixes:
        # Nothing was attempted and nothing is pending: the run produced a
        # diagnosis instead of a fix. That is an outcome, not a failure, so it
        # must not retry or page anyone.
        gate = "skipped"
        (AUDIT_DIR / ".skip-reason").write_text(
            diagnosis.skip_reason(_diagnosed[0]["status"].upper()))
        log(f"Diagnosed {len(_diagnosed)} cluster(s) as not locator-shaped — "
            f"no fix attempted")
    else:
        gate = "true"

    write_gate(gate)
    _tests_fixed = sum(len(f.get("test_names") or [f.get("test_name")]) for f in fixes)
    log(f"Gate: .fix-passed = {gate} ({len(fixes)} edit(s) → {_tests_fixed} test(s) verified, "
        f"{len(unverified_fixes)} unverified, {len(failed_fixes)} failed)")

    # Write JSON
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    def test_count(entries: list) -> int:
        """Fix entries can cover several tests; failures are always one each."""
        return sum(len(e.get("test_names") or [e.get("test_name")]) for e in entries)

    tests_fixed      = test_count(fixes)
    tests_unverified = test_count(unverified_fixes)

    result = {
        "timestamp":      ts,
        "build_tag":      build_tag,
        "fix_attempt":    FIX_ATTEMPT,
        "eligible_count": len(eligible),
        "attempted":      tests_fixed + tests_unverified + len(failed_fixes),
        "succeeded":      tests_fixed,
        "unverified":     tests_unverified,
        "failed":         len(failed_fixes),
        # How much rework clustering avoided: one edit can green several tests.
        "distinct_fixes":     len(fixes),
        "distinct_unverified": len(unverified_fixes),
        "pr_branch":      pr_branch,
        "candidates":     candidates_json,
        "fixes":          fixes,
        "unverified_fixes": unverified_fixes,
        "failed_fixes":   failed_fixes,
    }
    # Every attempt overwrote this file, so a retry destroyed the record of what
    # the previous attempt had actually tried. The only trace of a reverted edit
    # was incidental — buried inside the next prompt's prev_test_output. Keep a
    # compact, append-only history so "what did fix 1 change?" stays answerable.
    result["attempts"] = _attempt_history(result)

    json_path = AUDIT_DIR / "01-fix.json"
    json_path.write_text(json.dumps(result, indent=2, default=str))
    log(f"Wrote 01-fix.json ({json_path.stat().st_size // 1024}KB)")

    # Write Markdown
    md_lines = [
        "# Fix Results",
        "",
        f"**Build Tag:** {build_tag}  ",
        f"**Attempt:** {FIX_ATTEMPT}  ",
        f"**Eligible tests:** {len(eligible)} | **Distinct locator fixes:** {len(fixes)} | "
        f"**Tests verified:** {tests_fixed} | "
        f"**Applied but unverified:** {tests_unverified} | **Failed:** {len(failed_fixes)}  ",
        f"**Gate:** `{gate}`  ",
        f"**PR Branch:** `{pr_branch or 'none'}`",
        "",
    ]

    # What each attempt actually changed — the question the console log could
    # not answer once an attempt had been reverted.
    history = result.get("attempts") or []
    if len(history) > 1 or any(h.get("entries") for h in history):
        md_lines += ["## What each attempt changed", ""]
        for h in history:
            md_lines.append(f"**Attempt {h.get('attempt')}**")
            if not h.get("entries"):
                md_lines += ["", "- nothing was applied", ""]
                continue
            for e in h["entries"]:
                verdict = e.get("outcome", "?")
                if e.get("reverted"):
                    verdict += " — reverted"
                tgt = Path(e["target_file"]).name if e.get("target_file") else "(no file)"
                md_lines.append(f"- `{e.get('test_name')}` → **{verdict}** in `{tgt}`")
                if e.get("fix_description"):
                    md_lines.append(f"  - {e['fix_description']}")
                if e.get("unfixable_reason"):
                    md_lines.append(f"  - _{e['unfixable_reason']}_")
                if e.get("fix_diff"):
                    md_lines += ["", "  ```diff", *(f"  {ln}" for ln in
                                 e["fix_diff"].splitlines()[:40]), "  ```"]
            md_lines.append("")

    def fix_block(f: dict, icon: str) -> list:
        # Entries carried forward from an earlier attempt are rehydrated from
        # JSON and may predate a field, so every lookup here is defensive.
        dom = " _(selector confirmed in a live browser)_" if f.get("dom_verified") else ""
        names = f.get("test_names") or [f.get("test_name", "(unknown test)")]
        heading = (f"{f.get('cluster_description', names[0])} — {len(names)} tests"
                   if len(names) > 1 else names[0])
        covered = ("\n".join(f"  - `{n}`" for n in names) + "\n") if len(names) > 1 else ""
        return [
            f"### {icon} {heading}",
            covered,
            f"- **File:** `{f.get('target_file') or f.get('test_file') or '(unknown)'}`",
            f"- **Root Cause:** {f.get('root_cause') or '(not recorded)'}",
            f"- **Fix:** {f.get('fix_description', '')}{dom}",
            "",
            "```diff",
            (f.get("fix_diff") or "")[:2000] or "(no diff)",
            "```",
            "",
        ]

    if fixes:
        md_lines += ["## Verified Fixes", ""]
        for f in fixes:
            md_lines += fix_block(f, "✅")
    if unverified_fixes:
        md_lines += ["## Applied but NOT Verified", "",
                     "> No test runner was available, so these changes were never executed.", ""]
        for f in unverified_fixes:
            md_lines += fix_block(f, "⚠️")
    if failed_fixes:
        md_lines += ["## Failed Fixes", ""]
        for f in failed_fixes:
            status = f.get("status", "unknown")
            md_lines += [
                f"### ❌ {f.get('test_name', '(unknown test)')} (`{status}`)",
                f"- **Root Cause:** {f.get('root_cause') or '(not recorded)'}",
            ]
            if status in ("unfixable", "rejected_unsafe", "edit_failed"):
                md_lines.append(f"- **Reason:** {f.get('unfixable_reason', '')}")
            elif status == "test_failed":
                md_lines.append("- **Fix applied but test still failing**")
                md_lines += ["```", f.get("test_output", "")[-400:], "```"]
            md_lines.append("")

    (AUDIT_DIR / "01-fix.md").write_text("\n".join(md_lines) + "\n")
    log(f"Done — {len(fixes)} edit(s) fixed {tests_fixed} test(s), "
        f"{tests_unverified} unverified, {len(failed_fixes)} failed")


if __name__ == "__main__":
    main()
