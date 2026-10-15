#!/usr/bin/env python3
"""
learning-agent / 06 Ship

Writes the report, and with AUTO_PUSH=true opens the pull requests a person
approves lessons through. Nothing reaches an agent before that merge: this step
never writes config/lessons/ in the checkout it runs from.

  * **Lessons** (config/lessons/<agent>.yaml) — a PR to this repo, cut from a
    fresh worktree of its default branch.
  * **Conventions** (lever `convention`) — also kept in the lessons file, never put
    in a prompt, and proposed as a PR to the automation repo appending them to its
    CLAUDE.md, which every agent and every person working there already reads.

With AUTO_PUSH=false the proposed files stay under proposed/ in the audit dir.

Reads:  01..05 step files
Writes: 06-report.md, 06-ship.json
"""

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from shared import workspace  # noqa: E402
from shared.git import run_git, with_agent_trailer  # noqa: E402
from shared.github import create_pr  # noqa: E402
from shared.log import log as _log  # noqa: E402
from shared.slack import send_slack  # noqa: E402


def log(msg): _log("ship", msg)


AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
SESSION_ID = os.environ.get("SESSION_ID", AUDIT_DIR.name)
AUTO_PUSH = os.environ.get("AUTO_PUSH", "false").strip().lower() == "true"
AGENT = "learning-agent"


def _json(name: str) -> dict:
    try:
        return json.loads((AUDIT_DIR / name).read_text())
    except (OSError, ValueError):
        return {}


# ── Report ────────────────────────────────────────────────────────────────────

def _where(event_ids, events: dict) -> str:
    parts = []
    for event_id in event_ids or []:
        event = events.get(event_id) or {}
        artifact = event.get("artifact") or {}
        label = f"{event.get('kind', '?')}"
        if artifact.get("url"):
            label += f" on [PR #{artifact.get('pr')}]({artifact['url']})"
        elif event.get("session"):
            label += f" in {event['session']}"
        parts.append(f"`{event_id}` ({label})")
    return ", ".join(parts) or "—"


def report(collected, attributed, proposed, validated, events) -> str:
    decisions = proposed.get("decisions") or []
    accepted = [d for d in decisions if d["status"] == "accepted"]
    refused = [d for d in decisions if d["status"] != "accepted"]
    checks = {c["lesson"]: c for c in validated.get("checked") or []}
    roles = {"evidence": 0, "context": 0}
    for e in attributed.get("events") or []:
        roles[e.get("role", "evidence")] = roles.get(e.get("role", "evidence"), 0) + 1
    lines = ["# What the agents can learn from people's feedback", "",
             f"- Events collected this run: {collected.get('new_events', 0)}; "
             f"in the store: {collected.get('store_events', 0)}",
             f"- Usable: {roles['evidence']} as evidence, {roles['context']} as context only",
             f"- Proposals: {len(accepted)} accepted, {len(refused)} refused"
             + ("" if proposed.get("model_called") else " (nothing to learn from yet, so no model call)"),
             ""]
    if accepted:
        lines += ["## Proposed", ""]
        for d in accepted:
            lesson = d.get("lesson") or {}
            scope = ", ".join(f"{k}: {v}" for k, v in (lesson.get("scope") or {}).items()) or "every step"
            lines += [f"### {d['op']} `{lesson.get('id', d.get('id'))}` — {d['agent']}",
                      "", f"> {lesson.get('statement', '')}", "",
                      f"- Lever: {lesson.get('lever')} · Scope: {scope}",
                      f"- Evidence: {_where(lesson.get('evidence'), events)}"]
            if d.get("why"):
                lines.append(f"- Why: {d['why']}")
            check = checks.get(lesson.get("id")) or {}
            if check.get("may_contradict"):
                lines.append(f"- ⚠️ May contradict {', '.join(check['may_contradict'])}")
            if check.get("replay"):
                r = check["replay"]
                lines.append(f"- Replay: {r['result']}"
                             + (f" (person's lines found: {r['without']:.0%} without the lesson, "
                                f"{r['with']:.0%} with it)" if "with" in r else ""))
            lines.append("")
    if refused:
        lines += ["## Refused", ""] + [
            f"- {d['op']} for {d.get('agent')}: "
            f"\"{(d.get('lesson') or {}).get('statement', d.get('id', ''))[:160]}\" — {d.get('because')}"
            for d in refused] + [""]
    if proposed.get("not_learned"):
        lines += ["## Not learned yet", ""] + [
            f"- {_where(n.get('evidence'), events)}: {n.get('why', '')}"
            for n in proposed["not_learned"]] + [""]
    if proposed.get("guard_candidates"):
        lines += ["## Needs a code check, not a lesson", "",
                  "These lessons are in the prompt and people still correct the same thing. "
                  "A guard (shared/edit_guards.py) is the stronger lever.", ""] + [
            f"- `{g['id']}` ({g['agent']}): \"{g['statement']}\" — {g['why']} "
            f"({', '.join(g['sessions'])})" for g in proposed["guard_candidates"]] + [""]
    if proposed.get("outcomes"):
        lines += ["## How each lesson's runs were received", "",
                  "| Lesson | Agent | Runs given it | Accepted | After edits | Rejected | Not reviewed |",
                  "|---|---|---|---|---|---|---|"] + [
            f"| `{lid}` | {row['agent']} | {len(row['sessions'])} | {row.get('accept', 0)} | "
            f"{row.get('edited', 0)} | {row.get('reject', 0)} | {row.get('unreviewed', 0)} |"
            for lid, row in sorted(proposed["outcomes"].items())] + [""]
    return "\n".join(lines)


# ── Pull requests ─────────────────────────────────────────────────────────────

def repo_slug(path: Path) -> str:
    ok, url, _ = run_git(["remote", "get-url", "origin"], path)
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url.strip()) if ok else None
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def default_branch(path: Path) -> str:
    ok, out, _ = run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    return out.strip().split("/", 1)[-1] if ok and out.strip() else "main"


def open_pr(main_path: Path, slug: str, base: str, files: dict, title: str, body: str,
            message: str) -> dict:
    """Commit `files` ({relative path: text}) on a fresh worktree branch and open a PR."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not (slug and token):
        return {"ok": False, "reason": "no GitHub repo or GITHUB_TOKEN"}
    root = Path(os.environ.get("QA_WORKTREE_TEMP_DIR") or "/tmp/qa-runs")
    tree = root / f"{SESSION_ID}-{slug.split('/')[1]}"
    branch = f"learning/{SESSION_ID}"
    prepared = workspace.prepare_worktree(main_path, tree, base, log=log)
    if not prepared.get("ok"):
        return {"ok": False, "reason": prepared.get("reason")}
    try:
        for rel, text in files.items():
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        run_git(["checkout", "-b", branch], tree)
        run_git(["add", "--"] + list(files), tree)
        ok, _, err = run_git(["commit", "-m", with_agent_trailer(
            f"{message}\n\nSession: {SESSION_ID}", AGENT)], tree)
        if not ok:
            return {"ok": False, "reason": f"commit failed: {err[:200]}"}
        org, name = slug.split("/", 1)
        ok, _, err = run_git(["push", "origin", branch], tree,
                             push_url=workspace.authenticated_url(org, name, token))
        if not ok:
            return {"ok": False, "reason": f"push failed: {err[:200]}"}
        url, err = create_pr(tree, slug, title, body, branch, base)
        return {"ok": bool(url), "url": url, "reason": err}
    finally:
        workspace.cleanup_worktree(main_path, tree, log=log)


def conventions_text(main_path: Path, rel: str, items: list) -> str:
    ok, current, _ = run_git(["show", f"origin/{os.environ.get('GITHUB_DEFAULT_BRANCH', 'main')}:{rel}"],
                             main_path)
    current = current if ok else ""
    heading = "## Lessons from review"
    block = "\n".join(f"- {l['statement']}" for l in items)
    if heading in current:
        return current.rstrip() + "\n" + block + "\n"
    return current.rstrip() + f"\n\n{heading}\n\nLearned from people's reviews of the agents' " \
                              f"work (QA Agent Network, learning-agent).\n\n{block}\n"


def main():
    collected, attributed = _json("01-collect.json"), _json("02-attribute.json")
    proposed, validated = _json("04-propose.json"), _json("05-validate.json")
    events = {e["id"]: e for e in _json("03-classify.json").get("events") or []}
    text = report(collected, attributed, proposed, validated, events)
    (AUDIT_DIR / "06-report.md").write_text(text)
    log(f"Report: {AUDIT_DIR / '06-report.md'}")

    changed = proposed.get("changed_agents") or []
    accepted = [d for d in proposed.get("decisions") or [] if d["status"] == "accepted"]
    conventions = [d["lesson"] for d in accepted
                   if d["op"] == "add" and (d.get("lesson") or {}).get("lever") == "convention"]
    result = {"report": str(AUDIT_DIR / "06-report.md"), "prs": [], "slack_notified": False,
              "auto_push": AUTO_PUSH}
    if changed and AUTO_PUSH:
        files = {f"config/lessons/{a}.yaml":
                 (AUDIT_DIR / "proposed" / "config" / "lessons" / f"{a}.yaml").read_text()
                 for a in changed}
        pr = open_pr(REPO_ROOT, repo_slug(REPO_ROOT), default_branch(REPO_ROOT), files,
                     f"Lessons: {len(accepted)} change(s) learned from reviews", text,
                     f"learning: {len(accepted)} lesson change(s) from people's feedback")
        result["prs"].append({"repo": "agents", **pr})
        log(f"Lessons PR: {pr.get('url') or pr.get('reason')}")
        if conventions:
            automation = workspace.resolve(os.environ.get("WORKSPACE_DIR") or REPO_ROOT.parent,
                                           os.environ.get("GITHUB_REPO_AUTOMATION", ""),
                                           exclude=REPO_ROOT)
            slug = f"{os.environ.get('GITHUB_ORG', '')}/{os.environ.get('GITHUB_REPO_AUTOMATION', '')}"
            pr = open_pr(automation, slug, os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
                         {"CLAUDE.md": conventions_text(automation, "CLAUDE.md", conventions)},
                         f"Conventions: {len(conventions)} learned from reviews of agent PRs",
                         text, f"conventions: {len(conventions)} learned from reviews")
            result["prs"].append({"repo": "automation", **pr})
            log(f"Conventions PR: {pr.get('url') or pr.get('reason')}")
    elif changed:
        log(f"AUTO_PUSH=false — the proposed lesson files are in {AUDIT_DIR / 'proposed'}; "
            f"nothing was pushed")

    if accepted or proposed.get("guard_candidates"):
        links = " · ".join(p["url"] for p in result["prs"] if p.get("url"))
        result["slack_notified"] = send_slack(
            os.environ.get("SLACK_BOT_TOKEN", ""), os.environ.get("SLACK_NOTIFY_CHANNEL", ""),
            f"🧠 Learning: {len(accepted)} lesson change(s) proposed, "
            f"{len(proposed.get('guard_candidates') or [])} guard candidate(s)"
            + (f" — {links}" if links else f" — report: {SESSION_ID}"))
    (AUDIT_DIR / "06-ship.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
