#!/usr/bin/env python3
"""
Step 02 — Ship
Push the fix branch and create a GitHub PR. Send Slack notification.

Reads: audit/<session>/01-fix.json + .fix-passed gate
No AI calls. No code changes.
"""

import os, sys, json, subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

import warnings, urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
import logging
logging.basicConfig(level=logging.WARNING)

from shared.log import log as _log
from shared.slack import send_slack as _send_slack
from shared.github import create_pr
def log(msg): _log("ship", msg)
def send_slack(channel: str, text: str) -> bool:
    return _send_slack(SLACK_BOT_TOKEN, channel, text)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

AUTO_PUSH              = os.environ.get("AUTO_PUSH", "true").lower() == "true"
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
GITHUB_PR_REVIEWERS    = [r.strip() for r in os.environ.get("GITHUB_PR_REVIEWERS", "").split(",") if r.strip()]
WORKSPACE_DIR          = os.environ.get("WORKSPACE_DIR", str(REPO_ROOT.parent))

SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
SLACK_ALERT_CHANNEL  = os.environ.get("SLACK_ALERT_CHANNEL", "")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(filename, required=True):
    path = AUDIT_DIR / filename
    if not path.exists():
        if required:
            log(f"ERROR: {filename} not found")
            sys.exit(1)
        return {}
    return json.loads(path.read_text())


def read_gate() -> str:
    gate_path = AUDIT_DIR / ".fix-passed"
    return gate_path.read_text().strip() if gate_path.exists() else "skipped"


def get_workspace() -> Optional[Path]:
    workspace = Path(WORKSPACE_DIR)
    if GITHUB_REPO_AUTOMATION:
        repo_path = workspace / GITHUB_REPO_AUTOMATION
        if repo_path.exists():
            return repo_path
    return None


def short_name(test_name: str) -> str:
    """Return the simple class.method portion of a fully-qualified test name."""
    parts = test_name.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else test_name




def push_and_create_pr(fix_data: dict, failed_fixes: list) -> Optional[str]:
    pr_branch = fix_data.get("pr_branch")
    if not pr_branch:
        log("No PR branch from fix step — skipping PR creation")
        return None

    if not AUTO_PUSH:
        log("AUTO_PUSH=false — skipping push (dry-run)")
        return None

    if not GITHUB_ORG or not GITHUB_REPO_AUTOMATION:
        log("GitHub config not set — skipping PR")
        return None

    workspace = get_workspace()
    if not workspace:
        log("Automation workspace not found — skipping PR")
        return None

    full_repo = f"{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}"
    build_tag = fix_data.get("build_tag", "unknown")
    fixes     = fix_data.get("fixes", [])
    total     = len(fixes) + len(failed_fixes)

    # Push branch
    log(f"Pushing {pr_branch} to {full_repo}...")
    result = subprocess.run(
        ["git", "push", "origin", pr_branch, "--force-with-lease"],
        cwd=str(workspace), capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        log(f"Push failed: {result.stderr[:500]}")
        return None

    # PR title reflects partial/full fix
    if failed_fixes:
        pr_title = f"fix(automation): {len(fixes)}/{total} locator fixes — {build_tag}"
    else:
        pr_title = f"fix(automation): {len(fixes)} locator fix(es) — {build_tag}"

    # Fixed tests section
    fixed_lines = "\n".join(
        f"- ✅ `{short_name(f['test_name'])}` — {f.get('fix_description', '')} (`{Path(f['target_file']).name}`)"
        for f in fixes
    ) or "_(none)_"

    # Failed tests section
    if failed_fixes:
        failed_lines = "\n".join(
            _failed_pr_line(f) for f in failed_fixes
        )
        needs_manual = f"""
### ❌ Needs Manual Fix ({len(failed_fixes)})

{failed_lines}
"""
    else:
        needs_manual = ""

    pr_body = f"""## QA Auto-Fix — {build_tag}

### Summary
| | Count |
|---|---|
| Queued for fix | {total} |
| ✅ Auto-fixed (tests pass) | {len(fixes)} |
| ❌ Could not fix | {len(failed_fixes)} |
| Fix attempts | {fix_data.get('fix_attempt', 1)} |

### ✅ Fixed ({len(fixes)})

{fixed_lines}
{needs_manual}
### Validation
Every fixed test was re-run locally and passed before this PR was created.

> Audit trail: `{AUDIT_DIR.name}`
> 🤖 Generated by test-healing-agent
"""

    log("Creating PR...")
    pr_url, pr_err = create_pr(
        workspace=workspace,
        full_repo=full_repo,
        title=pr_title,
        body=pr_body,
        branch=pr_branch,
        base=GITHUB_DEFAULT_BRANCH,
        reviewers=GITHUB_PR_REVIEWERS,
    )
    if not pr_url:
        log(f"PR creation failed: {pr_err}")
        return None
    log(f"PR created: {pr_url}")
    return pr_url


def _failed_pr_line(f: dict) -> str:
    status = f.get("status", "unknown")
    name   = short_name(f["test_name"])
    if status == "unfixable":
        return f"- ❌ `{name}` — Claude declared unfixable: {f.get('unfixable_reason', '')}"
    if status == "test_failed":
        return f"- ❌ `{name}` — fix applied but test still failing"
    if status == "no_file":
        return f"- ❌ `{name}` — test file not found in workspace"
    return f"- ❌ `{name}` — {status}"


def _build_slack_message(build_tag: str, fixes: list, failed_fixes: list,
                          pr_url: Optional[str], fix_gate: str, fix_attempt: int) -> Tuple[str, str]:
    """
    Build a rich Slack message. Returns (channel, text).
    Uses SLACK_ALERT_CHANNEL if any failures, SLACK_NOTIFY_CHANNEL otherwise.
    """
    total     = len(fixes) + len(failed_fixes)
    n_fixed   = len(fixes)
    n_failed  = len(failed_fixes)

    if fix_gate == "skipped":
        # Nothing to report — no noise
        return "", ""

    # ── Header line ──────────────────────────────────────────────────────────
    if n_fixed == total:
        icon    = ":white_check_mark:"
        summary = f"*{n_fixed}/{total} tests fixed*"
        channel = SLACK_NOTIFY_CHANNEL
    elif n_fixed > 0:
        icon    = ":large_yellow_circle:"
        summary = f"*{n_fixed}/{total} tests fixed — {n_failed} need manual attention*"
        channel = SLACK_ALERT_CHANNEL or SLACK_NOTIFY_CHANNEL
    else:
        icon    = ":x:"
        summary = f"*0/{total} tests could be fixed — all need manual attention*"
        channel = SLACK_ALERT_CHANNEL or SLACK_NOTIFY_CHANNEL

    lines = [f"{icon} *QA Auto-Fix* — `{build_tag}`", summary]
    if fix_attempt > 1:
        lines.append(f"_(attempted {fix_attempt} fix cycle(s))_")
    lines.append("")

    # ── Fixed tests ──────────────────────────────────────────────────────────
    if fixes:
        lines.append(f":white_check_mark: *Fixed ({n_fixed}):*")
        for f in fixes[:8]:
            lines.append(f"  • `{short_name(f['test_name'])}` — {f.get('fix_description', '')}")
        if n_fixed > 8:
            lines.append(f"  _...and {n_fixed - 8} more_")
        if pr_url:
            lines.append(f"  PR: {pr_url}")
        elif not AUTO_PUSH:
            lines.append("  _(AUTO_PUSH=false — no PR created)_")
        lines.append("")

    # ── Failed tests ─────────────────────────────────────────────────────────
    if failed_fixes:
        lines.append(f":x: *Could not fix ({n_failed}) — manual review required:*")
        for f in failed_fixes[:8]:
            status = f.get("status", "unknown")
            name   = short_name(f["test_name"])
            if status == "unfixable":
                reason = f.get("unfixable_reason", "unclear")
                lines.append(f"  • `{name}` — unfixable: {reason[:80]}")
            elif status == "test_failed":
                lines.append(f"  • `{name}` — fix applied but test still failing")
            elif status == "no_file":
                lines.append(f"  • `{name}` — test file not found in workspace")
            else:
                lines.append(f"  • `{name}` — {status}")
        if n_failed > 8:
            lines.append(f"  _...and {n_failed - 8} more_")
        lines.append("")

    lines.append(f"_Audit: `{AUDIT_DIR.name}`_")

    return channel, "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    fix_data     = load_json("01-fix.json")
    fix_gate     = read_gate()
    build_tag    = fix_data.get("build_tag", "unknown")
    fixes        = fix_data.get("fixes", [])
    failed_fixes = fix_data.get("failed_fixes", [])
    fix_attempt  = fix_data.get("fix_attempt", 1)

    log(f"Build tag:  {build_tag}")
    log(f"Fix gate:   {fix_gate}")
    log(f"Succeeded:  {len(fixes)} | Failed: {len(failed_fixes)}")

    pr_url         = None
    slack_notified = False

    # ── Create PR whenever any fix succeeded ──────────────────────────────────
    # Even a partial result (some fixed, some not) deserves a PR for what passed.
    if fixes:
        pr_url = push_and_create_pr(fix_data, failed_fixes)
    elif fix_gate == "skipped":
        log("No fixes attempted — skipping PR")
    else:
        log("No successful fixes — skipping PR")

    # ── Slack ─────────────────────────────────────────────────────────────────
    channel, text = _build_slack_message(
        build_tag, fixes, failed_fixes, pr_url, fix_gate, fix_attempt
    )
    if channel and text:
        slack_notified = send_slack(channel, text)

    # ── Write JSON ─────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp":      ts,
        "build_tag":      build_tag,
        "fix_gate":       fix_gate,
        "total_queued":   len(fixes) + len(failed_fixes),
        "succeeded":      len(fixes),
        "failed":         len(failed_fixes),
        "pr_url":         pr_url,
        "slack_notified": slack_notified,
        "slack_channel":  channel,
    }
    (AUDIT_DIR / "02-ship.json").write_text(json.dumps(result, indent=2))

    # ── Write Markdown ─────────────────────────────────────────────────────────
    total = len(fixes) + len(failed_fixes)
    md_lines = [
        "# Ship Results",
        "",
        f"**Build Tag:** {build_tag}  ",
        f"**Timestamp:** {ts}  ",
        f"**Fix Gate:** `{fix_gate}`",
        "",
        "## Summary",
        "",
        "| | Count |",
        "|---|---|",
        f"| Queued for fix | {total} |",
        f"| ✅ Fixed | {len(fixes)} |",
        f"| ❌ Could not fix | {len(failed_fixes)} |",
        f"| PR | {pr_url or 'Not created'} |",
        f"| Slack | {'Sent to ' + channel if slack_notified else 'Skipped'} |",
    ]
    if fixes:
        md_lines += ["", "## Fixed Tests", ""]
        for f in fixes:
            md_lines.append(f"- ✅ `{short_name(f['test_name'])}` — {f.get('fix_description', '')}")
    if failed_fixes:
        md_lines += ["", "## Could Not Fix", ""]
        for f in failed_fixes:
            md_lines.append(f"- ❌ {_failed_pr_line(f)}")

    (AUDIT_DIR / "02-ship.md").write_text("\n".join(md_lines) + "\n")

    status = "PR_CREATED" if pr_url else ("DRY_RUN" if fixes and not AUTO_PUSH else "NO_PR")
    log(f"Done — status={status} | {len(fixes)}/{total} fixed")
    if pr_url:
        log(f"PR: {pr_url}")


if __name__ == "__main__":
    main()
