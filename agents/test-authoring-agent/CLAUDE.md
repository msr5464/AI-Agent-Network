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

### What "confirmed" means in step 02

Every locator in `02-validate-web.json` — both the `selectors` map and the
`interaction_hints` list, since step 03 generates from both — has been measured in
the live browser at **exactly one matching element**. Anything else is dropped
before the file is written:

| Case | Outcome |
|------|---------|
| `SELECTOR_FOUND` with `count=1` and `visible=1` | kept |
| `SELECTOR_FOUND` with `count != 1` | dropped — would be a runtime strict mode violation |
| `SELECTOR_FOUND` with `visible != 1` | dropped — a locator nobody can see makes a test fail for an invisible reason |
| `SELECTOR_FOUND` with no `count` at all | dropped — never measured, so not confirmed |
| `SELECTOR_FOUND` with no `visible` at all | kept, recorded as visibility-unmeasured (a pre-protocol cached run must not empty the map) |
| `INTERACTION_HINT` whose name has a confirmed selector | kept, with the hint's selector **replaced by the confirmed one** |
| `INTERACTION_HINT` with no confirmed selector and no `count: 1` | dropped |

The hint rules exist because a hint records an element the model *interacted with*,
including ones an interaction then failed on — an observed run hinted a profile edit
icon as `img[alt='PencilSimple']`, found clicking it did nothing, and confirmed the
parent `span` instead, leaving a hint pointing at the element that does not work.

A run that confirms nothing is retried once; if it still confirms nothing, step 03
aborts rather than generating from guesses (override with `ALLOW_MISSING_SELECTORS`).

Every dropped selector is recorded in `rejected_selectors` with its reason, so
"why is there no locator for the toast?" has an answer in the audit trail rather
than in a console line that has scrolled away.

### What a step outcome means

A step has three outcomes, not two. The third exists because "I did the action but
could not observe what it claims" used to collapse into a pass — which is how a run
reported `STEP_PASSED: Verify a success confirmation toast appears` for a toast that
never rendered, reasoning from the save API returning 200.

| Marker | Meaning |
|--------|---------|
| `STEP_PASSED` | the step's claim was observed. For a claim about a UI element that means **seeing the element**; a network response is never proof one rendered |
| `STEP_UNVERIFIED` | the action completed, the claimed outcome was never observed |
| `STEP_FAILED` | the step could not be performed |

This is enforced, not requested: a verification step reported as passed with no
`SELECTOR_FOUND` for the element it claims to have seen is downgraded to unverified
in Python (`enforce_verification_evidence`). Step outcomes were the last self-report
in this step that nothing checked.

### Assertions vs mechanisms

The two halves of a test are treated very differently, following the rule
`shared/intent.py` already states — *the mechanism becomes mutable and the proof
does not*.

**A verification names the proof, and it is fixed.** What happens to one step 02
could not observe depends on who asked for it (`shared/check_provenance.py` decides,
by measuring the check's vocabulary against the author's own words — never by
trusting the model's claim about itself):

| Check | Outcome |
|-------|---------|
| the input asked for it | **kept at full strength.** The test fails on purpose, the PR says why, and the verdict is NEEDS-REVIEW. The product does not do what was asked — that is a finding |
| the pipeline invented it | **dropped entirely** — locator, accessor and assertion. A failing check nobody asked for is exactly what gets "fixed" by deleting it |

Dropping is the irreversible direction, so it needs the harder test: a check is only
dropped when *nothing* in it traces back to the input. A partly-traceable check is
kept and the test goes red, because a wrongly-kept check is visible and a wrongly-
dropped one is silent.

**An action names an outcome, and the mechanism is ours to find.** "Save the profile"
does not mean "there is a Save button" — Naukri's profile summary autosaves about a
second after the last keystroke. When an action's named control is not visible, step
02 discovers how the outcome actually happens (rule 2e) and reports
`MECHANISM_FOUND: <action>|<kind>|<trigger>|<settles when>`, which step 03 generates
from. An action step never becomes an unverified check.

### What step 04 may not do

A fix may change how the test reaches its result; it may not change the result it
proves. Every assertion reachable from the test method is fingerprinted **before the
first run** into `.assertions-frozen.json`, and each attempt is compared against that
frozen copy with `shared/assertion_graph.conserved()` — so attempt 3 cannot launder a
weakening introduced by attempt 2. An assertion removed, moved down a strength ladder
(`assertEquals` → `assertNotNull`), or wrapped in a condition rejects the **whole**
fix and rolls every file back. `FORCE=true` overrides it, matching test-healing-agent.

This exists because none of the six per-file guards could see it: deleting an
assertion is a one-line diff that loses no method, adds no `Thread.sleep`, and is
invisible to `no_selector_broadening`, which only inspects `page.locator(...)` calls.

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
- `APPROVED`      — test passed, nothing the input asked for went unverified, no fix was rejected for weakening an assertion
- `NEEDS-REVIEW`  — test failing, OR a requested check could not be observed, OR a fix was rejected for weakening a test, OR no test ran at all

---

## Audit Trail

**Session folder:** `agents/test-authoring-agent/audit/$SESSION_ID/`

| File | Written by | Purpose |
|------|-----------|---------|
| `00-session-init.md` | run.sh | Session metadata, env snapshot |
| `01-parse.json` + `.md` | Parse | Generation plan |
| `02-validate-api.json` + `.md` | Validate API | Auth status, confirmed endpoint response shapes |
| `02-validate-web.json` + `.md` | Validate Web | Selector map, step results (passed/failed/**unverified**), `rejected_selectors`, `mechanisms` |
| `claude-*.log` | Validate Web | Raw `claude -p` stream, for diagnosing empty runs |
| `03-generate.json` + `.md` | Generate | List of files written, `dropped_unverified_checks`, `kept_unverified_checks`, `unconfirmed_locators` |
| `04-run-and-fix.json` + `.md` | Run & Fix | Test output, applied fixes |
| `.assertions-frozen.json` | Run & Fix | What the generated test proved before any fix — the conservation baseline |
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
| `FRAMEWORK_DIR` | Absolute path to the checkout, overriding `WORKSPACE_DIR/GITHUB_REPO_AUTOMATION` | optional |
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
| `PLAYWRIGHT_HEADLESS` | Set `false` to watch every browser this agent starts — step 02's validation and step 04's `mvn test` run | `true` |
| `PLAYWRIGHT_MCP_VERSION` | `@playwright/mcp` version the browser steps launch (pinned, not `latest`) | `0.0.79` |
| `VALIDATE_API_REQUEST_TIMEOUT_S` | Timeout (s) for each real HTTP call in Validate API | `15` |
| `VALIDATE_API_RETRY_ON_ERROR` | Set `false` to disable the one connection-error retry in Validate API | `true` |
| `ALLOW_MISSING_SELECTORS` | Let step 03 generate when step 02 confirmed nothing | `false` |
| `FORCE` | Let a step 04 fix through even when it weakens an assertion. For a human who has read the diff — never for the loop | `false` |
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

### URLs Are Properties, Never Java Literals

A URL welded into a test, page object or helper pins the module to one environment —
`Jarvis/CLAUDE.md` has always said so ("Hardcoded URL in test/page → put in properties
file"), but until this guardrail nothing enforced it, and generated modules shipped with
`private static final String LOGIN_URL = "https://..."` and no matching property.

The rule is enforced at four points, all reading `shared/url_properties.py`:

| Where | What happens |
|-------|--------------|
| **03 Generate**, before codegen | `collect_urls()` harvests every URL from the plan (`web_base_url`, `api_base_url`, validation steps) and from `02-validate-web.json`'s `steps_passed`, names a key for each, and writes them to `parameters/{environment}-{country}.properties`. The key table goes into the codegen prompt. |
| **03 Generate**, after codegen | Any file still holding a literal URL gets one targeted repair pass, guarded by `validate_fix`. What survives is logged and recorded in `03-generate.json` → `hardcoded_urls`. |
| **04 Run & Fix** | `ensure_url_properties()` rewrites the keys before the first run (`git checkout -f` in run.sh discards them). `no_hardcoded_url` is a fix guard: a fix that adds a literal URL is rejected before it reaches disk. |
| **05 Ship** | The URL keys are committed — added to HEAD's copy of the properties file, never the working copy, so the run's real credentials in that same file are not committed with them. |

Key naming: the host alone is `{feature}.url` (matching the existing `saucedemo.url`), the
API base is `{feature}.api.url`, and anything with a path is named for its last meaningful
segment — `/nlogin/login` → `{feature}.login.url`. Id-like segments are skipped.

Credentials use the same properties file through `shared/credential_properties.py` but are
the opposite case: never committed. Both share `shared/properties_file.py` so the file
location and the "never overwrite a human's value" rule exist in one place.

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
