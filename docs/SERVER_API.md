# HTTP Server API

`qa_agents_server` is a Flask + SSE server that runs the authoring, healing and
adaptation agents on request, streams their progress, and serves their history,
analytics and settings. AI-Test-Studio talks to it through its own backend proxy
(`/api/agents/*`, `/api/admin/agent-settings`, `/api/admin/analytics`). CLI users
do not need it.

The triaging agent is not served — it runs from the CLI or CI only.

---

## Starting the server

```bash
bash scripts/run-server.sh
# QA Agent Server listening on http://127.0.0.1:6001
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `QA_AGENT_SERVER_HOST` | `127.0.0.1` | Bind host. Binding anything else without `QA_AGENT_PROXY_SECRET` prints a warning — see [Identity and auth](#identity-and-auth) |
| `QA_AGENT_SERVER_PORT` | `6001` | Bind port |
| `QA_AGENT_PROXY_SECRET` | *(unset)* | Shared secret with AI-Test-Studio. When set, identity and admin headers are honoured only on requests carrying it as `X-Proxy-Secret` |
| `AI_TEST_STUDIO_URL` | `http://localhost:5001` | CORS allowlist |
| `QA_AGENT_SERVER_EXTRA_ORIGINS` | *(unset)* | Extra comma-separated CORS origins |
| `QA_MAX_CONCURRENT_RUNS` | `4` | Runs executing at once; the rest queue |
| `QA_WORKTREE_TEMP_DIR` | `/tmp/qa-runs` | Where each run's git worktree is created. Unsafe roots (`/`, `/tmp`, `$HOME`-level dirs) fall back to the default |
| `QA_AGENT_RUN_TIMEOUT_SECONDS` | `7200` | Kill a whole run after this long |
| `QA_AGENT_STALE_AFTER_SECONDS` | `900` | A session untouched this long is reported as `interrupted`, not running (legacy alias `QA_HEALING_STALE_AFTER_SECONDS`) |
| `QA_SEED_EXAMPLES` | `true` | Seed queues from `docs/examples/queue/` (see [Example seeding](#example-seeding)) |
| `RUN_ANALYTICS_FILE` | `qa_agents_server/storage/run_analytics.jsonl` | Append-only analytics store |

All of these can also live in `config/.env`; `run-server.sh` loads it through
`shared/load_env.sh`.

State on disk: `qa_agents_server/storage/agent_runs.json` (run registry, capped
at 500 entries; stranded runs are marked `interrupted` on boot) and
`storage/run_analytics.jsonl` (never pruned). Per-run artefacts live in each
agent's `audit/<session-id>/`.

---

## Identity and auth

The server has no login of its own. AI-Test-Studio authenticates the user, strips
any identity headers the browser sent, and injects its own:

| Header | Meaning |
|--------|---------|
| `X-User-ID` | `md5(username)[:12]`. Anything else is treated as the anonymous user `default` |
| `X-User-Name` | Username. When it hashes to `X-User-ID`, the caller's id becomes the readable `user-<name>` (used for queue and cache directory names) |
| `X-User-Role` | `admin` grants admin routes and cross-user visibility |
| `X-Proxy-Secret` | Must equal `QA_AGENT_PROXY_SECRET` when that is set; otherwise the request is treated as anonymous and non-admin |

With no `QA_AGENT_PROXY_SECRET`, headers are trusted as sent — safe only because
the server binds to localhost by default. **If you bind to another interface, set
the secret in both repos' `config/.env`.**

Ownership is default-deny: every session-scoped route (`/run/<id>/…`,
`/sessions/<id>/…`, `/artifact` under an audit dir) returns **403** unless the
caller owns the session or is admin. Session lists, queues and analytics are
filtered to the caller unless admin.

---

## Agents

All agent routes are under `/agents/<agent>/`. Capabilities come from each
agent's `AgentSpec` in `qa_agents_server/agents.py`:

| `<agent>` | Queue | Resume from step | Test catalogue | Steps (UI labels) |
|---|---|---|---|---|
| `test-authoring-agent` | writable `.txt` feature specs, per user | yes (2–5) | no | Parse · Validate · Generate · Run & Fix · Ship |
| `test-healing-agent` | read-only `.json` handoffs from triaging, global | no — retries internally | yes | Reproduce · Locate · Fix · Ship |
| `test-adaptation-agent` | writable `.txt` change notes, per user | yes (2–5) | yes | Parse Change · Scope · Explore · Adapt · Ship |

An unknown agent returns 404. A route an agent does not support returns 405
(e.g. `POST …/test-healing-agent/queue`) or 404 (e.g. `…/test-authoring-agent/tests`).

---

## Endpoints

### Health

```
GET /health
```
```json
{"status": "ok", "service": "qa_agents_server", "version": "0.1.0", "active_run": null}
```
`active_run` is always `null` (kept for old clients) — use `GET /agents/<agent>/run/active`.

### Queue (input files)

```
GET  /agents/<agent>/queue
GET  /agents/<agent>/queue/<name>
POST /agents/<agent>/queue
```

`.txt` queues are per user: the anonymous and CLI identities (`default`, `cli`)
use `agents/<agent>/queue/`, everyone else `agents/<agent>/queue/<user-id>/`.

`GET` list — newest first:
```json
{"items": [{"name": "payments", "filename": "payments.txt", "size": 412,
            "modified": 1759212000.1, "preview": "Module: payments …"}]}
```
Healing's `.json` queue lists `{name, size, modified}` only.

`GET /queue/<name>` → `{name, filename, size, modified, content}` (healing: `{name, content}`).

`POST` creates or replaces a file:
```json
{"name": "payments", "content": "Module: payments\nType: web\n\nSteps:\n1. …"}
```
→ **201** `{name, filename, size, modified, created_at}`. Errors: 400 bad name or
non-string content, **413** over 64 KB, 405 on healing.

### Run defaults and pickers

```
GET /agents/<agent>/config
```
Effective defaults, so the UI does not guess:
```json
{"agent": "test-authoring-agent", "auto_push_default": true, "adapt_apply_default": false,
 "default_branch": "main", "local_branch": "main", "local_dirty": false}
```
`local_branch` / `local_dirty` describe the local checkout a dry run would use
(`null` when the automation repo is missing).

```
GET /agents/<agent>/branches
```
`{"branches": ["main", "…"], "default": "main"}` — from `git ls-remote`, cached
60 s, never an error (an empty list if GitHub is unreachable).

```
GET /agents/<agent>/tests                       # healing, adaptation
GET /agents/<agent>/tests/intent?test=pkg.Class#method   # adaptation panel
```
`/tests` lists the automation repo's classes and `@Test` methods plus the
checkout's `branch` and `dirty` flag; **503** if the repo is not found.
`/tests/intent` returns one test's derived intent contract
(`test, source, proves, verifies, checks, identity, unresolved_count`); 400 unless
`test` names a single method.

### Start a run

```
POST /agents/<agent>/run
```

| Agent | Body |
|-------|------|
| authoring | `{"module": "payments", "auto_push": true, "base_branch": "main"}` — `module` is required and its queue file must exist (404 otherwise) |
| healing | `{"test": "LoginTest#testLogin", "repair": false, "force": false}` (standalone) **or** `{"build_tag": "ProdSanity-541"}` (a queued handoff). Exactly one of `test` / `build_tag`; `test_name` is accepted as an alias of `test` |
| adaptation | `{"module": "checkout", "explore_only": false, "apply": true}` — as authoring, plus `explore_only` (flow map only) and `apply` (`false` = propose only) |

All fields other than the identifying one are optional. Omitted `auto_push`,
`apply` and `base_branch` fall back to `config/.env` (`AUTO_PUSH`,
`ADAPTATION_APPLY`, `GITHUB_DEFAULT_BRANCH`) — an omitted field is **not** false.
A `base_branch` that does not exist on the remote is rejected with 400.

**201** — started:
```json
{"queued": false, "agent": "test-authoring-agent", "session_id": "20260924-101500-create-payments",
 "module": "payments", "auto_push": true, "base_branch": "main",
 "status": "running", "started_at": 1759212900.5}
```
**202** — the pool is full, so the run is queued (it starts on its own):
```json
{"queued": true, "position": 2, "agent": "test-authoring-agent",
 "module": "payments", "session_id": "20260924-101500-create-payments"}
```
Other errors: 400 validation, 404 queue file / session missing, 503 shutting down.

**Where it runs.** With auto-push on, each run gets its own detached git worktree
under `QA_WORKTREE_TEMP_DIR`, cut from `origin/<base_branch>`, so runs execute in
parallel. With auto-push off (a dry run) it runs **in your local checkout as it
stands, uncommitted changes included**, and only one such run executes at a time.
Queued runs start fewest-active-runs-per-user first, so one user cannot starve
the rest.

### Resume a session

```
POST /agents/<agent>/sessions/<session_id>/retry
{"from_step": 3, "auto_push": false}
```
Re-runs an existing session from step 2–5, reusing earlier step output and the
session's original base branch. Omitted `auto_push` inherits the original run's.
Same 201/202 responses as `/run`, plus `start_from_step`. 400 if `from_step` is
outside 2–5, **405** for healing.

### Active runs and the pending queue

```
GET /agents/<agent>/run/active
```
```jsonc
{
  "active": true,                 // this caller has a run of THIS agent in flight
  "busy": false,                  // the worker pool is full
  "busy_agent": "test-authoring-agent",
  "capacity": {"active": 2, "max": 4, "queued": 0, "busy": false,
               "mine_active": 1, "mine_queued": 0},
  "runs": [{"session_id": "…", "agent": "…", "module": "…", "status": "running", "started_at": 1759212900.5}],
  "other_agent_runs": [],         // the caller's runs on other agents
  // when active, the oldest run's fields at top level:
  "session_id": "…", "module": "payments", "status": "running", "started_at": 1759212900.5,
  "auto_push": true, "base_branch": "main", "step_progress": {"parse": "done", "validate_web": "running"},
  "step_metrics": {}, "metrics": {}, "start_from_step": 1
}
```
With nothing running for this agent: `{"active": false, "busy": …, "capacity": …, "runs": [], "other_agent_runs": […]}`.

```
GET    /agents/<agent>/run/queue            → {"queue": [ … rows with a "mine" flag … ], "capacity": {…}}
DELETE /agents/<agent>/run/queue/<index>    → {"removed": true, "queue": […], "capacity": {…}}
```
`index` is the row's position in the global queue. Non-admins may only remove
their own rows (404 otherwise).

### Stream a run (SSE)

```
GET /agents/<agent>/run/<session_id>/stream?offset=N
```
Live events for a running session, or a replay from the audit folder for a
finished one. `offset` skips events with `seq <= N`, so a reconnecting client
passes the last `seq` it saw. Frames are named, and each payload is
`{seq, kind, data, ts}`:

```
event: stdout
id: 42
data: {"seq": 42, "kind": "stdout", "data": {"line": "[parse] …"}, "ts": 1759212901.2}
```

`kind` is one of `stdout`, `step`, `status`, `done`, `error`, `heartbeat`. Because
frames carry `event:`, `EventSource.onmessage` never fires — listen by name:

```javascript
const es = new EventSource(`/agents/test-authoring-agent/run/${sessionId}/stream?offset=0`);
es.addEventListener('stdout', e => console.log(JSON.parse(e.data).data.line));
es.addEventListener('step',   e => console.log('step', JSON.parse(e.data).data));
es.addEventListener('done',   e => { console.log('done', JSON.parse(e.data).data); es.close(); });
```

For a **finished** session prefer the plain JSON form — it does not hold a
connection open:
```
GET /agents/<agent>/sessions/<session_id>/events   → {"events": [ … ]}
```

### Cancel a run

```
POST /agents/<agent>/run/<session_id>/cancel
```
→ `{"status": "cancelling", "session_id": "…"}`. Sends SIGTERM to the run's
process group and escalates to SIGKILL after a grace period; the session ends
`cancelled`. 404 if not running, 409 if the session belongs to another agent.

### History

```
GET /agents/<agent>/sessions?limit=50&offset=0
```
`limit` max 200. Admins see everyone's sessions, others only their own.
```json
{"items": [{"session_id": "20260924-101500-create-payments", "module": "payments",
            "started_at": "…", "status": "completed", "verdict": "APPROVED",
            "fix_gate": "true", "test_passed": true, "pr_url": "https://github.com/…/pull/12",
            "duration_s": 512.4, "cost_usd": 1.84, "…": "…"}]}
```
`status` is `running`, `completed`, `failed`, `cancelled`, `interrupted` or
`diagnosed` (healing stopped by its failure diagnosis). Fields vary slightly by
agent — see `qa_agents_server/audit_reader.py`.

```
GET /agents/<agent>/sessions/<session_id>
```
The summary fields plus `init_md` (`00-session-init.md`), `steps` (each step's
parsed JSON, keyed by step) and `reports` (each step's `.md`, same keys), and
`metrics` when recorded.

```
GET /agents/<agent>/sessions/<session_id>/metrics
```
`{session_id, metrics, totals, stages}` — time and cost per stage. Sessions from
before metrics capture return `metrics: null`.

### Artefacts

```
GET /agents/<agent>/artifact?path=<absolute path from a log line>
```
Serves a screenshot, DOM snapshot, trace, video or report. The path must resolve
inside the agent's audit dir (and a session the caller owns) or the automation
repo's `test-output/`, and have a known suffix; otherwise 403. HTML is sent as a
download, never rendered inline.

### Analytics

Cross-agent, so not under `/agents/`.

```
GET /analytics/summary?window=7d&agent=&user_id=&from=&to=
```
`window` is `24h`, `7d`, `30d` or `all` (400 otherwise); `from`/`to` are epoch
seconds. `user_id` is honoured for admins only — everyone else gets their own.
Returns `{window{from,to,label}, data_since, overall, by_agent, series}` with
runs, cost, duration and outcome counts. Time-saved is computed by the Studio.

```
DELETE /analytics/clear?window=7d&user_id=
```
**Irreversible.** Deletes matching analytics rows, run-registry entries **and the
sessions' audit directories**. Admins may name any user (or none = everyone);
members clear only their own; anonymous callers get 403.

### Agent settings (admin only)

```
GET /settings
PUT /settings
```
Backs the Studio's **🤖 Agent Settings** admin page, which it reaches through the
admin-gated `/api/admin/agent-settings`. Both return 403 unless the caller is admin.

- `GET` returns the schema (`qa_agents_server/agent_settings.py`) and current
  values. Secrets are partially masked (`ghp**********f9c`).
- `PUT` takes `{"<key>": value, …}` and writes `config/.env` **and** the server's
  `os.environ`, so changes apply from the next run without a restart. Sending a
  mask back unchanged keeps the stored secret. 400 lists validation errors.
- Per-run values (`TEST_NAME`, `BUILD_TAG`, `FORCE`, …) are deliberately not
  exposed. A key also set in `$REPO_ROOT/.env` or `agents/<agent>/.env` wins at
  run time (see `shared/load_env.sh`); the page flags those fields.

Adding a setting means adding one entry to `SETTINGS_SCHEMA`; the admin page
renders straight off it.

---

## Example seeding

On boot, each agent's queue root is seeded from `docs/examples/queue/<agent>/`,
and each user's own `.txt` queue is seeded the first time they open it. Seeding
never overwrites a queued file, never re-creates a name already in `processed/`,
and skips a queue that has its `.examples-seeded` marker — so a deleted example
stays deleted. Set `QA_SEED_EXAMPLES=false` to turn it off, or delete a queue
directory to get its examples back.

Seeded files are ordinary queue items: `./scripts/run-<agent>-agent.sh` with no
module or build tag picks the oldest one.

---

## Adding an agent

1. Add an `AgentSpec` to `AGENTS` in `qa_agents_server/agents.py`: paths, step
   list, `session_prefix`, `build_env` (request body → env vars),
   `describe_run`, and a `summary_kind` that `audit_reader.py` knows how to parse.
2. Add the agent to AI-Test-Studio: its proxy allowlist
   (`backend/api/agents/proxy.py`) and the step labels in
   `frontend/customer/index.html` (`STEP_LABELS`), which mirror the step names here.

Run registry, SSE streaming, cancellation, worktrees and queueing are
agent-agnostic and need no changes.

---

## Status codes

| Code | Meaning |
|------|---------|
| 200 | OK |
| 201 | Run started / queue file written |
| 202 | Run queued behind the worker pool |
| 400 | Validation error (missing `module`, bad branch, bad `from_step`, …) |
| 403 | Not your session, not admin, or artefact outside the allowed roots |
| 404 | Unknown agent, session, queue file or queue index |
| 405 | The agent does not support this route |
| 409 | Cancel for a session that belongs to another agent |
| 413 | Queue file over 64 KB |
| 500 | Server error (e.g. worktree creation failed) |
| 503 | Server shutting down, or automation repo not found (`/tests`) |
