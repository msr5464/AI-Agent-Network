# Troubleshooting

Common errors and how to debug each agent.

---

## Where to look first

Every run writes `agents/<agent>/audit/<session-id>/` (full layout in
[ARCHITECTURE.md](ARCHITECTURE.md#session-and-audit-structure)). In order:

1. `NN-<step>.md` — each step's report, written for a human. Usually enough.
2. `claude-<timestamp>.log` — the exact prompt Claude received and its raw
   response, one file per call. Read these when the model did something odd.
3. `NN-<step>.json` — the step's structured output.
4. Markers: `.fix-passed` (`true` / `false` / `skipped` / `stuck` / `defect`),
   `.verdict`, `.skip-reason`, `.crashed`.
5. `stdout.log` — the whole console, for runs started from the server/Studio.

```bash
make audit AGENT=test-healing-agent                  # list recent sessions
make audit AGENT=test-healing-agent SESSION=<id>     # one session
make dashboard                                       # web UI at http://localhost:8888
```

---

## All agents

### `claude: command not found`

```bash
which claude || npm list -g @anthropic-ai/claude-code
# then in config/.env:
CLAUDE_CLI_PATH=/usr/local/bin/claude
```
If it is installed but calls fail with an auth error, run `claude auth status`
and `claude auth login`.

### `gh: command not found` / PR creation fails

```bash
brew install gh     # macOS; see cli.github.com for others
gh auth status      # gh also accepts GITHUB_TOKEN from config/.env
```
The ship step's `NN-ship.md` carries gh's own error message.

### `GITHUB_REPO_AUTOMATION is not set` / `set FRAMEWORK_DIR, or WORKSPACE_DIR`

The agents cannot find the automation repo. Set `GITHUB_REPO_AUTOMATION` (always
required) and either `WORKSPACE_DIR` (the repo is `$WORKSPACE_DIR/$GITHUB_REPO_AUTOMATION`)
or `FRAMEWORK_DIR` (the checkout itself). If the repo is absent under
`WORKSPACE_DIR`, it is cloned — which also needs `GITHUB_TOKEN` and `GITHUB_ORG`.

### A setting seems to be ignored

`shared/load_env.sh` loads `config/.env`, then the repo-root `.env`, then
`agents/<agent>/.env` — later files win — and anything already exported in your
shell beats all three. A stray `agents/<agent>/.env` (for example a copied
`.env.example` with an empty `GITHUB_TOKEN=`) silently overrides `config/.env`.

Check what an agent will see without printing secrets:
```bash
REPO_ROOT=$PWD AGENT_DIR=$PWD/agents/test-healing-agent bash -c \
  'source shared/load_env.sh; for v in GITHUB_TOKEN GITHUB_ORG GITHUB_REPO_AUTOMATION WORKSPACE_DIR FRAMEWORK_DIR AUTO_PUSH; do
     [ -n "${!v}" ] && echo "$v=set" || echo "$v=unset"; done'
```

### A dry run changed my working tree

Expected. `AUTO_PUSH=false` runs in your local checkout as it stands — it edits
files there and leaves the result on a local branch for you to inspect. Commit or
stash your own work first.

### Slack notification silently skipped

`SLACK_BOT_TOKEN` is empty — Slack is optional. Set it, and invite the bot to each
channel with `/invite @your-bot-name`.

### The framework looks wrong (no locators extracted, traces rejected)

`AUTOMATION_FRAMEWORK` is set and disagrees with the repo — look for the
`[frameworks] WARNING` line in the log. Unset it and let detection read the
repo's build files. The Studio's Agent Settings page writes this key too.

---

## Agent 1 — test-authoring-agent

### `Queue is empty — nothing to create` / `Input file not found`

With `MODULE=payments`, `agents/test-authoring-agent/queue/payments.txt` must
exist; with no `MODULE`, the queue needs at least one `.txt`. A processed input is
in `queue/processed/` — copy it back to re-run it. (Files created from the Studio
live in `queue/<user-id>/`, not the queue root the CLI reads.)

### Generated tests fail in step 04

1. **Iterate with `TESTING_MODE=true`** — steps 01–02 are restored from cache, so
   each run goes straight to Generate.
2. **Raise `AUTHORING_FIX_RETRY_COUNT`** — only helps while attempts are still
   exploring: the loop stops early once an attempt can bring nothing new (no
   edits, the same guard rejects twice, or an edit set repeats), and
   `.fix-passed` then says `stuck`.
3. **Read the attempt:** `04-run-and-fix.md`, then the matching `claude-*.log`.

`.fix-passed` = `defect` means the test reproduces a known product defect named in
the plan — the test is right and the fix loop stops on purpose.

### Selector validation failing in step 02

Step 02 drives a real browser through the Playwright MCP server. Watch it:
```bash
HEADLESS_BROWSER=false ./scripts/run-authoring-agent.sh payments
AUTHORING_BROWSER_TIMEOUT_MS=60000 ./scripts/run-authoring-agent.sh payments   # slow pages
```
`npx` must be on PATH; the MCP version is pinned by `PLAYWRIGHT_MCP_VERSION`.

---

## Agent 2 — test-triaging-agent

### `No test results found in database`

1. The test runner must insert results into MySQL before triaging runs.
2. Check `TRIAGING_DB_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_NAME`.
3. The `buildTag` in MySQL must match the tag you pass.

```bash
python3 -c "import pymysql; pymysql.connect(host='localhost', user='root', password='', database='qa_results'); print('OK')"
```

### `No eligible build tags found after filtering` / `Scout did not select a build tag`

Every recent build is already analysed (listed in `feedback/skip-buildtags.json`)
or older than `TRIAGING_SCOUT_LOOKBACK_DAYS` (default 7). Pass `BUILD_TAG=` to
analyse one anyway, or `make clear-feedback AGENT=test-triaging-agent` to reset.

### Classification confidence is LOW

Read the classifier's `claude-*.log`. Usual causes: a truncated stack trace or
log in the HTML report, or an ambiguous error message. Failures the diagnosis
engine measured with HIGH confidence skip the model entirely.

### Verdict is NEEDS-HUMAN

The reviewer still disagreed after `TRIAGING_MAX_REVIEW_ROUNDS` rounds — the
failure is genuinely ambiguous. Read `04-review-r<N>.md` and
`03-classifier-rebuttal-r<N>.md` in the session folder. No handoff is written for
a NEEDS-HUMAN build.

### No handoff file written for Agent 3

Only locator failures qualify — see the
[handoff criteria](ARCHITECTURE.md#handoff-criteria). `PRODUCT_BUG`, lower
confidence, and non-locator verdicts (`WRONG_PAGE`, `TOO_SLOW`, …) are reported,
not handed off. That is correct behaviour.

---

## Agent 3 — test-healing-agent

### `Queue is empty — nothing to fix`

Nothing in `agents/test-healing-agent/queue/`. Run triaging first — or skip it
and heal one test directly: `./scripts/run-healing-agent.sh --test Class#method`.

### `No handoff file for BUILD_TAG=...`

No `queue/<build_tag>.json`. Either triaging has not run for that build, nothing
met the handoff criteria, or the handoff was already processed (look in
`queue/processed/`).

### Standalone run ends after Reproduce

`00-reproduce.md` says why: the test passed, or it failed for a reason that is not
a broken locator. Re-run with `FORCE=true` to proceed anyway, or `REPAIR=true` to
keep the failing browser open for inspection.

### Run stopped by the diagnosis

With `DIAGNOSIS_MODE=enforce`, a stop verdict (`WRONG_PAGE`, `ENV_UNREACHABLE`,
`TOO_SLOW`, …) ends the run before any model call, with `.skip-reason` =
`diagnosed` (or `infra` for an environment problem, which leaves the handoff
queued). The verdict and its reasons are in `01-fix.md`. `FORCE=true` overrides;
`DIAGNOSIS_MODE=shadow` only logs.

### Fix applied but test still failing

Locate and Fix repeat while each attempt makes progress — one repaired locator
often uncovers the next. The loop stops after `HEALING_RETRY_COUNT` attempts in a
row with *no* progress, or `HEALING_MAX_ATTEMPTS` in total; each retry gets the
previous failure. To debug:
1. `01-locate.md` — what the deterministic engine proposed, or why it refused
   (no baseline, not a locator, ambiguous, already tried, …).
2. `01-fix.md` and the `claude-*.log` for the attempt — what was applied and why.

### PR not created after a successful fix

Check `02-ship.md`. Common causes: `AUTO_PUSH=false` (dry run, by design), push
failed (`GITHUB_TOKEN` needs `repo` scope), or no test actually passed
(`.fix-passed` = `false`).

### Only some tests fixed

Normal: the PR carries the fixes that passed and Slack's alert channel lists the
rest. The handoff moves to `queue/processed/` either way — it is left queued only
after an infra skip or a crash.

---

## Agent 4 — test-adaptation-agent

### `change note not found` / `Queue is empty — nothing to adapt`

As authoring: `MODULE=checkout` needs `agents/test-adaptation-agent/queue/checkout.txt`.
Remember that healing may have left a `<module>-rebuilt.txt` **draft** in this
queue; review it before a queue-mode run picks it up.

### No login session / exploration signed out

Step 03 restores a saved session for the module or mints one the way the test
itself signs in. If both fail, it stops (`.skip-reason` = `no-session`) rather
than explore signed out; a test that never signs in is explored unauthenticated. Try
`python3 scripts/mint_session.py --module <module> --headed` to see the login
happen.

### The run refused, or the PR says NEEDS-REVIEW

NEEDS-REVIEW is always the case — adaptation never self-approves. A refusal names
its reason in the step report: the change note does not account for what the
browser saw, the expected *outcome* changed, a check would be weakened, or the
flow ends in something destructive (set `ADAPTATION_SANDBOX=true` with a
`ADAPTATION_SANDBOX_NOTE` only for a disposable environment).

### Edits were rolled back

Step 04 applies each change item as a transaction and restores every file on any
failure — guard, compile or verification. `04-adapt.md` lists which guard or
check failed. A crash mid-item is rolled back by `restore_snapshots.py`.

---

## Server (qa_agents_server / Studio)

### `403 forbidden`

The session belongs to another user, or the route is admin-only (`/settings`).
If every identity-bearing request is anonymous, `QA_AGENT_PROXY_SECRET` differs
between QA-Agent-Network's and AI-Test-Studio's `config/.env`.

### A run shows `interrupted`

The server restarted mid-run, or the session went `QA_AGENT_STALE_AFTER_SECONDS`
(default 900) without progress. Resume authoring/adaptation from the last good
step; re-run healing.

### `Failed to create isolated git worktree`

Usually a stale worktree or lock on the automation repo. Run
`git worktree prune` in the automation repo, and check that
`QA_WORKTREE_TEMP_DIR` (default `/tmp/qa-runs`) is writable.

### Run stays queued

Either `QA_MAX_CONCURRENT_RUNS` runs are active, or it is a dry run
(`auto_push` off) and another dry run already holds your local checkout. See
`GET /agents/<agent>/run/queue`.
