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

from shared.log import log as _log
from shared.slack import send_slack as _send_slack
from shared.git import run_git as _run_git
from shared.github import create_pr

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR          = Path(os.environ.get("WORKSPACE_DIR", str(REPO_ROOT.parent)))
AUTOMATION_FRAMEWORK_DIR          = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Jarvis")

AUTO_PUSH              = os.environ.get("AUTO_PUSH", "true").lower() == "true"
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "Jarvis")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
GITHUB_PR_REVIEWERS    = [r.strip() for r in os.environ.get("GITHUB_PR_REVIEWERS", "").split(",") if r.strip()]
BRANCH_PREFIX          = os.environ.get("AUTOCREATE_BRANCH_PREFIX", "feat/qa-autocreate")

SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
SLACK_ALERT_CHANNEL  = os.environ.get("SLACK_ALERT_CHANNEL", "")

SESSION_ID = os.environ.get("SESSION_ID", AUDIT_DIR.name)
FEATURE    = os.environ.get("FEATURE", "unknown")

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None: _log("05-ship", msg)

def send_slack(channel: str, text: str) -> bool:
    return _send_slack(SLACK_BOT_TOKEN, channel, text)

def git(args: list, cwd: Path) -> tuple:
    """Thin wrapper returning (returncode, stdout, stderr) for backward compat."""
    ok, stdout, stderr = _run_git(args, cwd)
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
        log(f"ERROR: Commit failed: {err}")
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
        log(f"ERROR: Automation framework repo not found: {AUTOMATION_FRAMEWORK_DIR}")
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

    if not step3_contents and not attempts_with_fixes:
        log("No files to commit")
        return None, None

    # ── Reset to GITHUB_DEFAULT_BRANCH (or stay on current HEAD if blank) ────────
    timestamp     = datetime.now().strftime("%Y%m%d%H%M%S")
    branch_name   = f"{BRANCH_PREFIX}/{FEATURE}-{timestamp}"
    feature_class = gen_data.get("feature_class", FEATURE.capitalize())
    log(f"Creating branch: {branch_name}")

    if GITHUB_DEFAULT_BRANCH:
        rc, _, err = git(["checkout", "-f", GITHUB_DEFAULT_BRANCH], AUTOMATION_FRAMEWORK_DIR)
        if rc != 0:
            log(f"WARNING: checkout -f failed ({err.strip()!r}), trying fetch + retry")
            git(["fetch", "origin"], AUTOMATION_FRAMEWORK_DIR)
            rc, _, err = git(["checkout", "-f", GITHUB_DEFAULT_BRANCH], AUTOMATION_FRAMEWORK_DIR)
            if rc != 0:
                log(f"ERROR: Could not checkout {GITHUB_DEFAULT_BRANCH}: {err}")
                return None, None
        rc, _, err = git(["pull", "origin", GITHUB_DEFAULT_BRANCH], AUTOMATION_FRAMEWORK_DIR)
        if rc != 0:
            log(f"ERROR: Pull from origin/{GITHUB_DEFAULT_BRANCH} failed — aborting to avoid stale branch: {err}")
            return None, None
    else:
        log("GITHUB_DEFAULT_BRANCH not set — branching from current HEAD")

    rc, _, err = git(["checkout", "-b", branch_name], AUTOMATION_FRAMEWORK_DIR)
    if rc != 0:
        log(f"ERROR: Could not create branch: {err}")
        return None, None

    # ── Commit 1: step-03 generated files ────────────────────────────────────────
    fix_gate    = read_gate()
    test_passed = (fix_attempts_data[-1].get("passed", False)
                   if fix_attempts_data else False)
    test_status = ("tests pass" if test_passed
                   else "tests not run" if fix_gate == "skipped"
                   else "tests need review")

    if step3_contents:
        msg = (
            f"[Authoring Agent]: First draft for {FEATURE}\n\n"
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
            f"[Authoring Agent]: Fix attempt-{n} for {FEATURE}\n\n"
            f"Claude-generated fix — patched: {list(fix_contents.keys())}\n"
            f"Session: {SESSION_ID}\n\n"
            f"Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        )
        if _stage_and_commit(fix_contents, msg):
            rc, sha, _ = git(["rev-parse", "--short", "HEAD"], AUTOMATION_FRAMEWORK_DIR)
            log(f"Committed step-04 attempt-{n} ({len(fix_contents)} file(s)): {sha.strip()}")

    rc, final_sha, _ = git(["rev-parse", "--short", "HEAD"], AUTOMATION_FRAMEWORK_DIR)
    return branch_name, final_sha.strip()


def push_and_create_pr(branch_name: str, gen_data: dict, fix_data: dict) -> Optional[str]:
    """Push branch and create GitHub PR. Returns PR URL or None."""
    if not AUTO_PUSH:
        log("AUTO_PUSH=false — skipping push (dry-run)")
        return None

    if not GITHUB_ORG:
        log("GITHUB_ORG not set — skipping PR")
        return None

    full_repo = f"{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}"
    log(f"Pushing {branch_name} to {full_repo}...")

    rc, _, err = git(["push", "-u", "origin", branch_name], AUTOMATION_FRAMEWORK_DIR)
    if rc != 0:
        log(f"Push failed: {err}")
        return None

    files_written = gen_data.get("files_written", [])
    files_fixed   = [f for f in fix_data.get("fixes_applied", []) if not f.startswith("auto:")]
    feature_class = gen_data.get("feature_class", FEATURE.capitalize())
    test_type     = gen_data.get("test_type", "api")
    test_passed   = fix_data.get("passed", False)
    fix_attempts  = fix_data.get("attempt", 1)

    # PR title
    if test_passed:
        pr_title = f"Authoring Agent: {feature_class} automation for {FEATURE} [done]"
    else:
        pr_title = f"Authoring Agent: {feature_class} automation for {FEATURE} [needs review]"

    # Files section — show generated + fixed separately so reviewers can tell what changed
    all_committed = list(dict.fromkeys(files_written + files_fixed))
    if all_committed:
        gen_lines   = [f"- `{f}`" for f in files_written] or ["_(none)_"]
        fix_lines   = [f"- `{f}` _(auto-fixed)_" for f in files_fixed if f not in files_written]
        files_lines = gen_lines + fix_lines
        files_section = "\n".join(files_lines)
    else:
        files_section = "_(none)_"

    # Test result section
    if test_passed:
        test_section = "✅ Generated test was run and passed before this PR was created."
    elif fix_data.get("skipped"):
        test_section = "⚠️ Test could not be run (Maven not available or infra issue)."
    else:
        test_section = (
            f"❌ Test is still failing after {fix_attempts} fix attempt(s). "
            "Please review the generated code manually."
        )

    pr_body = f"""## QA Auto-Create — {feature_class}

### Summary
| | Value |
|---|---|
| Feature | {feature_class} |
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
2. Confirm `allocateUser()` uses the correct `Feature` enum value for this module
3. Ensure the API endpoint paths match the actual backend routes
4. Run locally: `mvn test -Dtest={gen_data.get('test_class', '')}#{gen_data.get('test_method', '')} -Denvironment=staging`

> Audit trail: `{AUDIT_DIR.name}`
> 🤖 Generated by test-authoring-agent
"""

    log("Creating PR...")
    pr_url = create_pr(
        workspace=AUTOMATION_FRAMEWORK_DIR,
        full_repo=full_repo,
        title=pr_title,
        body=pr_body,
        branch=branch_name,
        base=GITHUB_DEFAULT_BRANCH,
        reviewers=GITHUB_PR_REVIEWERS,
    )
    if not pr_url:
        log("PR creation failed")
        return None
    log(f"PR created: {pr_url}")
    return pr_url


def build_slack_message(gen_data: dict, fix_data: dict, pr_url: Optional[str], fix_gate: str) -> tuple:
    """Returns (channel, text)."""
    feature_class = gen_data.get("feature_class", FEATURE.capitalize())
    files_count   = len(gen_data.get("files_written", []))
    test_passed   = fix_data.get("passed", False)

    if fix_gate == "skipped":
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
    elif not AUTO_PUSH:
        lines.append("_(AUTO_PUSH=false — no PR created)_")
    lines.append(f"_Audit: `{AUDIT_DIR.name}`_")

    return channel, "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    gen_data = load_json("03-generate.json")
    fix_data = load_json("04-run-and-fix.json", required=False)
    fix_gate = read_gate()

    feature_class = gen_data.get("feature_class", FEATURE.capitalize())
    test_passed   = fix_data.get("passed", False)

    log(f"Feature:   {feature_class}")
    log(f"Fix gate:  {fix_gate}")
    log(f"Files:     {len(gen_data.get('files_written', []))}")

    branch_name = None
    commit_sha  = None
    pr_url      = None
    slack_sent  = False

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
            pr_url = push_and_create_pr(branch_name, gen_data, fix_data)
        elif branch_name and not commit_sha:
            log("Nothing staged — skipping push")
        else:
            log("Branch creation failed — skipping push")
    else:
        log("No files generated — skipping git operations")

    # Slack
    if SLACK_NOTIFY_CHANNEL or SLACK_ALERT_CHANNEL:
        channel, text = build_slack_message(gen_data, fix_data, pr_url, fix_gate)
        if channel and text:
            slack_sent = send_slack(channel, text)

    # Verdict gate
    verdict = "APPROVED" if (test_passed or fix_gate == "skipped") else "NEEDS-REVIEW"
    (AUDIT_DIR / ".verdict").write_text(verdict)
    log(f"Verdict: {verdict}")

    # Write result JSON
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp":        ts,
        "feature":          FEATURE,
        "feature_class":    feature_class,
        "fix_gate":         fix_gate,
        "test_passed":      test_passed,
        "branch":           branch_name,
        "commit":           commit_sha,
        "pr_url":           pr_url,
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
        f"| Slack | {'Sent' if slack_sent else 'Skipped'} |",
    ]
    (AUDIT_DIR / "05-ship.md").write_text("\n".join(md_lines))

    log(f"Done — verdict={verdict} | PR={pr_url or 'none'}")


if __name__ == "__main__":
    main()
