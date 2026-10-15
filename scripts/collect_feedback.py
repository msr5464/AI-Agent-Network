#!/usr/bin/env python3
"""Read what reviewers did with the agents' PRs into the feedback store.

Read-only towards GitHub: it lists PRs, comments, reviews and commits through
`gh`, and changes nothing there. Every agent PR yields some of these events
(shared/feedback.py):

  verdict          the reviewer's ticks in the PR's verdict block      explicit
  lesson           a `/lesson ...` comment                             explicit
  comment, review  anything else a person wrote on the PR              explicit
  human_commit     a commit a person added after the agent's last one  inferred
  revert           a commit undoing an agent's commit, with its reason inferred
  post_merge_edit  a change to the PR's files soon after it merged     inferred
  pr_outcome       merged or closed                                    inferred

Running it again adds only what is new, so it can run as often as you like.

    python3 scripts/collect_feedback.py                       # everything, into the store
    python3 scripts/collect_feedback.py --dry-run --report /tmp/feedback.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from shared import feedback, verdict_feedback  # noqa: E402

KNOWN_ISSUES = REPO_ROOT / "agents" / "test-healing-agent" / "feedback" / "known-issues.json"
MAX_DIFF_CHARS = feedback.MAX_DIFF_CHARS

# Files of a merged PR followed for later edits. A PR touching more is
# regenerated wholesale, and its follow-up history says little about any one file.
MAX_POST_MERGE_FILES = 40

_AGENT_ROW = re.compile(r"\|\s*\*\*Agent\*\*\s*\|\s*`([^`]+)`")
_SESSION_ROW = re.compile(r"\|\s*\*\*Session ID\*\*\s*\|\s*`([^`]+)`")
_TARGET_ROW = re.compile(r"\|\s*\*\*Target\*\*\s*\|\s*`([^`]+)`")
_REVERT = re.compile(r'^Revert "(.+)"\s*$')
_LEGACY_AUTHORING = re.compile(r"^## QA Auto-Create\b", re.MULTILINE)


# ── Which agent, which run ────────────────────────────────────────────────────

def _message(commit: Dict) -> str:
    head = commit.get("messageHeadline") or ""
    body = commit.get("messageBody") or ""
    return f"{head}\n\n{body}" if body else head


def attribute(pr: Dict) -> Optional[Dict[str, str]]:
    """The agent and session behind a PR, or None when no agent opened it.

    The PR body's overview table names both. The authoring agent's PRs from before
    that table name neither, and are known by their own heading and title.

    Never by the commits a PR carries: a person's branch that merged an agent's
    branch carries the agent's commits too, and is still the person's PR.
    """
    body = pr.get("body") or ""
    agent = _AGENT_ROW.search(body)
    if agent:
        session = _SESSION_ROW.search(body)
        target = _TARGET_ROW.search(body)
        return {"agent": agent.group(1), "session": session.group(1) if session else "",
                "target": target.group(1) if target else ""}
    if _LEGACY_AUTHORING.search(body) or (pr.get("title") or "").startswith("Authoring Agent:"):
        return {"agent": "test-authoring-agent", "session": "", "target": ""}
    return None


def _artifact(repo: str, pr: Dict, **extra) -> Dict:
    return {"repo": repo, "pr": pr.get("number"), "url": pr.get("url", ""),
            "branch": pr.get("headRefName", ""), **extra}


# ── Events from the PR itself ─────────────────────────────────────────────────

def verdict_event(repo: str, pr: Dict, who: Dict) -> Optional[Dict]:
    """The reviewer's ticks, or None when the block is absent or nothing is ticked."""
    parsed = feedback.parse_verdict(pr.get("body") or "")
    if not parsed or not (parsed["verdict"] or parsed["reasons"]):
        return None
    return feedback.event(
        "verdict", source="github", explicit=True,
        key=(repo, pr.get("number"), parsed["verdict"], sorted(parsed["reasons"]),
             parsed["ambiguous"]),
        agent=who["agent"], session=who["session"], artifact=_artifact(repo, pr),
        verdict=parsed["verdict"], reasons=parsed["reasons"],
        text="more than one verdict is ticked" if parsed["ambiguous"] else "",
        at=pr.get("updatedAt") or "")


def discussion_events(repo: str, pr: Dict, who: Dict, comments: Iterable[Dict],
                      reviews: Iterable[Dict], inline: Iterable[Dict]) -> List[Dict]:
    """Comments, reviews and inline review comments; `/lesson` ones as lessons."""
    events: List[Dict] = []

    def add(kind_hint: str, record_id, text: str, author: str, at: str, **where):
        lesson = feedback.parse_lesson(text)
        kind = "lesson" if lesson else kind_hint
        events.append(feedback.event(
            kind, source="github", explicit=True, key=(repo, pr.get("number"), record_id),
            agent=who["agent"], session=who["session"],
            artifact=_artifact(repo, pr, **where), text=lesson or text.strip(),
            who=author, at=at))

    for c in comments:
        text = c.get("body") or ""
        if text.strip():
            add("comment", c.get("id") or c.get("url"), text,
                (c.get("author") or {}).get("login", ""), c.get("createdAt", ""))
    for r in reviews:
        text = r.get("body") or ""
        state = r.get("state") or ""
        if text.strip() or state in ("APPROVED", "CHANGES_REQUESTED"):
            add("review", r.get("id") or f"review-{r.get('submittedAt')}",
                text or state.lower().replace("_", " "),
                (r.get("author") or {}).get("login", ""), r.get("submittedAt", ""),
                review_state=state)
    for c in inline:
        text = c.get("body") or ""
        if text.strip():
            add("comment", c.get("id"), text, (c.get("user") or {}).get("login", ""),
                c.get("created_at", ""), path=c.get("path", ""), line=c.get("line"),
                diff_hunk=(c.get("diff_hunk") or "")[-1500:])
    return events


def outcome_event(repo: str, pr: Dict, who: Dict) -> Optional[Dict]:
    """Merged or closed. Inferred: a closed PR is as often a discarded trial run."""
    state = pr.get("state")
    if state not in ("MERGED", "CLOSED"):
        return None
    at = pr.get("mergedAt") if state == "MERGED" else pr.get("closedAt")
    return feedback.event(
        "pr_outcome", source="github", explicit=False, key=(repo, pr.get("number"), state),
        agent=who["agent"], session=who["session"], artifact=_artifact(repo, pr),
        text=state.lower(), at=at or "")


# ── Events from the PR's commits ──────────────────────────────────────────────

def _patch(files: Iterable[Dict]) -> str:
    parts = []
    for f in files:
        patch = f.get("patch")
        header = f"--- {f.get('filename')} ({f.get('status')}, +{f.get('additions')}/-{f.get('deletions')})"
        parts.append(f"{header}\n{patch}" if patch else header)
    text = "\n".join(parts)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n… (cut at {MAX_DIFF_CHARS} characters)"
    return text


def commit_events(repo: str, pr: Dict, who: Dict, commits: List[Dict],
                  details: Callable[[str], Dict]) -> List[Dict]:
    """Corrections and reverts people made on the agent's branch.

    Only commits after this PR's agent's last one count: a branch cut from a
    feature branch carries that branch's commits too, and those came before the
    agent did anything. Another agent's commit merged in from elsewhere is not the
    anchor, or a person's correction made before it would be missed. Merges bring
    the base in, so they are skipped. Each correction says whether it touched a
    file the agent's own commits wrote, which is what separates a correction of
    the agent's output from other work on the branch.
    """
    agent_idx = [i for i, c in enumerate(commits)
                 if feedback.agent_of_commit(_message(c)) == who["agent"]]
    if not agent_idx:
        return []
    after = commits[agent_idx[-1] + 1:]
    candidates = [c for c in after
                  if not feedback.agent_of_commit(_message(c))
                  and not (c.get("messageHeadline") or "").startswith("Merge ")]
    if not candidates:
        return []
    agent_files = set()
    for i in agent_idx:
        agent_files.update(f.get("filename") for f in details(commits[i]["oid"]).get("files") or [])
    events: List[Dict] = []
    for c in candidates:
        info = details(c["oid"])
        if len(info.get("parents") or []) > 1:
            continue
        files = info.get("files") or []
        names = [f.get("filename") for f in files]
        author = ((c.get("authors") or [{}])[0] or {}).get("login", "")
        revert = _REVERT.match(c.get("messageHeadline") or "")
        if revert:
            undone = revert.group(1)
            owner = feedback.agent_of_commit(undone) or who["agent"]
            events.append(feedback.event(
                "revert", source="github", explicit=False, key=(repo, c["oid"]),
                agent=owner, session=who["session"] if owner == who["agent"] else "",
                artifact=_artifact(repo, pr, commit=c["oid"], files=names,
                                   reverted_subject=undone,
                                   reverts_agent_commit=bool(feedback.agent_of_commit(undone))),
                text=(c.get("messageBody") or "").strip(), diff=_patch(files), who=author,
                at=c.get("committedDate", "")))
            continue
        touched = sorted(set(names) & agent_files)
        events.append(feedback.event(
            "human_commit", source="github", explicit=False, key=(repo, c["oid"]),
            agent=who["agent"], session=who["session"],
            artifact=_artifact(repo, pr, commit=c["oid"], files=names,
                               touches_agent_files=touched),
            text=_message(c).strip(), diff=_patch(files), who=author,
            at=c.get("committedDate", "")))
    return events


def post_merge_events(repo: str, pr: Dict, who: Dict, files: List[str], days: int,
                      branch: Optional[str],
                      history: Callable[[str, str, str, str], List[Dict]],
                      details: Callable[[str], Dict]) -> List[Dict]:
    """Changes people made to a merged PR's files on `branch` within `days` of the merge."""
    merged = pr.get("mergedAt")
    if pr.get("state") != "MERGED" or not merged or not files or not branch:
        return []
    start = datetime.fromisoformat(merged.replace("Z", "+00:00")) + timedelta(seconds=1)
    end = start + timedelta(days=days)
    seen: Dict[str, Dict] = {}
    for path in files[:MAX_POST_MERGE_FILES]:
        for c in history(branch, path,
                         start.isoformat().replace("+00:00", "Z"),
                         end.isoformat().replace("+00:00", "Z")):
            message = (c.get("commit") or {}).get("message", "")
            if feedback.agent_of_commit(message) or len(c.get("parents") or []) > 1:
                continue
            seen.setdefault(c["sha"], c)
    events = []
    for sha, c in seen.items():
        info = details(sha)
        message = (c.get("commit") or {}).get("message", "")
        events.append(feedback.event(
            "post_merge_edit", source="github", explicit=False, key=(repo, sha),
            agent=who["agent"], session=who["session"],
            artifact=_artifact(repo, pr, commit=sha,
                               files=[f.get("filename") for f in info.get("files") or []]),
            text=message.strip(), diff=_patch(info.get("files") or []),
            who=(c.get("author") or {}).get("login", ""),
            at=((c.get("commit") or {}).get("author") or {}).get("date", "")))
    return events


# ── gh ────────────────────────────────────────────────────────────────────────

class GitHub:
    """The few read-only `gh` calls the collector makes, each cached for the run."""

    # GitHub's own hiccups: a 5xx or a timeout clears on its own, a 404 never does.
    TRANSIENT = re.compile(r"HTTP 5\d\d|Gateway|timed? ?out|connection reset|EOF", re.I)
    RETRY_DELAYS_S = (3, 10)

    def __init__(self, repo: str, run=subprocess.run, sleep=time.sleep):
        self.repo = repo
        self._run, self._sleep = run, sleep
        self._commits: Dict[str, Dict] = {}
        self._branches: Dict[str, bool] = {}
        self._default: Optional[str] = None

    def _gh(self, *args: str) -> str:
        """Run gh; a transient failure is tried again, twice, before it counts."""
        for delay in (*self.RETRY_DELAYS_S, None):
            try:
                result = self._run(["gh", *args], capture_output=True, text=True, timeout=120)
                error = result.stderr.strip() if result.returncode != 0 else ""
            except subprocess.TimeoutExpired:
                result, error = None, "timed out after 120s"
            if not error:
                return result.stdout
            if delay is None or not self.TRANSIENT.search(error):
                raise RuntimeError(f"gh {' '.join(args[:3])}… failed: {error[:300]}")
            self._sleep(delay)
        raise AssertionError("unreachable")

    def default_branch(self) -> str:
        if self._default is None:
            self._default = self._gh("repo", "view", self.repo, "--json", "defaultBranchRef",
                                     "-q", ".defaultBranchRef.name").strip()
        return self._default

    def branch_exists(self, name: str) -> bool:
        if name not in self._branches:
            try:
                self._gh("api", f"repos/{self.repo}/branches/{name}", "-q", ".name")
                self._branches[name] = True
            except RuntimeError as exc:
                if "404" not in str(exc):
                    raise
                self._branches[name] = False
        return self._branches[name]

    def reaches(self, branch: str, sha: str) -> bool:
        """Whether `sha` is in `branch`'s history."""
        status = self._gh("api", f"repos/{self.repo}/compare/{branch}...{sha}",
                          "-q", ".status").strip()
        return status in ("behind", "identical")

    def pull_requests(self, limit: int) -> List[Dict]:
        fields = ("number,title,state,headRefName,baseRefName,createdAt,updatedAt,"
                  "mergedAt,closedAt,mergeCommit,url,body")
        return json.loads(self._gh("pr", "list", "-R", self.repo, "--state", "all",
                                   "--limit", str(limit), "--json", fields))

    def pull_request(self, number: int) -> Dict:
        return json.loads(self._gh("pr", "view", str(number), "-R", self.repo,
                                   "--json", "comments,reviews,commits,files"))

    def inline_comments(self, number: int) -> List[Dict]:
        out = self._gh("api", "--paginate", f"repos/{self.repo}/pulls/{number}/comments")
        return json.loads(out) if out.strip() else []

    def commit(self, sha: str) -> Dict:
        if sha not in self._commits:
            self._commits[sha] = json.loads(self._gh("api", f"repos/{self.repo}/commits/{sha}"))
        return self._commits[sha]

    def file_history(self, branch: str, path: str, since: str, until: str) -> List[Dict]:
        out = self._gh("api", "-X", "GET", f"repos/{self.repo}/commits",
                       "-f", f"sha={branch}", "-f", f"path={path}",
                       "-f", f"since={since}", "-f", f"until={until}", "-f", "per_page=100")
        return json.loads(out) if out.strip() else []


def branch_to_follow(gh, pr: Dict):
    """Where a merged PR's files are followed, or (None, why not).

    Its base branch, while that exists. A feature branch is often deleted once it
    is merged on; then the default branch, but only when the merge reached it:
    otherwise the PR's changes never got there, and later edits on it are not
    edits to them.
    """
    base = pr.get("baseRefName") or ""
    if base and gh.branch_exists(base):
        return base, ""
    default = gh.default_branch()
    merge = (pr.get("mergeCommit") or {}).get("oid", "")
    if merge and gh.reaches(default, merge):
        return default, ""
    return None, (f"its base branch {base} no longer exists, and the merge never "
                  f"reached {default}")


# ── Run ───────────────────────────────────────────────────────────────────────

def collect(gh, limit: int, post_merge_days: int, log=print) -> Dict:
    """Every event the repository's agent PRs hold, and how many PRs held them."""
    repo = gh.repo
    events: List[Dict] = []
    errors: List[str] = []
    not_followed: List[str] = []
    prs = gh.pull_requests(limit)
    agent_prs: List[Dict] = []
    for pr in prs:
        who = attribute(pr)
        if not who:
            continue
        agent_prs.append({**pr, "_who": who})
        found = [verdict_event(repo, pr, who), outcome_event(repo, pr, who)]
        # One PR whose history cannot be read (its base branch deleted, a commit
        # gone) costs that PR's events, never the whole collection. Each failure
        # is reported, so a short count is never mistaken for a quiet repo.
        try:
            detail = gh.pull_request(pr["number"])
            reviews = detail.get("reviews") or []
            # Inline comments always belong to a review, so a PR without one has none.
            inline = gh.inline_comments(pr["number"]) if reviews else []
            found += discussion_events(repo, pr, who, detail.get("comments") or [],
                                       reviews, inline)
            found += commit_events(repo, pr, who, detail.get("commits") or [], gh.commit)
        except RuntimeError as exc:
            errors.append(f"PR #{pr['number']}: {exc}")
            detail = {}
        try:
            branch = None
            if pr.get("state") == "MERGED":
                branch, why = branch_to_follow(gh, pr)
                if why:
                    not_followed.append(f"PR #{pr['number']}: {why}")
            found += post_merge_events(repo, pr, who,
                                       [f.get("path") for f in detail.get("files") or []],
                                       post_merge_days, branch, gh.file_history, gh.commit)
        except RuntimeError as exc:
            errors.append(f"PR #{pr['number']}, edits after the merge: {exc}")
        events.extend(e for e in found if e)
    log(f"{len(prs)} PR(s) read, {len(agent_prs)} opened by an agent, "
        f"{len(events)} event(s) found")
    for error in errors:
        log(f"  could not read {error}")
    for note in not_followed:
        log(f"  edits after the merge not followed for {note}")
    return {"repo": repo, "prs": agent_prs, "events": events, "errors": errors,
            "not_followed": not_followed}


def record_diagnosis_feedback(events: Iterable[Dict], prs: Iterable[Dict]) -> int:
    """Hand the verdicts that say a diagnosis was wrong to verdict_feedback."""
    targets = {pr.get("number"): pr["_who"].get("target", "") for pr in prs}
    written = 0
    for e in events:
        if e["kind"] != "verdict":
            continue
        pr = e["artifact"].get("pr")
        written += verdict_feedback.from_review(
            KNOWN_ISSUES, e["agent"], e["verdict"], e["reasons"], targets.get(pr, ""),
            session=e["session"],
            detail=f"PR #{pr} rejected by its reviewer as the wrong flow or page")
    return written


def report(result: Dict, new: List[Dict], dry_run: bool) -> str:
    """A short markdown summary: what was read, and what reviewers said or did."""
    events = result["events"]
    by_agent: Dict[str, Counter] = defaultdict(Counter)
    for pr in result["prs"]:
        row = by_agent[pr["_who"]["agent"]]
        row["PRs"] += 1
        row[pr.get("state", "").lower()] += 1
    for e in events:
        row = by_agent[e["agent"]]
        if e["kind"] == "verdict":
            row[f"verdict: {e['verdict'] or 'ambiguous'}"] += 1
        elif e["kind"] == "human_commit":
            row["corrections" if e["artifact"].get("touches_agent_files") else "other commits"] += 1
        elif e["kind"] != "pr_outcome":
            row[e["kind"]] += 1
    columns = ["PRs", "merged", "closed", "open", "verdict: accept", "verdict: edited",
               "verdict: reject", "lesson", "comment", "review", "corrections",
               "other commits", "revert", "post_merge_edit"]
    lines = [f"# Feedback collected from {result['repo']}", "",
             f"{len(result['prs'])} agent PR(s); {len(events)} event(s), "
             f"{len(new)} new{' (dry run: nothing written)' if dry_run else ''}.", "",
             "| Agent | " + " | ".join(columns) + " |",
             "|---|" + "---|" * len(columns)]
    for agent in sorted(by_agent):
        row = by_agent[agent]
        lines.append(f"| {agent} | " + " | ".join(str(row.get(c, 0)) for c in columns) + " |")
    explicit = [e for e in events if e["explicit"]]
    lines += ["", "## What reviewers said", ""]
    lines += [f"- {e['kind']} on [#{e['artifact'].get('pr')}]({e['artifact'].get('url')})"
              f" ({e['agent']}): {e.get('verdict') or ''} {', '.join(e['reasons'])} "
              f"{e['text'][:200]}".rstrip() for e in explicit] or ["- Nothing yet."]
    corrections = [e for e in events if e["kind"] == "human_commit"
                   and e["artifact"].get("touches_agent_files")]
    lines += ["", "## People's corrections to what an agent wrote", ""]
    lines += [f"- [#{e['artifact'].get('pr')}]({e['artifact'].get('url')}) "
              f"`{e['artifact'].get('commit', '')[:7]}` {e['text'].splitlines()[0][:100]}: "
              f"{', '.join(Path(p).name for p in e['artifact']['touches_agent_files'])}"
              for e in corrections] or ["- None."]
    others = [e for e in events if e["kind"] == "human_commit"
              and not e["artifact"].get("touches_agent_files")]
    lines += ["", "## Other commits people added to an agent's branch", ""]
    lines += [f"- [#{e['artifact'].get('pr')}]({e['artifact'].get('url')}) "
              f"`{e['artifact'].get('commit', '')[:7]}` {e['text'].splitlines()[0][:100]}: "
              f"{', '.join(Path(p).name for p in e['artifact'].get('files') or [])[:200]}"
              for e in others] or ["- None."]
    if result.get("errors"):
        lines += ["", "## Not read", ""] + [f"- {error}" for error in result["errors"]]
    if result.get("not_followed"):
        lines += ["", "## Edits after the merge not followed", ""] + \
                 [f"- {note}" for note in result["not_followed"]]
    reverts = [e for e in events if e["kind"] == "revert"]
    lines += ["", "## Reverts (inferred: read the reason before drawing anything from one)", ""]
    lines += [f"- [#{e['artifact'].get('pr')}]({e['artifact'].get('url')}) undid "
              f"\"{e['artifact'].get('reverted_subject')}\": "
              f"{(e['text'].splitlines() or [''])[0][:200]}" for e in reverts] or ["- None."]
    return "\n".join(lines) + "\n"


def _default_repo() -> str:
    org = (os.environ.get("GITHUB_ORG") or "").strip()
    name = (os.environ.get("GITHUB_REPO_AUTOMATION") or "").strip()
    return f"{org}/{name}" if org and name else ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=_default_repo(),
                    help="org/repo (default: GITHUB_ORG/GITHUB_REPO_AUTOMATION)")
    ap.add_argument("--limit", type=int, default=1000, help="newest PRs to read")
    ap.add_argument("--post-merge-days", type=int, default=14,
                    help="how long after a merge an edit to its files still counts")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--report", default="", help="also write a markdown summary here")
    args = ap.parse_args(argv)
    if not args.repo:
        ap.error("no repository: pass --repo org/repo, or set GITHUB_ORG and "
                 "GITHUB_REPO_AUTOMATION (config/.env)")

    result = collect(GitHub(args.repo), args.limit, args.post_merge_days)
    if args.dry_run:
        known = {e.get("id") for e in feedback.read()}
        new = [e for e in result["events"] if e["id"] not in known]
    else:
        new = feedback.append(result["events"])
        flagged = record_diagnosis_feedback(new, result["prs"])
        if flagged:
            print(f"{flagged} reviewer verdict(s) recorded as a wrong diagnosis in {KNOWN_ISSUES}")
    kinds = Counter(e["kind"] for e in new)
    print(f"{len(new)} new event(s)"
          + (f": {', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}" if kinds else "")
          + ("" if args.dry_run else f" → {feedback.store_path()}"))
    if args.report:
        Path(args.report).write_text(report(result, new, args.dry_run), encoding="utf-8")
        print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
