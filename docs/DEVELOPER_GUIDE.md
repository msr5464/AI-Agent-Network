# Developer Guide

This guide covers local development, testing, and debugging after you have run the initial setup.

---

## Prerequisites Checklist

Before your first run, verify:

```bash
# Python 3.9+
python3 --version

# Claude CLI (must be logged in)
claude --version
claude whoami

# GitHub CLI (for PR creation)
gh auth status

# Node.js (Agent 1 only — Playwright web validation)
node --version
```

If Claude CLI is not installed:
```bash
npm install -g @anthropic-ai/claude-code
claude login
```

---

## Initial Setup

```bash
git clone <repo-url>
cd QA-Agent-Network

./scripts/setup.sh          # macOS / Linux
.\scripts\setup.ps1         # Windows

# Edit config/.env with your credentials (see config/.env.example for all options)
cp config/.env.example config/.env
```

Configure MCP tools (GitHub + Slack — needed for PR creation and notifications):
```bash
make setup-mcp
```

---

## Running Agents

### Agent 1 — Test Authoring

```bash
# Create an input file
cat > agents/test-authoring-agent/queue/payments.txt << 'EOF'
Module: payments
Type: web
URL: https://app.staging.example.com

Steps:
1. Login as Admin user
2. Create a payment of 100 SGD to recipient ABC
3. Verify success message appears
EOF

# Run it
make run AGENT=test-authoring-agent MODULE=payments

# Dry-run (no GitHub PR created)
AUTO_PUSH=false make run AGENT=test-authoring-agent MODULE=payments
```

### Agent 2 — Test Triaging

```bash
# Auto-select most recent unanalysed build from MySQL
make run AGENT=test-triaging-agent

# Analyse a specific build tag
make run AGENT=test-triaging-agent BUILD_TAG=ProdSanity-All-Tests-541

# Stop early (useful for inspecting intermediate outputs)
STOP_AFTER=classify make run AGENT=test-triaging-agent BUILD_TAG=ProdSanity-All-Tests-541
```

`STOP_AFTER` accepts: `scout`, `collect`, `classify`, `review`.

### Agent 3 — Test Healing

```bash
# Process oldest item in queue (populated by Agent 2)
make run AGENT=test-healing-agent

# Fix a specific build tag
make run AGENT=test-healing-agent BUILD_TAG=ProdSanity-All-Tests-541

# Dry-run (fix + test locally, no PR)
AUTO_PUSH=false make run AGENT=test-healing-agent
```

---

## Speeding Up Development Iterations

### TESTING_MODE (Agent 1)

Steps 01 (Parse) and 02 (Validate Web) are slow — 1–2 minutes each. Enable `TESTING_MODE` to cache their outputs and skip them on re-runs:

```bash
# First run — runs all steps and saves cache
TESTING_MODE=true make run AGENT=test-authoring-agent MODULE=payments

# Subsequent runs — skips 01 and 02, goes straight to Generate
TESTING_MODE=true make run AGENT=test-authoring-agent MODULE=payments

# Clear cache for a module
rm -rf agents/test-authoring-agent/cache/payments/
```

Cache is stored at `agents/test-authoring-agent/cache/<module>/`.

### STOP_AFTER (Agent 2)

Run only the steps you're working on:
```bash
# Only collect data, don't classify
STOP_AFTER=collect make run AGENT=test-triaging-agent BUILD_TAG=MyBuild-123
# Now edit 03_classify.py and re-run from scratch with the real data
```

---

## Reading Audit Trails

Every run creates a session folder. This is where to look when something goes wrong.

```bash
# List recent sessions
make audit AGENT=test-triaging-agent

# Inspect a specific session
make audit AGENT=test-triaging-agent SESSION=20260507-143000-ProdSanity-All-Tests-541
```

Or use the web dashboard:
```bash
make dashboard            # opens at http://localhost:8888
make dashboard PORT=9000  # custom port
```

### Session folder layout

```
agents/<agent>/audit/<session-id>/
├── 01-*.json         # Step output (structured data)
├── 02-*.json
├── ...
├── *.md              # Claude prompt + response for each AI call (read these to debug LLM issues)
├── .verdict          # APPROVED or NEEDS-HUMAN
└── .fix-passed       # true / false / skipped
```

To debug a bad Claude response, open the `.md` files — they contain the full prompt that was sent and the raw response received.

---

## Running Unit Tests

```bash
make test               # runs tests/unit/ with pytest -v
make test-cov           # with coverage report for Agent 2 lib
```

Tests cover Agent 2's library (database queries, HTML parser, report generator, classifier, memory).

---

## Adding a New Input File (Agent 1)

Input files live in `agents/test-authoring-agent/queue/`. Claude is flexible about exact formatting — the minimum required fields are:

```
Module: <name>
Type: web | api | both
URL: https://...            # for web tests

Steps:
1. ...
2. ...
```

After a successful run the file is moved to `queue/processed/`. To re-run the same module, copy it back:
```bash
cp agents/test-authoring-agent/queue/processed/payments.txt \
   agents/test-authoring-agent/queue/payments.txt
```

---

## Extending a Shared Helper

Code shared across agents lives in `shared/`. When adding a new helper:

1. Add the Python module to `shared/` (e.g. `shared/testrail.py`)
2. Import it in the agent action that needs it — no registration required
3. Shell helpers (`load_env.sh`, `session.sh`) are sourced by each `run.sh` directly

Agent-specific logic stays inside `agents/<agent-name>/`. Only promote to `shared/` if two or more agents need it.

---

## Skipping Builds or Known Issues

To permanently skip a build tag from Agent 2's scout:
```bash
# Add to skip-buildtags.json
make feedback AGENT=test-triaging-agent
# Edit agents/test-triaging-agent/feedback/skip-buildtags.json directly
```

To mark a test pattern as un-fixable by Agent 3 (won't attempt auto-fix):
```bash
# Edit agents/test-healing-agent/feedback/known-issues.json
```

---

## Environment Variable Quick Reference

The most commonly tweaked variables during development:

| Variable | Purpose | Dev default |
|----------|---------|-------------|
| `AUTO_PUSH` | Skip GitHub PR creation | `false` |
| `TESTING_MODE` | Cache Agent 1 steps 01+02 | `true` |
| `STOP_AFTER` | Stop Agent 2 at a specific step | `collect` or `classify` |
| `MAX_FIX_ATTEMPTS` | Retry budget for Agent 1+3 | `1` (faster feedback) |
| `PLAYWRIGHT_HEADLESS` | Show every browser any agent starts (validation, DOM inspection, exploration, minting, test runs) | `false` |
| `CLAUDE_CLI_PATH` | Full path to claude binary | _(set if not on PATH)_ |

Full variable reference: `config/.env.example`.
