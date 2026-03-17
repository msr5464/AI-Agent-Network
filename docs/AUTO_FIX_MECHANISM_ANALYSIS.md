# Auto-Fix Mechanism Analysis

This document describes the flow and components of the automated code-repair system in `src/auto_fix/`.

## Overview

The Auto-Fix system takes **failure classifications** (from the agent analyzer), limits to auto-fixable items, clones the automation repository, and for each failure: runs the test locally, gathers context (including optional browser-based locator discovery), generates a fix via LLM, applies it via the Cursor CLI, verifies by re-running the test, then commits and opens a GitHub PR.

---

## High-Level Flow

```
Classifications (from analyzer)
       ↓
Filter: is_auto_fixable (PRODUCT_CHANGE | AUTOMATION_ISSUE, HIGH/MEDIUM confidence)
       ↓
Clone repo once (GitHubClient)
       ↓
For each auto-fixable classification:
  1. Find test file (CodeAnalyzer)
  2. Extract test method & build base context (ContextBuilder)
  3. [Optional] Run test locally → fresh logs + stack frames (TestRunner)
  4. If test passed locally → skip (no fix)
  5. [If ELEMENT_NOT_FOUND/TIMEOUT] Browser inspection → discovered locators (BrowserInspector)
  6. Generate fix (FixGenerator / LLM)
  7. Validate fix syntax
  8. Create branch, apply fix via Cursor CLI (CursorClient), verify test, commit, push, create PR (GitHubClient + PRCreator)
       ↓
Return list of AutoFixResult (success / skipped / error)
```

---

## Component Details

### 1. `manager.py` — AutoFixManager

**Role:** Orchestrates the entire auto-fix workflow.

**Initialization:**
- Builds: `CodeAnalyzer`, `ContextBuilder`, `FixGenerator`, `GitHubClient`, `PRCreator`, `CursorClient`.
- `BrowserInspector` is **lazy**: created only when first needed for ELEMENT_NOT_FOUND/TIMEOUT.
- Loads session data (passed tests) from `session_file` if provided; skips fix generation for tests that passed locally in a previous run.

**Key methods:**
- **`process_classifications(classifications)`**  
  Filters with `is_auto_fixable`, caps at `max_fixes_per_run`, clones repo once, then calls `_process_single_failure` for each. Persists session data after the run.
- **`is_auto_fixable(classification)`**  
  True only for `PRODUCT_CHANGE` or `AUTOMATION_ISSUE` with `HIGH` or `MEDIUM` confidence.
- **`_process_single_failure(classification, repo_path)`**  
  Per-failure pipeline:
  1. Find test file and extract method.
  2. Build base context (cached by `(test_file, method_name, root_cause_category)`).
  3. Up to 2 attempts: run test locally (if `run_tests_locally`), get fresh logs and stack frames; if test passes → return skipped.
  4. For ELEMENT_NOT_FOUND/TIMEOUT: optionally enrich context with page objects and call `_discover_locators_from_browser` (BrowserInspector).
  5. Generate fix with FixGenerator, validate syntax, then (unless dry_run) `_create_pr_with_fix`.
  6. On apply/verify failure, retry once with the same flow.
- **`_create_pr_with_fix(...)`**  
  Creates branch (or reuses existing PR branch), applies fix to method + any `additional_changes`, uses **CursorClient** to write files, runs test for verification, restores on failure, then commit → push → create/update PR via GitHubClient and PRCreator.
- **`_discover_locators_from_browser(classification, execution_log)`**  
  Lazy-inits BrowserInspector, extracts page URL and element name from logs/root cause, runs locator discovery, returns list of dicts for context.
- **`_verify_changes(repo_path, test_to_run)`**  
  Runs TestRunner; returns error string if test fails.
- Helpers: `_parse_stack_frames`, `_apply_fix_to_method` (signature preservation, brace balance, `_flex_replace`), `_extract_element_name_from_root_cause`, `_load_session_data` / `_save_session_data`.

**Session tracking:** Passed tests are stored in `session_file` (JSON with `passed_tests` and `last_updated`) and reused to skip fix generation for tests that already pass locally.

---

### 2. `cursor_client.py` — CursorClient (IDE integration)

**Role:** Applies and restores file contents in the repo using the **Cursor headless CLI** (`agent`).

**Requirements:**
- `agent` binary on PATH (Cursor CLI).
- `CURSOR_API_KEY` environment variable.

**Behavior:**
- **`_is_configured()`** — True only if both `agent_path` and `api_token` are set.
- **`_run_agent(prompt, cwd)`** — Runs `agent -p --force <prompt>` in `cwd` with `CURSOR_API_KEY`, 120s timeout; returns `(success, stdout_or_error)`.
- **`apply_changes(repo_path, file_changes)`** — For each `FileChange`, builds a prompt: “Write exactly the following content to &lt;file_path&gt;” with the new content in a code block; runs the agent once per file. Fails fast on first failure.
- **`restore_files(repo_path, originals)`** — Same mechanism: one prompt per file to write back original content. Used by the manager to roll back after apply or verification failure.

So the “IDE integration” is **file patching via Cursor’s headless agent**, not a direct API to the IDE UI.

---

### 3. `browser_inspector.py` — BrowserInspector (Selenium locator discovery)

**Role:** When a failure is ELEMENT_NOT_FOUND or TIMEOUT, inspects the live page with Selenium to suggest new locators.

**Dependencies:** `selenium` (Chrome). Optional at import; raises at use if not installed.

**Classes:**
- **`LocatorCandidate`** — `locator_type`, `locator_value`, `confidence`, `element_text`; `to_selenium_by()` maps type to Selenium `By` + value.
- **`BrowserInspector`** — Context manager: `start_browser()` / `close_browser()` (Chrome, headless by default).

**Key methods:**
- **`extract_page_url(execution_log, root_cause)`** — Regex over combined text: “Page URL:- …”, or first `https?://` (prefer app-like URLs: app., dashboard., qa-, staging).
- **`discover_element_locators(page_url, element_name, element_text_hint)`** — Navigates to `page_url`, then:
  - `_discover_by_text` (exact/partial text XPath),
  - `_discover_by_attributes` (id, name),
  - `_discover_by_data_attributes` (data-cy, data-testid),
  - `_discover_by_role_and_aria` (aria-label).
  Returns up to 10 `LocatorCandidate`s sorted by confidence (HIGH > MEDIUM > LOW).
- **`_generate_locator_for_element(element)`** — Prefer id → data-cy → data-testid → name → tag.class → XPath.

**Integration:** Manager calls `_discover_locators_from_browser` only for ELEMENT_NOT_FOUND/TIMEOUT, extracts URL from logs and element name from root cause (e.g. “PageName:ElementName” or “Element '…' is NOT”), then injects `discovered_locators` into context for the FixGenerator.

---

### 4. `github/` — Version control and PRs

**client.py — GitHubClient**

- **Dependencies:** PyGithub, GitPython.
- **Clone:** Removes existing dir, clones via token URL into `workspace/<repo_name>`.
- **Branch:** `create_branch(repo_path, branch_name)` — checkout default, pull, then create or checkout branch (including tracking remote if it exists).
- **Changes on disk:** `apply_changes(repo_path, file_changes)` — writes `FileChange.new_content` to repo paths (used for in-memory apply; actual apply in manager is via CursorClient).
- **Commit / Push:** `commit_changes(repo_path, message)`, `push_branch(repo_path, branch_name, force)`.
- **PRs:** `get_open_pr_by_branch(repo_name, branch_name)`; `create_pull_request(..., reuse_existing=True)` — reuses open PR for branch if present, else creates new PR, adds labels and reviewers.

**pr_creator.py — PRCreator**

- **`generate_pr_title(classification)`** — “🔄 Update test for product change” / “🔧 Fix automation issue” + test method name.
- **`generate_pr_body(classification, fix_proposal)`** — Markdown with test info, root cause, recommended action, fix explanation, confidence, review checklist.
- **`determine_labels(classification)`** — e.g. `automated-fix`, `qa`, `product-change` or `automation-issue`, confidence-based (`high-confidence` / `needs-review`).

---

## Data Flow Summary

| Stage            | Input                          | Output / Side effect                    |
|------------------|---------------------------------|----------------------------------------|
| Filter           | Classifications                 | Auto-fixable list, capped               |
| Clone            | Repo name                       | Local repo path                         |
| Find test        | Test name, repo path            | Test file path, method code             |
| Context          | Repo, test file, method, logs   | Context dict (stack, page objects, etc.)|
| Local run        | Test name, repo path            | Success/fail, fresh logs, stack frames  |
| Browser (opt.)   | Logs, root cause                | Discovered locators                     |
| LLM              | Classification, test code, ctx  | FixProposal                             |
| Apply            | FixProposal, repo path          | Files written via Cursor CLI           |
| Verify           | Repo path, test name            | Pass/fail → rollback on fail            |
| PR               | Branch, commit, title, body     | PR URL or error                         |

---

## Configuration (Manager)

Relevant constructor args: `github_token`, `github_org`, `github_repo_automation`, `github_default_branch`, `github_pr_reviewers`, `llm_provider`, OpenAI/Ollama settings, `max_fixes_per_run`, `dry_run`, `run_tests_locally`, `target_environment`, `session_file`.

---

## Safety and Limits

- **Dry run:** No PRs created; rest of flow can still run.
- **Confidence:** Only HIGH/MEDIUM classifications are auto-fixable.
- **Max fixes per run:** `max_fixes_per_run` (default 5).
- **Syntax checks:** Fix proposal validated (signature, braces) before apply.
- **Verification:** Test re-run after apply; rollback and PR failure if it fails.
- **Session:** Passed tests skipped for fix generation when session file is used.

This completes the analysis of the Auto-Fix mechanism in `src/auto_fix/`.
