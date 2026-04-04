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
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR          = Path(os.environ.get("WORKSPACE_DIR", str(REPO_ROOT.parent)))
THANOS_PW_DIR          = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Thanos-pw")

AUTO_PUSH              = os.environ.get("AUTO_PUSH", "true").lower() == "true"
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "Thanos-pw")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
GITHUB_PR_REVIEWERS    = [r.strip() for r in os.environ.get("GITHUB_PR_REVIEWERS", "").split(",") if r.strip()]
BRANCH_PREFIX          = os.environ.get("AUTOCREATE_BRANCH_PREFIX", "feat/qa-autocreate")

SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
SLACK_ALERT_CHANNEL  = os.environ.get("SLACK_ALERT_CHANNEL", "")

SESSION_ID = os.environ.get("SESSION_ID", AUDIT_DIR.name)
FEATURE    = os.environ.get("FEATURE", "unknown")

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [05-ship] {msg}", flush=True)


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


def git(args: list, cwd: Path) -> tuple:
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd), capture_output=True, text=True, timeout=60
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def send_slack(channel: str, text: str) -> bool:
    if not SLACK_BOT_TOKEN or not channel:
        return False
    payload = {"channel": channel, "text": text}
    try:
        req = Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        log(f"Slack send failed: {e}")
        return False


def create_branch_and_commit(gen_data: dict, fix_data: dict) -> tuple:
    """
    Creates a branch, stages only the generated files, commits, and returns
    (branch_name, commit_sha). Returns (None, None) on failure.
    """
    files_written = gen_data.get("files_written", [])
    if not files_written:
        log("No files to commit")
        return None, None

    if not THANOS_PW_DIR.exists():
        log(f"ERROR: Thanos-pw not found: {THANOS_PW_DIR}")
        return None, None

    # Build branch name
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    branch_name = f"{BRANCH_PREFIX}/{FEATURE}-{timestamp}"
    feature_class = gen_data.get("feature_class", FEATURE.capitalize())

    log(f"Creating branch: {branch_name}")

    # Ensure we're on the default branch first
    rc, _, err = git(["checkout", GITHUB_DEFAULT_BRANCH], THANOS_PW_DIR)
    if rc != 0:
        log(f"WARNING: Could not checkout {GITHUB_DEFAULT_BRANCH}: {err}")

    rc, _, err = git(["pull", "origin", GITHUB_DEFAULT_BRANCH], THANOS_PW_DIR)
    if rc != 0:
        log(f"WARNING: Pull failed: {err}")

    rc, _, err = git(["checkout", "-b", branch_name], THANOS_PW_DIR)
    if rc != 0:
        log(f"ERROR: Could not create branch: {err}")
        return None, None

    # Stage only the generated files
    for rel_path in files_written:
        full_path = THANOS_PW_DIR / rel_path
        if full_path.exists():
            rc, _, err = git(["add", rel_path], THANOS_PW_DIR)
            if rc != 0:
                log(f"WARNING: Could not stage {rel_path}: {err}")

    # Check if there's anything staged
    rc, status_out, _ = git(["diff", "--cached", "--name-only"], THANOS_PW_DIR)
    if not status_out.strip():
        log("Nothing to commit — files unchanged")
        return branch_name, None

    # Commit
    fix_gate    = read_gate()
    test_passed = fix_data.get("passed", False)
    if test_passed:
        test_status = "tests pass"
    elif fix_gate == "skipped":
        test_status = "tests not run"
    else:
        test_status = "tests need review"
    commit_msg = (
        f"feat(automation): add {feature_class} test automation — {test_status}\n\n"
        f"Generated by qa-auto-create agent\n"
        f"Session: {SESSION_ID}\n"
        f"Files: {len(files_written)}\n\n"
        f"Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
    )

    rc, _, err = git(["commit", "-m", commit_msg], THANOS_PW_DIR)
    if rc != 0:
        log(f"ERROR: Commit failed: {err}")
        return branch_name, None

    rc, sha, _ = git(["rev-parse", "--short", "HEAD"], THANOS_PW_DIR)
    log(f"Committed: {sha}")
    return branch_name, sha


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

    rc, _, err = git(["push", "-u", "origin", branch_name], THANOS_PW_DIR)
    if rc != 0:
        log(f"Push failed: {err}")
        return None

    files_written = gen_data.get("files_written", [])
    feature_class = gen_data.get("feature_class", FEATURE.capitalize())
    test_type     = gen_data.get("test_type", "api")
    test_passed   = fix_data.get("passed", False)
    fix_attempts  = fix_data.get("attempt", 1)

    # PR title
    if test_passed:
        pr_title = f"feat(automation): add {feature_class} test automation"
    else:
        pr_title = f"feat(automation): add {feature_class} test automation [needs review]"

    # Files section
    files_section = "\n".join(f"- `{f}`" for f in files_written) or "_(none)_"

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
> 🤖 Generated by qa-auto-create
"""

    pr_cmd = [
        "gh", "pr", "create",
        "--repo", full_repo,
        "--title", pr_title,
        "--body", pr_body,
        "--base", GITHUB_DEFAULT_BRANCH,
        "--head", branch_name,
    ]
    for reviewer in GITHUB_PR_REVIEWERS:
        pr_cmd += ["--reviewer", reviewer]

    log("Creating PR...")
    result = subprocess.run(pr_cmd, cwd=str(THANOS_PW_DIR), capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        log(f"PR creation failed: {result.stderr[:500]}")
        return None

    pr_url = result.stdout.strip()
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

    files_written = gen_data.get("files_written", [])
    if files_written:
        branch_name, commit_sha = create_branch_and_commit(gen_data, fix_data)

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
