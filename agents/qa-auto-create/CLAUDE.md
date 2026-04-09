# qa-auto-create — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Takes plain English test steps from a `.txt` file in the queue, generates complete
framework-compliant Java test code for the Jarvis automation repository, validates
generated web flows with a headless Playwright script, runs the generated test via Maven,
fixes any failures iteratively, and raises a GitHub PR.

Runs independently. One session = one feature input file = one PR (or Slack alert if tests fail).

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_parse.py          [Python + Claude]   Plain text → structured generation plan
  ├─ 02_validate_web.py   [Python + Claude]   Generate + run headless Playwright Node.js script → selector map
  ├─ 03_generate.py       [Python + Claude]   Write Java files to Thanos-pw repo
  ├─ 04_run_and_fix.py    [Python + Claude]   Run mvn test → fix failures → retry loop
  └─ 05_ship.py           [Python only]       Git branch + commit + push + gh pr create
```

---

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Parse** | Read plain text, call Claude, produce plan JSON | No file writes to Thanos-pw |
| **02 Validate Web** | Generate + run Node.js Playwright script, collect selectors | No Java codegen |
| **03 Generate** | Write all Java files to Thanos-pw | No test running |
| **04 Run+Fix** | Run mvn test, call Claude to fix failures, retry | No git push |
| **05 Ship** | Branch + commit + push + PR creation | No AI calls |

---

## Data Flow

```
queue/<feature>.txt  (plain English test steps)
    ↓
01-parse.json            (structured generation plan: classes, fields, methods)
    ↓
02-validate-web.json     (confirmed DOM selectors, or empty if not a web test)
    ↓
03-generate.json         (list of Java files written to Thanos-pw)
    ↓
04-run-and-fix.json      (test run results, applied fixes)
.fix-passed              (gate: true / false / skipped)
    ↓
05-ship.json             (PR URL, Slack status)
.verdict                 (APPROVED / NEEDS-REVIEW)
    ↓
queue/processed/<feature>.txt  (moved after completion)
```

---

## Input File Format

Plain text file at `queue/<feature>.txt`. Claude in step 01 is flexible about exact format.
The minimum required information:

```
Feature: payments
Type: both          # api | web | both
URL: https://app.staging.example.com
API URL: https://api.staging.example.com

Steps:
1. Login as Admin user
2. Create a payment of 100 SGD to recipient ABC
3. Verify the payment ID is returned in the response
4. Fetch the payment by ID and verify the status is PENDING

Web Steps:
1. Login as Admin user and navigate to Payments page
2. Click New Payment button
3. Fill in recipient field with Test Recipient
4. Fill amount as 100 and select currency SGD
5. Click Submit
6. Verify success message appears
```

---

## Gate Values

**.fix-passed**
- `true`    — generated test ran and passed → proceed to ship
- `false`   — test failed after all fix attempts → ship with NEEDS-REVIEW verdict
- `skipped` — no test could be run (infra issue) → clean exit

**.verdict**
- `APPROVED`      — test passed, PR created
- `NEEDS-REVIEW`  — test still failing, PR created with warning

---

## Audit Trail

**Session folder:** `agents/qa-auto-create/audit/$SESSION_ID/`

| File | Written by | Purpose |
|------|-----------|---------|
| `00-session-init.md` | run.sh | Session metadata, env snapshot |
| `01-parse.json` + `.md` | Parse | Generation plan |
| `02-validate-web.json` + `.md` | Validate Web | Selector map, step results |
| `02-validate-web.js` | Validate Web | The generated Playwright script |
| `03-generate.json` + `.md` | Generate | List of files written |
| `04-run-and-fix.json` + `.md` | Run+Fix | Test output, applied fixes |
| `.fix-passed` | Run+Fix | Gate: true / false / skipped |
| `05-ship.json` + `.md` | Ship | PR URL, Slack status |
| `.verdict` | Ship | APPROVED / NEEDS-REVIEW |

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_CLI_PATH` | Path to claude CLI binary | `claude` |
| `AUTOCREATE_MODEL` | Claude model for all AI steps | `claude-opus-4-6` |
| `WORKSPACE_DIR` | Parent directory containing Jarvis | required |
| `GITHUB_TOKEN` | GitHub auth token for PR creation | required |
| `GITHUB_ORG` | GitHub org/user owning the repo | required |
| `GITHUB_REPO_AUTOMATION` | Name of the Jarvis repo dir | `Jarvis` |
| `GITHUB_DEFAULT_BRANCH` | Base branch for PRs | `main` |
| `GITHUB_PR_REVIEWERS` | Comma-separated reviewer handles | optional |
| `AUTOCREATE_BRANCH_PREFIX` | Branch name prefix | `feat/qa-autocreate` |
| `MAX_FIX_ATTEMPTS` | Max retry cycles for failing tests | `3` |
| `AUTO_PUSH` | Set `false` to skip PR creation (dry-run) | `true` |
| `AUTOCREATE_ENVIRONMENT` | Maven `-Denvironment=` value | `staging` |
| `AUTOCREATE_COUNTRY` | Maven `-Dcountry=` value | `SG` |
| `PLAYWRIGHT_TIMEOUT_MS` | Timeout for Playwright validation steps | `30000` |
| `SLACK_BOT_TOKEN` | Slack bot token | optional |
| `SLACK_NOTIFY_CHANNEL` | Slack channel for success notifications | optional |
| `SLACK_ALERT_CHANNEL` | Slack channel for failure alerts | optional |
| `SESSION_ID`, `AUDIT_DIR`, `INPUT_FILE`, `FEATURE` | Set by run.sh — do not set manually | — |

---

## How to Run

```bash
# First-time setup: copy the example env file and fill in your values
cp agents/qa-auto-create/.env.example agents/qa-auto-create/.env
# Edit .env: set WORKSPACE_DIR, GITHUB_TOKEN, GITHUB_ORG at minimum

# Direct mode — process a specific feature input file
make run AGENT=qa-auto-create FEATURE=payments

# Queue mode — picks the oldest .txt in the queue
make run AGENT=qa-auto-create

# Dry-run — generates, tests, but no PR pushed
AUTO_PUSH=false make run AGENT=qa-auto-create FEATURE=payments

# View audit trail
make audit AGENT=qa-auto-create
make audit AGENT=qa-auto-create SESSION=20260330-143022-create-payments
```

---

## Jarvis Framework Conventions

> **All framework conventions are defined in `Jarvis/CLAUDE.md`** (the single source of truth).
> The agent scripts read that file directly and inject it into every Claude prompt.
> Do NOT duplicate framework rules here — update `Jarvis/CLAUDE.md` instead.

The section below covers **agent-specific generation rules** that are not in `Jarvis/CLAUDE.md`.

### Package Structure (new module)
```
src/main/java/automation/modules/{feature}/
  {Feature}Data.java
  {Feature}Builder.java
  {Feature}Helper.java          extends ApiHelper
  api/{Feature}Api.java         enum implements ApiDetails
  web/{Page}Page.java           extends BasePage

src/test/java/automation/{feature}/
  {Feature}ApiTest.java         extends TestBase
  {Feature}WebTest.java         extends TestBase
```

All patterns (Data POJO, Builder, API Enum, Helper, Page Object, Test classes, DO/DON'T rules)
are defined in `Jarvis/CLAUDE.md` and injected into every Claude prompt at runtime.
Refer to [Jarvis/CLAUDE.md](../../../Jarvis/CLAUDE.md) for the authoritative reference.

---

## Key Rules for Existing Module Appending

When `existing_module=true` in the plan:
- Do NOT recreate `{Feature}Data.java`, `{Feature}Builder.java`, or `{Feature}Api.java` unless
  new fields/endpoints are needed
- DO add new methods to `{Feature}Helper.java` (API and web workflows)
- DO add new page objects if new pages are involved
- DO create a new test class file (e.g., `{Feature}NewScenarioTest.java`) rather than modifying
  an existing test file — this avoids merge conflicts and preserves existing tests
- Read the existing Helper/Data files before generating to avoid duplicating methods or fields
