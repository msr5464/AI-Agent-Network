# Troubleshooting

Common errors, where to find logs, and how to debug each agent.

---

## Where to Look First

Every run writes a timestamped audit folder:

```
agents/<agent-name>/audit/<session-id>/
```

The most useful files:
- `*.md` — full Claude prompt + raw response for every AI call
- `0N-<step>.json` — structured output of each step
- `.verdict` — `APPROVED` or `NEEDS-HUMAN` (Agent 2)
- `.fix-passed` — `true` / `false` / `skipped` (Agent 1 + 3)

```bash
# List recent sessions
make audit AGENT=test-triaging-agent

# Inspect a session
make audit AGENT=test-triaging-agent SESSION=20260507-143000-ProdSanity-541

# Browse all agents in a web UI
make dashboard
```

---

## Common Errors — All Agents

### `claude: command not found`

The Claude CLI is not on your PATH.

```bash
# Find where it's installed
which claude || npm list -g @anthropic-ai/claude-code

# Set in config/.env
CLAUDE_CLI_PATH=/usr/local/bin/claude
```

### `gh: command not found` / PR creation fails

GitHub CLI is not installed or not authenticated.

```bash
# Install
brew install gh          # macOS
gh auth login
gh auth status
```

### Slack notification silently skipped

`SLACK_BOT_TOKEN` is empty — this is intentional (Slack is optional). Set it in `config/.env` and ensure the bot is invited to each channel with `/invite @your-bot-name`.

### Config not loading

Check the load order — `shared/load_env.sh` loads `config/.env` first, then `agents/<agent>/.env`. Values in the agent-level file override the shared one.

```bash
# Verify a variable is set correctly
source shared/load_env.sh && echo $GITHUB_TOKEN
```

---

## Agent 1 — test-authoring-agent

### `Queue is empty — nothing to create`

No `.txt` files in `agents/test-authoring-agent/queue/`. Create one:

```bash
cat > agents/test-authoring-agent/queue/payments.txt << 'EOF'
Module: payments
Type: web
URL: https://app.staging.example.com

Steps:
1. Login as Admin user
2. Verify dashboard loads
EOF
```

### `Input file not found`

When using `MODULE=payments`, the file `queue/payments.txt` must exist.

```bash
ls agents/test-authoring-agent/queue/
```

### Automation repo not cloned / `WORKSPACE_DIR not set`

Agent 1 needs the Jarvis automation repo. If it's missing, it will be cloned automatically — but only if `GITHUB_TOKEN`, `GITHUB_ORG`, and `GITHUB_REPO_AUTOMATION` are all set in `config/.env`.

```bash
# Verify
grep "GITHUB_TOKEN\|GITHUB_ORG\|GITHUB_REPO_AUTOMATION\|WORKSPACE_DIR" config/.env
```

### Generated tests fail in step 04

Claude wrote tests that don't compile or fail at runtime. Options:

1. **Use TESTING_MODE** to iterate on generation without re-running the slow Parse and Validate steps:
   ```bash
   TESTING_MODE=true make run AGENT=test-authoring-agent MODULE=payments
   ```

2. **Increase `AUTHORING_FIX_RETRY_COUNT`** — the agent will retry the fix loop more times.
   Note the loop stops early regardless once an attempt can bring nothing new (the model
   returns no edits, the same guard rejects twice running, or an edit set repeats), so
   raising this only helps when attempts are genuinely still exploring:
   ```bash
   AUTHORING_FIX_RETRY_COUNT=5 make run AGENT=test-authoring-agent MODULE=payments
   ```

3. **Read the Claude prompt** — open `agents/test-authoring-agent/audit/<session>/04-run-and-fix.md` to see exactly what Claude was asked and what it responded.

### Playwright selector validation failing in step 02

Agent 1 launches a headless browser to validate selectors. If selectors aren't found:

```bash
# Run in headed (visible) mode to watch what happens
PLAYWRIGHT_HEADLESS=false make run AGENT=test-authoring-agent MODULE=payments
```

Increase timeout if the page is slow:
```bash
AUTHORING_PLAYWRIGHT_TIMEOUT_MS=60000 make run AGENT=test-authoring-agent MODULE=payments
```

---

## Agent 2 — test-triaging-agent

### `No test results found in database`

Agent 2 reads from MySQL. Ensure:
1. The test runner inserts results before Agent 2 runs
2. DB credentials in `config/.env` are correct (`TRIAGING_DB_HOST`, `TRIAGING_DB_USER`, `TRIAGING_DB_PASSWORD`, `TRIAGING_DB_NAME`)
3. The `buildTag` in MySQL matches the tag you're passing

```bash
# Test the connection
python3 -c "import mysql.connector; c = mysql.connector.connect(host='localhost', user='root', password='', database='qa_results'); print('OK')"
```

### `Queue is empty — no unanalysed builds found`

All builds in the DB have already been analysed. Either:
- Pass a specific build tag: `make run AGENT=test-triaging-agent BUILD_TAG=MyBuild-123`
- Check if `TRIAGING_SCOUT_LOOKBACK_DAYS` is too short (default: 7 days)

### Classification confidence is LOW

Claude isn't confident about a failure. Check the classifier prompt in the audit `.md` files. Common causes:
- Truncated stack trace or log — ensure the full error is in the HTML report
- Ambiguous test name or error message

### Verdict is NEEDS-HUMAN

The reviewer disagreed with the classifier after `TRIAGING_MAX_REVIEW_ROUNDS` rounds. This is intentional — it means the failure is genuinely ambiguous. Check:
```bash
make audit AGENT=test-triaging-agent SESSION=<id>
# Then open the .verdict file and 04-review.json in the session folder
```

### No handoff file written for Agent 3

Agent 3's queue is only populated when **all three** hold:
- `classification = AUTOMATION_ISSUE`
- `confidence = HIGH`
- `root_cause_category = ELEMENT_NOT_FOUND`

If failures were `PRODUCT_BUG` or lower confidence, no handoff is written — this is correct behaviour.

### Build tag skipped silently

Check `agents/test-triaging-agent/feedback/skip-buildtags.json`:
```bash
make feedback AGENT=test-triaging-agent
```

---

## Agent 3 — test-healing-agent

### `Queue is empty — nothing to fix`

Run Agent 2 first. Agent 3's queue is only populated by Agent 2 when it finds eligible failures.

```bash
ls agents/test-healing-agent/queue/
```

### `No handoff file for BUILD_TAG=...`

The `.json` file for this build tag doesn't exist in the queue. Either Agent 2 hasn't run for this build, or the failures didn't meet the handoff criteria (see Agent 2 section above).

### Fix applied but test still failing

Claude generated a locator fix but the test still fails after applying it. The agent will retry up to `HEALING_RETRY_COUNT` times. On each retry, it injects the previous failure output into the prompt so Claude can try a different strategy.

To debug manually:
1. Open `agents/test-healing-agent/audit/<session>/01-fix.md` — read the Claude prompt and response
2. Check which locator was suggested and whether it matches the actual DOM

### PR not created after a successful fix

Check `agents/test-healing-agent/audit/<session>/02-ship.json`. Common causes:
- `AUTO_PUSH=false` — dry-run mode, intentionally no PR
- Push failed — check `GITHUB_TOKEN` has `repo` scope
- No tests actually passed — `.fix-passed` will be `false`

```bash
make audit AGENT=test-healing-agent SESSION=<id>
```

### Only some tests fixed

This is normal — Agent 3 ships a PR for the tests that passed and escalates the rest to `#qa-critical` on Slack. Tests that couldn't be fixed remain in the queue as unprocessed (the handoff file is **not** moved to `processed/` if any tests still fail, depending on configuration).

---

## Checking What Was Actually Run

To see the exact commands Claude received and what it responded:

```bash
# List all .md files in a session (one per AI call)
ls agents/test-triaging-agent/audit/<session>/

# Read a specific call
cat agents/test-triaging-agent/audit/<session>/03-classify.md
```

The `.md` files are your ground truth for understanding why an AI call produced a given result.
