# test-triaging-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Autonomously analyses test build results, classifies failures as PRODUCT_BUG vs AUTOMATION_ISSUE (or UNKNOWN), reviews classifications independently via an adversarial debate, generates an HTML report, and queues fixable automation issues for the `test-healing-agent` agent.

One session = one build tag = one HTML report + one handoff JSON (if APPROVED + eligible failures found).

---

## Architecture: Hybrid Python + Claude

Python handles all deterministic work (DB queries, HTML parsing). Claude (`claude -p`) handles intelligence (batch classification, adversarial review). Classifier and reviewer are stateless subprocess calls — zero shared context.

```
run.sh (orchestrator)
  │
  ├─ 01_scout.py     [Python only]      Query DB → score build tags → select best
  ├─ 02_collect.py   [Python only]      DB query + HTML parse + flaky detection + trends
  ├─ 03_classify.py  [Python + Claude]  Batch classify failures via claude -p
  ├─ 04_review.py    [Python + Claude]  Independent adversarial review + .verdict gate
  └─ 05_ship.py      [Python only]      HTML report → handoff.json → Slack
```

---

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Scout** | Query DB for buildTags, score by failure count & recency, skip known ones | No HTML parsing, no AI |
| **02 Collect** | DB query, HTML log parse, flaky detection, trend analysis, **attach DOM snapshot / trace / failure context** | No AI calls |
| **03 Classify** | Run `shared/diagnosis.py` on the evidence, then batch classify whatever it abstained on via Claude CLI | No DB writes, no git |
| **04 Review** | Independent review of classifications, multi-round debate, .verdict | No DB, no code changes |
| **05 Ship** | HTML report, write handoff.json to test-healing-agent queue, Slack notify | No AI, no code changes, no PR |

---

## Data Flow

```
01-scout.json
    ↓ .selected-buildtag
02-collect.json          (test_results, flaky_tests, trend, summary)
    ↓                    (failures now carry dom_snapshot / trace_path / failure_context —
                          attached here, because the classifier decides what kind of
                          failure each one is and cannot do that without the evidence)
03-classify.json         (classifications with confidence, root_cause_category, signature;
                          deterministic verdicts from the diagnosis engine come first and
                          are not sent to the model at all)
    ↓
.verdict                 (APPROVED or NEEDS-HUMAN)
    ↓
05-ship.json             (report_path, handoff_path, slack_notified)
    ↓
agents/test-healing-agent/queue/<build_tag>.json   ← consumed by test-healing-agent
audit/<session>/dom/<method>.html                 ← failure-time DOM, referenced by the handoff
audit/<session>/traces/<method>.zip               ← Playwright trace, referenced by the handoff
```

**Handoff criteria** (written only when verdict=APPROVED):
- `classification = AUTOMATION_ISSUE`
- `confidence = HIGH`
- `root_cause_category` is one the healing agent can act on: `LOCATOR_STALE`,
  `AMBIGUOUS_LOCATOR` (`diagnosis.ACTIONS`) or the classifier's `ELEMENT_NOT_FOUND`
  — and not a `diagnosis.STOP` verdict. Stop verdicts (`WRONG_PAGE`,
  `NOT_READY`, `TOO_SLOW`, `BLOCKED`, `DATA_PRECONDITION`, `ERROR_STATE`, …) are
  never forwarded: no locator edit can fix them.

This is `write_handoff()` in `actions/05_ship.py`, and
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md#handoff-criteria) states the same
rule. Selecting on `ELEMENT_NOT_FOUND` alone used to forward wrong-page failures
wearing a locator's label.

---

## Classification Criteria

### PRODUCT_BUG indicators
- Assertion failures on business logic (wrong data, wrong status, wrong count)
- API returning unexpected status codes (4xx with wrong semantics, wrong 5xx)
- OTP failures — always PRODUCT_BUG + ASSERTION_FAILURE category
- Data mismatches between what the app shows and what the test expects
- Feature not working correctly

### AUTOMATION_ISSUE indicators
- `NoSuchElementException`, `ElementClickInterceptedException`, `ElementNotInteractableException`
- `TimeoutException`, page load timeout messages ("'PageName' NOT loaded even after X seconds")
- `NullPointerException` in test code (not in application code)
- WebDriver session issues, Selenium errors
- Stale element references
- CSS/XPath locators that no longer match the DOM

### Confidence Levels
- **HIGH**: Clear exception type with specific selector/locator, unambiguous error pattern
- **MEDIUM**: Error pattern fits classification but could be either
- **LOW**: Ambiguous, needs human review

### Root Cause Categories
- `ELEMENT_NOT_FOUND` — NoSuchElementException, locator issues (what an LLM answers
  when it has only the error text; the diagnosis engine refines it into the verdicts below)
- `LOCATOR_STALE` / `AMBIGUOUS_LOCATOR` — measured, and fixable by a locator edit
- `WRONG_PAGE` / `PRIOR_STEP_FAILED` / `ERROR_STATE` / `ENV_UNREACHABLE` /
  `DATA_PRECONDITION` / `FLAKY_TRANSIENT` / `ELEMENT_GONE` / `NOT_READY` /
  `TOO_SLOW` / `BLOCKED` — measured, and not fixable by a locator edit (timing and
  obstruction need a wait or timeout change a human makes, since the framework's
  wait budget is global)
- `TIMEOUT` — TimeoutException, page load waits
- `ASSERTION_FAILURE` — expected vs actual mismatches
- `ENVIRONMENT_ISSUE` — API 500 errors, server connectivity
- `CODE_ISSUE` — NullPointerException in test code
- `OTHER` — unclassified

---

## Audit Trail

**Session folder:** `agents/test-triaging-agent/audit/$SESSION_ID/`

| File | Written by | Purpose |
|---|---|---|
| `00-session-init.md` | run.sh | Session metadata |
| `01-scout.json` + `.md` | Scout | Scored build tags, selected build tag |
| `.selected-buildtag` | Scout | Build tag for downstream steps |
| `02-collect.json` + `.md` | Collect | All test results, flaky list, trend, summary |
| `03-classify.json` + `.md` | Classify | All classifications with confidence |
| `04-review-r{N}.md` | Review | Each review round |
| `03-classifier-rebuttal-r{N}.md` | Review | Classifier rebuttals if challenged |
| `.verdict` | Review | APPROVED or NEEDS-HUMAN |
| `05-ship.json` + `.md` | Ship | Report path, handoff path, final status |

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `TRIAGING_DB_HOST`, `TRIAGING_DB_PORT`, `TRIAGING_DB_USER`, `TRIAGING_DB_PASSWORD`, `TRIAGING_DB_NAME` | MySQL database |
| `TRIAGING_INPUT_DIR` | Directory containing test report HTML |
| `TRIAGING_OUTPUT_DIR` | Where to save generated HTML reports |
| `CLAUDE_CLI_PATH` | Path to claude CLI binary (default: claude) |
| `TRIAGING_CLASSIFIER_MODEL` | Claude model for classification (default: claude-opus-4-6) |
| `TRIAGING_REVIEWER_MODEL` | Claude model for review (default: claude-sonnet-4-6) |
| `TRIAGING_CLASSIFIER_EFFORT`, `TRIAGING_REVIEWER_EFFORT` | Reasoning effort, `low` / `medium` / `high` (default: medium) |
| `TRIAGING_MAX_REVIEW_ROUNDS` | Max reviewer/classifier debate rounds (default: 2) |
| `TRIAGING_SCOUT_LOOKBACK_DAYS` | Days to look back for build tags (default: 7) |
| `BUILD_TAG` | Direct mode override — skip scout |
| `STOP_AFTER` | Stop after step: `scout`, `collect`, `classify`, `review` |
| `TRIAGING_AUTOFIX_QUEUE_DIR` | Path to test-healing-agent queue dir (default: `agents/test-healing-agent/queue` inside repo) |
| `SLACK_BOT_TOKEN`, `SLACK_NOTIFY_CHANNEL`, `SLACK_ALERT_CHANNEL` | Slack notifications (NEEDS-HUMAN goes to the alert channel) |
| `SESSION_ID`, `AUDIT_DIR` | Set by run.sh — do not set manually |

---

## How to Run

```bash
# Scout mode — agent finds the best unanalyzed build tag
./scripts/run-triaging-agent.sh

# Direct mode — analyze a specific build tag
./scripts/run-triaging-agent.sh ProdSanity-All-Tests-541

# Stop at any step for inspection
STOP_AFTER=collect ./scripts/run-triaging-agent.sh
STOP_AFTER=classify ./scripts/run-triaging-agent.sh ProdSanity-All-Tests-541

# View audit trail
make audit AGENT=test-triaging-agent
make audit AGENT=test-triaging-agent SESSION=20260328-143022-ProdSanity-All-Tests-541
```

---

## Key Rules

1. **Write audit entry before every irreversible action** (Slack messages)
2. **Classifier and reviewer run as separate subprocess calls** — zero shared context
3. **No handoff until .verdict=APPROVED** — escalate to Slack if NEEDS-HUMAN
4. **Handoff targets only HIGH-confidence locator failures** (`LOCATOR_STALE`, `AMBIGUOUS_LOCATOR`, `ELEMENT_NOT_FOUND`) — never PRODUCT_BUG
5. **Exit cleanly after every run** — success or failure. Not a daemon.
6. **Secrets never logged** — env var names are fine, never their values
7. **skip-buildtags.json is updated at the end of every run** — prevents re-processing

## Reviewer Self-Resolving Checklist (before NEEDS-HUMAN)

The reviewer (04_review.py) MUST exhaust this checklist before emitting VERDICT: NEEDS-HUMAN:

1. Re-read the full failure output and stack trace for each disputed classification
2. Check if the error pattern is consistent across multiple test runs (flaky vs systematic)
3. Check sibling tests in the same class — if others pass, the failure is likely isolated (automation issue)
4. Check for ENVIRONMENT_ISSUE signals (API 500s, connection timeouts) — these are NOT fixable by locator changes
5. Only escalate if >20% of classifications are wrong, or a HIGH-confidence AUTOMATION_ISSUE looks like PRODUCT_BUG

Minor disagreements on LOW/MEDIUM confidence tests do NOT require NEEDS-HUMAN.

## Prompt Templates

Static review prompt sections are loaded from `config/prompts/review.md` at
runtime — edit that file to change review criteria. The classification rules
are inline in `build_batch_prompt()` in `actions/03_classify.py`.

---

## DOM Snapshots

When the automation framework captured the page's HTML at the moment of failure
(`BrowserHelper.captureDomSnapshot` → `{resultsDirectory}/dom/<method>_<time>.html`),
step 02 copies it into `audit/<session>/dom/<method>.html` (along with the trace and
page baselines) so step 03's diagnosis can read it, and step 05 puts that path plus
the failure URL into the handoff, filling any gap step 02 left.

Copying rather than referencing matters: CI cleans up the report directory, and
the handoff has to stay valid until test-healing-agent picks it up. That snapshot
is what lets the healing agent fix a locator that broke deep inside a user journey
without replaying the flow, logging in, or reconstructing the test data.

Missing snapshots are never fatal — the fields are simply left empty.

The Playwright trace (`traces/<method>_<time>.zip`) is copied the same way when the
framework recorded one; the failing selector is read straight out of its action
timeline into `failed_selector`, and the handoff references the zip as `trace_path`. Whoever
reviews the PR can open that zip in Playwright Trace Viewer and step through the
whole flow.

---

## Root-Cause Grouping (step 03)

Before classifying, failures are grouped by a normalized signature — error type
plus the error message with run-specific noise (uuids, timestamps, durations,
ids) stripped out. One representative per group is classified, and the verdict is
shared with its siblings.

This is not only about cost. Classifying the same defect thirty times gives
thirty independent answers: the same broken locator can come back HIGH confidence
for one test and MEDIUM for another, and since the handoff filter requires HIGH,
only some siblings reach the healing agent. The rest stay red with no explanation.
One judgement per defect makes that impossible.

Sharing is deliberately restricted. A verdict is only inherited when the category
is `ELEMENT_NOT_FOUND`, `TIMEOUT` or a diagnosis verdict **and** confidence is HIGH
or MEDIUM.
Assertion failures with identical messages can have unrelated causes, so their
siblings are marked LOW confidence and flagged for individual review instead.

Each classification carries `cause_group_key`, `cause_group_size` and
`is_group_representative`, and these flow into the handoff so the healing agent
starts with the grouping already known.
