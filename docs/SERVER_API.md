# HTTP Server API

`qa_agents_server` is a thin Flask + SSE server that exposes Agent 1 (test-authoring-agent) over HTTP. It is used by the AI-Test-Studio "QA Agents" tab to trigger authoring runs and stream live progress. CLI users do not need it.

---

## Starting the Server

```bash
bash scripts/run-server.sh
# Listens on http://0.0.0.0:8765 by default
```

Environment overrides:

| Variable | Default | Purpose |
|----------|---------|---------|
| `QA_AGENT_SERVER_HOST` | `0.0.0.0` | Bind host |
| `QA_AGENT_SERVER_PORT` | `8765` | Bind port |
| `AI_TEST_STUDIO_URL` | `http://localhost:5001` | CORS allowlist |
| `QA_AGENT_RUN_TIMEOUT_SECONDS` | `1800` | Kill a run after this many seconds |

The server does not implement authentication — it is expected to run on localhost and be proxied by AI-Test-Studio's backend (which enforces auth).

---

## Base URL

```
http://localhost:8765
```

All agent routes are scoped under `/agents/<agent>/`, where `<agent>` is one of:

| agent | queue | resumable | test catalogue |
|---|---|---|---|
| `test-authoring-agent` | writable `.txt` (feature specs) | yes | no |
| `test-healing-agent` | read-only `.json` (handoffs from triaging) | no | yes |
| `test-adaptation-agent` | writable `.txt` (change notes) | yes | yes |

The examples below use `test-authoring-agent`; substitute any agent from the table.
Capabilities come from that agent's `AgentSpec` in `qa_agents_server/agents.py`, so
`POST /agents/test-healing-agent/queue` returns 405 (its queue is written by another
agent) and `GET /agents/test-authoring-agent/tests` returns 404 (it has no catalogue).

---

## Endpoints

### Health Check

```
GET /health
```

Returns server status and the currently active run (if any).

**Response:**
```json
{
  "status": "ok",
  "active_run": null
}
```

---

### Queue — List Feature Files

```
GET /agents/test-authoring-agent/queue
```

Lists all `.txt` files currently in the agent's input queue.

**Response:**
```json
{
  "items": [
    { "name": "payments", "content": "Module: payments\n..." }
  ]
}
```

---

### Queue — Create / Update Feature File

```
POST /agents/test-authoring-agent/queue
```

Creates or overwrites a feature file in the queue.

**Body:**
```json
{
  "name": "payments",
  "content": "Module: payments\nType: web\n\nSteps:\n1. Login as Admin user\n..."
}
```

**Response (201):**
```json
{
  "name": "payments",
  "path": "agents/test-authoring-agent/queue/payments.txt"
}
```

---

### Queue — Read Feature File

```
GET /agents/test-authoring-agent/queue/<name>
```

**Response:**
```json
{
  "name": "payments",
  "content": "Module: payments\n..."
}
```

---

### Config — Effective Defaults

```
GET /agents/<agent>/config
```

What `config/.env` actually says, so a checkbox can render its real default. This
matters more than it looks: the value a checkbox sends is exported into the run and
beats `config/.env`, so a box that always renders unticked silently overrides an
admin's setting.

**Response:**
```json
{
  "agent": "test-adaptation-agent",
  "auto_push_default": true,
  "adapt_apply_default": false
}
```

---

### Test Catalogue

```
GET /agents/<agent>/tests
```

Every test class in the automation repo with its `@Test` metadata, for the UI's
Module → Class → Test picker. Available only where the agent's `AgentSpec` sets
`uses_test_catalog` (healing and adaptation); 404 otherwise. Returns 503 with a
detail message when `WORKSPACE_DIR` / `GITHUB_REPO_AUTOMATION` do not resolve to a
directory — "no tests found" and "the repo is not where I was told" are different
answers.

**Response:** `{workspace, classes[], modules[], total_classes, total_tests}`, where
each class is `{module, package, name, qualified_name, path, methods[], web_count}`
and each method is `{name, description, groups, enabled, is_web, data_provider}`.

---

### Test Catalogue — What One Test Proves

```
GET /agents/<agent>/tests/intent?test=<pkg.Class%23method>
```

One test's intent contract, derived from source alone — no model call, no run. The
adaptation UI shows it beside the "what changed" box so a human describes a change
against what the test actually does today.

`test` **must name a single method**; a bare class is a 400. (`intent.derive` splits
on the last dot, so `automation.saucedemo.SauceDemoWebTest` would be read as class
`saucedemo`, method `SauceDemoWebTest` and return an empty contract — a wrong answer
that looks like a real one.) Same 404 / 503 gates as `GET /tests`.

**Response:**
```json
{
  "test": "automation.saucedemo.SauceDemoWebTest#addProductToCart",
  "source": "derived",
  "proves": [
    "Login to SauceDemo and add Sauce Labs Backpack to cart",
    "Verify cart badge shows 1 item"
  ],
  "verifies": ["Cart badge should show 1 after adding a product"],
  "identity": [],
  "unresolved_count": 5
}
```

- `proves` — the `logStep(...)` narration reachable from the test method, in call
  order. **An empty list is a legitimate answer**: a test with no `logStep` calls has
  no narration to read, and the UI says so rather than showing an empty box.
- `verifies` — the message argument of each reachable assertion (`AssertHelper` puts
  it last in every signature), de-duplicated.
- `source` — `authored` if a contract file exists under
  `src/test/resources/intents/`, else `derived`.
- `unresolved_count` — calls the analyser could not follow. Non-zero means the two
  lists above may be incomplete, and the UI says so.

The underlying member index is cached against the newest test-source mtime — ~0.2 s
to build, ~1 ms warm, and an edited test invalidates it without a server restart.

---

### Run — Start a Run

```
POST /agents/test-authoring-agent/run
```

Triggers an agent run. Only one run is active at a time; additional requests are queued.

**Body:**
```json
{
  "module": "payments",
  "auto_push": false
}
```

- `module` — which queue file to process (omit to use queue mode: oldest file)
- `auto_push` — `false` for dry-run (no GitHub PR created)

**Response (201 — run started immediately):**
```json
{
  "queued": false,
  "session_id": "20260507-102341-payments",
  "module": "payments",
  "auto_push": false,
  "status": "running",
  "started_at": "2026-05-07T10:23:41Z"
}
```

**Response (202 — queued behind an active run):**
```json
{
  "queued": true,
  "position": 1,
  "module": "payments",
  "session_id": "20260507-102341-payments"
}
```

---

### Run — Active Run

```
GET /agents/test-authoring-agent/run/active
```

Returns the currently running session (for UI re-attach after page reload).

**Response:**
```json
{
  "session_id": "20260507-102341-payments",
  "module": "payments",
  "status": "running",
  "started_at": "2026-05-07T10:23:41Z"
}
```

Returns `{ "active_run": null }` if nothing is running.

---

### Run — Pending Queue

```
GET /agents/test-authoring-agent/run/queue
```

Returns runs waiting to start (behind the active run).

**Response:**
```json
{
  "queue": [
    { "session_id": "...", "module": "checkout", "position": 1 }
  ]
}
```

---

### Run — Stream Progress (SSE)

```
GET /agents/test-authoring-agent/run/<session_id>/stream?offset=0
```

Server-Sent Events stream. Returns live output while the run is active; replays from `offset` if called after the run completes (for history replay).

**Event format:**

Each SSE event is a JSON envelope with a `seq`, a `kind`, and a `data` payload:

```
data: {"seq": 0, "kind": "stdout", "data": {"line": "[10:23:49] ▶ [01/05] Parse"}}

data: {"seq": 7, "kind": "step",   "data": {"key": "parse", "display": "Parse",
        "status": "done", "duration_s": 49.2, "cost_usd": 0.1132,
        "input_tokens": 31, "output_tokens": 2140, "llm_calls": 1,
        "num_turns": 6, "tool_duration_s": 0, "attempts": 1}}

data: {"seq": 42, "kind": "done",  "data": {"status": "completed", "exit_code": 0,
        "verdict": "APPROVED", "pr_url": "https://…", "duration": 612.4,
        "metrics": {"cost_usd": 1.94, "llm_calls": 6, "num_turns": 71,
                    "input_tokens": 130, "output_tokens": 21610,
                    "duration_s": 612.4, "by_model": {...}, "stages": [...]}}}
```

- `stdout` — one event per log line emitted by `run.sh`
- `step` — a step changed state. On `done`/`failed` the payload also carries that
  stage's time and spend (fields above; absent for sessions that predate metrics
  capture).
- `done` — terminal event, with run-level totals under `metrics`.
- Use `?offset=N` to skip events already received (for reconnect)

The replayed stream (`audit_reader.replay_events`) emits the **same** fields as the
live stream, so a run reads identically before and after a page reload.

**Example (curl):**
```bash
curl -N "http://localhost:8765/agents/test-authoring-agent/run/20260507-102341-payments/stream"
```

**Example (JavaScript):**
```javascript
const es = new EventSource(
  `/agents/test-authoring-agent/run/${sessionId}/stream?offset=0`
);
es.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.done) { es.close(); return; }
  console.log(event.line);
};
```

---

### Run — Cancel

```
POST /agents/test-authoring-agent/run/<session_id>/cancel
```

Sends SIGTERM to the running process.

**Response:**
```json
{ "cancelled": true }
```

---

### Sessions — List

```
GET /agents/test-authoring-agent/sessions
```

Returns past sessions (completed, failed, or cancelled) in reverse chronological order.

**Response:**
```json
{
  "sessions": [
    {
      "session_id": "20260507-102341-payments",
      "module": "payments",
      "status": "success",
      "started_at": "2026-05-07T10:23:41Z",
      "ended_at": "2026-05-07T10:29:21Z"
    }
  ]
}
```

---

### Sessions — Detail

```
GET /agents/test-authoring-agent/sessions/<session_id>
```

Returns full session detail including all captured log lines, plus a `metrics`
block (run totals and the per-stage breakdown) when the session has one.

**Response:**
```json
{
  "session_id": "20260507-102341-payments",
  "module": "payments",
  "status": "success",
  "started_at": "...",
  "ended_at": "...",
  "lines": [
    "[10:23:41] test-authoring-agent | mode=direct",
    "..."
  ]
}
```

---

### Sessions — Metrics

```
GET /agents/<agent>/sessions/<session_id>/metrics
```

Time and cost for one session: run totals plus the per-stage breakdown.

**Response:**
```json
{
  "session_id": "20260828-130528-create-payments",
  "totals": {"cost_usd": 1.94, "llm_calls": 6, "num_turns": 71,
             "input_tokens": 130, "output_tokens": 21610,
             "llm_duration_s": 498.1, "tool_duration_s": 94.0,
             "duration_s": 612.4, "by_model": {"claude-sonnet-4-6": {...}}},
  "stages": [
    {"key": "parse", "label": "[01/05] Parse", "index": 1, "duration_s": 48.0,
     "cost_usd": 0.113, "llm_calls": 1, "num_turns": 6, "attempts": 1,
     "skipped": false}
  ]
}
```

A session with no metrics (one that predates capture) returns
`{"metrics": null, "stages": [], "totals": {}}` with HTTP 200 — that is a normal
case, not an error.

---

### Analytics — Summary

```
GET /analytics/summary?window=24h|7d|30d|all
```

Per-agent and overall rollups over a time window. Optional `from`/`to` (epoch
seconds) override the window, and `agent=<name>` filters to one agent.

Deliberately **not** under `/agents/<agent>/` — it spans agents.

Reads `qa_agents_server/storage/run_analytics.jsonl`, an append-only store
written at the end of every run (including CLI `make run` invocations, which
never touch the run registry). Cost figures come from the Claude CLI itself, so
they are exact rather than estimated.

**Response:**
```json
{
  "window": {"from": 1787300000, "to": 1787900000, "label": "Last 7 days"},
  "data_since": 1787200000,
  "overall": {"runs": 12, "succeeded": 7, "failed": 3, "cancelled": 2,
              "cost_usd": 18.42, "duration_s": 7420, "llm_calls": 61,
              "num_turns": 402, "tests_created": 4, "tests_fixed": 9,
              "items_adapted": 2, "cost_per_outcome_usd": 1.23},
  "by_agent": {"test-healing-agent": {"…same shape…": 0}},
  "series": [{"bucket": "2026-08-21", "cost_usd": 2.1, "runs": 2}]
}
```

Spend accumulates for **every** terminal status, not just successes — a run that
failed or was cancelled still cost money. `window` must be one of the four listed
values; anything else returns HTTP 400.

---

## Error Responses

All endpoints return errors as:
```json
{ "error": "description of what went wrong" }
```

Common HTTP status codes:
- `400` — bad request (missing required field)
- `404` — session or queue file not found
- `409` — conflict (e.g. run already active when one-at-a-time is enforced)
- `500` — internal server error
