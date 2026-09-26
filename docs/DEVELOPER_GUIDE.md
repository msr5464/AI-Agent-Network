# Developer Guide

Local development, testing and debugging. For first-time install and the
minimum configuration, follow the [README Quick Start](../README.md#quick-start)
first; for how the pieces fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Prerequisites check

```bash
python3 --version                    # 3.10+
claude --version && claude auth status   # Claude Code CLI, signed in
gh --version                         # GitHub CLI (PRs; it reads GITHUB_TOKEN)
node --version && npx --version      # Playwright MCP (authoring, healing, adaptation)
java -version && mvn -version        # JDK 21 + Maven, for the automation repo
python3 -c "import playwright"       # Python Playwright; also run: python -m playwright install chromium
```

Claude CLI missing? `npm install -g @anthropic-ai/claude-code`, then `claude auth login`.
(`scripts/setup.sh` tries the npm install for you.)

`make setup-mcp` is **optional** — PRs use the `gh` CLI and Slack uses the Bot
API directly. It merges GitHub/Slack MCP servers (for connectors whose token is
set) into the `mcpServers` block of `~/.claude.json`, keeping servers you added
yourself, and saves a backup to `~/.claude.json.bak` first.

---

## Running agents

`make help` lists everything. Queue files for authoring and adaptation live in
`agents/<agent>/queue/` — a git-ignored live inbox; each run moves its input to
`queue/processed/`. Worked examples are in [`docs/examples/queue/`](examples/queue/README.md).

### Agent 1 — Test Authoring

```bash
cat > agents/test-authoring-agent/queue/payments.txt << 'EOF'
Module: payments
Type: web
URL: https://app.staging.example.com

Steps:
1. Login as Admin user
2. Create a payment of 100 SGD to recipient ABC
3. Verify success message appears
EOF

./scripts/run-authoring-agent.sh payments
AUTO_PUSH=false ./scripts/run-authoring-agent.sh payments   # dry run: your checkout, no PR
```

Minimum input: `Module:` (decides the Java package; matched literally),
`Type: web | api | both`, `URL:` for web tests, and numbered `Steps:`. The
declared `Type:` is a hint — step 01 resolves the real type from the steps.

To re-run a processed input, copy it back:
```bash
cp agents/test-authoring-agent/queue/processed/payments.txt agents/test-authoring-agent/queue/
```

### Agent 2 — Test Triaging

```bash
./scripts/run-triaging-agent.sh                                        # scout: newest unanalysed build
./scripts/run-triaging-agent.sh ProdSanity-All-Tests-541
STOP_AFTER=classify ./scripts/run-triaging-agent.sh <build_tag>        # scout | collect | classify | review
```

Triaging records every analysed build in `feedback/skip-buildtags.json`, so a
second run on the same tag needs the tag passed explicitly (direct mode). `make clear-feedback
AGENT=test-triaging-agent` makes every build eligible again.

### Agent 3 — Test Healing

```bash
./scripts/run-healing-agent.sh                               # oldest handoff in queue/
./scripts/run-healing-agent.sh ProdSanity-All-Tests-541
./scripts/run-healing-agent.sh /tmp/handoff.json             # the file is moved to processed/ afterwards

./scripts/run-healing-agent.sh --test LoginTest#testLogin    # standalone: reproduce, then fix
REPAIR=true ./scripts/run-healing-agent.sh --test ...        # park the failing browser (CDP) for live repair
FORCE=true  ./scripts/run-healing-agent.sh --test ...        # proceed on a non-locator failure / stop verdict
```

Useful while developing healing:
- `DIAGNOSIS_MODE=shadow` logs what the diagnosis *would* have stopped without
  stopping; `scripts/diagnosis_soak.py` measures shadow verdicts against outcomes.
- `HEALING_LOCATE_MODE=shadow` keeps Locate's proposal in `01-locate.md` without
  applying it.

### Agent 4 — Test Adaptation

```bash
./scripts/run-adaptation-agent.sh checkout
EXPLORE_ONLY=true      ./scripts/run-adaptation-agent.sh checkout   # stop after the flow map
ADAPTATION_APPLY=true  ./scripts/run-adaptation-agent.sh checkout   # apply and verify (default: propose only)
```

Exploration needs a login session for the app under test; step 03 restores a
saved one or mints one from the framework's own properties
(`scripts/mint_session.py` does the same by hand).

### Resuming a session

Authoring and adaptation can restart an existing session from step 2–5,
reusing the earlier steps' output:

```bash
START_FROM_STEP=4 SESSION_ID=20260924-101500-create-payments ./scripts/run-authoring-agent.sh
```

Healing has no resume; it retries internally.

---

## Speeding up iterations

### TESTING_MODE (authoring, adaptation)

Caches the slow early steps — authoring 01–02, adaptation 01–03 — and restores
them on the next run with the same input. Editing the input file invalidates the
cache.

```bash
TESTING_MODE=true ./scripts/run-authoring-agent.sh payments   # first run fills the cache
TESTING_MODE=true ./scripts/run-authoring-agent.sh payments   # later runs skip to 03
rm -rf agents/test-authoring-agent/cache/cli/payments/        # clear it
```

The cache is per user: `agents/<agent>/cache/<user-id>/<module>/`, where CLI
runs use `cli`.

### STOP_AFTER (triaging)

```bash
STOP_AFTER=collect ./scripts/run-triaging-agent.sh MyBuild-123
# edit 03_classify.py, then re-run with the real data
```

### Common dev settings

| Variable | Purpose | Dev value |
|----------|---------|-----------|
| `AUTO_PUSH` | `false`: run in your checkout, no push, no PR | `false` |
| `TESTING_MODE` | Cache early steps (authoring, adaptation) | `true` |
| `STOP_AFTER` | Stop triaging after a step | `collect` / `classify` |
| `AUTHORING_FIX_RETRY_COUNT`, `HEALING_RETRY_COUNT`, `ADAPTATION_RETRY_COUNT` | Retry budgets | `1` for faster feedback |
| `HEADLESS_BROWSER` | `false` shows every browser any agent starts, including Maven test runs | `false` |
| `CLAUDE_CLI_PATH` | Full path to `claude` | set if not on PATH |

Everything else: [`config/.env.example`](../config/.env.example).

---

## Reading audit trails

Every run writes `agents/<agent>/audit/<session-id>/`. The layout, marker files
and session-ID patterns are in [ARCHITECTURE.md](ARCHITECTURE.md#session-and-audit-structure).
The short version for debugging:

- `NN-<step>.md` — each step's human-readable report. Start here.
- `claude-<timestamp>.log` — the full prompt and raw response of one Claude call.
  Open these to debug model behaviour.
- `.fix-passed`, `.verdict`, `.skip-reason` — how the run ended.

```bash
make audit AGENT=test-healing-agent                 # list recent sessions
make audit AGENT=test-healing-agent SESSION=<id>    # one session
make dashboard                                      # web UI, http://localhost:8888 (PORT=9000 to change)
```

---

## Tests

```bash
make test         # every unit test in tests/unit/ (all agents, shared/, the server)
make test-cov     # same run, coverage for the triaging lib only
python -m pytest locator-eval/     # locator engine tests — not part of make test
```

`tests/conftest.py` pins the framework plugin, so results do not depend on your
`AUTOMATION_FRAMEWORK`. `tests/unit/test_prompt_files.py` fails if a file in
`config/prompts/` has no loader.

---

## Working on the server

```bash
bash scripts/run-server.sh     # http://127.0.0.1:6001, loads config/.env
curl -s localhost:6001/health
```

Without AI-Test-Studio in front, requests carry no identity headers, so you act
as the anonymous user `default` (queue root, non-admin). See
[SERVER_API.md](SERVER_API.md) for headers and endpoints. Server-started runs
stream their console to `audit/<session>/stdout.log`.

---

## Scripts

| Script | Use |
|--------|-----|
| `scripts/run-{authoring,triaging,healing,adaptation}-agent.sh` | One per agent: live console in the terminal, saved to `<session>/stdout.log` afterwards so History and `make dashboard` replay CLI runs too. Usage in each file's header. Windows: `run-triaging-agent.ps1`, `run-healing-agent.ps1` |
| `scripts/run-server.sh` | Start `qa_agents_server` |
| `scripts/audit_viewer.py` | The `make dashboard` UI |
| `scripts/blast_radius.py` | Which tests a change reaches (`--affects <glob>`, `--test <Class#method>`, `--module <name>`) and the cost to verify them — adaptation step 02 on its own |
| `scripts/mint_session.py` | Mint a login session for exploration |
| `scripts/commit_baselines.py` | Commit refreshed page baselines after a green run (CI) |
| `scripts/diagnosis_soak.py` | Score shadow-mode diagnosis verdicts before switching to `enforce` |
| `scripts/setup.sh` / `.ps1`, `setup-mcp.sh` | Install; optional MCP registration |

Each script's docstring or header has its options.

---

## Extending

- **Shared helpers** live in `shared/`; import them from an action, no
  registration needed. Promote code there only when two or more agents need it.
- **Prompts** live in `config/prompts/` and must each have a loader
  ([README](../config/prompts/README.md)).
- **A new framework** — see [FRAMEWORK_INTEGRATION.md](FRAMEWORK_INTEGRATION.md).
- **A new server agent** — see [SERVER_API.md](SERVER_API.md#adding-an-agent).

## Feedback files

- `agents/test-triaging-agent/feedback/skip-buildtags.json` — builds scout skips
  (written automatically; add known-bad builds by hand). `make feedback
  AGENT=test-triaging-agent` prints it.
- `agents/test-healing-agent/feedback/known-issues.json` — test patterns healing
  must never auto-fix.
