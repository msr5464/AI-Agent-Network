# Architecture

## System Overview

QA Agent Network is three independent agents that own distinct slices of the QA lifecycle. They share a common configuration, a set of Python/shell helpers (`shared/`), and communicate via file-based handoffs.

```
Plain English test steps
        │
        ▼
┌────────────────────────────────────────────────┐
│  Agent 1: test-authoring-agent                 │
│  queue/<module>.txt → Java code → GitHub PR    │
└────────────────────────────────────────────────┘

CI build finishes (test results written to MySQL)
        │
        ▼
┌────────────────────────────────────────────────┐
│  Agent 2: test-triaging-agent                  │
│  MySQL → classify failures → HTML report       │
└──────────────────────┬─────────────────────────┘
                       │ queue/<build_tag>.json
                       │ (AUTOMATION_ISSUE + HIGH + ELEMENT_NOT_FOUND only)
                       ▼
┌────────────────────────────────────────────────┐
│  Agent 3: test-healing-agent                   │
│  handoff → fix locators → GitHub PR            │
└────────────────────────────────────────────────┘
```

Each agent runs completely independently. There is no orchestration layer — the handoff file written by Agent 2 is the only coupling between Agent 2 and Agent 3.

---

## Agent Responsibilities

### Agent 1 — test-authoring-agent

Turns a plain English feature file into production-ready Java test code and raises a PR.

| Step | What happens |
|------|-------------|
| 01 Parse | Claude reads the `.txt` file and produces a structured generation plan (classes, methods, UI steps) |
| 02 Validate Web | A headless Playwright script visits each URL and confirms element selectors exist in the real DOM |
| 03 Generate | Claude writes the Java test class, page object, and data provider to the automation repo |
| 04 Run + Fix | Maven runs the generated test; if it fails, Claude fixes the code and retries (up to `MAX_FIX_ATTEMPTS`) |
| 05 Ship | Git branch → commit → push → `gh pr create` → Slack notification |

**Input:** `agents/test-authoring-agent/queue/<module>.txt`  
**Output:** GitHub PR on the Jarvis automation repo

---

### Agent 2 — test-triaging-agent

Reads CI test results from MySQL, classifies every failure as `PRODUCT_BUG` or `AUTOMATION_ISSUE`, and produces an HTML report. Fixable locator failures are queued for Agent 3.

| Step | What happens |
|------|-------------|
| 01 Scout | Queries MySQL for build tags not yet analysed; scores and selects the best candidate |
| 02 Collect | Full DB query + HTML log parse + flaky test detection + trend analysis |
| 03 Classify | Claude batch-classifies each failure with confidence (`HIGH` / `MEDIUM` / `LOW`) |
| 04 Review | A second Claude call independently reviews all classifications; up to `MAX_REVIEW_ROUNDS` debate rounds; issues a `.verdict` (APPROVED / NEEDS-HUMAN) |
| 05 Ship | Generates HTML report, writes handoff JSON to Agent 3's queue (if APPROVED + eligible failures), sends Slack notification |

**Input:** MySQL database containing test run results  
**Output:** HTML report + `agents/test-healing-agent/queue/<build_tag>.json` (if actionable)

**Handoff criteria** — a failure is queued for Agent 3 only when **all three** hold:
- `classification = AUTOMATION_ISSUE`
- `confidence = HIGH`
- `root_cause_category = ELEMENT_NOT_FOUND`

---

### Agent 3 — test-healing-agent

Picks up the handoff from Agent 2, fixes broken locators using Claude, verifies each fix by running Maven, and raises a PR.

| Step | What happens |
|------|-------------|
| 01 Fix | Reads handoff; for each failed test: builds code context (test file + page object), calls Claude for a locator fix, applies it, runs `mvn verify`; commits passing fixes; retries failing ones |
| 02 Ship | Pushes branch, creates PR, sends Slack notification (all-fixed to `#qa-reports`, partial to `#qa-critical`) |

**Input:** `agents/test-healing-agent/queue/<build_tag>.json` written by Agent 2  
**Output:** GitHub PR on the Jarvis automation repo

---

## Session and Audit Structure

Every agent run creates a timestamped session folder. These are the source of truth for debugging.

```
agents/<agent-name>/audit/<session-id>/
│
├── 01-parse.json          # Step 01 output (authoring: generation plan)
├── 02-collect.json        # Step 02 output (triaging: collected test data)
├── 03-classify.json       # Step 03 output (triaging: classifications)
├── .verdict               # APPROVED or NEEDS-HUMAN (triaging)
├── .fix-passed            # true / false / skipped (authoring + healing)
├── 05-ship.json           # Final ship result: PR URL, Slack status
└── *.md                   # Claude prompts and responses (one per AI call)
```

Session IDs follow the pattern `YYYYMMDD-HHMMSS-<module-or-buildtag>`.

To browse sessions interactively:
```bash
make dashboard          # web UI at http://localhost:8888
make audit AGENT=test-triaging-agent                  # list recent sessions
make audit AGENT=test-triaging-agent SESSION=<id>     # inspect one session
```

---

## Configuration and .env Load Order

```
shared/load_env.sh loads:
  1. config/.env          (shared base — all agents)
  2. agents/<agent>/.env  (agent-level override — wins over config/.env)
```

Shell environment variables always take precedence over `.env` file values, so you can override for a single run:
```bash
AUTO_PUSH=false make run AGENT=test-healing-agent
TESTING_MODE=true make run AGENT=test-authoring-agent MODULE=payments
```

---

## Shared Helpers (`shared/`)

| File | Purpose |
|------|---------|
| `claude.py` | Subprocess wrapper for `claude -p` with streaming and logging |
| `github.py` | GitHub REST API helpers (PR creation, branch management) |
| `slack.py` | Slack Bot API (`chat.postMessage`) |
| `git.py` | Git command wrappers |
| `audit.py` | Writes structured audit files per session |
| `load_env.sh` | Two-level `.env` loader (config/ → agent override) |
| `session.sh` | `log()`, `run_step()`, `fmt_duration()` used by every `run.sh` |

---

## Feedback Loops

Each agent has a `feedback/` directory for manual corrections:

| File | Agent | Purpose |
|------|-------|---------|
| `feedback/skip-buildtags.json` | Triaging | Build tags to skip during scout (e.g. known-bad builds) |
| `feedback/known-issues.json` | Triaging + Healing | Patterns that should never be auto-fixed |

View and clear feedback:
```bash
make feedback AGENT=test-triaging-agent
make clear-feedback AGENT=test-triaging-agent
```
