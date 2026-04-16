<div>
  <img src="https://raw.githubusercontent.com/msr5464/Basic-Automation-Framework/refs/heads/master/Logo-full.png" height="50">

  # QA Agent Network

  [![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
  [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
</div>

*An AI-driven multi-agent system for end-to-end QA automation*

---

## How It Works

```
Test build finishes
       │
       ▼
┌─────────────────────────────────────────────────────┐
│  Agent 1: test-triaging-agent                           │
│                                                     │
│  01 Scout  → pick unanalyzed build tag from DB      │
│  02 Collect→ DB query + HTML log parse              │
│  03 Classify → Claude batch-classifies failures     │
│  04 Review → adversarial review + verdict gate      │
│  05 Ship   → HTML report + Slack + handoff JSON     │
└──────────────────────────┬──────────────────────────┘
                           │  queue/<build_tag>.json
                           │  (AUTOMATION_ISSUE HIGH ELEMENT_NOT_FOUND only)
                           ▼
┌─────────────────────────────────────────────────────┐
│  Agent 2: test-healing-agent   (runs independently)        │
│                                                     │
│  01 Fix  → Claude generates locator fix per test    │
│            → applies fix, runs test, rolls back     │
│              on failure, retries up to N times      │
│  02 Ship → push branch → GitHub PR → Slack          │
└─────────────────────────────────────────────────────┘
```

**Agent 1** classifies every failure as `PRODUCT_BUG` or `AUTOMATION_ISSUE`, writes an HTML report, and queues only the fixable ones.

![Sample Report](sample_report.png)

**Agent 2** picks up the queue, fixes broken locators using Claude, verifies each fix by running the test locally, and opens a GitHub PR. If only some tests are fixed, it still ships a PR for what passed and alerts on what didn't.

**Example Slack messages from Agent 2:**

All tests fixed → posted to `#qa-reports`
```
✅ QA Auto-Fix — ProdSanity-All-Tests-541
8/8 tests fixed

✅ Fixed (8):
  • TestLogin.testLoginWithValidCredentials — updated locator [data-cy='submit-btn']
  • TestDashboard.testDashboardLoadsCorrectly — updated locator #dashboard-header
  • TestProfile.testEditProfileSaves — @FindBy css updated to [data-testid='save-btn']
  • ... and 5 more
  PR: https://github.com/org/automation-repo/pull/214

Audit: 20260329-143022-fix-ProdSanity-All-Tests-541
```

Partial fix → posted to `#qa-critical`
```
🟡 QA Auto-Fix — ProdSanity-All-Tests-541
5/8 tests fixed — 3 need manual attention

✅ Fixed (5):
  • TestLogin.testLoginWithValidCredentials — updated locator [data-cy='submit-btn']
  • TestDashboard.testDashboardLoadsCorrectly — updated locator #dashboard-header
  • ... and 3 more
  PR: https://github.com/org/automation-repo/pull/214

❌ Could not fix (3) — manual review required:
  • TestCheckout.testCheckoutFlow — fix applied but test still failing
  • TestPayment.testPaymentWithCard — unfixable: multiple candidate locators, ambiguous
  • TestLogout.testSessionExpiry — test file not found in workspace

Audit: 20260329-143022-fix-ProdSanity-All-Tests-541
```

No tests fixed → posted to `#qa-critical`
```
❌ QA Auto-Fix — ProdSanity-All-Tests-541
0/8 tests could be fixed — all need manual attention

❌ Could not fix (8) — manual review required:
  • TestLogin.testLoginWithValidCredentials — fix applied but test still failing
  • TestDashboard.testDashboardLoadsCorrectly — unfixable: dynamic element, no stable locator
  • ... and 6 more

Audit: 20260329-143022-fix-ProdSanity-All-Tests-541
```

---

## Quick Start

### 1. Install

```bash
git clone <repository-url>
cd QA-Agent-Network

./scripts/setup.sh          # macOS / Linux
.\scripts\setup.ps1         # Windows
```

### 2. Configure

```bash
cp config/.env.example config/.env
```

Minimum required settings in `config/.env`:

```bash
# Database (stores test results and history)
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=qa_results

# Claude CLI
CLAUDE_CLI_PATH=claude          # or full path if not on $PATH

# Report output
INPUT_DIR=testdata              # where HTML test reports live
OUTPUT_DIR=reports              # where to write generated HTML reports
```

### 3. Run Agent 1 — Analyse

```bash
# macOS / Linux
./scripts/run-analyse.sh                              # auto-selects most recent unanalyzed build
./scripts/run-analyse.sh ProdSanity-All-Tests-541     # specific build tag
STOP_AFTER=classify ./scripts/run-analyse.sh          # stop early for inspection

# Windows
.\scripts\run-analyse.ps1 -BuildTag ProdSanity-All-Tests-541
```

Outputs:
- HTML report → `OUTPUT_DIR/`
- Handoff file → `agents/test-healing-agent/queue/<build_tag>.json` (if fixable issues found)
- Slack notification → `SLACK_NOTIFY_CHANNEL` or `SLACK_ALERT_CHANNEL`

### 4. Run Agent 2 — Auto-fix

```bash
# macOS / Linux
./scripts/run-autofix.sh                              # process oldest item in queue
./scripts/run-autofix.sh ProdSanity-All-Tests-541     # specific build tag
./scripts/run-autofix.sh /path/to/handoff.json        # pass handoff file directly
AUTO_PUSH=false ./scripts/run-autofix.sh              # dry-run: fix + test locally, no PR

# Windows
.\scripts\run-autofix.ps1 -BuildTag ProdSanity-All-Tests-541
.\scripts\run-autofix.ps1 -HandoffFile C:\path\to\handoff.json
```

Outputs:
- GitHub PR with all passing fixes (even if some tests couldn't be fixed)
- Slack notification with per-test breakdown (see examples above)

---

## Configuration Reference

### Database & Claude

| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_HOST` | `localhost` | MySQL host |
| `DB_PORT` | `3306` | MySQL port |
| `DB_USER` | `root` | MySQL user |
| `DB_PASSWORD` | | MySQL password |
| `DB_NAME` | `qa_results` | MySQL database name |
| `CLAUDE_CLI_PATH` | `claude` | Path to Claude CLI binary |
| `CLASSIFIER_MODEL` | `claude-opus-4-6` | Model for failure classification |
| `REVIEWER_MODEL` | `claude-sonnet-4-6` | Model for adversarial review |
| `AUTOFIX_MODEL` | `claude-opus-4-6` | Model for fix generation |

### Agent 1 — test-triaging-agent

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUILD_TAG` | | Skip scout and analyse this build directly |
| `STOP_AFTER` | | Stop after: `scout` `collect` `classify` `review` |
| `SCOUT_LOOKBACK_DAYS` | `7` | How far back scout looks for unanalyzed builds |
| `MAX_REVIEW_ROUNDS` | `2` | Max classifier ↔ reviewer debate rounds |
| `INPUT_DIR` | `testdata` | Directory containing test report HTML |
| `OUTPUT_DIR` | `reports` | Directory for generated HTML reports |
| `AUTOFIX_QUEUE_DIR` | `agents/test-healing-agent/queue` | Where to write handoff files |

### Agent 2 — test-healing-agent

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKSPACE_DIR` | parent of QA-Agent-Network | Parent directory for the automation repo. If the repo is absent, it is cloned automatically. |
| `GITHUB_REPO_AUTOMATION` | | Automation repo directory name under `WORKSPACE_DIR` |
| `GITHUB_TOKEN` | | GitHub token — used for clone + PR creation |
| `GITHUB_ORG` | | GitHub organisation owning the automation repo |
| `GITHUB_DEFAULT_BRANCH` | `main` | Base branch for PRs |
| `GITHUB_PR_REVIEWERS` | | Comma-separated list of PR reviewers |
| `AUTOFIX_BRANCH_PREFIX` | `chore/qa-autofix` | Branch name prefix (full: `<prefix>/<build-tag>`) |
| `AUTO_PUSH` | `true` | Set `false` for dry-run (fix and test locally, skip PR) |
| `MAX_FIX_ATTEMPTS` | `2` | Retry cycles if tests still fail after fix |
| `AUTO_FIX_MAX_FIXES_PER_RUN` | `5` | Max tests to fix per session |
| `TEST_RUNNER_CMD` | auto-detect | Override test runner. Placeholders: `{class}` `{class_simple}` `{method}` |
| `REPO_CONTEXT_FILE` | `CONVENTIONS.md` | Path to conventions file Claude reads before generating fixes |

---

## Slack Notifications

Both agents post to Slack using the **Slack Bot API** (`chat.postMessage`).

### Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch
2. **OAuth & Permissions** → Bot Token Scopes → add `chat:write`
3. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`)
4. In each target channel, run `/invite @your-bot-name`

### Configuration

```bash
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_NOTIFY_CHANNEL=#qa-reports      # normal results (reports, successful fixes)
SLACK_ALERT_CHANNEL=#qa-critical      # failures and escalations needing human action
```

If `SLACK_ALERT_CHANNEL` is not set, all messages go to `SLACK_NOTIFY_CHANNEL`.
If `SLACK_BOT_TOKEN` is not set, Slack is silently skipped.

### What gets posted

| Event | Channel | Message includes |
|-------|---------|-----------------|
| Analysis complete, verdict APPROVED | `NOTIFY` | Report summary, pass/fail counts, handoff queued count |
| Analysis complete, verdict NEEDS-HUMAN | `ALERT` | Reason for escalation, audit trail link |
| All tests fixed | `NOTIFY` | List of fixed tests, PR link |
| Some tests fixed, some failed | `ALERT` | Fixed list + PR link, failed list with reasons |
| No tests could be fixed | `ALERT` | Failed list with reasons per test, audit trail link |

---

## Project Structure

```
QA-Agent-Network/
├── agents/
│   ├── test-triaging-agent/       # Agent 1 — analyse, classify, report
│   │   ├── run.sh             # Orchestrator (steps 01–05)
│   │   ├── CLAUDE.md          # Full agent spec (read by Claude CLI)
│   │   ├── feedback/          # skip-buildtags.json
│   │   └── actions/
│   │       ├── 01_scout.py    # Pick unanalyzed build tag from DB
│   │       ├── 02_collect.py  # DB query + HTML parse + flaky detection
│   │       ├── 03_classify.py # Batch classify failures via Claude
│   │       ├── 04_review.py   # Adversarial review + .verdict gate
│   │       └── 05_ship.py     # HTML report + handoff JSON + Slack
│   └── test-healing-agent/           # Agent 2 — fix locators, raise PR
│       ├── run.sh             # Orchestrator (queue / direct / file-path mode)
│       ├── CLAUDE.md          # Full agent spec (read by Claude CLI)
│       ├── queue/             # Pending handoffs from test-triaging-agent
│       ├── feedback/          # known-issues.json (patterns to skip)
│       └── actions/
│           ├── 01_fix.py      # Fix locators → run tests → commit
│           └── 02_ship.py     # Push branch → PR → Slack
├── config/
│   ├── .env.example           # All env vars documented with defaults
│   └── prompts.yaml           # Prompt templates
├── docs/
│   ├── AUTO_FIX_GUIDE.md      # Full autofix setup and walkthrough
│   └── AUTO_FIX_TEST_SELECTION.md  # How tests are selected and filtered
├── scripts/
│   ├── run-analyse.sh / .ps1  # Entry point for Agent 1
│   ├── run-autofix.sh / .ps1  # Entry point for Agent 2
│   └── setup.sh / .ps1        # Install dependencies
├── src/                       # Shared Python library
│   ├── agent/                 # DB-backed memory, LLM analyzer, summary generator
│   ├── parsers/               # HTML parser, data builder, models
│   ├── reporters/             # HTML report generator, category rules
│   ├── database.py
│   ├── settings.py
│   └── utils.py
├── tests/                     # Unit tests
└── requirements.txt
```

---

## HTTP Server (for UI integration)

In addition to the Makefile / shell entry points, the repo ships a thin HTTP + SSE server that wraps `test-authoring-agent/run.sh`. It is used by the AI-Test-Studio "QA Agents" tab to trigger runs and stream live progress; CLI users do not need it.

```bash
bash scripts/run-server.sh
# Listens on http://0.0.0.0:8765 by default
```

Key endpoints (all scoped to `test-authoring-agent` for v1):

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Service health + active run id |
| `GET`  | `/agents/test-authoring-agent/queue` | List feature files in `queue/` |
| `POST` | `/agents/test-authoring-agent/queue` | Create/update a feature file |
| `POST` | `/agents/test-authoring-agent/run` | Trigger a run — returns `session_id` |
| `GET`  | `/agents/test-authoring-agent/run/active` | Active run (for UI re-attach) |
| `GET`  | `/agents/test-authoring-agent/run/<id>/stream?offset=N` | SSE: live + replay |
| `POST` | `/agents/test-authoring-agent/run/<id>/cancel` | SIGTERM the run |
| `GET`  | `/agents/test-authoring-agent/sessions` | Audit history |
| `GET`  | `/agents/test-authoring-agent/sessions/<id>` | Full session detail |

The server reuses the agent's existing `run.sh` — it does not reimplement any pipeline logic. CLI-triggered runs and UI-triggered runs write to the same `agents/test-authoring-agent/audit/` folder, so the history endpoint surfaces both.

Environment overrides:

| Variable | Default | Purpose |
|---|---|---|
| `QA_AGENT_SERVER_HOST` | `0.0.0.0` | Bind host |
| `QA_AGENT_SERVER_PORT` | `8765` | Bind port |
| `AI_TEST_STUDIO_URL` | `http://localhost:5001` | CORS allowlist (the proxying frontend) |
| `QA_AGENT_RUN_TIMEOUT_SECONDS` | `1800` | SIGKILL after this many seconds |

---

## Troubleshooting

**"Queue is empty — nothing to fix"**
Run Agent 1 first. The queue is only populated when Agent 1 finds `AUTOMATION_ISSUE + HIGH confidence + ELEMENT_NOT_FOUND` failures and the review verdict is `APPROVED`.

**"No test results found in database"**
Your test runner must insert results into MySQL before running Agent 1. The agent queries by `buildTag` (the directory name of the test report).

**"claude: command not found"**
Set `CLAUDE_CLI_PATH` in `config/.env` to the full path of the Claude CLI binary.

**"GitHub configuration is missing"**
Set `GITHUB_TOKEN`, `GITHUB_ORG`, and `GITHUB_REPO_AUTOMATION` in `config/.env`.

**PR not created after fix**
Check `agents/test-healing-agent/audit/<session>/02-ship.md` for the exact reason. Common causes: push failed, no successful fixes, `AUTO_PUSH=false`.

---

## License

MIT — see [LICENSE](LICENSE)

---

## Creator

**Mukesh Rajput** · [LinkedIn](https://www.linkedin.com/in/mukesh-rajput/)

<div align="center"><strong>Made with ❤️ for the Engineering Team</strong></div>
