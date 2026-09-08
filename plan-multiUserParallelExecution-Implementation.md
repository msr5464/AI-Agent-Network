# Comprehensive Implementation Plan: Multi-User Parallel Execution

This document outlines the end-to-end implementation plan for migrating `QA-Agent-Network` to a multi-user, parallel-execution architecture using **Git Worktrees** and **Threaded Worker Pools**, while ensuring compatibility with `Ai-Test-Studio`.

## Phase 1: `Ai-Test-Studio` (Proxy & UI) Updates

Since `Ai-Test-Studio` acts as the authentication boundary and front-end interface, it must securely propagate user identity to the `qa_agents_server`.

### 1. User Signup, Authentication & Admin Approval / Role Gate
- **Signup Flow (Self-Service + Admin Gate):**
  - User fills out the Signup Form with Name, Email, and Password.
  - Upon submission, a new user account is created with `status = 'pending_approval'` and `role = 'member'`.
  - The user sees an immediate onboarding state: *"Your account has been created and is pending administrator approval."*
- **Login Behavior for Pending Users:**
  - If a user attempts to log in while `status = 'pending_approval'`, the login responds with `403 Forbidden` (or a dedicated status code) and the UI routes to a clean **Pending Approval Status Screen**.
  - No access is granted to queues, active runs, or agent triggers until approved.
- **Reusing & Extending Existing Admin User Management:**
  - Leverage and extend the existing **Admin User Management** interface (`/admin/users`) in `Ai-Test-Studio`.
  - **Approval Workflow:** Admins can view pending requests and execute one-click **Approve** (activates account) or **Reject**.
  - **Role Management / Role Promotion:** Admins can promote any user to `admin` (or demote an admin to `member`) via a role toggle dropdown anytime.
  - **Account Controls:** Admins can suspend or deactivate users on demand.
- **Hierarchical Role Model (`admin` $\supset$ `member`):**
  - Role permissions are strictly hierarchical: an `admin` automatically inherits **full `member` access**.
  - Admins can author, heal, and adapt tests, run workflows, maintain user-scoped queues, and inspect live sessions just like regular members, while retaining exclusive privileges to access admin settings and user approval panels.
- **User Data Model & Storage in `Ai-Test-Studio`:**
  - Uses `Ai-Test-Studio`'s native lightweight file/JSON/SQLite storage (e.g. `storage/users.json` / `data/users.json` with atomic writes **and file-level locking** (e.g., via `filelock` package) to prevent corruption during concurrent signups/updates — **no external MySQL database required**).
  - **First Boot Bootstrap:** On initial boot, if no admin exists, automatically seed the default admin account with `role = 'admin'` and `status = 'active'` (credentials generated securely and printed on boot, or configured via `ADMIN_DEFAULT_EMAIL` / `ADMIN_DEFAULT_PASSWORD`). This account immediately has **both Admin and Member access** out-of-the-box.
  - **User Record Schema:**
    - `id`: string (e.g., `usr_9812`)
    - `name`: string (e.g., `Mukesh Rajput`)
    - `email`: string (e.g., `engineer@company.com`)
    - `password_hash`: string (bcrypt hashed)
    - `role`: string (`admin` | `member`)
    - `status`: string (`pending_approval` | `active` | `rejected` | `suspended`)
    - `created_at`: ISO timestamp string
    - `approved_by`: string | null (admin user ID)
    - `approved_at`: ISO timestamp string | null
- **Auth Middleware & Route Protection:**
  - `requireAuth`: Validates JWT / session cookie (allows both `admin` and `member`).
  - `requireActiveUser`: Ensures `req.user.status === 'active'`. Blocks API proxying to `qa_agents_server` if not active.
  - `requireAdmin`: Protects admin settings, role elevation, and user approval endpoints. Allows `admin` only.

### 2. Backend API Proxy Enhancements
- **Intercept & Inject User ID:** In the API gateway/proxy layer (`backend/routes/api.js` or similar proxy controller), parse the authenticated user's JWT/session token.
- **Header Injection:** For all proxied requests to `qa_agents_server` (e.g., `/api/agents/*`), inject the following headers:
  - `X-User-ID`: The unique identifier of the user (e.g., `usr_123`).
  - `X-User-Name`: The user's name/handle for audit trails.
- **Settings API Gate:** For `GET/PUT /settings` (`/api/admin/agent-settings`), continue enforcing admin-only access (`requireAdmin`). Global settings still apply across all users.

### 3. Frontend UI Enhancements
- **Queue Partitioning:** Update the UI for managing queue files so that users understand they are seeing *their* workspace queues.
- **Multi-Run Active Panel & Session Switcher:**
  - Instead of binding the UI panel to a single global active session, allow the user to see a list of their active in-flight runs (e.g. tabs or a session selector: `payments [running]`, `checkout [running]`).
  - The live SSE log stream listens to the currently selected active session.
- **Busy Indicator & Global Capacity:**
  - Update the "System Busy" indicator. Since `qa_agents_server` will now allow up to $N$ concurrent runs, the UI displays capacity (e.g., "2/4 Workers Busy") and handles `queued: true` states gracefully with "Queue Position: X" when the thread pool is saturated.
- **User-Level Analytics Dashboard (`Ai-Test-Studio` Admin UI):**
  - Update the Analytics panel to include a **User Filter** dropdown.
  - By default, it displays aggregate data ("All Users"), but admins can select a specific user to view individual LLM cost, time saved, and run counts.

---

## Phase 2: `qa_agents_server` (Python Backend) Updates

The Python server needs to stop using global locks and single directories, and start respecting the `X-User-ID` header.

### 1. User-Scoped Queue Files (`qa_agents_server/feature_files.py`)
- Extract `user_id` from Flask `request.headers.get("X-User-ID", "default")`.
- Update `_queue_dir()` and `_processed_dir()` to append the `user_id` to the path:
  `agents/<agent>/queue/<user_id>/<module>.txt`
- Ensure directory creation (`mkdir -p`) handles the new nested structure.

### 2. Thread Pool & Concurrency Release (`qa_agents_server/runner.py`)
- Remove the strict `_active_session_id` singleton.
- Introduce `_active_runs = {}` (Dictionary of `session_id -> RunState`).
- Define `MAX_CONCURRENT_RUNS = int(os.environ.get("QA_MAX_CONCURRENT_RUNS", 4))`.
- Update `start_run()`:
  - If `len(_active_runs) < MAX_CONCURRENT_RUNS`, spawn the process immediately.
  - Else, append to `_pending_queue`.
- **Fair Queue Scheduling:** Update the queue popping mechanism (`_start_next_from_queue()`) to prioritize fairly (e.g. round-robin by `user_id`) rather than strict FIFO, so one user submitting 10 runs doesn't starve other users.
- Update `RunState` dataclass to include `user_id: str`.
- Update `get_active_session_id()` / `run_active()` to accept `user_id` and return the specific user's active run:
  `next((r for r in _active_runs.values() if r.user_id == user_id), None)`

### 3. Audit Storage, History, Streams & Artifacts (`qa_agents_server/storage.py` & `routes.py`)
- Add `user_id` to the JSON snapshot saved in `agent_runs.json` and ensure it propagates into the analytics store (`storage/run_analytics.jsonl`).
- Update `routes.py` `GET /agents/<agent>/sessions` to filter the loaded sessions by `request.headers.get("X-User-ID")` (allow bypassing if an admin flag is passed).
- **Stream & Session Privacy Guard:** Ensure that `GET /run/<session_id>/stream` (live SSE logs) and `GET /sessions/<session_id>` both validate that the requested session belongs to the requesting `X-User-ID` (unless the requester is an Admin). This prevents User A from snooping on User B's live logs or history.
- **User-Level Analytics (`qa_agents_server/analytics.py` & `routes.py`):**
  - Update `analytics.py` queries to accept an optional `user_id` filter.
  - Expose this via `GET /analytics/summary?user_id=...`.
  - Admins can query by specific users, while regular members only see their own rolled-up analytics.
- **Dynamic Artifact Serving (`_artefact_roots` in `routes.py`):**
  - Update `_artefact_roots` to include `/tmp/qa-runs/` so screenshots, DOM snapshots, and traces generated inside ephemeral worktrees can be served securely via `/agents/<agent>/artifact?path=...`.

---

## Phase 3: Workspace & Git Isolation (`shared/workspace.py` & `run.sh`)

This is the core execution isolation. Instead of editing files in `WORKSPACE_DIR/Jarvis`, every run gets a temporary Git Worktree.

### 1. Git Worktree Integration (`shared/workspace.py`)
- Maintain a clean bare repository or main clone at `WORKSPACE_DIR/Jarvis`.
- In `prepare-base` or a new `prepare-worktree` function:
  - Generate a unique worktree path: `worktree_path = f"/tmp/qa-runs/{session_id}"`
  - **Detached HEAD Creation (Avoids Branch Lock Collisions):**
    - Execute: `git fetch origin +refs/heads/{base_branch}:refs/remotes/origin/{base_branch}`
    - Execute: `git --git-dir=WORKSPACE_DIR/Jarvis/.git worktree add --detach {worktree_path} origin/{base_branch}`
    - *Why `--detach`:* Git prohibits two worktrees from checking out the same local branch simultaneously. Detached HEAD allows $N$ parallel runs on `main` without collision.
  - Export the environment variable `FRAMEWORK_DIR={worktree_path}`.

### 2. Subprocess Environment (`qa_agents_server/runner.py`)
- When calling `subprocess.Popen(["bash", str(spec.run_sh)])`, ensure the `FRAMEWORK_DIR` environment variable is explicitly set to the worktree path so `run.sh` inherently targets the isolated directory.
- Scope any local cache (`TESTING_MODE` cache) to `agents/<agent>/cache/<user_id>/<module>` to prevent cache race conditions.

### 3. Bash Script Updates (`agents/*/run.sh`)
- `run.sh` currently has prerequisite checks for `AUTOMATION_FRAMEWORK_DIR`. These checks will pass cleanly because `FRAMEWORK_DIR` is provided.
- **Queue Location Fallback:** Modify the script to locate the input file correctly based on the user context:
  - If `USER_ID` is present, it looks in `queue/<USER_ID>/<module>.txt`.
  - If `USER_ID` is missing (e.g. running from CLI), it defaults to `cli` or the root `queue/` folder to maintain **100% backward compatibility for standalone scripts**.
- Change the `cd "$REPO_ROOT" && python3 -m shared.workspace prepare-base` step to use the new worktree logic.
- Ensure all Maven commands (`mvn test`) run inside `AUTOMATION_FRAMEWORK_DIR` (each worktree has its own isolated `target/` build directory).
- Ensure Git pushes (`05_ship.py`, `02_ship.py`) operate correctly from within the worktree (branch creation `git checkout -b <branch_name>` and `git push origin <branch_name>` work seamlessly in a worktree).

### 4. Cleanup & Resource Management
- In `qa_agents_server/runner.py` process reapers (when the process completes, fails, or is cancelled), add a hook to tear down the worktree:
  - Execute: `git --git-dir=WORKSPACE_DIR/Jarvis/.git worktree remove --force {worktree_path}`
  - Execute: `rm -rf {worktree_path}` (to remove untracked Maven `target/` files)
  - Execute: `git --git-dir=WORKSPACE_DIR/Jarvis/.git worktree prune`
- **Boot Reconciliation (`reconcile_on_boot`):**
  - On server restart, prune any orphaned worktrees in `/tmp/qa-runs/` left behind from interrupted or crashed processes.

---

## Phase 4: CI/CD, Deployment & Admin Settings

### 1. Config Variables (`config/.env`) & Admin Settings UI
- Introduce `QA_MAX_CONCURRENT_RUNS` (Default: 4).
- Introduce `QA_WORKTREE_TEMP_DIR` (Default: `/tmp/qa-runs`).
- **Update `qa_agents_server/agent_settings.py`:** Add these two new variables to the exposed configuration schema so they immediately appear in the **Admin Settings UI** in `Ai-Test-Studio`. This allows admins to dynamically scale the worker pool or change the temp directory without a server restart.

### 2. Machine Sizing
- If running 4 concurrent agents that each run headless Chrome (Playwright) + `mvn test`, the host machine should have at least 8 vCPUs and 16GB of RAM.
- Ensure the `~/.m2` Maven cache is safe for concurrent access. (Maven 3+ handles concurrent local repository downloads fairly well, but keep an eye on download lock warnings).

## Final Checklist & E2E Validation

### Code Implementation
- [ ] Build Customer Portal Signup/Login UI, user storage schema (`data/users.json` with `filelock`) with `status` and `role` fields, and Auth flow in `Ai-Test-Studio`.
- [ ] Extend existing Admin User Management (`/admin/users`) with Signup Approvals and Admin Role Promotion/Demotion.
- [ ] Add `requireActiveUser` guard in `Ai-Test-Studio` proxy to block unapproved users from agent routes.
- [ ] Update `Ai-Test-Studio` proxy to pass `X-User-ID` and `X-User-Name`.
- [ ] Update `feature_files.py` to scope queues by user.
- [ ] Add `QA_MAX_CONCURRENT_RUNS` and `QA_WORKTREE_TEMP_DIR` to `agent_settings.py` so they are manageable via the Admin Settings UI.
- [ ] Refactor `runner.py` to use a bounded worker pool (`_active_runs`) with Fair Queue Scheduling (round-robin).
- [ ] Enhance `storage.py`, SSE streams, and history endpoints to enforce `X-User-ID` privacy bounds.
- [ ] Add User filter to Analytics UI and update `analytics.py` to support `user_id` filtering.
- [ ] Implement `git worktree add --detach` in `shared/workspace.py`.
- [ ] Inject `FRAMEWORK_DIR={worktree_path}` during subprocess spawn and handle fallback paths in `run.sh`.
- [ ] Implement `git worktree remove --force` upon process termination and boot reconciliation.

### Full End-to-End GUI Testing Scenarios
1. **The Onboarding Flow:** User A signs up via the UI. Verifies they hit the `pending_approval` gate and cannot run tests.
2. **The Admin Approval:** Admin logs into the UI, navigates to `/admin/users`, approves User A. User A logs back in and gains full dashboard access.
3. **Stream Privacy:** User A runs a test. User B logs in. Verify User B *cannot* see User A's run in the Active Run Panel, History Panel, or via direct `/stream` URL interception.
4. **Parallel Execution (Same Agent):** User A submits Feature X. User B submits Feature Y to the *same* agent. Verify the UI correctly streams the logs for both independently, both spawn isolated worktrees in `/tmp/qa-runs/`, and both Maven tests compile without `~/.m2` cache race conditions.
5. **Parallel Execution (Different Agents):** User A runs Authoring. User A runs Healing in parallel. Verify the Session Switcher / UI Tabs elegantly track both active streams without state bleeding.
6. **Thread Pool Saturation & Queueing:** Set Admin setting `QA_MAX_CONCURRENT_RUNS=2`. Submit 3 jobs. Verify the third job stays queued, UI shows "Queue Position: 1", and it automatically spins up when a slot frees.