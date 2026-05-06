# test-healing-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Picks up automation issues queued by `test-triaging-agent`, fixes broken locators via Claude,
verifies each fix by running the test, and ships a GitHub PR with all successful fixes.

Runs independently — no DB access required. All context comes from the handoff file.

One session = one handoff file = one PR (or Slack escalation if fixes fail).

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_fix.py    [Python + Claude]   Read handoff → fix locators → run tests → commit
  └─ 02_ship.py   [Python only]       Push branch → create PR → Slack notify
```

---

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Fix** | Read handoff, build context, call Claude, apply fix, run test, commit | No DB, no HTML parsing |
| **02 Ship** | Push branch, create PR, Slack notify | No AI, no code changes |

---

## Data Flow

```
agents/test-healing-agent/queue/<build_tag>.json   ← written by test-triaging-agent
    ↓
01-fix.json + .fix-passed   (per-test results, pr_branch)
    ↓
02-ship.json                (pr_url, slack_notified)
    ↓
queue/processed/<build_tag>.json   ← moved after completion
```

**Retry loop (in run.sh):** If `.fix-passed=false`, re-runs `01_fix.py` up to `MAX_FIX_ATTEMPTS`.
On retry, `01_fix.py` injects the previous test failure output into the Claude prompt so it
tries a different locator strategy.

---

## Handoff File Format

Written by `test-triaging-agent/actions/05_ship.py`. Contains everything needed — no DB required.

```json
{
  "build_tag": "ProdSanity-All-Tests-541",
  "created_at": "...",
  "source_session": "...",
  "source_audit_dir": "/abs/path/to/test-triaging-agent/audit/<session>",
  "automation_issues": [
    {
      "test_name": "...",
      "classification": "AUTOMATION_ISSUE",
      "confidence": "HIGH",
      "root_cause_category": "ELEMENT_NOT_FOUND",
      "root_cause": "...",
      "error_type": "...",
      "error_message": "...",
      "stack_trace": "...",
      "execution_log": "...",
      "class_name": "...",
      "method_name": "..."
    }
  ]
}
```

Only issues with `AUTOMATION_ISSUE + HIGH + ELEMENT_NOT_FOUND` are included.

---

## Fix Gate Values (.fix-passed)

- `true`    — all targeted fixes applied and tests passed → PR created
- `false`   — one or more fixes failed tests → retry loop, then Slack alert
- `skipped` — no eligible issues or infra not configured → clean exit

---

## Audit Trail

**Session folder:** `agents/test-healing-agent/audit/$SESSION_ID/`

| File | Written by | Purpose |
|---|---|---|
| `00-session-init.md` | run.sh | Session metadata |
| `01-fix.json` + `.md` | Fix | Per-test context, diffs, test output |
| `.fix-passed` | Fix | Gate: true / false / skipped |
| `02-ship.json` + `.md` | Ship | PR URL, Slack status |

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `CLAUDE_CLI_PATH` | Path to claude CLI binary (default: claude) |
| `AUTOFIX_MODEL` | Claude model for fix generation (default: claude-opus-4-6) |
| `WORKSPACE_DIR` | Parent directory for the automation repo. Must be outside QA-Agent-Network. If the repo is not present, test-healing-agent clones it automatically using `GITHUB_TOKEN` + `GITHUB_ORG` + `GITHUB_REPO_AUTOMATION`. |
| `GITHUB_REPO_AUTOMATION` | Name of the automation repo dir under `WORKSPACE_DIR` |
| `GITHUB_TOKEN` | GitHub authentication for PR creation |
| `GITHUB_ORG` | GitHub org owning the automation repo |
| `GITHUB_DEFAULT_BRANCH` | Base branch for PRs (default: main) |
| `AUTOFIX_BRANCH_PREFIX` | Prefix for fix branches (default: `chore/qa-autofix`). Full name: `<prefix>/<build-tag>` |
| `GITHUB_PR_REVIEWERS` | Comma-separated list of PR reviewers |
| `REPO_CONTEXT_FILE` | Path to conventions file in the automation repo (relative to repo root or absolute). If unset or not found, falls back to `agents/test-healing-agent/CONVENTIONS.md` bundled in this agent. |
| `TEST_RUNNER_CMD` | Override test runner — use `{class}`, `{class_simple}`, `{method}` placeholders |
| `AUTO_FIX_MAX_FIXES_PER_RUN` | Max fixes per session (default: 5) |
| `MAX_FIX_ATTEMPTS` | Max retry cycles if tests fail (default: 2) |
| `AUTO_PUSH` | Set `false` to skip PR creation (dry-run) |
| `SLACK_BOT_TOKEN`, `SLACK_NOTIFY_CHANNEL` | Slack notifications on success |
| `SLACK_ALERT_CHANNEL` | Slack channel for failures/partial fixes |
| `SESSION_ID`, `AUDIT_DIR`, `HANDOFF_FILE` | Set by run.sh — do not set manually |

---

## How to Run

```bash
# Queue mode — picks the oldest unprocessed handoff
make run AGENT=test-healing-agent

# Direct mode — process a specific build tag
make run AGENT=test-healing-agent BUILD_TAG=ProdSanity-All-Tests-541

# Dry-run — fixes applied and tested locally, but no PR pushed
AUTO_PUSH=false make run AGENT=test-healing-agent

# View audit trail
make audit AGENT=test-healing-agent
make audit AGENT=test-healing-agent SESSION=20260328-143022-fix-ProdSanity-All-Tests-541
```

---

## Key Rules

1. **Only fix AUTOMATION_ISSUE + HIGH + ELEMENT_NOT_FOUND** — handoff already filtered
2. **Every fix must pass the test before it is committed** — restore original on failure
3. **Write audit entry before any irreversible action** (git commit, push)
4. **Use wrapper methods, not raw Selenium** — CONVENTIONS.md teaches Claude the patterns
5. **Exit cleanly** — success or failure. Not a daemon.
6. **Secrets never logged** — env var names are fine, never their values
