# Auto-Fix Guide: Self-Healing Locator Fixes

The QA pipeline uses two independent agents to analyse and fix test failures:

1. **`qa-auto-analyse`** — Analyses test reports, classifies failures, writes an HTML report, and queues fixable automation issues.
2. **`qa-auto-fix`** — Picks up queued issues, fixes broken locators via Claude, runs each test to verify, and creates a GitHub PR.

---

## Prerequisites

### 1. Install dependencies

```bash
# macOS / Linux
./scripts/setup.sh

# Windows
.\scripts\setup.ps1
```

### 2. Configure environment variables

Copy the template and fill in your values:

```bash
cp config/.env.example config/.env
```

Required variables for the autofix flow:

```bash
# GitHub — needed for PR creation
GITHUB_TOKEN=your_github_token_here
GITHUB_ORG=your_org_name
GITHUB_REPO_AUTOMATION=your_automation_repo_name
GITHUB_DEFAULT_BRANCH=main
GITHUB_PR_REVIEWERS=reviewer1,reviewer2

# Parent directory for the automation repo (not the repo itself).
# Must be OUTSIDE of QA-AI-Agent. If the repo is not already present,
# qa-auto-fix will clone it automatically using GITHUB_TOKEN + GITHUB_ORG + GITHUB_REPO_AUTOMATION.
# e.g. to place the repo at /home/runner/work/automation-repo:
#   WORKSPACE_DIR=/home/runner/work
#   GITHUB_REPO_AUTOMATION=automation-repo
WORKSPACE_DIR=/home/runner/work

# Claude CLI (autofix uses claude directly)
CLAUDE_CLI_PATH=claude
AUTOFIX_MODEL=claude-opus-4-6

# Test runner (override default if your framework needs extra flags)
# Placeholders: {class}, {class_simple}, {method}
TEST_RUNNER_CMD=./gradlew test --tests {class_simple}.{method}

# Slack notifications (optional)
SLACK_BOT_TOKEN=xoxb-...
SLACK_NOTIFY_CHANNEL=#qa-reports
SLACK_ALERT_CHANNEL=#qa-critical
```

---

## Running the Pipeline

### Step 1 — Analyse (produces the handoff)

```bash
# macOS / Linux
./scripts/run-analyse.sh                          # scout mode: auto-selects build
./scripts/run-analyse.sh ProdSanity-All-Tests-541  # direct mode: specific build tag

# Windows
.\scripts\run-analyse.ps1
.\scripts\run-analyse.ps1 -BuildTag ProdSanity-All-Tests-541
```

This writes:
- An HTML report to `OUTPUT_DIR`
- A handoff file to `agents/qa-auto-fix/queue/<build_tag>.json` (only when verdict=APPROVED and eligible failures exist)

### Step 2 — Fix (picks up the handoff and raises a PR)

```bash
# macOS / Linux
./scripts/run-autofix.sh                           # queue mode: picks oldest handoff
./scripts/run-autofix.sh ProdSanity-All-Tests-541  # direct: specific build tag
./scripts/run-autofix.sh /path/to/handoff.json     # direct: pass handoff file path

# Windows
.\scripts\run-autofix.ps1
.\scripts\run-autofix.ps1 -BuildTag ProdSanity-All-Tests-541
.\scripts\run-autofix.ps1 -HandoffFile C:\path\to\handoff.json
```

### Dry run (no PR created)

```bash
AUTO_PUSH=false ./scripts/run-autofix.sh
```

---

## How It Works

### What gets queued for autofix

Only failures matching **all three** of these criteria are included in the handoff:

| Criterion | Value |
|-----------|-------|
| Classification | `AUTOMATION_ISSUE` |
| Confidence | `HIGH` |
| Root cause category | `ELEMENT_NOT_FOUND` |

Product bugs, timeouts, and low/medium confidence issues are reported but not queued.

### Fix process (qa-auto-fix)

1. **Read handoff** — loads the queue file with full failure context (error type, stack trace, execution log, class/method names)
2. **Extract base class** — finds the relevant page object files in `WORKSPACE_DIR/GITHUB_REPO_AUTOMATION`
3. **Generate fix** — calls Claude with: failing test code, stack trace, execution log, and `CONVENTIONS.md` (wrapper methods, `@FindBy` style, naming conventions)
4. **Apply and verify** — applies the fix and runs the test locally; rolls back on failure
5. **Retry** — if any test still fails, re-runs the fix step (up to `MAX_FIX_ATTEMPTS`) with the failed test output injected into the prompt
6. **Ship** — commits all passing fixes, creates a branch, pushes, and opens a GitHub PR

### Audit trail

Every session writes to `agents/qa-auto-fix/audit/$SESSION_ID/`:

| File | Content |
|------|---------|
| `00-session-init.md` | Session metadata, env snapshot |
| `01-fix.json` + `.md` | Per-test fix results, diffs, test output |
| `.fix-passed` | Gate: `true` / `false` / `skipped` |
| `02-ship.json` + `.md` | PR URL, Slack status |

View audit trail:
```bash
make audit AGENT=qa-auto-fix
make audit AGENT=qa-auto-fix SESSION=20260329-143022-fix-ProdSanity-All-Tests-541
```

---

## Queue Management

Handoff files live in `agents/qa-auto-fix/queue/`.

```bash
# View pending queue
ls agents/qa-auto-fix/queue/

# View processed items
ls agents/qa-auto-fix/queue/processed/
```

After `qa-auto-fix` completes, the handoff file is automatically moved to `queue/processed/`.

To override the queue path (e.g., if agents run on different machines):
```bash
AUTOFIX_QUEUE_DIR=/shared/mount/qa-fix-queue make run AGENT=qa-auto-fix
```

---

## Troubleshooting

### "Queue is empty — nothing to fix"

Run `qa-auto-analyse` first to populate the queue, or check that the build had eligible failures (AUTOMATION_ISSUE + HIGH + ELEMENT_NOT_FOUND).

### "No handoff file for BUILD_TAG"

The handoff is only written when `qa-auto-analyse` finishes with verdict=APPROVED and finds eligible failures. Check the analyse audit:
```bash
make audit AGENT=qa-auto-analyse SESSION=<session-id>
```

### "Fix gate: false" after max attempts

Some locator fixes may require manual review. The agent creates a PR with all fixes that _did_ pass and sends a Slack alert to `SLACK_ALERT_CHANNEL` listing the failed ones.

### "GitHub configuration is missing"

Ensure `GITHUB_TOKEN`, `GITHUB_ORG`, and `GITHUB_REPO_AUTOMATION` are set in `config/.env`.

### "claude: command not found"

Set `CLAUDE_CLI_PATH` in `config/.env` to the full path of the Claude CLI binary.

---

## Best Practices

1. **Review PRs before merging** — Claude-generated fixes are good but always worth a quick code review.
2. **Start with dry-run** — use `AUTO_PUSH=false` to see what would be fixed without creating a PR.
3. **Limit fixes per run** — set `AUTO_FIX_MAX_FIXES_PER_RUN=1` initially to test one fix at a time.
4. **Keep `CONVENTIONS.md` up to date** — the fix quality directly depends on this file teaching Claude your wrapper methods and locator patterns.
