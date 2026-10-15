# Architecture

## System Overview

QA Agent Network is four independent agents that own distinct slices of the QA
lifecycle. They share a common configuration, a set of Python/shell helpers
(`shared/`), and communicate through files.

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
│  MySQL → diagnose + classify → HTML report     │
└──────────────────────┬─────────────────────────┘
                       │ queue/<build_tag>.json
                       │ (locator failures only — see Handoff criteria)
                       ▼
┌────────────────────────────────────────────────┐
│  Agent 3: test-healing-agent                   │
│  handoff, or one named test → locate → fix     │
│    → verify with the real test → GitHub PR     │
└──────────────────────┬─────────────────────────┘
                       │ queue/<module>-rebuilt.txt (DRAFT change note,
                       │ only when a page was rebuilt in place)
                       ▼
┌────────────────────────────────────────────────┐
│  Agent 4: test-adaptation-agent                │
│  queue/<module>.txt (change note)              │
│    → blast radius + frozen intent contracts    │
│    → explore the live product                  │
│    → update steps & logic → PR (NEEDS-REVIEW)  │
└────────────────────────────────────────────────┘
```

Each agent runs on its own, from the CLI (`scripts/run-<agent>-agent.sh`) or through
`qa_agents_server` (authoring, healing, adaptation only). The couplings are files:

- **Triaging → healing:** a handoff JSON in `agents/test-healing-agent/queue/`.
- **Healing → adaptation:** when a failing page's locators have all stopped
  matching but its URL shape still matches the last good run, the page was
  rebuilt, not missed. Healing does not try to fix that; it writes a
  **draft** change note, `<module>-rebuilt.txt`, into the adaptation queue
  (`shared/adaptation_handoff.py`). A human reviews and edits it before running
  it. Note that queue-mode `./scripts/run-adaptation-agent.sh` with no
  module picks the oldest file, drafts included.
- **Server:** `qa_agents_server` queues and schedules runs, but adds no logic
  between agents — see [Server execution model](#server-execution-model).

**Learning-agent.** A fifth agent tests nothing. It reads what people said about and
did to the other agents' work, and proposes **lessons** for them as a PR to this repo
(`make learn`). Nothing reaches an agent until a person merges that PR. See
[Human feedback](#human-feedback).

**Framework plugins.** The agents are framework-agnostic. They reach the target
automation repo through `shared/frameworks/` (Playwright and Selenium plugins),
which is chosen by detecting the repo's build files. See
[FRAMEWORK_INTEGRATION.md](FRAMEWORK_INTEGRATION.md), which also lists what the
target repo must provide.

**Agents 3 and 4 are deliberately separate.** Healing is reactive and holds exactly one
hypothesis: the selector string is stale. That narrowness is enforced at six layers on
purpose, because the verification loop cannot catch a fix built on a wrong diagnosis —
the easiest way to make an assertion pass is to weaken it. Adaptation may change test
*steps*, so it cannot rely on "the test went green" and needs a different acceptance
criterion (an intent contract), different evidence (an observed flow map) and a
different shipping policy (always NEEDS-REVIEW). Putting both in one agent would leave
the unattended nightly path one environment variable away from whole-file rewrites.

---

## Agent Responsibilities

### Agent 1 — test-authoring-agent

Turns a plain English feature file into production-ready Java test code and raises a PR.

| Step | What happens |
|------|-------------|
| 01 Parse | Claude reads the `.txt` file and the target repo's `CLAUDE.md`, and produces a structured generation plan (classes, methods, UI/API steps) |
| 02 Validate API | For API steps: plain HTTP calls against the real endpoints, no model call |
| 02 Validate Web | For UI steps: Claude drives a browser through the Playwright MCP server, recording which selectors match exactly one visible element |
| 03 Generate | Claude writes the test class, page objects and data; URL and credential keys go into the repo's properties file, and a compile gate runs before the step ends |
| 04 Run + Fix | Runs the generated test; on failure Claude fixes the code and retries. Only attempts that make no progress count against `AUTHORING_FIX_RETRY_COUNT` (a fix that lets the test reach a new failure does not), up to `AUTHORING_MAX_FIX_ATTEMPTS`, stopping early once an attempt can bring nothing new. Assertions are frozen, so a fix cannot weaken them |
| 05 Ship | Branch → commit → push → `gh pr create` → Slack. Verdict `APPROVED` only when the test ran and passed honestly, otherwise `NEEDS-REVIEW` |

**Input:** `agents/test-authoring-agent/queue/<module>.txt`  
**Output:** a PR on the automation repo, branch `<AUTHORING_BRANCH_PREFIX>/<module>-<timestamp>`

Resume a session from a later step with `START_FROM_STEP=<2-5> SESSION_ID=<id>`.

---

### Agent 2 — test-triaging-agent

Reads CI test results from MySQL, classifies every failure, and produces an HTML
report. Fixable locator failures are queued for Agent 3. CLI and CI only — the
server does not run it.

| Step | What happens |
|------|-------------|
| 01 Scout | Queries MySQL for build tags not yet analysed; scores and selects the best candidate |
| 02 Collect | Full DB query + HTML log parse + flaky test detection + trend analysis; copies each failure's DOM snapshot, trace and baselines into the audit dir |
| 03 Classify | Runs the [failure diagnosis](#failure-diagnosis) first — a HIGH-confidence verdict is authoritative — then Claude batch-classifies the rest as `PRODUCT_BUG`, `AUTOMATION_ISSUE` or `UNKNOWN`, with confidence |
| 04 Review | A second Claude call reviews the classifications; up to `TRIAGING_MAX_REVIEW_ROUNDS` rebuttal rounds; writes `.verdict` (`APPROVED` / `NEEDS-HUMAN`) |
| 05 Ship | HTML report `AI-Generated-Report_<tag>.html`, handoff JSON to Agent 3's queue (if `APPROVED` and anything is eligible), Slack; appends the build to `feedback/skip-buildtags.json` |

**Input:** MySQL database containing test run results  
**Output:** HTML report + `agents/test-healing-agent/queue/<build_tag>.json` (if actionable)

#### Handoff criteria

A failure is queued for Agent 3 only when **all** hold
(`agents/test-triaging-agent/actions/05_ship.py:write_handoff`):

- `classification = AUTOMATION_ISSUE`
- `confidence = HIGH`
- `root_cause_category` is `LOCATOR_STALE`, `AMBIGUOUS_LOCATOR` or `ELEMENT_NOT_FOUND`
  (i.e. `diagnosis.ACTIONS` plus the classifier's legacy `ELEMENT_NOT_FOUND`),
  and is not one of the `diagnosis.STOP` verdicts.

Everything else is reported, not handed off. This is the single definition of the
rule; other docs link here.

---

### Agent 3 — test-healing-agent

Fixes broken locators — nothing else — verifies each fix by running the real
test, and raises a PR.

Two ways in:
- **Pipeline:** a handoff from Agent 2 (`./scripts/run-healing-agent.sh`,
  optionally a build tag or a handoff file).
- **Standalone:** name the test (`./scripts/run-healing-agent.sh --test Class#method`,
  a whole class also works). Step 00 runs it, reproduces the failure and writes
  the handoff itself.

| Step | What happens |
|------|-------------|
| 00 Reproduce | Standalone only. Runs the test; a pass or a non-locator failure ends the run with an explanation (`REPAIR=true` parks the failing browser for live inspection; `FORCE=true` proceeds on non-locator failures) |
| 01 Locate | Deterministic, offline: compares the element fingerprint recorded on the last green run with the capture taken at failure, and proposes a selector proved unique against the saved DOM. No model call, no browser. Applied by Fix only when `HEALING_LOCATE_MODE=enforce` |
| 01 Fix | Runs the [failure diagnosis](#failure-diagnosis); on a locator verdict, applies Locate's proposal or asks Claude for one, then re-runs the real test. Commits passing fixes, rolls back failing ones |
| ↺ | Locate + Fix repeat while each attempt makes progress (one repaired locator often uncovers the next). Budget: `HEALING_RETRY_COUNT` attempts *without progress*, hard ceiling `HEALING_MAX_ATTEMPTS` |
| 02 Ship | Push branch → PR → Slack (all fixed → notify channel, partial/none → alert channel) |

**Input:** `agents/test-healing-agent/queue/<build_tag>.json`, or a test name  
**Output:** a PR on the automation repo, branch `<HEALING_BRANCH_PREFIX>/<session-id>`

After the run the handoff moves to `queue/processed/` — except on an infra skip
(`.skip-reason` = `infra`) or a crash, where it goes back to the queue for a retry.
Queue mode claims a handoff by atomic `mv`, so concurrent runs never fix the same one.

---

### Agent 4 — test-adaptation-agent

Updates tests when the **product** changes, driven by a plain-English change note,
before the tests go red.

| Step | What happens |
|------|-------------|
| 01 Parse Change | Headers (`Module:`, `Type:`, `Affects:`/`Tests:`, URLs) parsed in Python; Claude classifies what kind of change each numbered item is, which sets how much authority the repair gets |
| 02 Scope | Static read of the repo, no model: the blast radius (named tests, tests sharing the changed surface, excluded infrastructure, cost to verify) and a **frozen intent contract** per test — what it proves today |
| 03 Explore | API half: HTTP against the live endpoints. Web half: Claude drives the live product through the Playwright MCP, from a saved login session (minted first if none is valid). Combined into one ordered **flow map**; every selector is re-counted in Python |
| 04 Adapt | Edits Java per change item, as a transaction: snapshot → apply → guards → compile → verify → restore everything on any failure. Retries up to `ADAPTATION_RETRY_COUNT`. `ADAPTATION_APPLY=false` proposes without editing |
| 05 Ship | Commits one change item at a time, opens a PR that is **always NEEDS-REVIEW**, with each edit tied to the observed step that justified it |

**Input:** `agents/test-adaptation-agent/queue/<module>.txt`  
**Output:** a PR on the automation repo, branch `<ADAPTATION_BRANCH_PREFIX>/<module>-<timestamp>`

`EXPLORE_ONLY=true` stops after step 03 (flow map only). Resume with
`START_FROM_STEP=<2-5> SESSION_ID=<id>`. It refuses rather than guesses when the
change note does not explain what it observed, when the expected *outcome*
changed, or when the flow ends in something irreversible. See
[agents/test-adaptation-agent/CLAUDE.md](../agents/test-adaptation-agent/CLAUDE.md).

---

## How healing decides

### Failure diagnosis

`shared/diagnosis.py` answers "why wasn't the element there?" from evidence already
on disk — the DOM snapshot, the trace, page baselines, the test's own step
history — before anything edits a locator. It returns one verdict:

| Kind | Verdicts | Effect |
|------|----------|--------|
| Actionable | `LOCATOR_STALE`, `AMBIGUOUS_LOCATOR` | Healing may edit the selector (narrow it, for ambiguity) |
| Stop | `WRONG_PAGE`, `PRIOR_STEP_FAILED`, `ERROR_STATE`, `ENV_UNREACHABLE`, `DATA_PRECONDITION`, `FLAKY_TRANSIENT`, `ELEMENT_GONE`, `NOT_READY`, `TOO_SLOW`, `BLOCKED` | No locator edit can fix it; report the cause and the change a human would make |
| Abstain | `INSUFFICIENT_EVIDENCE` | Fall back to the pre-diagnosis behaviour |

Triaging uses it in step 03; healing in step 01. In healing,
`DIAGNOSIS_MODE=shadow` logs what a stop verdict *would* have done, and `enforce`
ends the run on it (`FORCE=true` overrides either way). Measure a mode switch
with `scripts/diagnosis_soak.py`.

### Locator engine and baselines

`shared/locator_*.py` fingerprint elements (`locator_capture.py`, using the target
repo's `locator-capture.js`), score candidates, and emit a selector in the repo's
own syntax. Page **baselines** — title, URL shape, per-locator match counts — are
written by the target framework on every successful page load; diagnosis and
Locate compare the failing page against them (`shared/baseline.py`). The ship
steps commit refreshed baselines, and `scripts/commit_baselines.py` does the same
after a green CI run. `locator-eval/` is the offline benchmark for this engine.

---

## Server execution model

`qa_agents_server` (see [SERVER_API.md](SERVER_API.md)) lets several people run
agents at once without seeing or disturbing each other's work.

- **Isolation.** A run with auto-push on gets its own detached git worktree under
  `QA_WORKTREE_TEMP_DIR` (default `/tmp/qa-runs/<session_id>`), cut from
  `origin/<base_branch>`. The runner points `FRAMEWORK_DIR` at it. Worktree
  artefacts (`test-output/`) are copied into the session's audit dir before the
  worktree is removed. Git operations on the shared `.git` are serialised by a
  lockfile.
- **Local mode.** A run with auto-push off (a dry run) executes **in the
  developer's own checkout as it stands**, uncommitted changes included, and only
  one such run executes at a time.
- **Concurrency.** Up to `QA_MAX_CONCURRENT_RUNS` (default 4) run at once; the rest
  queue. The queue is drained fewest-active-runs-per-user first, so one user
  firing ten runs cannot starve everyone behind them.
- **Identity.** AI-Test-Studio authenticates users and injects `X-User-ID` /
  `X-User-Name` / `X-User-Role`. The server validates them once
  (`routes.current_user_id`) and never uses a raw header as a path. Session-scoped
  routes are default-deny (`@owns_session`). The server binds `127.0.0.1` by
  default; if it must listen elsewhere, set `QA_AGENT_PROXY_SECRET` in both repos
  so identity headers are only trusted from the Studio proxy.
- **Per-user state.** `.txt` queues and `CACHE_STEPS` caches are per user:
  `queue/<user-id>/` and `cache/<user-id>/<module>/`. The CLI and anonymous
  identities (`cli`, `default`) use the queue root. Healing's `.json` queue is
  global. Each healing run gets its own CDP port for repair mode (`repairPort`).

---

## Session and Audit Structure

Every agent run creates a session folder, the source of truth for debugging:
`agents/<agent-name>/audit/<session-id>/`.

Session IDs:

| Agent | Pattern |
|-------|---------|
| authoring | `YYYYMMDD-HHMMSS-create-<module>` |
| triaging | `YYYYMMDD-HHMMSS-<build-tag>` (scout mode starts as `…-scout` and is renamed) |
| healing | `YYYYMMDD-HHMMSS-fix-<build-tag>`, or `…-fix-local-<Class>-<method>` standalone |
| adaptation | `YYYYMMDD-HHMMSS-adapt-<module>` |

Contents:

```
agents/<agent>/audit/<session-id>/
├── 00-session-init.md          # mode, inputs, env keys (values masked)
├── NN-<step>.json / .md        # each step's structured output and human-readable report
├── claude-<timestamp>.log      # full prompt + response for every Claude call
├── stdout.log                  # the whole run's console (server runs)
├── metrics/                    # time and cost, appended as the run proceeds
│   ├── llm-calls.jsonl         #   one line per `claude -p` invocation
│   ├── stages.jsonl            #   one line per completed run_step
│   └── tools.jsonl             #   one line per maven/compile subprocess
├── metrics.json                # rolled-up totals — what the server and UI read
├── test-output/, screenshots/, baselines/   # artefacts copied from the run
└── .<marker>                   # small state files, below
```

| Marker | Written by | Values |
|--------|-----------|--------|
| `.verdict` | triaging, authoring, adaptation | triaging `APPROVED` / `NEEDS-HUMAN`; authoring `APPROVED` / `NEEDS-REVIEW`; adaptation always `NEEDS-REVIEW` |
| `.fix-passed` | authoring, healing, adaptation | `true`, `false`, `skipped` (no test ran), `stuck` (retries could bring nothing new), `defect` (the product, not the test) |
| `.fix-retry` | healing | `retry` or `stop: <reason>` — drives the Locate/Fix loop |
| `.skip-reason` | healing, adaptation | `infra` (handoff stays queued), `no-work`, `diagnosed` (a stop verdict), `stuck`; adaptation also `no-session`, `unreachable`, `unsafe` — the step report says why |
| `.fix-history.json` | authoring, adaptation | what each fix attempt tried, so the next one does not repeat it |
| `.snapshots.json`, `.check-changes.json` | adaptation | originals for rollback; declared check changes |
| `.base-branch` | base checkout (`shared/workspace.py`) | the base branch and SHA; a resumed run reuses them |
| `.crashed`, `.cancelled`, `.interrupted` | run.sh / server | how a run ended abnormally |
| `.selected-buildtag` | triaging | the build scout picked |

Triaging's review writes `04-review-r<N>.md` and `03-classifier-rebuttal-r<N>.md`
per round.

To browse sessions:
```bash
make dashboard                                     # web UI at http://localhost:8888
make audit AGENT=test-triaging-agent               # list recent sessions
make audit AGENT=test-triaging-agent SESSION=<id>  # inspect one session
```

### Time and cost metrics

Every run records what it spent, in wall time and in dollars:

- **Per-stage time** comes from `run_step()` in `shared/session.sh`. A run's
  duration is the sum of its stage times, not first-start to last-end, so a
  resumed run doesn't count the idle gap between attempts.
- **Tokens and dollars** come from the Claude CLI's own `result` event, which
  reports `total_cost_usd` and per-model usage. No rate card is needed on our
  side, and cost is attributed to the model the CLI actually *ran* — which is not
  always the one requested.
- **Tool time** comes from the places that time a Maven or compile subprocess.

Capture is centralised: `shared/claude.py` records every LLM call from inside
`call_claude_ex()`, so all call sites are instrumented without touching any of
them. Recording is best-effort throughout — a metrics failure must never fail a
run.

At the end of a run (including a crashed one, via an `EXIT` trap) the JSONL
streams are rolled up into `metrics.json`, and one row is appended to
`qa_agents_server/storage/run_analytics.jsonl`. That store is append-only and
never pruned, because audit directories are gitignored and local-only, a resumed
run overwrites its predecessor's step files, and the run registry is capped at
500 entries — none of which survive a reporting window. The Studio's Analytics
page reads it through `GET /analytics/summary`.

A row's `status` is how the run counts. `analytics._status` reads it from the
session's own gate, skip-reason, ship and marker files, never from whoever
launched the run, so a CLI run and a Studio run with the same outcome score the
same:
- `completed` and `diagnosed` (a correct stop or hand-off) are successes, and `failed` is the only other verdict.
- `blocked` (infrastructure), `cancelled`, `interrupted` and `explore-only` are left out of the success rate, though their spend still counts.
- A push or PR that fails after the work passed doesn't change that: the run is still `completed` and its tests are credited. The work survives on a local branch or in the audit trail, and the run's own history flags the delivery.
- A row with no tokens, no spend and no output is ignored when the store is read.

---

## Configuration and .env Load Order

`config/.env.example` is the reference for every setting. `shared/load_env.sh`
activates `.venv` if present, then loads, in order (later wins):

```
1. config/.env           shared base — all agents (what the Studio's Agent Settings page edits)
2. .env                  repo-root override
3. agents/<agent>/.env   agent-level override
```

A variable already exported in the calling shell always wins over every `.env`
file, so you can override for a single run:
```bash
AUTO_PUSH=false ./scripts/run-healing-agent.sh
CACHE_STEPS=false ./scripts/run-authoring-agent.sh payments
```

---

## Shared Helpers (`shared/`)

Grouped by concern; each module's docstring explains its rules.

| Concern | Modules |
|---------|---------|
| Running Claude | `claude.py` (the `claude -p` wrapper: streaming, logging, cost), `mcp_config.py` (Playwright MCP config), `json_extract.py`, `narration.py` |
| Repo and git | `workspace.py` (find/clone the automation repo, base checkout, worktrees, git lock), `git.py`, `github.py` (`gh pr create`), `repo_config.py`, `properties_file.py`, `url_properties.py`, `credential_properties.py`, `credential_extraction.py` |
| Framework access | `frameworks/` (plugins + detection), `telemetry.py` (action timelines), `dom_snapshot.py`, `trace_network.py`, `test_runner.py`, `test_catalog.py`, `code_analyzer.py`, `module_index.py` (what code already exists, and what a change changed), `browser_mode.py` |
| Diagnosis and locators | `diagnosis.py`, `baseline.py`, `page_identity.py`, `preconditions.py`, `failure_context.py`, `failure_identity.py`, `failure_clusters.py`, `history.py`, `locator_*.py` (capture, candidates, score, decide, resolve, verify, emit, patch, classify, assertions), `option_sets.py` (the options a choice offered, from browser evidence) |
| What a test proves | `intent.py`, `assertion_graph.py`, `logstep_narration.py`, `step_provenance.py`, `check_provenance.py`, `edit_guards.py`, `fix_history.py` |
| Adaptation | `blast_radius.py`, `flow_map.py`, `entry_path.py`, `session_state.py`, `mint_session.py`, `adaptation_handoff.py` |
| Session plumbing | `load_env.sh`, `session.sh` (`log`, `run_step`, `fmt_duration`), `log.py`, `audit.py`, `run_artifacts.py`, `metrics.py`, `slack.py`, `verdict_feedback.py` |
| Human feedback (below) | `feedback.py` (the store and its events), `slack_feedback.py` (Slack replies), `lessons.py` (lessons into prompts), `precedents.py` (accepted fixes as examples) |

---

## Feedback Files

| File | Agent | Purpose |
|------|-------|---------|
| `feedback/skip-buildtags.json` | Triaging | Builds already analysed (appended automatically by step 05) plus any you add by hand. Scout skips them |
| `feedback/known-issues.json` | Healing | Patterns that must never be auto-fixed, and diagnoses that proved wrong: a forced fix that verified (a false stop), or a fix its reviewer rejected as the wrong flow or page (a false `LOCATOR_STALE`) |
| `feedback/proposals.json` | Adaptation | Edits proposed (not applied) by step 04, awaiting a human verdict (`make verdict`). How often they are accepted verbatim is the evidence for turning `ADAPTATION_APPLY` on |

```bash
make feedback AGENT=test-triaging-agent         # print
make clear-feedback AGENT=test-triaging-agent   # reset — every build becomes eligible for re-analysis
```

---

## Human feedback

What people do with the agents' work is the evidence for improving them. It is
captured in one append-only store, `qa_agents_server/storage/feedback.jsonl`
(`shared/feedback.py`), one event per line. The learning-agent reads it and
proposes lessons ([Learning from it](#learning-from-it)). Nothing changes how an
agent behaves until a person approves the change.

**Where it comes from**

| Channel | How | Event |
|---|---|---|
| The reviewer verdict block at the end of every agent PR (added by `shared/github.create_pr`) | The reviewer ticks *Accept as is*, *Accept after my edits* or *Reject*, plus any reasons from a fixed list. Unticked means not reviewed, never rejected | `verdict` |
| A PR comment that starts with `/lesson` | The reviewer teaches the agent in one line | `lesson` |
| Other PR comments and reviews | | `comment`, `review` |
| A commit a person adds to an agent's branch after the agent's last commit | Every agent commit carries an `Agent:` trailer beside its `Session:` one (`shared/git.with_agent_trailer`); a commit without one is a person's. It says whether it touched a file the agent wrote | `human_commit` |
| A revert of an agent's commit | Kept with the revert's own message | `revert` |
| A person's change to a merged PR's files within 14 days | | `post_merge_edit` |
| Merged or closed | | `pr_outcome` |
| `make verdict` | For a run with no PR to tick: a CI or dry run, or adaptation's proposals | `verdict` |
| `make reclassify` | A person corrects (or confirms) how triaging classified a failure; recorded beside what the agent said. Every triage report ends with the command for its build | `reclassification` |
| `FORCE=true` | A person told the agent to go past its own refusal: healing's reproduce step (not locator-shaped) and fix step (a stop verdict), authoring's assertion conservation. The outcome is recorded with it: `verified`, `advanced`, `failed`, or `proceeded`/`applied` when the run's own result says how it ended | `override` |
| An input rewritten before the next run | Authoring's and adaptation's step 01 compare the input with the previous run's copy in `queue/…/processed/`, and record how that run ended | `input_edit` |
| A draft an agent wrote, edited by a person | Healing stores the change note it drafts (`draft`); adaptation's step 01 compares the note it is given with it, and the edit is attributed to healing | `draft_edit` |
| Cancel or retry in the Studio | The server records the step a run was on, or the step it is retried from, and how it had ended | `cancel`, `retry` |
| *Your verdict* in a Studio session detail | The same choices and reasons as the PR block, plus a note; the same recording as `make verdict` (`POST …/sessions/<id>/verdict`) | `verdict` |
| Requirements → Tests in the Studio | When a run completes, the tests it offered (`generation`); when a person pushes an AI-written case to TestRail, the AI's text beside the pushed text (`test_push`, accepted as is or edited, with the diff). Matching the two by target says which tests were never pushed | `generation`, `test_push` |
| A reply in the thread of an agent's Slack message | `lesson: <what to do next time>`, `verdict: reject wrong-flow — <note>`, or anything else (kept as a comment). Each run remembers the messages it posted (`slack-messages.json` in its audit dir), and `shared/slack_feedback.py` reads their threads. A Slack message can't start with `/`, so these use plain words. The bot needs `channels:history` (`groups:history` for a private channel); without it, collecting says so and skips Slack | `lesson`, `verdict`, `comment` |

The PR verdict, `/lesson`, comments, `make verdict`, the Studio verdict,
`make reclassify`, `FORCE`, a pushed test case and Slack replies are explicit: a
person said something about the agent's work. The rest are
inferred, kept with `explicit: false`, and are never evidence on their own that an
agent was wrong. A closed PR is as often a discarded trial run, the one revert of
an agent's fix on record undid it on purpose to stage a failure for testing, and a
rewritten input may be a new request rather than a correction. Corrections to what
an agent wrote (`human_commit`, `draft_edit`) carry their diff, which is what makes
them usable later.

**Collecting**

```bash
make collect-feedback                          # read the agents' PRs into the store (read-only on GitHub)
make collect-feedback REPORT=reports/feedback.md   # and summarise what was found
make verdict AGENT=test-adaptation-agent SESSION=<id> VERDICT=edited REASON=convention,no-reuse
make reclassify BUILD_TAG=<tag> TEST=CartTest#checkout AS=PRODUCT_BUG CATEGORY=ASSERTION_FAILURE
```

The collector is idempotent: the same record always gets the same id, so a run
adds only what is new. A PR whose history cannot be read (a deleted base branch, a
GitHub error) is reported, never silently skipped. PRs from before the overview
table named the agent are recognised by their own heading or title, and commits
from before the `Agent:` trailer by their fixed subjects. GitHub errors that clear
on their own (a 5xx, a timeout) are tried again twice. A merged PR whose base branch
was deleted is followed on the default branch when its merge reached it, and listed
as not followed when it did not. `scripts/commit_baselines.py` signs its commits too
(`Agent: commit-baselines`), so a fingerprint refresh is never read as a person's
edit.

`make verdict` also writes where an agent already looks: it marks adaptation's
proposals judged, and a healing run rejected as the wrong flow or page is recorded
in healing's `known-issues.json` as a false `LOCATOR_STALE`. The collector does the
same for a ticked verdict.

Recording inside a run, or in the server, never fails or stops it
(`feedback.record_quietly`).

The store is git-ignored runtime data like the analytics store. Everything that
came from GitHub can be collected again; what runs, the server and the `make`
commands record exists only here. A live test run of an agent should set
`FEEDBACK_FILE` to a scratch file, as it sets `RUN_ANALYTICS_FILE`.

### Learning from it

```bash
make learn                       # collect, propose lessons, write a report; changes nothing
AUTO_PUSH=true make learn        # and open the lesson PR
```

**The learning-agent** ([agents/learning-agent/CLAUDE.md](../agents/learning-agent/CLAUDE.md))
collects from GitHub and Slack, then sorts each event:

- **Evidence** is a person saying something or changing what an agent wrote.
- **Context** is a revert or a rewritten input. The model reads it but it never
  counts.

The agent tags what each correction was about (a locator, an assertion, a wait,
test data…) and guesses where a lesson would apply. Then one model call proposes
adding, updating or retiring lessons, one at a time. Python checks every proposal
and refuses any that fails a rule:

- **Evidence.** A new lesson needs a `/lesson` (or Slack `lesson:`), or two
  independent pieces of work pointing the same way.
- **Generic text.** No session ids, PR numbers or links in a statement.
- **Never relaxes.** No statement that reads as skipping, weakening or removing a
  check.

Before the PR, `LEARNING_REPLAY_LIMIT` lessons are replayed. The model call a lesson
is for is re-sent with and without it, and the two answers are compared on how many
of the person's added lines each reproduces. With no evidence, it calls no model.

**Lessons** live in `config/lessons/<agent>.yaml`, one sentence each, with a scope:

- the agent's steps, and optionally the automation repo, framework, module, page
  object or diagnosis verdict
- the evidence ids
- a review date

A lesson is active once its PR is merged, and stays editable by hand.
`shared/lessons.py` puts the active ones whose scope fits in front of the step they
are for, most specific first, within `LESSONS_MAX_CHARS`:

- authoring: parse, generate, run-and-fix
- healing: fix
- adaptation: explore, adapt
- triaging: classify, review
- the Studio's Requirements → Tests: it fetches them from `GET /lessons/test-design-agent`

`LESSONS_MODE` in `config/.env` decides what happens:

| Mode | Effect |
|---|---|
| `off` | Nothing |
| `shadow` | Each step records which lessons matched but its prompt gets none |
| `on` | The lessons are put in the prompt |

Each session's `00-lessons.json` and its analytics row (`lessons`) say which lessons
it was given. The console says so once per step.

A lesson's lever says where it belongs:

| Lever | Where it belongs |
|---|---|
| `lesson` | A prompt, through `shared/lessons.py` |
| `convention` | The automation repo's `CLAUDE.md`, which the learning-agent proposes as a second PR when pushing. Every agent and every person reads it already |
| guard candidate | A lesson people still correct the same thing under, after `LEARNING_GUARD_AFTER` runs. It is listed in the report for a person to turn into a code check (`shared/edit_guards.py`). A prompt cannot enforce it |

**Precedents** (`shared/precedents.py`) are single pieces of work a person explicitly
accepted, shown as examples under `LESSONS_MODE=on`:

- **Healing's fix step:** a verified locator fix for the same page object or
  diagnosis verdict.
- **Authoring's run-and-fix step:** a passing fix for the same module.

When the person accepted the work after their own edits, their diff is the example.

**Review outcomes.** `GET /feedback/summary` (the Studio's Analytics page, *Review
Outcomes*) counts the same runs as Analytics. For each agent, it gives the newest
verdict each run got: accepted, accepted after edits, rejected, or not reviewed. It
also shows:

- for each lesson, how the runs it was given were received, which is the evidence a
  retirement must cite
- for test design, how many tests were offered and how many were pushed, as is or
  edited
- which channels the explicit feedback came from

