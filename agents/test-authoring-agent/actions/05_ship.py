#!/usr/bin/env python3
"""
Step 05 — Ship
Creates a git branch in Thanos-pw with the generated files, commits, pushes,
and creates a GitHub PR. Sends a Slack notification.

No AI calls. No code changes.

Reads:  $AUDIT_DIR/03-generate.json
        $AUDIT_DIR/04-run-and-fix.json
        $AUDIT_DIR/.fix-passed
Writes: $AUDIT_DIR/05-ship.json
        $AUDIT_DIR/05-ship.md
        $AUDIT_DIR/.verdict
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

from shared import workspace as workspace_helper

from shared.log import log as _log
from shared.log import blocked
from shared import baseline as baseline_store
from shared import properties_file
from shared.slack import send_slack as _send_slack
from shared.git import run_git as _run_git
from shared.github import create_pr
from shared.credential_masking import mask_credentials
from shared.credential_extraction import credentials_from_plan

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR          = Path(os.environ.get("WORKSPACE_DIR", str(REPO_ROOT.parent)))
AUTOMATION_FRAMEWORK_DIR          = workspace_helper.resolve(
    WORKSPACE_DIR, os.environ.get("GITHUB_REPO_AUTOMATION", ""),
    exclude=REPO_ROOT)

AUTO_PUSH              = os.environ.get("AUTO_PUSH", "true").lower() == "true"
GITHUB_TOKEN           = os.environ.get("GITHUB_TOKEN", "")
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
GITHUB_PR_REVIEWERS    = [r.strip() for r in os.environ.get("GITHUB_PR_REVIEWERS", "").split(",") if r.strip()]
BRANCH_PREFIX          = os.environ.get("AUTOCREATE_BRANCH_PREFIX", "feat/qa-autocreate")

SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
SLACK_ALERT_CHANNEL  = os.environ.get("SLACK_ALERT_CHANNEL", "")

SESSION_ID = os.environ.get("SESSION_ID", AUDIT_DIR.name)
MODULE     = os.environ.get("MODULE", "unknown")

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None: _log("05-ship", msg)

def send_slack(channel: str, text: str) -> bool:
    return _send_slack(SLACK_BOT_TOKEN, channel, text)

def _push_url() -> str:
    # "x-access-token" is a fixed, non-secret placeholder username (the same
    # convention GitHub Actions itself uses) — the real secret is the token,
    # held only in memory for the one push call that uses this.
    return f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}.git"


def git(args: list, cwd: Path, use_token: bool = False) -> tuple:
    """Thin wrapper returning (returncode, stdout, stderr) for backward compat.

    use_token=True substitutes a credential-bearing URL for "origin" on THIS
    call only (see shared/git.py's run_git docstring for why — a plain
    "origin" push otherwise triggers an interactive credential prompt with
    no TTY to answer it). Local-only commands (add, commit, diff) never need
    this.
    """
    ok, stdout, stderr = _run_git(args, cwd, push_url=_push_url() if use_token else None)
    return (0 if ok else 1), stdout, stderr


def load_json(filename: str, required: bool = True) -> dict:
    path = AUDIT_DIR / filename
    if not path.exists():
        if required:
            log(f"ERROR: {filename} not found")
            sys.exit(1)
        return {}
    return json.loads(path.read_text())


def read_gate() -> str:
    gate = AUDIT_DIR / ".fix-passed"
    return gate.read_text().strip() if gate.exists() else "skipped"


def _stage_and_commit(contents: dict, message: str) -> bool:
    """Write contents to disk, stage them, and commit. Returns True if a commit was made."""
    for rel_path, content in contents.items():
        full = AUTOMATION_FRAMEWORK_DIR / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    for rel_path in contents:
        git(["add", rel_path], AUTOMATION_FRAMEWORK_DIR)
    rc, staged, _ = git(["diff", "--cached", "--name-only"], AUTOMATION_FRAMEWORK_DIR)
    if not staged.strip():
        return False
    rc, _, err = git(["commit", "-m", message], AUTOMATION_FRAMEWORK_DIR)
    if rc != 0:
        log(blocked(f"commit failed ({err.strip()[:160]})",
                    "no PR will be raised; the generated files stay in the "
                    "working tree",
                    f"git -C {AUTOMATION_FRAMEWORK_DIR} status"))
        return False
    return True


def create_branch_and_commit(gen_data: dict, fix_attempts_data: list) -> tuple:
    """
    Creates a PR branch with one commit per pipeline step:
      • Commit 1 — step-03: the AI-generated code as initially produced
      • Commit N — step-04 attempt N: each Claude fix attempt as its own commit

    This makes it easy to spot which step has quality issues by inspecting
    individual commits in the PR.  Returns (branch_name, tip_sha) or (None, None).
    """
    if not AUTOMATION_FRAMEWORK_DIR.exists():
        log(blocked(f"the automation repo was not found at {AUTOMATION_FRAMEWORK_DIR}",
                    "no PR will be raised",
                    "set FRAMEWORK_DIR, or WORKSPACE_DIR and GITHUB_REPO_AUTOMATION"))
        return None, None

    # Step-03 file contents (saved by 03_generate.py in files_content)
    step3_contents: dict = gen_data.get("files_content", {})
    if not step3_contents:
        # Fallback for older runs that don't have files_content
        step3_contents = {
            p: (AUTOMATION_FRAMEWORK_DIR / p).read_text()
            for p in gen_data.get("files_written", [])
            if (AUTOMATION_FRAMEWORK_DIR / p).exists()
        }

    attempts_with_fixes = [a for a in fix_attempts_data if a.get("fix_file_contents")]

    # Read the locator fingerprints step 04's run recorded, BEFORE the branch
    # checkout below — it is a `checkout -f`, so anything untracked in the working
    # tree is gone by the time there is a branch to commit onto. This is exactly
    # how NaukriLoginPage.json kept ending up untracked: the framework wrote it
    # during the green run, ship reset the tree, and the PR carried the new page
    # object with no record of what its locators matched when they worked.
    baseline_contents: dict = {}
    for path in baseline_store.promoted(AUTOMATION_FRAMEWORK_DIR):
        try:
            rel = path.relative_to(AUTOMATION_FRAMEWORK_DIR).as_posix()
            baseline_contents[rel] = path.read_text()
        except OSError as exc:
            log(f"Could not read baseline {path.name} ({exc}) — skipping it")

    if not step3_contents and not attempts_with_fixes:
        log("No files to commit")
        return None, None

    if not AUTO_PUSH:
        # Dry run: put the files on disk and stop there.
        #
        # Branching would mean the `checkout -f` below, which discards whatever
        # the user has in progress — a rough thing to do to someone who asked
        # only to skip the PR. Committing would hide the diff they wanted to
        # read. So the generated code lands in the working tree, uncommitted,
        # on whatever branch they are on.
        latest = dict(step3_contents)
        for attempt in attempts_with_fixes:
            latest.update(attempt.get("fix_file_contents") or {})
        # Nothing was reset in this mode, so the baselines are already on disk
        # where the framework wrote them — no need to rewrite them here.
        for rel_path, content in latest.items():
            full = AUTOMATION_FRAMEWORK_DIR / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
        log(f"AUTO_PUSH=false — wrote {len(latest)} file(s) to the working tree, "
            f"uncommitted (no branch, no commit)")
        log(f"  review with: git -C {AUTOMATION_FRAMEWORK_DIR} status")
        return None, None

    # ── Reset to GITHUB_DEFAULT_BRANCH (or stay on current HEAD if blank) ────────
    timestamp     = datetime.now().strftime("%Y%m%d%H%M%S")
    branch_name   = f"{BRANCH_PREFIX}/{MODULE}-{timestamp}"
    feature_class = gen_data.get("feature_class", MODULE.capitalize())
    log(f"Creating branch: {branch_name}")

    if GITHUB_DEFAULT_BRANCH:
        # prepare_base makes origin/<base> exist and be current — which the old
        # `checkout -f` / `fetch origin` / `pull` sequence here could not do for
        # a branch this checkout had never seen, because a bare `git fetch`
        # against a single-branch clone never creates the missing ref. It also
        # always authenticates: the `pull` it replaces passed no token, so on a
        # checkout cloned by another agent (which strips the token from
        # .git/config) it hit GIT_TERMINAL_PROMPT=0 and failed on a private repo.
        prepared = workspace_helper.prepare_base(
            AUTOMATION_FRAMEWORK_DIR, GITHUB_ORG, GITHUB_REPO_AUTOMATION,
            GITHUB_TOKEN, GITHUB_DEFAULT_BRANCH, log=log)
        if not prepared["ok"]:
            log(blocked(
                f"could not prepare {GITHUB_DEFAULT_BRANCH} ({prepared['reason'][:160]})",
                "no PR will be raised; aborting rather than branching from a "
                "stale base",
                f"git -C {AUTOMATION_FRAMEWORK_DIR} status"))
            return None, None
        # Safe to force here, unlike in the other agents: every file below is
        # rewritten from the audit JSON, so there is no working-tree state to lose.
        moved = workspace_helper.checkout_base(
            AUTOMATION_FRAMEWORK_DIR, prepared["branch"], prepared["sha"], log=log)
        if not moved["ok"]:
            log(blocked(
                f"{moved['reason'][:160]}",
                "no PR will be raised; no branch was created",
                f"git -C {AUTOMATION_FRAMEWORK_DIR} status"))
            return None, None
    else:
        log("GITHUB_DEFAULT_BRANCH not set — branching from current HEAD")

    rc, _, err = git(["checkout", "-b", branch_name], AUTOMATION_FRAMEWORK_DIR)
    if rc != 0:
        log(blocked(f"could not create {branch_name} ({err.strip()[:160]})",
                    "no PR will be raised",
                    f"git -C {AUTOMATION_FRAMEWORK_DIR} status"))
        return None, None

    # ── Commit 1: step-03 generated files ────────────────────────────────────────
    fix_gate    = read_gate()
    test_passed = (fix_attempts_data[-1].get("passed", False)
                   if fix_attempts_data else False)
    test_status = ("tests pass" if test_passed
                   else "tests not run" if fix_gate == "skipped"
                   else "tests need review")

    # The URL properties belong in the PR: code that reads {feature}.login.url is
    # broken for every other checkout if the key never reaches the repo. Credentials
    # in that same file are the opposite and must NEVER be committed — so the
    # committed content is built from HEAD's copy plus the URL keys, never from the
    # working copy, which is where step 03/04 wrote this run's real credentials.
    url_props = gen_data.get("url_properties") or {}
    if url_props:
        props_path = properties_file.properties_path(AUTOMATION_FRAMEWORK_DIR)
        props_rel  = str(props_path.relative_to(AUTOMATION_FRAMEWORK_DIR))
        rc, committed, _ = git(["show", f"HEAD:{props_rel}"], AUTOMATION_FRAMEWORK_DIR)
        updated, filled, appended = properties_file.apply(
            committed if rc == 0 else "", url_props,
            f"{gen_data.get('feature', MODULE).capitalize()} URLs "
            f"(auto-added by test-authoring-agent)")
        if filled or appended:
            step3_contents[props_rel] = updated
            log(f"Committing {len(filled) + len(appended)} URL propert(ies) in "
                f"{props_rel}: {', '.join(sorted({**filled, **appended}))}")

    if step3_contents:
        msg = (
            f"[Authoring Agent]: First draft for {MODULE}\n\n"
            f"AI-generated test code — review before merge\n"
            f"Session: {SESSION_ID}  Files: {len(step3_contents)}\n\n"
            f"Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        )
        if _stage_and_commit(step3_contents, msg):
            rc, sha, _ = git(["rev-parse", "--short", "HEAD"], AUTOMATION_FRAMEWORK_DIR)
            log(f"Committed step-03 ({len(step3_contents)} file(s)): {sha.strip()}")

    # ── Commits 2+: one per fix attempt ──────────────────────────────────────────
    for attempt_data in attempts_with_fixes:
        n            = attempt_data.get("attempt", "?")
        fix_contents = attempt_data.get("fix_file_contents", {})
        msg = (
            f"[Authoring Agent]: Fix attempt-{n} for {MODULE}\n\n"
            f"Claude-generated fix — patched: {list(fix_contents.keys())}\n"
            f"Session: {SESSION_ID}\n\n"
            f"Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        )
        if _stage_and_commit(fix_contents, msg):
            rc, sha, _ = git(["rev-parse", "--short", "HEAD"], AUTOMATION_FRAMEWORK_DIR)
            log(f"Committed step-04 attempt-{n} ({len(fix_contents)} file(s)): {sha.strip()}")

    # ── Final commit: the locator fingerprints the run recorded ──────────────────
    #
    # Last, because they describe the state the test finally reached. Only the
    # ones whose substance differs from HEAD: the framework rewrites every
    # baseline it loads with a fresh `recordedAt`, so committing on raw file
    # change would put an otherwise-empty diff in every single PR.
    baseline_changed = baseline_store.changed(AUTOMATION_FRAMEWORK_DIR, baseline_contents)
    if baseline_changed:
        log(f"Committing {len(baseline_changed)} locator baseline(s): "
            f"{', '.join(Path(p).name for p in baseline_changed)}")
        msg = (
            f"[Authoring Agent]: Locator baselines for {MODULE}\n\n"
            f"Element fingerprints recorded while the generated test ran. They\n"
            f"describe what each page object locator matched when it worked, so a\n"
            f"later drift can be diagnosed by comparison rather than by guesswork.\n"
            f"Session: {SESSION_ID}\n\n"
            f"Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        )
        if _stage_and_commit(baseline_changed, msg):
            rc, sha, _ = git(["rev-parse", "--short", "HEAD"], AUTOMATION_FRAMEWORK_DIR)
            log(f"Committed baselines ({len(baseline_changed)} file(s)): {sha.strip()}")
    elif baseline_contents:
        log(f"{len(baseline_contents)} baseline(s) on disk, none changed — nothing to commit")
    else:
        log("No locator baselines were recorded by this run")
    gen_data["baselines_committed"] = sorted(baseline_changed)

    rc, final_sha, _ = git(["rev-parse", "--short", "HEAD"], AUTOMATION_FRAMEWORK_DIR)
    return branch_name, final_sha.strip()


def _read_original_test_case(plan: dict) -> str:
    """Best-effort read of the raw input file's original text, for showing
    the actual test case in the PR description.

    Falls back gracefully — not a hard requirement, just advisory context for
    reviewers: the file may already have moved to queue/processed/ by the
    time this runs (e.g. a retried session's step 05 runs again well after
    the original run already moved it).
    """
    raw_path = plan.get("_input_file")
    if not raw_path:
        return ""
    candidates = [Path(raw_path), AGENT_DIR / "queue" / "processed" / Path(raw_path).name]
    for candidate in candidates:
        if candidate.exists():
            try:
                return candidate.read_text()
            except OSError:
                continue
    return ""


def push_and_create_pr(branch_name: str, gen_data: dict, fix_data: dict) -> tuple:
    """Push branch and create GitHub PR.

    Returns (pr_url, ship_status, ship_detail):
      ship_status is one of:
        "shipped"    — pushed and PR created successfully
        "dry_run"    — AUTO_PUSH=false or GITHUB_ORG unset; nothing attempted,
                       not a failure — this is an intentional no-op mode
        "push_failed"— push was attempted and failed; the code exists on a
                       local branch but nobody can see or review it — a real
                       failure, must not be reported as APPROVED
        "pr_failed"  — push succeeded but `gh pr create` failed — branch is
                       on GitHub but no PR was opened; also a real failure
      ship_detail carries the actual error text for the failure cases (empty
      otherwise) — so a human (or the Slack alert / audit trail) sees WHY,
      not just that something failed.
    """
    if not AUTO_PUSH:
        log("AUTO_PUSH=false — skipping push (dry-run)")
        return None, "dry_run", ""

    if not GITHUB_ORG:
        log("GITHUB_ORG not set — skipping PR")
        return None, "dry_run", ""

    full_repo = f"{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}"
    log(f"Pushing {branch_name} to {full_repo}...")

    rc, _, err = git(["push", "-u", "origin", branch_name], AUTOMATION_FRAMEWORK_DIR, use_token=True)
    if rc != 0:
        log(blocked(f"push of {branch_name} was rejected ({err.strip()[:160]})",
                    "no PR will be raised; the commits are on the local branch",
                    f"git -C {AUTOMATION_FRAMEWORK_DIR} log --oneline "
                    f"{GITHUB_DEFAULT_BRANCH}..{branch_name}"))
        return None, "push_failed", err

    files_written = gen_data.get("files_written", [])
    files_fixed   = [f for f in fix_data.get("fixes_applied", []) if not f.startswith("auto:")]
    feature_class = gen_data.get("feature_class", MODULE.capitalize())
    test_type     = gen_data.get("test_type", "api")
    test_passed   = fix_data.get("passed", False)
    fix_attempts  = fix_data.get("attempt", 1)

    # PR title
    if test_passed:
        pr_title = f"Authoring Agent: {feature_class} automation for {MODULE} [done]"
    else:
        pr_title = f"Authoring Agent: {feature_class} automation for {MODULE} [needs review]"

    # Files section — show generated + fixed separately so reviewers can tell what changed
    baselines     = gen_data.get("baselines_committed") or []
    all_committed = list(dict.fromkeys(files_written + files_fixed + baselines))
    if all_committed:
        gen_lines   = [f"- `{f}`" for f in files_written] or ["_(none)_"]
        fix_lines   = [f"- `{f}` _(auto-fixed)_" for f in files_fixed if f not in files_written]
        # Named in the PR rather than left as a silent extra commit: a reviewer
        # who sees a fingerprint file should know it was recorded by this run,
        # not hand-written.
        base_lines  = [f"- `{f}` _(locator baseline recorded by the run)_"
                       for f in baselines if f not in files_written]
        files_lines = gen_lines + fix_lines + base_lines
        files_section = "\n".join(files_lines)
    else:
        files_section = "_(none)_"

    # Test result section
    if test_passed:
        test_section = "✅ Generated test was run and passed before this PR was created."
    elif fix_data.get("stuck"):
        # Distinct from the infra-skip case below: this test genuinely failed rather
        # than never getting a fair shot. The loop stopped short of its budget because
        # a further attempt could not have differed from one already made — the exact
        # reason varies (the same failure location after a fix, the same guard rejecting
        # twice, or the model reporting it has no fix to offer), so quote the one the
        # fix step actually recorded rather than assuming which it was.
        why = (fix_data.get("reason") or "").strip()
        test_section = (
            f"❌ Test is reproducibly failing after {fix_attempts} fix attempt(s), and "
            f"the fix loop stopped early rather than spend the rest of its budget: "
            f"{why or 'no further fix was available'}. "
            "See root_cause and .fix-history.json in the audit trail for everything "
            "already tried. Please review manually."
        )
    elif fix_data.get("skipped"):
        test_section = "⚠️ Test could not be run (Maven not available or infra issue)."
    else:
        test_section = (
            f"❌ Test is still failing after {fix_attempts} fix attempt(s). "
            "Please review the generated code manually."
        )

    # Original test case — masked and shown up top so a reviewer sees WHAT was
    # actually asked for before anything else, without needing to dig through
    # the audit trail. Best-effort: absent entirely if the input file can no
    # longer be found (see _read_original_test_case) or is empty.
    test_case_section = ""
    parse_path = AUDIT_DIR / "01-parse.json"
    if parse_path.exists():
        try:
            plan = json.loads(parse_path.read_text())
        except (OSError, json.JSONDecodeError):
            plan = {}
        raw_case = _read_original_test_case(plan)
        if raw_case.strip():
            masked_case = mask_credentials(raw_case, credentials_from_plan(plan))
            test_case_section = f"""### Test Case
<details open>
<summary>Original request (credentials masked)</summary>

```
{masked_case.strip()}
```
</details>

"""

    # What the browser could not confirm. Split by who asked for it, because the
    # two need opposite things from a reviewer: one is a finding about the product,
    # the other is a note that the pipeline stopped short of inventing a test.
    kept_unverified = gen_data.get("kept_unverified_checks") or []
    dropped_checks  = gen_data.get("dropped_unverified_checks") or []
    checks_section = ""
    if kept_unverified:
        checks_section += (
            "### ⚠️ Asked for, but never seen on the page\n\n"
            "The test input asks for these, and step 02 could not observe any of "
            "them in the live UI. The assertions are generated at full strength, "
            "so **this test fails on purpose** — it is reporting that the product "
            "does not do what was asked. Decide whether this is a product bug or a "
            "test-case correction; do not fix it by weakening the assertion.\n\n"
            + "".join(f"- {c}\n" for c in kept_unverified) + "\n")
    if dropped_checks:
        checks_section += (
            "### Checks not generated\n\n"
            "These were added by the pipeline rather than requested, and the browser "
            "never saw the elements they assert on — so no locator, accessor or "
            "assertion was generated for them. If one of these is actually wanted, "
            "say so in the test input and re-run.\n\n"
            + "".join(f"- {c}\n" for c in dropped_checks) + "\n")

    pr_body = f"""## QA Auto-Create — {feature_class}

{test_case_section}{checks_section}### Summary
| | Value |
|---|---|
| Module | {feature_class} |
| Test type | {test_type} |
| Files generated | {len(files_written)} |
| Fix attempts | {fix_attempts} |
| Test result | {'✅ Passed' if test_passed else '❌ Needs review'} |

### Generated Files

{files_section}

### Validation

{test_section}

### How to review
1. Verify locators match the actual DOM (check `[data-cy='...']` attributes)
2. Confirm `allocateUser()` uses the correct `Module` enum value for this module
3. Ensure the API endpoint paths match the actual backend routes
4. Run locally: `mvn test -Dtest={gen_data.get('test_class', '')}#{gen_data.get('test_method', '')} -Denvironment=staging`

> Audit trail: `{AUDIT_DIR.name}`
> 🤖 Generated by test-authoring-agent
"""

    log("Creating PR...")
    pr_url, pr_err = create_pr(
        workspace=AUTOMATION_FRAMEWORK_DIR,
        full_repo=full_repo,
        title=pr_title,
        body=pr_body,
        branch=branch_name,
        base=GITHUB_DEFAULT_BRANCH,
        reviewers=GITHUB_PR_REVIEWERS,
    )
    if not pr_url:
        log(blocked(f"PR creation failed ({str(pr_err).strip()[:160]})",
                    f"no PR will be raised; {branch_name} is pushed and can be "
                    f"opened by hand",
                    f"https://github.com/{full_repo}/pull/new/{branch_name}"))
        return None, "pr_failed", pr_err
    log(f"PR created: {pr_url}")
    return pr_url, "shipped", ""


def build_slack_message(gen_data: dict, fix_data: dict, pr_url: Optional[str], fix_gate: str,
                         ship_status: str = "dry_run", ship_detail: str = "") -> tuple:
    """Returns (channel, text)."""
    feature_class = gen_data.get("feature_class", MODULE.capitalize())
    files_count   = len(gen_data.get("files_written", []))
    test_passed   = fix_data.get("passed", False)
    ship_failed   = ship_status in ("push_failed", "pr_failed")

    if ship_failed:
        # Shipping itself failed — needs attention regardless of whether the
        # generated test passed; nobody can review code stuck on a local
        # branch. Distinct from (and takes priority over) a test-quality
        # problem, since it's the reason there's nothing to review at all.
        channel = SLACK_ALERT_CHANNEL or SLACK_NOTIFY_CHANNEL
        icon    = ":x:"
        what    = "push to GitHub" if ship_status == "push_failed" else "PR creation"
        status  = (f"generated and tests {'pass' if test_passed else 'ran'}, but {what} FAILED — "
                   "code is stuck on a local branch, needs manual intervention")
    elif fix_gate == "stuck":
        # This is a genuine, reproducible failure — must go to the alert channel
        # like any other failure, not the "generated (test not run)" notify-only
        # path, which would hide a known-broken test from whoever watches alerts.
        channel = SLACK_ALERT_CHANNEL or SLACK_NOTIFY_CHANNEL
        icon    = ":warning:"
        _why = (fix_data.get("reason") or "").strip()
        status  = ("generated but stuck — test reproducibly failing, fix attempts stopped "
                   "early, needs review" + (f" ({_why})" if _why else ""))
    elif fix_gate == "skipped":
        channel = SLACK_NOTIFY_CHANNEL
        icon    = ":large_yellow_circle:"
        status  = "generated (test not run)"
    elif test_passed:
        channel = SLACK_NOTIFY_CHANNEL
        icon    = ":white_check_mark:"
        status  = "generated and tests pass"
    else:
        channel = SLACK_ALERT_CHANNEL or SLACK_NOTIFY_CHANNEL
        icon    = ":x:"
        status  = "generated but tests failing — needs review"

    lines = [
        f"{icon} *QA Auto-Create* — `{feature_class}`",
        f"Status: {status}",
        f"Files generated: {files_count}",
    ]
    if pr_url:
        lines.append(f"PR: {pr_url}")
    elif ship_status == "dry_run":
        lines.append("_(AUTO_PUSH=false or no GitHub org configured — no PR created)_")
    elif ship_failed:
        lines.append(f"_Ship error: {ship_detail[:300]}_" if ship_detail
                     else "_(no further detail captured — see audit trail)_")
    lines.append(f"_Audit: `{AUDIT_DIR.name}`_")

    return channel, "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    gen_data = load_json("03-generate.json")
    fix_data = load_json("04-run-and-fix.json", required=False)
    fix_gate = read_gate()

    feature_class = gen_data.get("feature_class", MODULE.capitalize())
    test_passed   = fix_data.get("passed", False)

    log(f"Feature:   {feature_class}")
    log(f"Fix gate:  {fix_gate}")
    log(f"Files:     {len(gen_data.get('files_written', []))}")

    branch_name = None
    commit_sha  = None
    pr_url      = None
    slack_sent  = False
    ship_status = "dry_run"  # overwritten below if a push/PR was actually attempted
    ship_detail = ""

    # Load per-attempt audit files for per-step commits in create_branch_and_commit
    fix_attempts_data = []
    i = 1
    while True:
        attempt_path = AUDIT_DIR / f"04-run-and-fix-attempt-{i}.json"
        if not attempt_path.exists():
            break
        fix_attempts_data.append(json.loads(attempt_path.read_text()))
        i += 1
    if fix_attempts_data:
        log(f"Loaded {len(fix_attempts_data)} fix-attempt file(s) for per-step commits")

    files_generated = gen_data.get("files_written", [])
    files_fixed     = [f for f in fix_data.get("fixes_applied", []) if not f.startswith("auto:")]
    files_written   = list(dict.fromkeys(files_generated + files_fixed))  # for result JSON
    if files_generated or files_fixed:
        branch_name, commit_sha = create_branch_and_commit(gen_data, fix_attempts_data)

        if branch_name and commit_sha:
            pr_url, ship_status, ship_detail = push_and_create_pr(branch_name, gen_data, fix_data)
        elif branch_name and not commit_sha:
            log("Nothing staged — skipping push")
            # Nothing to ship, not a failure — e.g. a resumed session where
            # this step already committed everything on a prior attempt.
            ship_status = "dry_run"
        else:
            log("Branch creation failed — skipping push")
            # Code generated fine but couldn't even get a local branch/commit
            # made — same category as a failed push: it exists, but nobody
            # can see or review it. A real failure, not a dry run.
            ship_status = "push_failed"
            ship_detail = "git branch/commit creation failed — see log above"
    else:
        log("No files generated — skipping git operations")

    # Slack
    if SLACK_NOTIFY_CHANNEL or SLACK_ALERT_CHANNEL:
        channel, text = build_slack_message(gen_data, fix_data, pr_url, fix_gate, ship_status, ship_detail)
        if channel and text:
            slack_sent = send_slack(channel, text)

    # Verdict gate — APPROVED requires BOTH the test passing (or being
    # deliberately skipped) AND shipping actually succeeding (or being a
    # deliberate dry-run). A push/PR/branch failure means the generated code
    # exists but nobody can see or review it — that's never APPROVED,
    # regardless of whether the test itself passed.
    ship_failed = ship_status in ("push_failed", "pr_failed")
    # A run that could not observe something the input asked for is never
    # APPROVED, even if the suite is green — green here means the pipeline
    # declined to assert on it, which is precisely the thing a human has to look
    # at. Same for a fix that was rejected for weakening a test.
    weakening_rejected = [r for r in (fix_data.get("fix_rejections") or [])
                          if "assertion_conservation" in str(r.get("reason", ""))]
    kept_unverified = gen_data.get("kept_unverified_checks") or []
    honest = not kept_unverified and not weakening_rejected
    # `fix_gate == "skipped"` means no test ever ran (an infra failure). That was
    # treated as APPROVED, which reads as "verified" for something never executed.
    ran_and_passed = test_passed
    verdict = ("APPROVED" if (ran_and_passed and honest and not ship_failed)
               else "NEEDS-REVIEW")
    (AUDIT_DIR / ".verdict").write_text(verdict)
    if kept_unverified:
        log(f"NEEDS-REVIEW: {len(kept_unverified)} requested check(s) could not be "
            f"observed in the UI — the test asserts them and fails on purpose.")
    if weakening_rejected:
        log(f"NEEDS-REVIEW: {len(weakening_rejected)} fix attempt(s) were rejected "
            f"for weakening an assertion.")
    if fix_gate == "skipped":
        log("NEEDS-REVIEW: no test ever ran (infrastructure) — nothing was verified.")
    (AUDIT_DIR / ".verdict").write_text(verdict)
    log(f"Verdict: {verdict}")
    if ship_failed:
        log(f"  → Ship failed ({ship_status}): {ship_detail}")

    # Write result JSON
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp":        ts,
        "module":           MODULE,
        "feature_class":    feature_class,
        "fix_gate":         fix_gate,
        "test_passed":      test_passed,
        "branch":           branch_name,
        "commit":           commit_sha,
        "pr_url":           pr_url,
        "ship_status":      ship_status,
        "ship_detail":      ship_detail,
        "verdict":          verdict,
        "slack_notified":   slack_sent,
        "files_count":      len(files_written),
    }
    (AUDIT_DIR / "05-ship.json").write_text(json.dumps(result, indent=2))

    # Markdown summary
    md_lines = [
        "# Ship Results",
        "",
        f"**Feature:**  {feature_class}  ",
        f"**Timestamp:** {ts}  ",
        f"**Verdict:**  `{verdict}`",
        "",
        "## Summary",
        "",
        "| | Value |",
        "|---|---|",
        f"| Branch | `{branch_name or 'N/A'}` |",
        f"| Commit | `{commit_sha or 'N/A'}` |",
        f"| PR | {pr_url or 'Not created'} |",
        f"| Test result | {'✅ Passed' if test_passed else '⚠️ Not run' if fix_gate == 'skipped' else '❌ Failed'} |",
        f"| Ship status | {ship_status} |",
        f"| Slack | {'Sent' if slack_sent else 'Skipped'} |",
    ]
    if ship_failed:
        md_lines += [
            "",
            "## ⚠️ Ship Failed",
            "",
            f"Generated code was committed to a local branch (`{branch_name}`), but "
            f"{'the push to GitHub' if ship_status == 'push_failed' else 'PR creation'} "
            "failed — nobody can see or review it until this is resolved:",
            "",
            "```",
            ship_detail or "(no further detail captured)",
            "```",
        ]
    (AUDIT_DIR / "05-ship.md").write_text("\n".join(md_lines))

    log(f"Done — verdict={verdict} | PR={pr_url or 'none'}")


if __name__ == "__main__":
    main()
