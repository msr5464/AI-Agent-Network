"""Admin-configurable agent settings, persisted to config/.env.

Mirrors the schema-first approach AI-Test-Studio uses for its own config
(backend/services/settings_service.py): a list of field descriptors is served to
the browser, a generic renderer turns it into inputs, and saves are written back
into config/.env in place with comments and unrelated keys preserved.

Two writes happen on every save, and both matter:

  1. config/.env  — so the value survives a server restart. Written atomically
     (tempfile + os.replace, same as storage.py) because every agent run sources
     this file; a truncated .env breaks all three agents.
  2. os.environ   — so the value applies to the *next run* without a restart.
     runner.py builds each run's environment from os.environ.copy(), and
     shared/load_env.sh deliberately re-applies caller-exported vars after
     sourcing the .env files so the server's exports win.

Values are never logged — see shared/log.py and shared/session.sh for the
redaction this repo applies everywhere else.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qa_agents_server.paths import AGENTS_DIR, CONFIG_ENV_FILE, REPO_ROOT

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------
# Each entry describes one configurable setting. Field contract is identical to
# AI-Test-Studio's SETTINGS_SCHEMA so the frontend renderer is shared in spirit:
#   key         - internal key used in API and frontend
#   env_var     - exact os.environ / .env key
#   label       - display label in UI
#   description - help text shown under the input
#   type        - text | password | number | boolean | select
#   category    - common | authoring | healing | triaging | server
#   default     - default value (used when key absent from env)
#   sensitive   - if True, value is partially masked in API responses
#   options     - list of {"value": ..., "label": ...} for type=select
#   min / max   - for type=number
#
# Deliberately NOT exposed: per-invocation vars the server sets on each run
# (TEST_NAME, FORCE, REPAIR, BUILD_TAG, MODULE, SESSION_ID, AUDIT_DIR,
# AGENT_DIR, REPO_ROOT, HANDOFF_FILE, INPUT_FILE, FIX_ATTEMPT, START_FROM_STEP).
# Writing those into config/.env would pin every run to one test.

_EFFORT_OPTIONS = [
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
]

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {
        "key": "automation_framework",
        "env_var": "AUTOMATION_FRAMEWORK",
        "label": "Automation Framework",
        "description": "Select the automation framework used in your target repository.",
        "type": "select",
        "options": [
            {"label": "Playwright", "value": "playwright"},
            {"label": "Selenium", "value": "selenium"}
        ],
        "category": "common",
        "default": "playwright",
        "sensitive": False,
    },
    # ── test-adaptation-agent ────────────────────────────────────────────────
    {"key": "adaptation_model", "env_var": "ADAPTATION_MODEL", "label": "Adaptation model",
     "description": "Claude model used to classify the change note and write edits.",
     "type": "text", "category": "adaptation", "default": "claude-opus-5", "sensitive": False},
    {"key": "adaptation_apply", "env_var": "ADAPTATION_APPLY", "label": "Apply edits",
     "description": "Off = propose-only: full diffs and guard results are recorded "
                    "but nothing is written. Turn on once the proposals are being "
                    "accepted verbatim.",
     "type": "boolean", "category": "adaptation", "default": False, "sensitive": False},
    {"key": "adaptation_verify_policy", "env_var": "ADAPTATION_VERIFY_POLICY",
     "label": "Verification scope",
     "description": "named_only re-runs the tests the change note named; tiered adds "
                    "the shared surface. The verify set holds the single global run "
                    "slot, so 'all' can make the platform single-tasked for an hour.",
     "type": "select", "category": "adaptation", "default": "named_only",
     "options": [
         {"value": "named_only", "label": "named_only"},
         {"value": "tiered", "label": "tiered"},
         {"value": "all", "label": "all"}
     ], "sensitive": False},
    {"key": "adaptation_explore_timeout_s", "env_var": "ADAPTATION_EXPLORE_TIMEOUT_S",
     "label": "Exploration budget (s)",
     "description": "Wall-clock limit for one browser exploration.",
     "type": "number", "category": "adaptation", "default": 1800, "min": 60, "max": 7200, "sensitive": False},
    {"key": "adaptation_explore_attempts", "env_var": "ADAPTATION_EXPLORE_ATTEMPTS",
     "label": "Extra exploration attempts",
     "description": "Full re-runs on a recoverable failure. Each one restarts the "
                    "flow in a fresh browser — there is no mid-flow resume.",
     "type": "number", "category": "adaptation", "default": 1, "min": 0, "max": 3, "sensitive": False},
    {"key": "adaptation_retry_count", "env_var": "ADAPTATION_RETRY_COUNT",
     "label": "Adapt retry count",
     "description": "Re-runs of the adapt step when verification fails. Each attempt is "
                    "handed the previous one's unapplied items, so a second failure on the "
                    "same item is evidence the approach is wrong.",
     "type": "number", "category": "adaptation", "default": 2, "min": 1, "max": 10, "sensitive": False},
    {"key": "adaptation_max_files", "env_var": "ADAPTATION_MAX_FILES_PER_RUN",
     "label": "Max files per run",
     "description": "Exceeding this flips the run to propose-only rather than "
                    "truncating the work.",
     "type": "number", "category": "adaptation", "default": 6, "min": 1, "max": 20, "sensitive": False},
    {"key": "adaptation_max_total_diff", "env_var": "ADAPTATION_MAX_TOTAL_DIFF_LINES",
     "label": "Max changed lines per run",
     "description": "Total across all files. A reviewer has to read this.",
     "type": "number", "category": "adaptation", "default": 200, "min": 20, "max": 1000, "sensitive": False},
    {"key": "adaptation_blast_max_tests", "env_var": "ADAPTATION_BLAST_MAX_TESTS",
     "label": "Max tests in scope",
     "description": "Above this the change is bigger than one agent run and escalates.",
     "type": "number", "category": "adaptation", "default": 40, "min": 1, "max": 500, "sensitive": False},
    {"key": "adaptation_hub_threshold", "env_var": "ADAPTATION_HUB_THRESHOLD",
     "label": "Hub threshold",
     "description": "A class referenced by more files than this is shared "
                    "infrastructure and does not propagate the blast radius.",
     "type": "number", "category": "adaptation", "default": 8, "min": 2, "max": 100, "sensitive": False},
    {"key": "adaptation_branch_prefix", "env_var": "ADAPTATION_BRANCH_PREFIX",
     "label": "Branch prefix",
     "description": "Branch name is <prefix>/<module>-<timestamp>.",
     "type": "text", "category": "adaptation", "default": "adaptation", "sensitive": False},
    {"key": "adaptation_sandbox", "env_var": "ADAPTATION_SANDBOX", "label": "Sandbox environment",
     "description": "Assert the target environment is disposable, allowing "
                    "exploration to walk a destructive final step. Requires "
                    "ADAPTATION_SANDBOX_NOTE, which is reproduced in the PR body.",
     "type": "boolean", "category": "adaptation", "default": False, "sensitive": False},

    # ── Common ───────────────────────────────────────────────────────────────
    {
        "key": "workspace_dir",
        "env_var": "WORKSPACE_DIR",
        "label": "Workspace Directory",
        "description": "Parent directory that contains the automation repo. Must be an "
                       "absolute path OUTSIDE of QA-Agent-Network.",
        "type": "text",
        "category": "common",
        "default": "",
        "sensitive": False,
    },
    {
        "key": "framework_dir",
        "env_var": "FRAMEWORK_DIR",
        "label": "Automation Repo Path",
        "description": "Absolute path to the automation repo checkout, overriding "
                       "<workspace>/<automation repo>. Leave blank unless the checkout "
                       "is named differently or lives elsewhere.",
        "type": "text",
        "category": "common",
        "default": "",
        "sensitive": False,
    },
    {
        "key": "github_org",
        "env_var": "GITHUB_ORG",
        "label": "GitHub Org",
        "description": "GitHub org or username that owns the automation repo",
        "type": "text",
        "category": "common",
        "default": "",
        "sensitive": False,
    },
    {
        "key": "github_repo_automation",
        "env_var": "GITHUB_REPO_AUTOMATION",
        "label": "Automation Repo",
        "description": "Name of the automation repo — the directory under the workspace directory, and the repo name on GitHub. Required even when Automation Repo Path is set.",
        "type": "text",
        "category": "common",
        "default": "Jarvis",
        "sensitive": False,
    },
    {
        "key": "github_default_branch",
        "env_var": "GITHUB_DEFAULT_BRANCH",
        "label": "Default Branch",
        "description": ("Default base branch: agents check the automation repo "
                        "out on it and raise their PRs against it. A run can "
                        "override it from the run panel's branch field."),
        "type": "text",
        "category": "common",
        "default": "main",
        "sensitive": False,
    },
    {
        "key": "github_pr_reviewers",
        "env_var": "GITHUB_PR_REVIEWERS",
        "label": "PR Reviewers",
        "description": "Comma-separated list of GitHub reviewer handles (optional)",
        "type": "text",
        "category": "common",
        "default": "",
        "sensitive": False,
    },
    {
        "key": "github_token",
        "env_var": "GITHUB_TOKEN",
        "label": "GitHub Token",
        "description": "Personal access token with repo scope. Required for PR creation.",
        "type": "password",
        "category": "common",
        "default": "",
        "sensitive": True,
    },
    {
        "key": "slack_bot_token",
        "env_var": "SLACK_BOT_TOKEN",
        "label": "Slack Bot Token",
        "description": "Bot token used to post run summaries and failure alerts",
        "type": "password",
        "category": "common",
        "default": "",
        "sensitive": True,
    },
    {
        "key": "slack_notify_channel",
        "env_var": "SLACK_NOTIFY_CHANNEL",
        "label": "Slack Notify Channel",
        "description": "Channel for routine run summaries (e.g. #qa-reports)",
        "type": "text",
        "category": "common",
        "default": "#qa-reports",
        "sensitive": False,
    },
    {
        "key": "slack_alert_channel",
        "env_var": "SLACK_ALERT_CHANNEL",
        "label": "Slack Alert Channel",
        "description": "Channel for failures and critical alerts (e.g. #qa-critical)",
        "type": "text",
        "category": "common",
        "default": "#qa-critical",
        "sensitive": False,
    },
    {
        "key": "claude_cli_path",
        "env_var": "CLAUDE_CLI_PATH",
        "label": "Claude CLI Path",
        "description": "Path to the claude CLI binary. Leave as 'claude' if it is on PATH.",
        "type": "text",
        "category": "common",
        "default": "claude",
        "sensitive": False,
    },
    {
        "key": "auto_push",
        "env_var": "AUTO_PUSH",
        "label": "Auto Push & Create PR",
        "description": "When off, agents apply and test changes locally but skip push and "
                       "PR creation (dry run)",
        "type": "boolean",
        "category": "common",
        "default": False,
        "sensitive": False,
    },
    {
        "key": "playwright_headless",
        "env_var": "PLAYWRIGHT_HEADLESS",
        "label": "Headless Browser",
        "description": "Turn off to watch every browser any agent starts — selector "
                       "validation, DOM inspection, exploration, session login, and the "
                       "test runs themselves. Useful for debugging; keep on for CI runs.",
        "type": "boolean",
        "category": "common",
        "default": True,
        "sensitive": False,
    },
    {
        "key": "testing_mode",
        "env_var": "TESTING_MODE",
        "label": "Testing Mode (cache steps)",
        "description": "Cache parse and validate-web outputs per feature so they are reused "
                       "on re-runs. Useful when iterating on code generation.",
        "type": "boolean",
        "category": "common",
        "default": False,
        "sensitive": False,
    },
    # ── Test Authoring ───────────────────────────────────────────────────────
    {
        "key": "authoring_model",
        "env_var": "AUTHORING_MODEL",
        "label": "Claude Model",
        "description": "Model used for all AI steps: parse, validate, generate, fix",
        "type": "text",
        "category": "authoring",
        "default": "claude-opus-4-6",
        "sensitive": False,
    },
    {
        "key": "authoring_branch_prefix",
        "env_var": "AUTHORING_BRANCH_PREFIX",
        "label": "Branch Prefix",
        "description": "Full branch name becomes <prefix>/<feature>-<timestamp>",
        "type": "text",
        "category": "authoring",
        "default": "authoring",
        "sensitive": False,
    },
    {
        "key": "authoring_environment",
        "env_var": "AUTHORING_ENVIRONMENT",
        "label": "Environment",
        "description": "Passed as -Denvironment when verifying generated tests",
        "type": "text",
        "category": "authoring",
        "default": "staging",
        "sensitive": False,
    },
    {
        "key": "authoring_country",
        "env_var": "AUTHORING_COUNTRY",
        "label": "Country",
        "description": "Passed as -Dcountry when verifying generated tests",
        "type": "text",
        "category": "authoring",
        "default": "SG",
        "sensitive": False,
    },
    {
        "key": "authoring_fix_retry_count",
        "env_var": "AUTHORING_FIX_RETRY_COUNT",
        "label": "Fix Retry Count",
        "description": "Fix-and-retry cycles for a failing generated test before shipping "
                       "with a NEEDS-REVIEW verdict. The initial run is not counted. The "
                       "loop also stops early on its own once an attempt can bring nothing "
                       "new \u2014 so this is a ceiling, not a target.",
        "type": "number",
        "category": "authoring",
        "default": 2,
        "sensitive": False,
        "min": 1,
        "max": 10,
    },
    {
        "key": "authoring_playwright_timeout_ms",
        "env_var": "AUTHORING_PLAYWRIGHT_TIMEOUT_MS",
        "label": "Playwright Step Timeout (ms)",
        "description": "Timeout for each Playwright step during selector validation",
        "type": "number",
        "category": "authoring",
        "default": 30000,
        "sensitive": False,
        "min": 1000,
        "max": 600000,
    },
    # ── Test Healing ─────────────────────────────────────────────────────────
    {
        "key": "healing_model",
        "env_var": "HEALING_MODEL",
        "label": "Claude Model",
        "description": "Model used to generate locator fixes",
        "type": "text",
        "category": "healing",
        "default": "claude-opus-4-6",
        "sensitive": False,
    },
    {
        "key": "healing_max_fixes_per_run",
        "env_var": "HEALING_MAX_FIXES_PER_RUN",
        "label": "Max Fixes Per Run",
        "description": "Maximum number of DISTINCT LOCATOR FIXES per session — not tests. "
                       "One fix can green several tests, so 30 failures caused by 6 broken "
                       "locators fit in a budget of 6. Clusters are attempted largest-first; "
                       "any left over are reported as deferred rather than dropped.",
        "type": "number",
        "category": "healing",
        "default": 5,
        "sensitive": False,
        "min": 1,
        "max": 50,
    },
    {
        "key": "healing_retry_count",
        "env_var": "HEALING_RETRY_COUNT",
        "label": "Fix Retry Count",
        "description": "Retry cycles when a locator fix fails verification. An attempt that "
                       "repairs one locator and uncovers the next keeps its edit, so the loop "
                       "walks a chain of broken locators rather than re-guessing at one \u2014 "
                       "which is why this is higher than the authoring agent's.",
        "type": "number",
        "category": "healing",
        "default": 4,
        "sensitive": False,
        "min": 1,
        "max": 10,
    },
    {
        "key": "healing_branch_prefix",
        "env_var": "HEALING_BRANCH_PREFIX",
        "label": "Branch Prefix",
        "description": "Full branch name becomes <prefix>/<build-tag>",
        "type": "text",
        "category": "healing",
        "default": "healing",
        "sensitive": False,
    },
    {
        "key": "healing_inspect_dom",
        "env_var": "HEALING_INSPECT_DOM",
        "label": "Live DOM Inspection",
        "description": "Before asking for a fix, open the failing page in a real browser and "
                       "read the element's actual selector. Only used when the handoff carries "
                       "no failure-time DOM snapshot — a snapshot always wins, since a live "
                       "browser cannot reproduce mid-flow state.",
        "type": "boolean",
        "category": "healing",
        "default": True,
        "sensitive": False,
    },
    {
        "key": "healing_base_url",
        "env_var": "HEALING_BASE_URL",
        "label": "Page URL Override",
        "description": "Explicit page URL for DOM inspection, overriding whatever is recovered "
                       "from the failure log. Leave blank to auto-recover.",
        "type": "text",
        "category": "healing",
        "default": "",
        "sensitive": False,
    },
    {
        "key": "healing_test_timeout_s",
        "env_var": "HEALING_TEST_TIMEOUT_S",
        "label": "Verification Timeout (s)",
        "description": "Timeout for a single verification test run",
        "type": "number",
        "category": "healing",
        "default": 300,
        "sensitive": False,
        "min": 30,
        "max": 7200,
    },
    {
        "key": "test_runner_cmd",
        "env_var": "TEST_RUNNER_CMD",
        "label": "Test Runner Command",
        "description": "Override the auto-detected runner. Placeholders: {class}, "
                       "{class_simple}, {method}. If no runner can be detected and this is "
                       "unset, fixes are applied but reported as UNVERIFIED — never as passing.",
        "type": "text",
        "category": "healing",
        "default": "",
        "sensitive": False,
    },
    # ── Test Triaging ────────────────────────────────────────────────────────
    {
        "key": "triaging_db_host",
        "env_var": "TRIAGING_DB_HOST",
        "label": "DB Host",
        "description": "Hostname of the test-results database",
        "type": "text",
        "category": "triaging",
        "default": "localhost",
        "sensitive": False,
    },
    {
        "key": "triaging_db_port",
        "env_var": "TRIAGING_DB_PORT",
        "label": "DB Port",
        "description": "Port of the test-results database",
        "type": "number",
        "category": "triaging",
        "default": 3306,
        "sensitive": False,
        "min": 1,
        "max": 65535,
    },
    {
        "key": "triaging_db_user",
        "env_var": "TRIAGING_DB_USER",
        "label": "DB User",
        "description": "Username for the test-results database",
        "type": "text",
        "category": "triaging",
        "default": "root",
        "sensitive": False,
    },
    {
        "key": "triaging_db_password",
        "env_var": "TRIAGING_DB_PASSWORD",
        "label": "DB Password",
        "description": "Password for the test-results database",
        "type": "password",
        "category": "triaging",
        "default": "",
        "sensitive": True,
    },
    {
        "key": "triaging_db_name",
        "env_var": "TRIAGING_DB_NAME",
        "label": "DB Name",
        "description": "Name of the test-results database",
        "type": "text",
        "category": "triaging",
        "default": "qa_results",
        "sensitive": False,
    },
    {
        "key": "triaging_classifier_model",
        "env_var": "TRIAGING_CLASSIFIER_MODEL",
        "label": "Classifier Model",
        "description": "Model used to classify each failure's root cause",
        "type": "text",
        "category": "triaging",
        "default": "claude-opus-4-6",
        "sensitive": False,
    },
    {
        "key": "triaging_classifier_effort",
        "env_var": "TRIAGING_CLASSIFIER_EFFORT",
        "label": "Classifier Effort",
        "description": "Reasoning effort for the classification pass",
        "type": "select",
        "category": "triaging",
        "default": "medium",
        "sensitive": False,
        "options": _EFFORT_OPTIONS,
    },
    {
        "key": "triaging_reviewer_model",
        "env_var": "TRIAGING_REVIEWER_MODEL",
        "label": "Reviewer Model",
        "description": "Model used to review and challenge the classifier's verdicts",
        "type": "text",
        "category": "triaging",
        "default": "claude-sonnet-4-6",
        "sensitive": False,
    },
    {
        "key": "triaging_reviewer_effort",
        "env_var": "TRIAGING_REVIEWER_EFFORT",
        "label": "Reviewer Effort",
        "description": "Reasoning effort for the review pass",
        "type": "select",
        "category": "triaging",
        "default": "medium",
        "sensitive": False,
        "options": _EFFORT_OPTIONS,
    },
    {
        "key": "triaging_scout_lookback_days",
        "env_var": "TRIAGING_SCOUT_LOOKBACK_DAYS",
        "label": "Scout Lookback (days)",
        "description": "How far back to look for test runs when no build tag is given",
        "type": "number",
        "category": "triaging",
        "default": 7,
        "sensitive": False,
        "min": 1,
        "max": 365,
    },
    {
        "key": "triaging_max_review_rounds",
        "env_var": "TRIAGING_MAX_REVIEW_ROUNDS",
        "label": "Max Review Rounds",
        "description": "How many classify/review rounds to run before accepting the verdict",
        "type": "number",
        "category": "triaging",
        "default": 2,
        "sensitive": False,
        "min": 1,
        "max": 10,
    },
    {
        "key": "triaging_flaky_tests_last_runs",
        "env_var": "TRIAGING_FLAKY_TESTS_LAST_RUNS",
        "label": "Flaky Window (runs)",
        "description": "How many recent runs to inspect when deciding whether a test is flaky",
        "type": "number",
        "category": "triaging",
        "default": 10,
        "sensitive": False,
        "min": 2,
        "max": 200,
    },
    {
        "key": "triaging_flaky_tests_min_failures",
        "env_var": "TRIAGING_FLAKY_TESTS_MIN_FAILURES",
        "label": "Flaky Threshold (failures)",
        "description": "Failures within that window before a test is labelled flaky",
        "type": "number",
        "category": "triaging",
        "default": 5,
        "sensitive": False,
        "min": 1,
        "max": 200,
    },
    {
        "key": "triaging_dashboard_base_url",
        "env_var": "TRIAGING_DASHBOARD_BASE_URL",
        "label": "Dashboard Base URL",
        "description": "Used to build links back to the QA dashboard in reports",
        "type": "text",
        "category": "triaging",
        "default": "",
        "sensitive": False,
    },
    {
        "key": "triaging_jira_base_url",
        "env_var": "TRIAGING_JIRA_BASE_URL",
        "label": "Jira Base URL",
        "description": "Used to build issue links in reports",
        "type": "text",
        "category": "triaging",
        "default": "",
        "sensitive": False,
    },
    # ── Server ───────────────────────────────────────────────────────────────
    {
        "key": "qa_agent_run_timeout_seconds",
        "env_var": "QA_AGENT_RUN_TIMEOUT_SECONDS",
        "label": "Run Timeout (s)",
        "description": "Wall-clock budget for a single agent run before it is killed",
        "type": "number",
        "category": "server",
        "default": 7200,
        "sensitive": False,
        "min": 60,
        "max": 86400,
    },
    {
        "key": "qa_agent_stale_after_seconds",
        "env_var": "QA_AGENT_STALE_AFTER_SECONDS",
        "label": "Stale After (s)",
        "description": "How long a run may go without progress before the UI marks it stale",
        "type": "number",
        "category": "server",
        "default": 900,
        "sensitive": False,
        "min": 30,
        "max": 86400,
    },
]

_SCHEMA_BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SETTINGS_SCHEMA}

CATEGORIES: List[str] = ["common", "authoring", "healing", "adaptation",
                         "triaging", "server"]


class SettingsValidationError(Exception):
    """Raised when a submitted value would produce a config that breaks the agents."""

    def __init__(self, errors: Dict[str, str]):
        super().__init__("invalid settings")
        self.errors = errors


# ── Read ──────────────────────────────────────────────────────────────────────

def get(key: str, default: Any = None) -> Any:
    """Return the setting value: os.environ, then schema default, then `default`."""
    entry = _SCHEMA_BY_KEY.get(key)
    if entry is None:
        return default
    val = os.environ.get(entry["env_var"])
    if val is not None:
        return val
    return entry.get("default", default)


def get_all_for_api() -> Dict[str, Any]:
    """Return schema + current values safe for the frontend.

    Sensitive values are partially masked. `shadowed` flags any key that a
    lower-precedence .env file would override — see _find_shadowed().
    """
    values: Dict[str, Any] = {}
    for entry in SETTINGS_SCHEMA:
        raw = get(entry["key"])
        if entry["sensitive"]:
            values[entry["key"]] = _partial_mask(str(raw)) if raw not in (None, "") else ""
        else:
            values[entry["key"]] = raw if raw is not None else entry.get("default", "")
    return {
        "schema": SETTINGS_SCHEMA,
        "values": values,
        "categories": CATEGORIES,
        "env_file": str(CONFIG_ENV_FILE),
        "shadowed": _find_shadowed(),
    }


def _find_shadowed() -> Dict[str, str]:
    """Map key → path of a .env file that overrides config/.env for that key.

    shared/load_env.sh sources config/.env, then $REPO_ROOT/.env, then
    $AGENT_DIR/.env — last wins. Neither of the latter two exists today, but if
    one appears, a value saved here is silently ignored at run time. Surfacing
    that is the difference between "the setting did nothing" and a clear warning.
    """
    shadowed: Dict[str, str] = {}
    candidates = [REPO_ROOT / ".env"] + sorted(AGENTS_DIR.glob("*/.env"))
    for path in candidates:
        declared = _declared_keys(path)
        if not declared:
            continue
        for entry in SETTINGS_SCHEMA:
            if entry["env_var"] in declared:
                try:
                    rel = str(path.relative_to(REPO_ROOT))
                except ValueError:
                    rel = str(path)
                shadowed[entry["key"]] = rel
    return shadowed


def _declared_keys(path: Path) -> set:
    """Return the set of uncommented KEY= names declared in an env file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return {
        m.group(1)
        for m in (re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line) for line in text.splitlines())
        if m
    }


# ── Write ─────────────────────────────────────────────────────────────────────

def set_many(updates: Dict[str, Any]) -> None:
    """Save a batch of setting updates to config/.env and os.environ.

    Rules:
      - Unknown keys (not in SETTINGS_SCHEMA) are silently ignored.
      - Sensitive fields: if the submitted value matches the partial mask of the
        currently stored value, skip it so the real secret is preserved.

    Raises SettingsValidationError before writing anything if a value would
    produce a config the agents cannot run with.
    """
    resolved: Dict[str, Tuple[Dict[str, Any], str]] = {}  # key → (entry, env string)

    for key, value in updates.items():
        entry = _SCHEMA_BY_KEY.get(key)
        if entry is None:
            continue
        if entry["sensitive"]:
            current = str(get(key, "") or "")
            if current and value == _partial_mask(current):
                continue  # the display mask came back unchanged — keep the real value
        resolved[key] = (entry, _to_env_str(_coerce(entry, value)))

    errors = _validate({k: v[1] for k, v in resolved.items()})
    if errors:
        raise SettingsValidationError(errors)

    env_updates = {entry["env_var"]: env_str for entry, env_str in resolved.values()}
    if not env_updates:
        return

    with _lock:
        _update_env_file(env_updates)
        # Only after the file write succeeds, so a failed save does not leave the
        # process running on values that were never persisted.
        for env_var, env_str in env_updates.items():
            os.environ[env_var] = env_str


def _validate(env_by_key: Dict[str, str]) -> Dict[str, str]:
    """Return {key: message} for values that would break the agents."""
    errors: Dict[str, str] = {}

    workspace = env_by_key.get("workspace_dir")
    if workspace is not None:
        stripped = workspace.strip()
        if not stripped:
            # run.sh hard-exits on an empty WORKSPACE_DIR, so refuse to write one.
            errors["workspace_dir"] = "Workspace directory is required — agents cannot run without it."
        else:
            path = Path(stripped).expanduser()
            if not path.is_absolute():
                errors["workspace_dir"] = "Must be an absolute path."
            elif not path.is_dir():
                errors["workspace_dir"] = f"Not an existing directory: {path}"
            elif path == REPO_ROOT or REPO_ROOT in path.parents:
                # Only *inside* the repo is wrong. A parent directory holding both
                # this repo and the automation repo side by side is the normal setup.
                errors["workspace_dir"] = (
                    "Must be outside the QA-Agent-Network repo — the automation repo is "
                    "cloned here and would collide with it."
                )

    return errors


# ── .env file I/O ─────────────────────────────────────────────────────────────

def _update_env_file(updates: Dict[str, str]) -> None:
    """Update config/.env in place, preserving comments and unrelated keys.

    Written atomically: every agent run sources this file, so a partial write
    would break all three agents rather than just this save.
    """
    CONFIG_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = (
        CONFIG_ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if CONFIG_ENV_FILE.exists()
        else []
    )
    remaining = dict(updates)

    # Pass 1: update existing uncommented KEY= lines in place
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in remaining:
            env_var = m.group(1)
            lines[i] = f"{env_var}={remaining.pop(env_var)}\n"

    # Pass 2: append keys the file does not declare yet
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("\n# --- Settings updated via Admin UI ---\n")
        for env_var, val in remaining.items():
            lines.append(f"{env_var}={val}\n")

    _atomic_write("".join(lines))


def _atomic_write(text: str) -> None:
    """Write config/.env via a same-directory temp file + rename."""
    fd, tmp = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=str(CONFIG_ENV_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)  # the file holds tokens and DB credentials
        os.replace(tmp, CONFIG_ENV_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _partial_mask(value: str) -> str:
    """Show first 3 and last 3 characters; mask the middle.

    Values of 6 chars or fewer are masked entirely — showing 3+3 of a 6-char
    secret would reveal all of it.
    """
    s = str(value)
    if len(s) <= 6:
        return "***"
    return s[:3] + "**********" + s[-3:]


def _to_env_str(value: Any) -> str:
    """Convert a Python value to a string suitable for .env and os.environ.

    Booleans are written lowercase: run.sh and the agent actions compare against
    "true"/"false" (e.g. `[ "$AUTO_PUSH" = "true" ]`), so "True" would read as off.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce(entry: Dict[str, Any], value: Any) -> Any:
    """Coerce a submitted value to the type the schema declares."""
    t = entry.get("type")
    if t == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes", "on")
    if t == "number":
        if value == "" or value is None:
            return entry.get("default", 0)
        try:
            default = entry.get("default", 0)
            num = int(value) if isinstance(default, int) else float(value)
        except (TypeError, ValueError):
            return entry.get("default", 0)
        lo, hi = entry.get("min"), entry.get("max")
        if lo is not None:
            num = max(num, lo)
        if hi is not None:
            num = min(num, hi)
        return num
    if t == "select":
        allowed = [o["value"] for o in entry.get("options", [])]
        s = str(value) if value is not None else ""
        return s if s in allowed else entry.get("default", "")
    return str(value) if value is not None else ""
