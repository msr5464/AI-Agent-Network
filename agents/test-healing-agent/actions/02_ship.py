#!/usr/bin/env python3
"""
Step 02 — Ship
Push the fix branch and create a GitHub PR. Send Slack notification.

Reads: audit/<session>/01-fix.json + .fix-passed gate
No AI calls. No code changes.
"""

import os, sys, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → shared.*

import warnings, urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
import logging
logging.basicConfig(level=logging.WARNING)

from shared.log import log as _log
from shared.log import blocked
from shared.slack import send_slack as _send_slack
from shared.github import create_pr
from shared.git import run_git
from shared import workspace as workspace_helper
def log(msg): _log("ship", msg)
def send_slack(channel: str, text: str) -> bool:
    return _send_slack(SLACK_BOT_TOKEN, channel, text)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))
SESSION_ID = os.environ.get("SESSION_ID", AUDIT_DIR.name)

AUTO_PUSH              = os.environ.get("AUTO_PUSH", "true").lower() == "true"
GITHUB_TOKEN           = os.environ.get("GITHUB_TOKEN", "")
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
    """The automation checkout: FRAMEWORK_DIR, else WORKSPACE_DIR/repo."""
    candidate = workspace_helper.expected(WORKSPACE_DIR, GITHUB_REPO_AUTOMATION)
    return candidate if candidate and candidate.exists() else None


def _authenticated_url() -> str:
    """Token-bearing remote URL, supplied per-invocation and never stored.

    See shared/git.py: a remote with no embedded credentials makes git try to
    negotiate interactively, which fails in a headless subprocess, and a URL
    written into .git/config leaves the token in plaintext on disk.
    """
    return (f"https://x-access-token:{GITHUB_TOKEN}@github.com/"
            f"{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}.git")


def short_name(test_name: str) -> str:
    """Return the simple class.method portion of a fully-qualified test name."""
    parts = test_name.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else test_name


def push_and_create_pr(fix_data: dict, unverified_fixes: list, failed_fixes: list) -> Optional[str]:
    pr_branch = fix_data.get("pr_branch")
    if not pr_branch:
        # With AUTO_PUSH off this is the dry run the user asked for. With it on,
        # the fix step could not get onto a branch and already said why — this is
        # the second half of that story, at the point the PR fails to appear.
        log("No PR branch from fix step — skipping PR creation" if not AUTO_PUSH
            else blocked("the fix step produced no branch to push",
                         "no PR will be raised; see the BLOCKED line above for the "
                         "git failure that caused it"))
        return None

    if not AUTO_PUSH:
        log("AUTO_PUSH=false — skipping push (dry-run)")
        return None

    if not GITHUB_ORG or not GITHUB_REPO_AUTOMATION:
        log(blocked("GITHUB_ORG or GITHUB_REPO_AUTOMATION is not set",
                    "no PR will be raised",
                    "set both in config/.env, or Agent Settings"))
        return None

    workspace = get_workspace()
    if not workspace:
        log(blocked("the automation repo was not found",
                    "no PR will be raised",
                    "set FRAMEWORK_DIR, or WORKSPACE_DIR and GITHUB_REPO_AUTOMATION"))
        return None

    full_repo = f"{GITHUB_ORG}/{GITHUB_REPO_AUTOMATION}"
    build_tag = fix_data.get("build_tag", "unknown")
    fixes     = fix_data.get("fixes", [])
    total     = len(fixes) + len(unverified_fixes) + len(failed_fixes)

    # Push branch. run_git swaps "origin" for a token-bearing URL for this one
    # command, so the credential never lands in .git/config.
    log(f"Pushing {pr_branch} to {full_repo}...")
    ok, _, err = run_git(
        ["push", "origin", pr_branch, "--force-with-lease"],
        workspace, push_url=_authenticated_url(),
    )
    if not ok:
        redacted = (err or "").replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else (err or "")
        log(blocked(f"push of {pr_branch} was rejected ({redacted.strip()[:160]})",
                    "no PR will be raised; the commits are on the local branch",
                    f"git -C {workspace} log --oneline {GITHUB_DEFAULT_BRANCH}..{pr_branch}"))
        return None

    # PR title reflects partial/full fix
    if failed_fixes or unverified_fixes:
        pr_title = f"Healing: Fixed {len(fixes)}/{total} locator failures for {build_tag} [NEEDS-REVIEW]"
    else:
        pr_title = f"Healing: Fixed {len(fixes)}/{total} locator failures for {build_tag} [PASSED]"

    def target_name(f: dict) -> str:
        target = f.get("target_file") or f.get("test_file")
        return Path(target).name if target else "unknown file"

    def evidence_links(f: dict) -> str:
        """Point the reviewer at what the agent looked at.

        A PR that says "updated the selector" asks to be trusted. One that links
        the page as the test left it lets the reviewer check in a glance whether
        the agent was even on the right page — which is the failure mode no amount
        of green CI will surface.
        """
        links = []
        for label, key in (("screenshot", "screenshot"), ("DOM", "dom_snapshot_path")):
            path = f.get(key)
            if path:
                links.append(f"[{label}]({Path(path).as_uri()})")
        return " — " + " · ".join(links) if links else ""

    def fixed_line(f: dict) -> str:
        dom = " _(selector confirmed in a live browser)_" if f.get("dom_verified") else ""
        return (f"- ✅ `{short_name(f.get('test_name', 'unknown'))}` — "
                f"{f.get('fix_description', '')} (`{target_name(f)}`){dom}"
                f"{evidence_links(f)}")

    fixed_lines = "\n".join(fixed_line(f) for f in fixes) or "_(none)_"

    # Applied but never executed — must be visibly distinct from a verified fix.
    needs_review = ""
    if unverified_fixes:
        unverified_lines = "\n".join(
            f"- ⚠️ `{short_name(f.get('test_name', 'unknown'))}` — "
            f"{f.get('fix_description', '')} (`{target_name(f)}`)"
            for f in unverified_fixes
        )
        needs_review = f"""
### ⚠️ Applied but NOT Verified ({len(unverified_fixes)})

No test runner was available in the workspace, so these changes were **never
executed**. Review and run them manually before merging.

{unverified_lines}
"""

    # Failed tests section
    if failed_fixes:
        failed_lines = "\n".join(_failed_pr_line(f) for f in failed_fixes)
        needs_manual = f"""
### ❌ Needs Manual Fix ({len(failed_fixes)})

{failed_lines}
"""
    else:
        needs_manual = ""

    if fixes and not unverified_fixes:
        validation = ("Every fix listed above was re-run locally and passed before this PR "
                      "was created.")
    elif fixes and unverified_fixes:
        validation = (f"The {len(fixes)} fix(es) under **Fixed** were re-run locally and "
                      f"passed. The {len(unverified_fixes)} under **Applied but NOT Verified** "
                      f"were not executed — no test runner was available.")
    else:
        validation = ("⚠️ **Nothing in this PR was verified by a test run** — no test runner "
                      "was available in the workspace.")

    status_tag = "NEEDS-REVIEW" if (failed_fixes or unverified_fixes) else "PASSED"
    status_summary = (f"{len(fixes)}/{total} locator fixes verified and passing locally."
                      if not (failed_fixes or unverified_fixes)
                      else f"{len(fixes)}/{total} locator fixes applied; manual review needed.")

    all_files = {target_name(f) for f in (fixes + unverified_fixes + failed_fixes) if target_name(f) != "unknown file"}
    files_changed_count = len(all_files) or len(fixes)

    pr_body = f"""## 🤖 Test Healing Agent — {build_tag}

> Status: **{status_tag}** — {status_summary}

### 📋 Overview
| Property | Value |
|---|---|
| **Agent** | `test-healing-agent` |
| **Target** | `{build_tag}` |
| **Status** | `{'✅ Passed' if status_tag == 'PASSED' else '⚠️ Needs Review'}` |
| **Files Changed** | `{files_changed_count}` |
| **Fix Attempts** | `{fix_data.get('fix_attempt', 1)}` |
| **Session ID** | `{SESSION_ID}` |

### 🛠️ Changes Applied

{fixed_lines}
{needs_review}{needs_manual}
### 🧪 Validation & Test Results

{validation}

### 🔍 How to Review
1. Review the locator updates in the changed page objects / test files.
2. Check the attached DOM snapshot and screenshot links to confirm the element selector matches the live page.
3. Run the healed test(s) locally against the target environment.

---
> 🤖 Generated by **test-healing-agent** · Audit Session: `{AUDIT_DIR.name}`
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
        log(blocked(f"PR creation failed ({str(pr_err).strip()[:160]})",
                    f"no PR will be raised; {pr_branch} is pushed and can be "
                    f"opened by hand",
                    f"https://github.com/{full_repo}/pull/new/{pr_branch}"))
        return None
    log(f"PR created: {pr_url}")
    return pr_url


def _failed_pr_line(f: dict) -> str:
    status = f.get("status", "unknown")
    name   = short_name(f.get("test_name", "unknown"))
    if status == "unfixable":
        return f"- ❌ `{name}` — Claude declared unfixable: {f.get('unfixable_reason', '')}"
    if status == "test_failed":
        return f"- ❌ `{name}` — fix applied but test still failing"
    if status == "advanced":
        # Kept, not reverted: the element it targeted works now. Reporting this
        # as a plain failure hides the only progress the run made.
        moved = (f.get("progressed_to") or {}).get("element") or "a later element"
        return (f"- ⏩ `{name}` — the locator it targeted is fixed; the test now "
                f"stops at {moved}")
    if status == "no_file":
        return f"- ❌ `{name}` — test file not found in workspace"
    if status == "rejected_unsafe":
        return f"- ❌ `{name}` — fix rejected by safety guard: {f.get('unfixable_reason', '')}"
    if status == "edit_failed":
        return f"- ❌ `{name}` — edit could not be applied: {f.get('unfixable_reason', '')}"
    return f"- ❌ `{name}` — {status}"


def _build_slack_message(build_tag: str, fixes: list, unverified_fixes: list,
                          failed_fixes: list, pr_url: Optional[str], fix_gate: str,
                          fix_attempt: int) -> Tuple[str, str]:
    """
    Build a rich Slack message. Returns (channel, text).
    Uses SLACK_ALERT_CHANNEL if any failures, SLACK_NOTIFY_CHANNEL otherwise.
    """
    total      = len(fixes) + len(unverified_fixes) + len(failed_fixes)
    n_fixed    = len(fixes)
    n_unverif  = len(unverified_fixes)
    n_failed   = len(failed_fixes)

    if fix_gate == "skipped":
        # Nothing to report — no noise
        return "", ""

    # ── Header line ──────────────────────────────────────────────────────────
    if n_fixed == total:
        icon    = ":white_check_mark:"
        summary = f"*{n_fixed}/{total} tests fixed*"
        channel = SLACK_NOTIFY_CHANNEL
    elif n_fixed > 0 or n_unverif > 0:
        icon    = ":large_yellow_circle:"
        parts   = [f"*{n_fixed}/{total} tests fixed*"]
        if n_unverif:
            parts.append(f"{n_unverif} applied but unverified")
        if n_failed:
            parts.append(f"{n_failed} need manual attention")
        summary = " — ".join(parts)
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
            dom = " _(live-DOM confirmed)_" if f.get("dom_verified") else ""
            lines.append(f"  • `{short_name(f.get('test_name', 'unknown'))}` — "
                         f"{f.get('fix_description', '')}{dom}")
        if n_fixed > 8:
            lines.append(f"  _...and {n_fixed - 8} more_")
        lines.append("")

    # ── Applied but unverified ───────────────────────────────────────────────
    if unverified_fixes:
        lines.append(f":warning: *Applied but NOT verified ({n_unverif}) — no test runner available:*")
        for f in unverified_fixes[:8]:
            lines.append(f"  • `{short_name(f.get('test_name', 'unknown'))}` — "
                         f"{f.get('fix_description', '')}")
        if n_unverif > 8:
            lines.append(f"  _...and {n_unverif - 8} more_")
        lines.append("")

    # ── Failed tests ─────────────────────────────────────────────────────────
    if failed_fixes:
        lines.append(f":x: *Could not fix ({n_failed}) — manual review required:*")
        for f in failed_fixes[:8]:
            status = f.get("status", "unknown")
            name   = short_name(f.get("test_name", "unknown"))
            if status == "unfixable":
                reason = f.get("unfixable_reason", "unclear")
                lines.append(f"  • `{name}` — unfixable: {reason[:80]}")
            elif status == "test_failed":
                lines.append(f"  • `{name}` — fix applied but test still failing")
            elif status == "advanced":
                moved = (f.get("progressed_to") or {}).get("element") or "a later element"
                lines.append(f"  • `{name}` — locator fixed and kept; now stops at "
                             f"{moved}")
            elif status == "no_file":
                lines.append(f"  • `{name}` — test file not found in workspace")
            elif status == "rejected_unsafe":
                lines.append(f"  • `{name}` — rejected by safety guard: "
                             f"{f.get('unfixable_reason', '')[:80]}")
            else:
                lines.append(f"  • `{name}` — {status}")
        if n_failed > 8:
            lines.append(f"  _...and {n_failed - 8} more_")
        lines.append("")

    if pr_url:
        lines.append(f"PR: {pr_url}")
    elif (fixes or unverified_fixes) and not AUTO_PUSH:
        lines.append("_(AUTO_PUSH=false — no PR created)_")
    lines.append(f"_Audit: `{AUDIT_DIR.name}`_")

    return channel, "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    fix_data         = load_json("01-fix.json")
    fix_gate         = read_gate()
    build_tag        = fix_data.get("build_tag", "unknown")
    fixes            = fix_data.get("fixes", [])
    unverified_fixes = fix_data.get("unverified_fixes", [])
    failed_fixes     = fix_data.get("failed_fixes", [])
    fix_attempt      = fix_data.get("fix_attempt", 1)

    log(f"Build tag:  {build_tag}")
    log(f"Fix gate:   {fix_gate}")
    log(f"Verified:   {len(fixes)} | Unverified: {len(unverified_fixes)} | "
        f"Failed: {len(failed_fixes)}")

    pr_url         = None
    slack_notified = False

    # ── Create PR whenever anything was applied ───────────────────────────────
    # Even a partial result (some fixed, some not) deserves a PR for what landed.
    if fixes or unverified_fixes:
        pr_url = push_and_create_pr(fix_data, unverified_fixes, failed_fixes)
    elif fix_gate == "skipped":
        log("No fixes attempted — skipping PR")
    else:
        log("No successful fixes — skipping PR")

    # ── Slack ─────────────────────────────────────────────────────────────────
    channel, text = _build_slack_message(
        build_tag, fixes, unverified_fixes, failed_fixes, pr_url, fix_gate, fix_attempt
    )
    if channel and text:
        slack_notified = send_slack(channel, text)

    # ── Write JSON ─────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp":      ts,
        "build_tag":      build_tag,
        "fix_gate":       fix_gate,
        "total_queued":   len(fixes) + len(unverified_fixes) + len(failed_fixes),
        "succeeded":      len(fixes),
        "unverified":     len(unverified_fixes),
        "failed":         len(failed_fixes),
        "pr_url":         pr_url,
        "slack_notified": slack_notified,
        "slack_channel":  channel,
    }
    (AUDIT_DIR / "02-ship.json").write_text(json.dumps(result, indent=2))

    # ── Write Markdown ─────────────────────────────────────────────────────────
    total = len(fixes) + len(unverified_fixes) + len(failed_fixes)
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
        f"| ✅ Fixed (verified) | {len(fixes)} |",
        f"| ⚠️ Applied but not verified | {len(unverified_fixes)} |",
        f"| ❌ Could not fix | {len(failed_fixes)} |",
        f"| PR | {pr_url or 'Not created'} |",
        f"| Slack | {'Sent to ' + channel if slack_notified else 'Skipped'} |",
    ]
    if fixes:
        md_lines += ["", "## Fixed Tests (verified)", ""]
        for f in fixes:
            md_lines.append(f"- ✅ `{short_name(f['test_name'])}` — {f.get('fix_description', '')}")
    if unverified_fixes:
        md_lines += ["", "## Applied but Not Verified", ""]
        for f in unverified_fixes:
            md_lines.append(f"- ⚠️ `{short_name(f['test_name'])}` — {f.get('fix_description', '')}")
    if failed_fixes:
        md_lines += ["", "## Could Not Fix", ""]
        for f in failed_fixes:
            md_lines.append(f"- ❌ {_failed_pr_line(f)}")

    (AUDIT_DIR / "02-ship.md").write_text("\n".join(md_lines) + "\n")

    applied = len(fixes) + len(unverified_fixes)
    status = "PR_CREATED" if pr_url else ("DRY_RUN" if applied and not AUTO_PUSH else "NO_PR")
    log(f"Done — status={status} | {len(fixes)}/{total} verified, "
        f"{len(unverified_fixes)} unverified")
    if pr_url:
        log(f"PR: {pr_url}")


if __name__ == "__main__":
    main()
