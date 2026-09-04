# test-healing-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Works out **why** a test could not find an element, and acts only where that answer
permits. Where the cause is a stale locator it fixes it via Claude, verifies the fix by
running the test, and ships a GitHub PR. Where it is not, it stops and says what the
cause actually was.

That distinction is the whole point. An element is missing identically however it went
missing — the page was still loading, a request failed, an earlier click silently did
nothing, an overlay covered it, the wait was two seconds short, the session expired and
left the flow on a page it was never meant to reach. The agent used to hold one
hypothesis for all of them, and the verification loop could not catch the mistake:
the easiest way to make a page assertion pass is to weaken it, so a fix built on a wrong
diagnosis goes green and ships a permanently broken test.

## Diagnosis

`shared/diagnosis.py` combines evidence that already existed and was never read — the
DOM captured at failure, the page object's own locator coverage, the network log inside
the Playwright trace, the step timeline, the framework's structured failure context, and
a baseline of what the page looked like when the test last passed.

| Verdict | Meaning | What the agent may do |
|---|---|---|
| `LOCATOR_STALE` | Right page, element renamed or moved | **edit the selector** |
| `AMBIGUOUS_LOCATOR` | The selector matches several elements, so Playwright refuses to act | **narrow the selector to the one element meant** |
| `WRONG_PAGE` | None of the page object's own locators are here | stop |
| `PRIOR_STEP_FAILED` | An earlier interaction happened and the page never moved | stop |
| `NOT_READY` | Page still rendering when the wait expired | stop — suggests a readiness wait |
| `TOO_SLOW` | Element arrived after the budget ran out | stop — suggests raising `ObjectWaitTime` deliberately |
| `BLOCKED` | Present, and still hidden after the wait spent its budget | stop — suggests revealing it rather than reselecting |
| `ERROR_STATE` / `ENV_UNREACHABLE` | The application or its host failed | stop |
| `DATA_PRECONDITION` | A fixture the test loads is stale or missing | stop |
| `ELEMENT_GONE` | Right page, and absent on the last passing run too | stop |
| `FLAKY_TRANSIENT` | Nothing structural, and it has recovered before unaided | stop |
| `INSUFFICIENT_EVIDENCE` | Cannot tell | fall through to the pre-existing behaviour |

Timing and obstruction were briefly in the fixable column. They came out on a
constraint that only appeared during implementation: `WaitHelper.getTimeout` reads
the global `ObjectWaitTime`, so there is no way to give one element more time
without slowing every test — and a change like that hides the thing most worth
knowing, which is that the page got slower. They now report precisely and stop.

Two rules keep it honest. **Unevaluable is not absent** — a selector that could not be
tested, a trace that could not be read and an artefact that was never referenced all
contribute nothing, rather than contributing zero. And **abstaining is a valid answer**:
a weak signal must never block a genuine fix.

**Gating differs by path, deliberately.** Standalone runs gate at MEDIUM because a
probe stands behind the verdict. Pipeline runs never probe, so they gate only at
HIGH, where several independent channels agreed. The property that holds in both:
nothing blocks work unless it was measured or corroborated.

Verdicts below HIGH confidence are **measured, not assumed**. One targeted re-run either
confirms or refutes them (`lib/probes.py`), and for `TOO_SLOW` / `NOT_READY` that probe
is the experiment itself — a run with a larger budget that passes has proved the fix
before a line is edited. A refuted verdict falls back to abstention rather than flipping
to its opposite: a probe that disagrees says the reasoning was wrong, not what is right.

Runs independently — no DB access required. All context comes from the handoff file.

One session = one handoff file = one PR (or Slack escalation if fixes fail).

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_fix.py    [Python + Claude]   Read handoff → inspect live DOM → fix locators
  │                                   → run tests → commit
  └─ 02_ship.py   [Python only]       Push branch → create PR → Slack notify
```

**How a locator actually gets fixed.** A locator breaks because the DOM changed,
so the correct new value exists nowhere in the source — inferring it from stale
code is guessing. Step 01 grounds the fix in the real DOM, in three tiers:

| Tier | Source | Mid-flow? | Can count selector matches? |
|---|---|---|---|
| 1 | **A browser parked on the failing page** (repair mode, attached over CDP) | Yes | **Yes** — live |
| 2 | **The DOM captured when the test failed** (`dom_snapshot` in the handoff) | Yes | No — static |
| 3 | Re-opening the page in a browser (Playwright MCP) | Only if URL-addressable | Yes |
| 4 | Inference from source, clearly labelled as such in the prompt | n/a | No |

Independently of all four, when the handoff carries a **Playwright trace**
(`trace_path`) the prompt also gets the runtime action timeline: every selector
the test really used and exactly which one failed. That comes from the recorded
run, so it is not subject to the source being out of date.

**Tier 1 is automatic, and only on retry.** The first attempt works from the
failure-time DOM snapshot, which handles most broken locators and stays fast and
headless. If that attempt produces no working fix, the agent re-runs the test
with the browser parked and attaches to it — the one thing a snapshot cannot do
is count how many elements a candidate selector matches, or try the corrected
locator for real before the code is edited.

Mechanically: the framework launches Chromium as a **detached OS process** and
attaches with `connectOverCDP`. That detail is load-bearing — Playwright kills
every browser it launched when the JVM exits, so simply adding
`--remote-debugging-port` to a normal launch left the "parked" browser dead by
the time anything tried to attach. Because the browser now genuinely outlives the
run, the healing agent **terminates it** (by the pid in
`test-output/.repair-session.json`) once it has finished inspecting, and clears
the session file — including when the inspection fails.

It is skipped automatically when: the run came from a triaging handoff rather
than a named test, `CI` is set, there is no display, the port is already busy, or
`REPAIR=false`. `REPAIR=true` forces it from the first attempt.

Tier 2 is the normal unattended path and needs no browser, no credentials and no replay: the
automation framework writes the page's HTML next to the failure screenshot
(`BrowserHelper.captureDomSnapshot`, called from `TestListener.onTestFailure`),
test-triaging-agent copies it into its audit session and references it in the
handoff, and `shared/dom_snapshot.py` distils it down to the elements worth
showing — ranked so the most likely replacement appears first, each with a
suggested selector.

Tier 3 only runs when no snapshot was shipped. It cannot reproduce state reached
partway through a journey (a modal that was never opened, a record that was never
created), so the browser prompt requires it to emit `UNREACHABLE_STATE` rather
than pick a plausible-looking element. Login is handled by reusing a session the
framework already saved (`src/test/resources/{module}/loginStorage/*.json`, passed
as `--storage-state`), so no credential enters the prompt; failing that,
credentials and the module entry URL come from
`parameters/{environment}-{country}.properties` — the same file the tests
themselves read via `config.getRunTimeProperty()`.

Claude returns minimal search/replace `edits`, never a whole file: it is only ever
shown an excerpt of a large page object, so a regenerated file would silently drop
what it never saw. A safety guard rejects any fix that empties a file, removes a
method, or changes more lines than a locator change plausibly needs.

---

## Two ways in

**Pipeline mode** — `test-triaging-agent` writes a handoff into `queue/`, the agent
drains it. This is the nightly path.

**Standalone mode** — you already know which test is broken, so name it:

```bash
make run AGENT=test-healing-agent TEST=LoginTest#testLogin
make run AGENT=test-healing-agent TEST=automation.saucedemo.SauceDemoWebTest   # whole class
./scripts/run-fix-test.sh LoginTest#testLogin
```

Step `00_reproduce.py` runs the test locally with `-DtraceMode=on`, reproduces the
failure, and writes the same handoff file triaging would have. Everything after
that is identical — the fix step cannot tell where the work came from.

Accepted name forms: `Class#method`, `Class.method`, `pkg.Class.method`, or a bare
`Class` (every failing test in it, which clustering then collapses to one fix per
locator).

Three outcomes, all exit 0:

| Outcome | What happens |
|---|---|
| Test passes | "Nothing to fix" — no model call, no PR |
| Locator-shaped failure | Handoff written, normal fix pipeline runs, PR raised |
| Any other failure | Diagnosis written to `00-reproduce.md`, **stops before any model call** |

That last row matters. An assertion mismatch, a dead database or a 401 are not
things a locator edit can fix, and letting the model try produces a confident
wrong answer. The shape is decided from the framework's own wording
(`Element not visible after timeout:`), Playwright and Selenium exception text,
and — strongest of all — whether the trace's failing action carried a selector.
`FORCE=true` overrides.

`REPAIR=true` additionally passes `-DrepairMode=true`, so the framework parks the
browser on the failing page and the fix step attaches to it live.

The synthetic handoff lives in the audit session, not `queue/` — a standalone run
never touches the queue that triaging feeds.

## Driving it from the GUI

`qa_agents_server` (`bash scripts/run-server.sh`, port 8765) exposes this agent at
`/agents/test-healing-agent/*`, alongside the authoring agent. AI-Test-Studio
proxies those paths under `/api/agents/*` and renders the **Auto-Heal Tests**
panel against them.

```
POST /agents/test-healing-agent/run
     {"test": "LoginTest#testLogin", "repair": false, "force": false, "auto_push": true}
     {"build_tag": "ProdSanity-541"}          # pipeline: a handoff already queued
GET  /agents/test-healing-agent/run/<sid>/stream     # SSE: stdout + step events
GET  /agents/test-healing-agent/queue                # handoffs waiting from triaging
GET  /agents/test-healing-agent/sessions             # history
```

The run slot is **global across agents**, not per agent: both drive the same
automation-repo checkout, and repair mode binds a fixed CDP port, so a second
request is queued rather than run concurrently. Steps stream as
Reproduce → Fix → Ship; Reproduce only appears in standalone mode.

## Fixing by defect, not by test

One broken locator fails every test that walks past it. A build with 30 failures
is usually a handful of defects, so the agent works in phases:

1. **Understand everything first.** Context is built for every failing test —
   test file, page objects, element names, trace — with no model calls at all.
2. **Group by what actually broke.** Failures are clustered on
   `(page object file, element)`, so five tests dying on the same `@FindBy` are
   one unit of work. The selector recorded in the trace is the strongest key;
   element names and the triage grouping are fallbacks. The same element name in
   two different page objects is never merged.
3. **`AUTO_FIX_MAX_FIXES_PER_RUN` caps distinct fixes, not tests.** Clusters are
   attempted largest-first, so a capped run unblocks the most tests it can.
   Deferred clusters are reported as such, not silently dropped.
4. **One investigation, one edit, per cluster.** The member with the best
   evidence (a DOM snapshot, then a trace, then matched page objects) is the one
   sent to the model, and it sees every element name its siblings reported.
5. **Every affected test must still prove it.** All members are re-run. A fix is
   credited only for the ones that actually pass; members that still fail keep
   their own failure record so the next attempt re-investigates them separately.
6. **A test that now fails on a *different* element is progress, not failure.**
   The edit is kept, and the next attempt is handed the NEW failure — refreshed
   from the artifacts the verification run just wrote. Only a test that still
   fails on the *same* element condemns the edit, and only then is it reverted.

Rule 6 is what lets one run walk a chain of broken locators. Without it the gate
was whole-test pass/fail: a fix that repaired the login button, got the flow onto
a page it had never reached, and then met a second broken locator scored as a
failure and was reverted — so the next attempt started over on the locator that
was already fixed, and the run could never get past the first one.

Without this, the first test's fix lands and the other four arrive to find the
file already corrected — their edit fails to apply, and they get reported as
"needs manual fix" while their fix is sitting in the same PR.

Repository scans are cached for the run, so 30 failures cost one walk of the
source tree rather than 30.

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Fix** | Read handoff, build context, inspect live DOM, call Claude, apply edits, run test, commit | No DB, no HTML parsing |
| **02 Ship** | Push branch, create PR, Slack notify | No AI, no code changes |

---

## Data Flow

```
agents/test-healing-agent/queue/<build_tag>.json   ← written by test-triaging-agent
    ↓
01-fix.json + .fix-passed   (per-test results, pr_branch)
   + .skip-reason            (infra / no-work — controls whether the handoff is consumed)
    ↓
02-ship.json                (pr_url, slack_notified)
    ↓
queue/processed/<build_tag>.json   ← moved after completion
```

**Retry loop (in run.sh):** If `.fix-passed=false`, re-runs `01_fix.py` up to `MAX_FIX_ATTEMPTS`
(default 4 — each attempt either fixes an element or proves it cannot, so the loop walks a
chain of broken locators rather than re-guessing at one).
A retry re-attempts **only the tests that actually failed** — fixes already applied and
committed by an earlier attempt are carried forward into the report rather than redone.
On retry, `01_fix.py` injects the previous test failure output into the Claude prompt so it
tries a different locator strategy. Where the previous attempt's edit *worked* and merely
uncovered the next broken locator, the retry gets that new failure instead: new selector,
new DOM snapshot, new diagnosis (`next_issue` in `01-fix.json`).

If `01_fix.py` crashes, run.sh's ERR trap posts to `SLACK_ALERT_CHANNEL` and leaves the
handoff queued — a crash is never silent.

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
      "method_name": "...",
      "dom_snapshot": "/abs/path/to/triaging-audit/<session>/dom/<method>.html",
      "failure_url": "https://app.example.com/people/42/details",
      "trace_path": "/abs/path/to/triaging-audit/<session>/traces/<method>.zip",
      "failed_selector": "#blockReasonHeader"
    }
  ]
}
```

Only issues with `AUTOMATION_ISSUE + HIGH + ELEMENT_NOT_FOUND` are included.

`dom_snapshot` and `failure_url` are empty when the framework captured no DOM for
that test (an API test, screenshots disabled, or a framework without the capture
hook). The healing agent then falls back to tier 2.

---

## Fix Gate Values (.fix-passed)

- `true`    — every attempted fix was applied, and passed wherever a runner existed → PR created
- `false`   — one or more fixes failed tests → retry loop, then Slack alert
- `skipped` — no eligible issues or infra not configured → clean exit

A fix applied where **no test runner could be found** is recorded as
`unverified`, never as a pass: it is committed and shipped, but the PR body, the
Slack message and `01-fix.md` all mark it "Applied but NOT Verified". Set
`TEST_RUNNER_CMD` to get real verification on a layout the auto-detection misses.

## Skip Reasons (.skip-reason)

- `infra`   — nothing was attempted (no GitHub token, workspace missing). run.sh
              leaves the handoff in the queue so the work is not lost.
- `no-work` — the handoff held nothing eligible. The handoff is consumed.

---

## Audit Trail

**Session folder:** `agents/test-healing-agent/audit/$SESSION_ID/`

| File | Written by | Purpose |
|---|---|---|
| `00-session-init.md` | run.sh | Session metadata |
| `00-reproduce.json` + `.md` | Reproduce | Standalone only: what was run, the failure shape, why it did or did not proceed |
| `00-handoff.json` | Reproduce | Standalone only: the synthesised handoff |
| `01-fix.json` + `.md` | Fix | Per-test context, diffs, test output |
| `.fix-passed` | Fix | Gate: true / false / skipped |
| `02-ship.json` + `.md` | Ship | PR URL, Slack status |

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `CLAUDE_CLI_PATH` | Path to claude CLI binary (default: claude) |
| `AUTOFIX_MODEL` | Claude model for fix generation (default: `claude-opus-5`) |
| `AUTOFIX_INSPECT_DOM` | Read the failing page in a real browser before fixing (default: true) |
| `AUTOFIX_BASE_URL` | Page URL for DOM inspection, overriding whatever is recovered from the execution log |
| `AUTOFIX_DOM_TIMEOUT_S` | Wall-clock budget for one browser inspection (default: 600) |
| `PLAYWRIGHT_HEADLESS` | Set `false` to watch every browser this agent starts: DOM inspection, the locate replay, session minting, and the reproduce / verification / probe runs (as `-Dheadless`) |
| `AUTOFIX_LOGIN_USERNAME`, `AUTOFIX_LOGIN_PASSWORD` | Credentials override. Normally unnecessary — a saved session or `parameters/*.properties` is used first |
| `AUTOFIX_ENVIRONMENT`, `AUTOFIX_COUNTRY` | Which `parameters/{environment}-{country}.properties` to read (default: `staging` / `SG`) |
| `AUTOFIX_REPAIR_SESSION` | Explicit path to a `.repair-session.json`. Unset → looked for under the workspace's `test-output/` |
| `AUTOFIX_MAX_DIFF_LINES` | Reject a fix whose diff exceeds this many lines (default: 40) |
| `DIAGNOSIS_PROBE` | `false` to skip confirmation probes (default: on). A probe costs one test run and buys a measured verdict instead of an assumed one |
| `BASELINE_DIR` | Where page baselines are read from. Unset → `<workspace>/test-output/baselines`. Point CI at a path that survives between builds, or baselines are discarded with every report directory |
| `DIAGNOSIS_MODE` | `shadow` (default) — diagnose and log, but let the old behaviour decide. `enforce` — a stop verdict skips the work before any model call. Shadow exists so the verdicts can be measured against real outcomes before they refuse work the agent used to do |
| `AUTOFIX_PAGE_OBJECT_CHARS` | Budget per page object shown to Claude (default: 8000). Declarations are always kept in full |
| `PAGE_OBJECT_DIRS` | Comma-separated page-object search dirs. Unset → derived from the repo layout |
| `AUTOFIX_TEST_TIMEOUT_S` | Timeout for one verification test run (default: 300) |
| `WORKSPACE_DIR` | Parent directory for the automation repo. Must be outside QA-Agent-Network. If the repo is not present, test-healing-agent clones it automatically using `GITHUB_TOKEN` + `GITHUB_ORG` + `GITHUB_REPO_AUTOMATION`. |
| `GITHUB_REPO_AUTOMATION` | Name of the automation repo — the dir under `WORKSPACE_DIR` and the repo name on GitHub |
| `FRAMEWORK_DIR` | Absolute path to the checkout, overriding `WORKSPACE_DIR/GITHUB_REPO_AUTOMATION`. Unset → the derived path |
| `GITHUB_TOKEN` | GitHub authentication for PR creation |
| `GITHUB_ORG` | GitHub org owning the automation repo |
| `GITHUB_DEFAULT_BRANCH` | Base branch for PRs (default: main) |
| `AUTOFIX_BRANCH_PREFIX` | Prefix for fix branches (default: `chore/qa-autofix`). Full name: `<prefix>/<build-tag>` |
| `GITHUB_PR_REVIEWERS` | Comma-separated list of PR reviewers |
| `REPO_CONTEXT_FILE` | Path to conventions file in the automation repo (relative to repo root or absolute). If unset or not found, falls back to `agents/test-healing-agent/CONVENTIONS.md` bundled in this agent. |
| `TEST_RUNNER_CMD` | Override test runner — use `{class}`, `{class_simple}`, `{method}` placeholders. Without it, runners are auto-detected at the repo root and one level down; if none is found, fixes are reported `unverified` |
| `AUTO_FIX_MAX_FIXES_PER_RUN` | Max **distinct locator fixes** per session, not tests (default: 5). One fix can green several tests |
| `MAX_FIX_ATTEMPTS` | Max retry cycles if tests fail (default: 2) |
| `AUTO_PUSH` | Set `false` to skip PR creation (dry-run) |
| `SLACK_BOT_TOKEN`, `SLACK_NOTIFY_CHANNEL` | Slack notifications on success |
| `SLACK_ALERT_CHANNEL` | Slack channel for failures/partial fixes |
| `TEST_NAME` (or `TEST=`) | Standalone mode: the test to run and fix. `Class#method`, `Class.method`, `pkg.Class.method`, or a bare `Class` |
| `REPAIR` | Override the automatic behaviour. `true` → park a browser from the first attempt; `false` → never. Unset → automatic, on retry only |
| `FORCE` | `true` → attempt a fix even when the failure does not look locator-shaped. In the GUI this is offered after a run stops, not before it |
| `AUTOFIX_REPRODUCE_TIMEOUT_S` | Budget for the reproduce run (default: 900) |
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

1. **Diagnose before editing.** `shared/diagnosis.py` decides *why* the element was
   missing — a stale locator is one answer among several, and only `LOCATOR_STALE`
   and `AMBIGUOUS_LOCATOR` authorise a selector edit (the two defects that live in
   the selector itself: one no longer matches its element, the other matches more
   than one). Stop verdicts exit without a model call. Abstention
   (`INSUFFICIENT_EVIDENCE`) falls through to the pre-existing behaviour, so a weak
   signal never blocks a genuine fix
2. **Every fix must pass the test before it is committed** — restore original on failure
3. **Write audit entry before any irreversible action** (git commit, push)
4. **Use wrapper methods, not raw Selenium** — CONVENTIONS.md teaches Claude the patterns
   (plus `config/skills/automation-repo.md`, passed as `--system-prompt-file`, and
   `config/prompts/fix.md`, which holds the instructions and output contract)
5. **Exit cleanly** — success or failure. Not a daemon.
6. **Secrets never logged** — env var names are fine, never their values
7. **Tokens never persisted** — clone and push supply the credential per-invocation
   via `shared/git.py`'s `push_url`; it is never written into `.git/config`
8. **Never report an unverified fix as verified** — the three states are
   verified / unverified / failed, and they stay distinct all the way to the PR
