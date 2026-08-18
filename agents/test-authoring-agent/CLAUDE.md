# test-authoring-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Takes plain English test steps from a `.txt` file in the queue, generates complete
framework-compliant Java test code for the Jarvis automation repository, validates
generated web flows by driving a real browser via Playwright MCP, runs the generated test via Maven,
fixes any failures iteratively, and raises a GitHub PR.

Runs independently. One session = one module input file = one PR (or Slack alert if tests fail).

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_parse.py          [Python + Claude]   Plain text → structured generation plan
  ├─ 02_validate_api.py   [Python only]       Real HTTP calls: confirm auth + safe endpoints
  ├─ 02_validate_web.py   [Python + Claude]   Drive browser via Playwright MCP → selector map
  ├─ 03_generate.py       [Python + Claude]   Write Java files to Thanos-pw repo
  ├─ 04_run_and_fix.py    [Python + Claude]   Run mvn test → fix failures → retry loop
  └─ 05_ship.py           [Python only]       Git branch + commit + push + gh pr create
```

---

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Parse** | Read plain text, call Claude, produce plan JSON | No file writes to Thanos-pw |
| **02 Validate API** | Real HTTP auth + safe-endpoint calls against `api_base_url`, no LLM | Never call unsafe (POST/PUT/DELETE or path-param) endpoints |
| **02 Validate Web** | Drive the browser via Playwright MCP, collect confirmed selectors | No Java codegen |
| **03 Generate** | Write all Java files to Thanos-pw | No test running |
| **04 Run & Fix** | Run mvn test, call Claude to fix failures, retry | No git push |
| **05 Ship** | Branch + commit + push + PR creation | No AI calls |

---

## Data Flow

```
queue/<module>.txt  (plain English test steps)
    ↓
01-parse.json            (structured generation plan: classes, fields, methods)
    ↓
02-validate-api.json     (confirmed auth + endpoint shapes, or skipped if not an API test)
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
queue/processed/<module>.txt  (moved after completion)
```

---

## Input File Format

Plain text file at `queue/<module>.txt`. Claude in step 01 is flexible about exact format.
The minimum required information:

```
Module: payments
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

**Session folder:** `agents/test-authoring-agent/audit/$SESSION_ID/`

| File | Written by | Purpose |
|------|-----------|---------|
| `00-session-init.md` | run.sh | Session metadata, env snapshot |
| `01-parse.json` + `.md` | Parse | Generation plan |
| `02-validate-api.json` + `.md` | Validate API | Auth status, confirmed endpoint response shapes |
| `02-validate-web.json` + `.md` | Validate Web | Selector map, step results |
| `claude-*.log` | Validate Web | Raw `claude -p` stream, for diagnosing empty runs |
| `03-generate.json` + `.md` | Generate | List of files written |
| `04-run-and-fix.json` + `.md` | Run & Fix | Test output, applied fixes |
| `.fix-passed` | Run & Fix | Gate: true / false / skipped |
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
| `MAVEN_TEST_TIMEOUT_S` | Timeout (s) for a single `mvn test` run in step 04 | `300` |
| `TEST_RESULTS_DIR_NAME` | Java framework's report/screenshot output dir name | `test-output` |
| `PLAYWRIGHT_TIMEOUT_MS` | Timeout (ms) for each individual browser action | `30000` |
| `VALIDATE_WEB_TIMEOUT_S` | Wall-clock budget (s) for the whole step-02 run | `1800` |
| `VALIDATE_WEB_RETRY_ATTEMPTS` | Extra full re-runs step 02 attempts on recoverable failures | `1` |
| `PLAYWRIGHT_HEADLESS` | Set `false` to watch the browser during step 02 | `true` |
| `VALIDATE_API_REQUEST_TIMEOUT_S` | Timeout (s) for each real HTTP call in Validate API | `15` |
| `VALIDATE_API_RETRY_ON_ERROR` | Set `false` to disable the one connection-error retry in Validate API | `true` |
| `ALLOW_MISSING_SELECTORS` | Let step 03 generate when step 02 confirmed nothing | `false` |
| `SLACK_BOT_TOKEN` | Slack bot token | optional |
| `SLACK_NOTIFY_CHANNEL` | Slack channel for success notifications | optional |
| `SLACK_ALERT_CHANNEL` | Slack channel for failure alerts | optional |
| `SESSION_ID`, `AUDIT_DIR`, `INPUT_FILE`, `MODULE` | Set by run.sh — do not set manually | — |
| `START_FROM_STEP` | Resume an existing session from step 1-5 instead of a fresh run (see "Resuming a Session" below) | `1` |

---

## How to Run

```bash
# First-time setup: copy the example env file and fill in your values
cp agents/test-authoring-agent/.env.example agents/test-authoring-agent/.env
# Edit .env: set WORKSPACE_DIR, GITHUB_TOKEN, GITHUB_ORG at minimum

# Direct mode — process a specific module input file
make run AGENT=test-authoring-agent MODULE=payments

# Queue mode — picks the oldest .txt in the queue
make run AGENT=test-authoring-agent

# Dry-run — generates, tests, but no PR pushed
AUTO_PUSH=false make run AGENT=test-authoring-agent MODULE=payments

# View audit trail
make audit AGENT=test-authoring-agent
make audit AGENT=test-authoring-agent SESSION=20260330-143022-create-payments
```

### Resuming a Session

If a session got through some steps successfully but failed at (or you just want to
re-run) a later step, resume it in place instead of starting over from Parse. This reuses
the existing session's audit dir — steps before `START_FROM_STEP` are reused as-is, and
any stale output for `START_FROM_STEP` onward (including per-attempt fix files from a
prior failed try) is cleared before it re-runs.

```bash
# Re-run step 4 (Run & Fix) and 05 (Ship) for a session that failed there, reusing
# its 01-parse.json / 02-validate-api.json / 02-validate-web.json / 03-generate.json as-is.
START_FROM_STEP=4 SESSION_ID=20260330-143022-create-payments \
  make run AGENT=test-authoring-agent
```

`MODULE` is recovered automatically from the session's own `00-session-init.md` if not
given — the original queue `.txt` file may already have moved to `processed/` by the run
being resumed, so it isn't required to still exist. Resuming fails fast with a clear error
if the step immediately before `START_FROM_STEP` never actually completed in that session
(e.g. `START_FROM_STEP=4` requires `03-generate.json` to exist).

The same capability is exposed to `qa_agents_server` as
`POST /agents/test-authoring-agent/sessions/<session_id>/retry` with body
`{"from_step": 4}` — this is what a "Retry from step N" action in a UI would call; wiring
up that UI button is a separate change in the AI-Test-Studio frontend, not in this repo.

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
