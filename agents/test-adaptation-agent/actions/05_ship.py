#!/usr/bin/env python3
"""
Step 05 — Ship

Branch, commit one change item at a time, open a PR, notify Slack.

The PR is **always NEEDS-REVIEW**. Not conditionally, not "unless everything
passed" — a flow rewrite is much harder to eyeball than a one-line locator change,
and the whole design rests on a human reading it. That is asserted here rather than
branched on, so it cannot drift.

The body is the real product of this agent. A reviewer has to be able to tell what
the agent *observed* from what it *assumed*, so it carries: the change note as
written (credential-masked), the flow map with every step marked observed /
refused / unreachable, each edit against the flow step that justified it, every
guard result including the ones that passed, what was verified versus merely
listed, and everything that escalated.

One commit per change item, not per pipeline step: a reviewer can then read "the
workspace-picker step" as a unit and revert one item without unpicking the rest.

Reads:   01-parse-change.json, 02-scope.json, 03-explore.json, 04-adapt.json
Writes:  05-ship.json + .md, .verdict
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.log import log as _log
from shared.log import blocked
def log(msg): _log("ship", msg)

from shared import flow_map
from shared.git import run_git
from shared.github import create_pr
from shared.slack import send_slack

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
SESSION_ID = os.environ.get("SESSION_ID", "")
MODULE = os.environ.get("MODULE", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO_AUTOMATION", "")
BASE_BRANCH = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
BRANCH_PREFIX = os.environ.get("ADAPT_BRANCH_PREFIX", "chore/qa-adapt")
REVIEWERS = [r.strip() for r in os.environ.get("GITHUB_PR_REVIEWERS", "").split(",") if r.strip()]
AUTO_PUSH = os.environ.get("AUTO_PUSH", "true").lower() != "false"

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
SLACK_ALERT = os.environ.get("SLACK_ALERT_CHANNEL", "") or SLACK_NOTIFY

# Skip reasons that mean a person has to look at this, not that nothing happened.
ESCALATING = ("escalate", "unsafe", "no-session", "unreachable")


def load(name: str) -> dict:
    path = AUDIT_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def push_url() -> str:
    if GITHUB_TOKEN and GITHUB_ORG and GITHUB_REPO:
        return f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_ORG}/{GITHUB_REPO}.git"
    return ""


def open_prs_touching(edit_candidates: list) -> list:
    """Open PRs that already change a file this run edits.

    Both branches came off main, so whoever merges them second gets the
    conflict. Saying so in the body costs one `gh` call and saves that.
    """
    if not (GITHUB_TOKEN and GITHUB_ORG and GITHUB_REPO) or not edit_candidates:
        return []
    wanted = {Path(c.get("path", "")).name for c in edit_candidates}
    try:
        listed = subprocess.run(
            ["gh", "pr", "list", "--repo", f"{GITHUB_ORG}/{GITHUB_REPO}",
             "--state", "open", "--json", "number,title,files", "--limit", "30"],
            capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return []
    if listed.returncode != 0:
        return []
    try:
        rows = json.loads(listed.stdout or "[]")
    except ValueError:
        return []
    clashes = []
    for row in rows:
        names = {Path(f.get("path", "")).name for f in (row.get("files") or [])}
        shared_files = sorted(names & wanted)
        if shared_files:
            clashes.append({"number": row.get("number"), "title": row.get("title"),
                            "files": shared_files})
    return clashes


def build_body(plan: dict, scope: dict, explore: dict, adapt: dict,
               skip_reason: str) -> str:
    flow = explore.get("flow") or {}
    items = adapt.get("items") or []
    verified = adapt.get("verified") or []
    not_run = scope.get("not_verified") or []

    parts = [
        "## Why this PR exists",
        "",
        "The **product** changed. These tests were updated to match it — not because "
        "they were flaky, and not because a locator went stale.",
        "",
        "> ⚠️ **This PR is always NEEDS-REVIEW.** An agent may change test *steps* "
        "here, not just selectors. Every mechanical check below passed, but only a "
        "human can confirm the test still means what it should.",
        "",
        "## The change note, as written",
        "",
        "```",
        (plan.get("note_masked") or "").strip(),
        "```",
        "",
        "## What was explored, and what was only assumed",
        "",
    ]
    if flow.get("steps"):
        parts += [flow_map.describe(flow), ""]
        # Which page object each observed page turned out to be. A reviewer
        # checking "did it edit the right file?" should not have to take the
        # filename's word for it.
        mapping = flow_map.describe_page_objects(flow)
        if mapping:
            parts += ["### Measured page objects", mapping, ""]
    else:
        parts += ["_No flow map — nothing in the product was observed._", ""]

    if explore.get("unexplained_failures"):
        parts += ["### ⚠️ Failures the change note does not account for", "",
                  "A human asserted one change; that says nothing about a second, "
                  "unrelated defect. These escalated rather than being adapted to.",
                  ""]
        parts += [f"- step {u.get('index')}: {u.get('target') or u.get('endpoint')} "
                  f"({u.get('category')})" for u in explore["unexplained_failures"]]
        parts.append("")

    parts += ["## Changes, and what justified each one", ""]
    for item in items:
        parts.append(f"### Item {item['index']} — `{item['kind']}` — **{item['status']}**")
        parts.append("")
        if item.get("summary"):
            parts += [item["summary"], ""]
        if item.get("reason"):
            parts += [f"_{item['reason']}_", ""]
        if item.get("justification"):
            parts += ["| file | justified by flow step |", "|---|---|"]
            parts += [f"| `{j['file']}` | {j['step'] if j['step'] is not None else '—'} |"
                      for j in item["justification"]]
            parts.append("")
        guards = item.get("guards") or []
        if guards:
            passed = sum(1 for g in guards if g["ok"])
            parts.append(f"<details><summary>Guards: {passed}/{len(guards)} passed"
                         f"</summary>\n")
            parts += [f"- {'✅' if g['ok'] else '❌'} `{g['guard']}` "
                      f"{g.get('reason','')}" for g in guards]
            parts += ["", "</details>", ""]
        for report in item.get("conservation") or []:
            if not report.get("ok") or report.get("verdict") == "PLAUSIBLE":
                parts += [f"- assertion conservation ({report.get('test','')}): "
                          f"**{report.get('verdict')}** {report.get('reason','')}", ""]

    parts += ["## Verification", ""]
    parts += [f"- ✅ verified: `{t}`" for t in verified] or ["- _nothing verified_"]
    if adapt.get("failed"):
        parts += [f"- ❌ still failing: `{t}`" for t in adapt["failed"]]
    if not_run:
        parts += ["", f"**Not re-run** under `ADAPT_VERIFY_POLICY="
                      f"{scope.get('verify_policy','')}` — please run these in CI:",
                  ""]
        parts += [f"- `{t}`" for t in not_run[:20]]
        klass = not_run[0].split("#")[0].split(".")[-1] if not_run else ""
        if klass:
            parts += ["", "```bash", f"mvn test -Dtest={klass}", "```"]

    clashes = open_prs_touching(scope.get("edit_candidates") or [])
    if clashes:
        parts += ["", "## ⚠️ Open PRs touching the same files", "",
                  "Both branches came off `main`; whoever merges second gets the "
                  "conflict.", ""]
        parts += [f"- #{c['number']} {c['title']} — {', '.join(c['files'])}"
                  for c in clashes]

    if adapt.get("unresolved_receivers") or scope.get("unresolved_receivers"):
        holes = scope.get("unresolved_receivers") or []
        parts += ["", "<details><summary>Calls the assertion graph could not "
                  f"follow ({len(holes)})</summary>", "",
                  "Conservation is **PLAUSIBLE** rather than CONFIRMED wherever "
                  "these appear — an unfollowable call is a hole in the guarantee, "
                  "not a pass.", ""]
        parts += [f"- `{h}`" for h in holes[:20]]
        parts += ["", "</details>"]

    if adapt.get("escalations"):
        parts += ["", "## Escalated — not attempted", ""]
        parts += [f"- **{e['what']}** — {e['why']}" for e in adapt["escalations"]]

    if skip_reason:
        parts += ["", f"_Run outcome: `{skip_reason}`._"]

    parts += ["", "---", f"_Session `{SESSION_ID}` · test-adaptation-agent_"]
    return "\n".join(parts)


def main():
    plan, scope = load("01-parse-change.json"), load("02-scope.json")
    explore, adapt = load("03-explore.json"), load("04-adapt.json")
    skip_reason = (AUDIT_DIR / ".skip-reason").read_text().strip() \
        if (AUDIT_DIR / ".skip-reason").exists() else ""

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "module": MODULE, "session_id": SESSION_ID,
        "ship_status": "dry_run", "ship_detail": "", "pr_url": None,
        "verdict": "NEEDS-REVIEW", "skip_reason": skip_reason,
        "escalations": adapt.get("escalations") or [],
        "verified": adapt.get("verified") or [],
        "failed": adapt.get("failed") or [],
    }

    applied = [i for i in (adapt.get("items") or [])
               if i.get("status") in ("applied", "partial")]
    body = build_body(plan, scope, explore, adapt, skip_reason)

    if not applied or not AUTO_PUSH or not (GITHUB_TOKEN and GITHUB_ORG and GITHUB_REPO):
        reason = ("nothing was applied" if not applied else
                  "AUTO_PUSH=false" if not AUTO_PUSH else "GitHub not configured")
        result["ship_detail"] = f"no PR — {reason}"
        log(result["ship_detail"])
    else:
        workspace = Path(scope["workspace"])
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        branch = f"{BRANCH_PREFIX}/{MODULE}-{stamp}"
        run_git(["fetch", "origin"], workspace, push_url=push_url())
        ok, _, err = run_git(["checkout", "-B", branch, f"origin/{BASE_BRANCH}"], workspace)
        if not ok:
            result.update({"ship_status": "push_failed",
                           "ship_detail": f"could not create {branch}: {err}"})
            # Recorded in the result and, until now, nowhere a watcher could see
            # it: the console simply stopped mentioning the PR.
            log(blocked(f"could not create {branch} ({err.strip()[:160]})",
                        "no PR will be raised; the edits stay in the working tree",
                        f"git -C {workspace} status"))
        else:
            log(f"Branch: {branch}")
            for item in applied:
                # Stage exactly the files this item edited. `git add -A` would
                # sweep up anything else sitting in the tree — a minted login
                # session, a developer's scratch file — which is the very thing
                # the cleanliness gate in step 02 exists to prevent. Re-creating
                # that risk at commit time would defeat it.
                paths = item.get("files") or []
                if not paths:
                    log(f"  item {item['index']} recorded no files — skipping commit")
                    continue
                run_git(["add", "--"] + paths, workspace)
                message = (f"test(adapt): item {item['index']} — {item['kind']}\n\n"
                           f"{item.get('summary','')}\n\n"
                           f"Change note: {plan.get('module','')}\n"
                           f"Session: {SESSION_ID}")
                run_git(["commit", "-m", message], workspace)
                log(f"  committed item {item['index']}")
            pushed, _, perr = run_git(["push", "-u", "origin", branch], workspace,
                                      push_url=push_url())
            if not pushed:
                result.update({"ship_status": "push_failed", "ship_detail": perr})
                log(blocked(f"push of {branch} was rejected ({perr.strip()[:160]})",
                            "no PR will be raised; the commits are on the local "
                            "branch",
                            f"git -C {workspace} log --oneline {BASE_BRANCH}..{branch}"))
            else:
                title = f"[NEEDS-REVIEW] Adapt {MODULE} tests to product change"
                url, gerr = create_pr(workspace, f"{GITHUB_ORG}/{GITHUB_REPO}",
                                      title, body, branch, BASE_BRANCH, REVIEWERS)
                if url:
                    result.update({"ship_status": "shipped", "pr_url": url})
                    log(f"PR: {url}")
                else:
                    result.update({"ship_status": "pr_failed", "ship_detail": gerr})
                    log(blocked(f"PR creation failed ({str(gerr).strip()[:160]})",
                                f"no PR will be raised; {branch} is pushed and "
                                f"can be opened by hand",
                                f"https://github.com/{GITHUB_ORG}/{GITHUB_REPO}"
                                f"/pull/new/{branch}"))

    # The verdict is asserted, not computed. A flow rewrite always needs a human.
    assert result["verdict"] == "NEEDS-REVIEW"
    (AUDIT_DIR / ".verdict").write_text(result["verdict"])

    if SLACK_TOKEN:
        escalating = skip_reason in ESCALATING or bool(result["escalations"])
        channel = SLACK_ALERT if (escalating or result["ship_status"] in
                                  ("push_failed", "pr_failed")) else SLACK_NOTIFY
        if escalating:
            headline = (f":raised_hand: *QA Adaptation needs a human* — `{MODULE}`\n"
                        f"The agent stopped rather than guessing.")
            detail = "\n".join(f"• {e['what']}: {e['why'][:160]}"
                               for e in result["escalations"][:4]) or skip_reason
        else:
            headline = (f":arrows_counterclockwise: *QA Adaptation — NEEDS REVIEW* "
                        f"— `{MODULE}`")
            detail = (f"{len(applied)} change item(s) applied, "
                      f"{len(result['verified'])} test(s) verified.")
        if channel:
            send_slack(SLACK_TOKEN, channel,
                       f"{headline}\n{detail}\n"
                       + (f"{result['pr_url']}\n" if result["pr_url"] else "")
                       + f"_Audit: `{SESSION_ID}`_")
            result["slack_notified"] = True
            log(f"Slack: notified {channel}")

    (AUDIT_DIR / "05-ship.json").write_text(json.dumps(result, indent=2))
    (AUDIT_DIR / "05-ship.md").write_text(
        f"# Ship\n\nStatus: **{result['ship_status']}** · verdict "
        f"**{result['verdict']}**\n\n{result['ship_detail']}\n\n---\n\n{body}\n")
    log(f"Verdict: {result['verdict']} ({result['ship_status']})")


if __name__ == "__main__":
    main()
