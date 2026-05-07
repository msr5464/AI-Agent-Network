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

All agent routes are scoped under `/agents/test-authoring-agent/`.

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

Each SSE event is a JSON line:

```
data: {"line": "[10:23:49] ▶ [01/05] Parse", "index": 0}

data: {"line": "[10:24:38] ✓ [01/05] Parse — 49s", "index": 1}

data: {"done": true, "status": "success", "exit_code": 0}
```

- While running: one `data:` event per log line emitted by `run.sh`
- On completion: a terminal event with `done: true` and final `status`
- Use `?offset=N` to skip lines already received (for reconnect)

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

Returns full session detail including all captured log lines.

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
