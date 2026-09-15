# Multi-User Parallel Execution

**Status: implemented.** This document describes what the system does, the
defects found when it was audited against the original plan, and what was
discarded as the wrong shape.

## Goal

Several people run agents at the same time, isolated from each other's files,
history and logs. One user's run must not see, disturb or be delayed by another's.

## How isolation works

**Execution** — every run gets its own git worktree under `QA_WORKTREE_TEMP_DIR`
(default `/tmp/qa-runs/<session_id>`), created detached:

```
git worktree add --detach /tmp/qa-runs/<session_id> origin/<base_branch>
```

Detached because git refuses to check out one local branch in two worktrees at
once, which is exactly the collision N parallel runs on `main` would hit. The
runner exports `FRAMEWORK_DIR` at the worktree and `QA_ISOLATED_WORKTREE_READY=1`,
which is what makes `checkout_base` detach rather than move a branch.

**Concurrency** — `runner._active_runs` holds up to `QA_MAX_CONCURRENT_RUNS`
(default 4). Beyond that, runs queue. The queue is drained fewest-active-runs
first, ties broken by position, so one user firing ten runs cannot starve
everyone behind them.

**Identity** — `AI-Test-Studio` authenticates, then injects `X-User-ID` /
`X-User-Name` / `X-User-Role` into every proxied request, stripping any copy the
client sent. `qa_agents_server` validates the id once at the edge
(`routes.current_user_id`) and never trusts it as a path segment.

## Decisions

**Identity is resolved once, and ownership is a decorator.** The original plan
said to add ownership checks endpoint by endpoint. That produced seven
session-scoped endpoints of which two were checked — and both copies had the same
bug, `if run_record and user_id and ...`, where **omitting the header granted
access**. Five had no check at all, including `/events` (the endpoint the UI
actually uses), `/cancel` (any user could kill any run) and `/retry`.

Replaced with `@owns_session` plus `current_user_id()`. One implementation,
default-deny, impossible to forget on the next endpoint — which is the failure
mode that produced the list.

**The server binds localhost, and admin needs a shared secret.**
`qa_agents_server` implements no auth of its own; `routes.py` documented that it
"is expected to bind to localhost" while defaulting to `0.0.0.0`, contradicting
its own precondition. Anyone reaching the port bypassed login, approval and roles
entirely, and `X-User-Role: admin` — a header any client can type — was the only
admin assertion in the server.

Now `127.0.0.1` by default, and `QA_AGENT_PROXY_SECRET` (set in both repos'
`config/.env`) must match for identity or admin headers to be honoured. This one
change makes most header-spoofing analysis moot, which is why it outranks the
per-endpoint work.

**Artifacts are copied out before teardown.** `/tmp/qa-runs` was added as an
artifact root so worktree artifacts could be served — but `_wait_and_reap`
deletes the worktree *before* the terminal event, so every such link was a 404,
while the root itself exposed every user's screenshots to every other user and,
under world-writable `/tmp`, let any local process drop a servable file in.
Runs now copy `test-output/` into their own audit directory
(`_preserve_worktree_artefacts`), and the temp root is no longer an artifact root.

**Per-worktree Maven builds are fine here.** Considered and rejected as a
concern: the target repo is 12 test files with a 2.5 MB `target/`, so cold
compiles are cheap and no worktree pooling is needed.

## Defects found and fixed

Every item below shipped and was verified broken before being fixed.

| | Defect |
|---|---|
| **Deadlock** | `_start_next_from_queue` took `_queue_lock` → `_registry_lock`; `start_run` took them the other way. Textbook ABBA — a run finishing while another started wedged the whole run subsystem, permanently. `_unique_session_id`'s own docstring warned about this hazard. Fixed by one global order, registry before queue, everywhere. |
| **Worktree isolation never engaged** | The runner exported `QA_ISOLATED_WORKTREE_READY`; `workspace.py` read `QA_ISOLATED_WORKTREE`. The names never matched, so every run took the `checkout -B` path. Already visible in committed audit output as *"'adaptation-agent' is already used by worktree at ..."*. |
| **Stale base for every run** | `prepare_worktree` never fetched, on a comment's claim that `prepare_base` handled it — but the server calls `ensure()` → `prepare_worktree()` directly and never goes through `prepare_base`. Combined with a `--depth 1 --branch` clone, any non-default base branch had no `origin/<b>` ref at all. |
| **False success** | `prepare_worktree` returned `ok: True` whenever the path merely *existed* — an existence check dressed as a validity check. Now probes with `rev-parse --is-inside-work-tree` and recreates. |
| **Cancel broken three ways** | `run.status = "cancelled"` was set nowhere, so cancelled runs reported as **failed** and the `.cancelled` marker was never written; `CANCEL_GRACE_SECONDS` was unused so there was no SIGKILL escalation; and cleanup `rm -rf`'d the worktree microseconds after SIGTERM, out from under a still-running JVM. |
| **Queue could strand** | A failed `start_run` popped the item, logged, and did not re-kick — so if that was the last active run, the rest of the queue waited for a restart. |
| **Arbitrary file write** | `X-User-ID` was joined straight onto a path (`spec.queue_dir / user_id`) while the feature *name* beside it was rigorously validated. `Path("…/queue") / "/tmp/pwn"` is `/tmp/pwn`. Validated at both the edge and in `feature_files`. |
| **Blanket `rm -rf` on boot** | `reconcile_on_boot` deleted every subdirectory of `QA_WORKTREE_TEMP_DIR` despite a comment claiming it checked validity, pruned *before* deleting (so it pruned nothing and orphaned admin entries), and the setting had no validation — a typo in the admin UI became recursive deletion on next boot. |
| **No git lock** | `workspace.py` had no `threading` import at all. Concurrent `fetch` / `worktree add` / `prune` on the shared `.git` contend on `index.lock`, `config.lock` and `refs/remotes/origin/<b>.lock`. Now serialised by a lockfile on the **common** git dir, so worktrees and agent subprocesses share it. |
| **All cost billed to admin** | `build_record` accepted `user_id` and neither writer passed it, though `run.user_id` was on the object. Every row was `"default"`, which `query()` rewrote to the admin id — so members saw an empty dashboard. History and analytics also disagreed about legacy rows, defaulting them to `"default"` and to the admin id respectively; both now use one `_owner_of()`. |
| **Cross-user cache leak** | `cache/<module>` was shared, so with `TESTING_MODE=true` two users on the same module name read each other's cached step output. Now `cache/<user_id>/<module>`. |
| **`.mcp.json` collision** | The authoring agent wrote it to the shared repo root while the other two agents used their audit dir — one of which carries the comment *"the repo root is shared mutable state"*. |
| **One repair browser per host** | The CDP port was fixed at 9222, so the first healing run took it and every other silently skipped live repair. Now derived per session and passed to the framework as `repairPort`. |
| **Two runs, one handoff** | Healing's queue mode picked the oldest `.json` with no claim, so concurrent runs both fixed the same test. Now claimed by atomic `mv`. |

## Verification

- `pytest tests/unit/` — 1240 tests, including `test_parallel_isolation.py`,
  which pins each defect above that can be expressed as a unit test: lock
  ordering (read from the AST, since a timing test would pass by luck), the
  worktree flag agreement between writer and reader, hostile `X-User-ID` values,
  and ownership default-deny.
- Concurrency, checked directly: six simultaneous `prepare_worktree` calls all
  succeed, detached, serialised, with no stale admin entries after cleanup.
- Privacy, checked directly: another user's session returns 403 on
  `/stream`, `/events`, `/metrics`, `/cancel` and `/retry` — **with and without**
  the header — while the owner passes.
- Claim safety, checked directly: six concurrent claimers against three handoffs
  claim each exactly once.
- Cancellation: `test_parallel_isolation.py` covers status, SIGKILL escalation
  and teardown ordering, each mutation-tested — reverting any one of the three
  fixes fails its test. The escalation case needs a child that genuinely ignores
  SIGTERM *and* a readiness handshake; without the handshake the signal arrives
  before bash installs its trap, the child dies with rc=-15, and the test passes
  without ever reaching the SIGKILL path.
- Full GUI end-to-end against `Playwright-Automation-Framework`: signup → pending
  gate → approval; two concurrent authoring runs with independent worktrees and a
  working session switcher; authoring + healing concurrently, both running Maven;
  a third run queued at position 1 that auto-started 2s after a slot freed; and
  a second user receiving 403 on every path to the first user's live session.

## Seeing your runs

`GET /run/active` reports every run the caller has in flight, not just the first
one the registry happened to yield:

```jsonc
{
  "active": true, "session_id": "…", "module": "payments",   // unchanged
  "runs": [ {session_id, agent, module, status, started_at}, … ],  // this agent
  "other_agent_runs": [ … ],                                       // their other agents
  "capacity": {"active": 3, "max": 5, "queued": 1, "busy": false,
               "mine_active": 2, "mine_queued": 1}
}
```

`runs` is oldest-first, so tabs do not reorder underneath the person using them.
The additions are purely additive — every historic top-level field is still
there, so panels that were never updated keep working.

The UI renders this as one row of chips above the live console, plus an
occupancy pill in the header. Both are written once
(`QA_RENDER_SWITCHER` / `QA_RENDER_CAPACITY`, driven by panel prefix) rather than
pasted into three panels that would then drift.

**Both hide themselves when they have nothing to say** — the switcher below two
runs, the pill below two active workers. A permanent "1/5" is noise, and noise is
what teaches people to stop reading an indicator that will later matter.
Cross-agent runs appear as dashed, non-clickable chips: a healing session
rendered under authoring's step labels is exactly the confusion the per-agent
scoping exists to prevent.

## Still open

- `/admin/users` is a tab, not a route, so that URL 404s.
- The Studio's role vocabulary is split three ways (`customer`/`member`/`admin`);
  the create and edit forms disagree.
- `SESSION_COOKIE_SECURE` defaults false — set it true when serving over HTTPS.
- The queue card lists rows with a `mine` flag but does not yet visually
  distinguish someone else's queued runs from your own.
- The Studio has no JavaScript test harness, so the switcher's behaviour is
  pinned by browser-driven checks rather than by a committed test. Two bugs in
  it were found only by driving the real UI — `apiFetch` being out of scope in
  the global watcher, and the queue card never being populated on init — both of
  which a synthetic test that called the render functions directly had missed.
