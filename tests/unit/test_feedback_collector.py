"""Tests for scripts/collect_feedback.py, scripts/record_verdict.py and the parts
of shared/verdict_feedback.py they write through.

The commit histories mirror what the agents' PRs really carry: commits from the
branch a run was cut from, ahead of the agent's own; another agent's commit merged
in from elsewhere; and a revert made on purpose to stage a failure.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from shared import feedback, verdict_feedback

ROOT = Path(__file__).resolve().parents[2]
REPO = "acme/automation"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = _load("collect_feedback")
record_verdict = _load("record_verdict")

AUTHORING_BODY = """## 🤖 Test Authoring Agent — Shop

| Property | Value |
|---|---|
| **Agent** | `test-authoring-agent` |
| **Target** | `Shop` |
| **Session ID** | `20261014-090000-create-shop` |
"""
HEALING_BODY = AUTHORING_BODY.replace("test-authoring-agent", "test-healing-agent") \
    .replace("`Shop`", "`local-ShopWebTest`").replace("create-shop", "fix-ShopWebTest")


def _pr(number=7, body=AUTHORING_BODY, state="OPEN", **kw):
    return {"number": number, "title": "Authoring: Created Shop automated tests",
            "state": state, "headRefName": "authoring/shop-20261014", "baseRefName": "main",
            "url": f"https://example.com/{REPO}/pull/{number}", "body": body, **kw}


def _commit(oid, headline, body="", login="person"):
    return {"oid": oid, "messageHeadline": headline, "messageBody": body,
            "authors": [{"login": login}], "committedDate": "2026-10-14T09:00:00Z"}


AGENT_SIGNED = "AI-generated test code\n\nAgent: test-authoring-agent\nSession: 20261014-090000-create-shop"

COMMITS = [
    _commit("base1", "object chaining"),                         # from the branch the run was cut from
    _commit("a1", "authoring: generate initial test draft for shop", AGENT_SIGNED),
    _commit("a2", "authoring: apply fix attempt 1 for shop", AGENT_SIGNED),
    _commit("h1", "keep the cart total check on the cart page"),  # a correction
    _commit("h2", "bump the framework's wait helper"),            # other work on the branch
    _commit("m1", "Merge branch 'main' into authoring/shop-20261014"),
    _commit("x1", "fix(automation): update locators for 1 test(s)"),   # another agent, merged in
    _commit("r1", 'Revert "fix(automation): update locators for 1 test(s)"',
            "Put the broken locator back so the healing agent has a real failure to reproduce."),
]

FILES = {
    "a1": ["src/main/java/shop/CartPage.java", "src/test/java/shop/ShopWebTest.java"],
    "a2": ["src/main/java/shop/CartPage.java"],
    "h1": ["src/main/java/shop/CartPage.java"],
    "h2": ["src/main/java/core/WaitHelper.java"],
    "m1": ["README.md"],
    "x1": ["src/main/java/shop/LoginPage.java"],
    "r1": ["src/main/java/shop/LoginPage.java"],
}


def _details(sha):
    parents = [{"sha": "p1"}, {"sha": "p2"}] if sha == "m1" else [{"sha": "p1"}]
    return {"parents": parents,
            "files": [{"filename": f, "status": "modified", "additions": 1, "deletions": 1,
                       "patch": "@@ -1 +1 @@\n-old\n+new"} for f in FILES.get(sha, [])]}


WHO = {"agent": "test-authoring-agent", "session": "20261014-090000-create-shop", "target": "Shop"}


class TestAttribute:
    def test_reads_the_overview_table(self):
        assert collector.attribute(_pr()) == WHO

    def test_older_authoring_prs_are_known_by_their_heading_or_title(self):
        legacy = {"agent": "test-authoring-agent", "session": "", "target": ""}
        assert collector.attribute(_pr(body="## QA Auto-Create — Shop\n\n| Module | Shop |")) == legacy
        assert collector.attribute({"title": "Authoring Agent: Shop automation [done]",
                                    "body": "anything"}) == legacy

    def test_a_person_s_pr_is_not_an_agent_s(self):
        assert collector.attribute({"title": "chore: tidy", "body": "Merged the shop branch"}) is None


class TestCommitEvents:
    @pytest.fixture
    def events(self):
        return collector.commit_events(REPO, _pr(), WHO, COMMITS, _details)

    def test_only_commits_after_the_agent_s_own_count(self, events):
        commits = {e["artifact"]["commit"] for e in events}
        assert "base1" not in commits and "a1" not in commits

    def test_a_correction_names_the_agent_s_files_it_touched(self, events):
        h1 = next(e for e in events if e["artifact"]["commit"] == "h1")
        assert h1["kind"] == "human_commit" and h1["explicit"] is False
        assert h1["artifact"]["touches_agent_files"] == ["src/main/java/shop/CartPage.java"]
        assert "+new" in h1["diff"]

    def test_other_work_on_the_branch_touches_none(self, events):
        h2 = next(e for e in events if e["artifact"]["commit"] == "h2")
        assert h2["artifact"]["touches_agent_files"] == []

    def test_merges_and_other_agents_commits_are_not_people_s(self, events):
        assert not {"m1", "x1"} & {e["artifact"]["commit"] for e in events}

    def test_a_revert_is_kept_with_its_reason_and_inferred(self, events):
        revert = next(e for e in events if e["kind"] == "revert")
        assert revert["explicit"] is False
        assert revert["agent"] == "test-healing-agent"     # whose commit was undone
        assert revert["session"] == ""                     # not this PR's run
        assert revert["artifact"]["reverts_agent_commit"] is True
        assert "real failure to reproduce" in revert["text"]

    def test_a_branch_with_no_agent_commit_yields_nothing(self):
        assert collector.commit_events(REPO, _pr(), WHO, COMMITS[:1], _details) == []

    def test_details_are_fetched_only_when_a_person_committed(self):
        asked = []
        collector.commit_events(REPO, _pr(), WHO, COMMITS[1:3],
                                lambda sha: asked.append(sha) or _details(sha))
        assert asked == []


class TestPullRequestEvents:
    def test_a_ticked_verdict_is_explicit(self):
        body = AUTHORING_BODY + "\n" + feedback.verdict_block().replace(
            "- [ ] Reject", "- [x] Reject").replace("- [ ] Test data", "- [x] Test data")
        event = collector.verdict_event(REPO, _pr(body=body), WHO)
        assert event["explicit"] is True and event["verdict"] == "reject"
        assert event["reasons"] == ["test-data"] and event["session"] == WHO["session"]

    def test_an_unticked_block_records_nothing(self):
        body = AUTHORING_BODY + "\n" + feedback.verdict_block()
        assert collector.verdict_event(REPO, _pr(body=body), WHO) is None

    def test_comments_reviews_and_lessons(self):
        events = collector.discussion_events(
            REPO, _pr(), WHO,
            comments=[{"id": "c1", "body": "/lesson Read the total from the cart page",
                       "author": {"login": "rev"}, "createdAt": "t"},
                      {"id": "c2", "body": "Why a new helper?", "author": {"login": "rev"}}],
            reviews=[{"id": "r1", "body": "", "state": "CHANGES_REQUESTED",
                      "author": {"login": "rev"}},
                     {"id": "r2", "body": "", "state": "COMMENTED"}],
            inline=[{"id": 9, "body": "duplicate of CartPage.total()", "path": "CartPage.java",
                     "line": 12, "user": {"login": "rev"}}])
        kinds = [(e["kind"], e["text"]) for e in events]
        assert ("lesson", "Read the total from the cart page") in kinds
        assert ("comment", "Why a new helper?") in kinds
        assert ("review", "changes requested") in kinds
        assert len([k for k in kinds if k[0] == "review"]) == 1   # an empty COMMENTED says nothing
        inline = next(e for e in events if e["text"].startswith("duplicate"))
        assert inline["artifact"]["path"] == "CartPage.java"

    @pytest.mark.parametrize("state, kind", [("MERGED", "pr_outcome"), ("CLOSED", "pr_outcome"),
                                             ("OPEN", None)])
    def test_an_outcome_is_only_ever_inferred(self, state, kind):
        event = collector.outcome_event(REPO, _pr(state=state, mergedAt="m", closedAt="c"), WHO)
        assert (event and event["kind"]) == kind
        assert event is None or event["explicit"] is False


class TestPostMerge:
    def test_people_s_edits_after_the_merge(self):
        history = [{"sha": "e1", "parents": [{"sha": "p"}],
                    "commit": {"message": "tighten the cart check", "author": {"date": "d"}},
                    "author": {"login": "rev"}},
                   {"sha": "e2", "parents": [{"sha": "p"}],
                    "commit": {"message": "healing: repair locators\n\nAgent: test-healing-agent"}}]
        events = collector.post_merge_events(
            REPO, _pr(state="MERGED", mergedAt="2026-10-14T09:00:00Z"), WHO,
            ["src/main/java/shop/CartPage.java"], 14, "main", lambda *a: history,
            lambda sha: {"files": [{"filename": "src/main/java/shop/CartPage.java"}]})
        assert [e["artifact"]["commit"] for e in events] == ["e1"]
        assert events[0]["kind"] == "post_merge_edit" and events[0]["explicit"] is False

    def test_an_unmerged_pr_has_none(self):
        assert collector.post_merge_events(REPO, _pr(), WHO, ["a"], 14, "main", None, None) == []

    def test_no_branch_to_follow_means_none(self):
        merged = _pr(state="MERGED", mergedAt="2026-10-14T09:00:00Z")
        assert collector.post_merge_events(REPO, merged, WHO, ["a"], 14, None, None, None) == []


class Branches:
    def __init__(self, existing=("main",), reached=True):
        self.existing, self.reached = set(existing), reached

    def branch_exists(self, name):
        return name in self.existing

    def default_branch(self):
        return "main"

    def reaches(self, branch, sha):
        return self.reached


class TestBranchToFollow:
    def _merged(self, base):
        return _pr(state="MERGED", baseRefName=base, mergeCommit={"oid": "m"})

    def test_the_base_branch_while_it_exists(self):
        assert collector.branch_to_follow(Branches({"main", "feature"}),
                                          self._merged("feature")) == ("feature", "")

    def test_a_deleted_base_whose_merge_reached_the_default_branch(self):
        assert collector.branch_to_follow(Branches(), self._merged("feature")) == ("main", "")

    def test_a_deleted_base_whose_merge_went_nowhere(self):
        branch, why = collector.branch_to_follow(Branches(reached=False), self._merged("feature"))
        assert branch is None and "feature no longer exists" in why


class TestRetries:
    def _github(self, outcomes):
        calls, slept = [], []

        def run(cmd, **kwargs):
            calls.append(cmd)
            outcome = outcomes.pop(0)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, 120)
            code, err = outcome
            return subprocess.CompletedProcess(cmd, code, "[]" if code == 0 else "", err)

        return collector.GitHub(REPO, run=run, sleep=slept.append), calls, slept

    def test_a_gateway_timeout_is_tried_again(self):
        gh, calls, slept = self._github([(1, "HTTP 504: 504 Gateway Timeout"), "timeout", (0, "")])
        assert gh.inline_comments(1) == []
        assert len(calls) == 3 and slept == [3, 10]

    def test_a_not_found_is_not_retried(self):
        gh, calls, slept = self._github([(1, "gh: Not Found (HTTP 404)")])
        with pytest.raises(RuntimeError, match="404"):
            gh.inline_comments(1)
        assert len(calls) == 1 and slept == []

    def test_it_gives_up_after_two_retries(self):
        gh, calls, _ = self._github([(1, "HTTP 502")] * 3)
        with pytest.raises(RuntimeError, match="502"):
            gh.inline_comments(1)
        assert len(calls) == 3

    def test_a_missing_branch_is_a_no_not_an_error(self):
        gh, _, _ = self._github([(1, "gh: Not Found (HTTP 404)")])
        assert gh.branch_exists("gone") is False


class FakeGitHub:
    repo = REPO

    def __init__(self, prs, broken=()):
        self.prs, self.broken, self.viewed = prs, set(broken), []

    def pull_requests(self, limit):
        return self.prs

    def pull_request(self, number):
        self.viewed.append(number)
        if number in self.broken:
            raise RuntimeError("gh: Not Found (HTTP 404)")
        return {"comments": [{"id": "c", "body": "/lesson keep it small"}], "reviews": [],
                "commits": COMMITS, "files": []}

    def inline_comments(self, number):
        raise AssertionError("not asked: the PR has no review")

    def commit(self, sha):
        return _details(sha)

    def file_history(self, *args):
        return []

    def branch_exists(self, name):
        return False

    def default_branch(self):
        return "main"

    def reaches(self, branch, sha):
        return False


class TestCollect:
    def test_skips_people_s_prs_and_reports_what_could_not_be_read(self):
        gh = FakeGitHub([_pr(1), {"number": 2, "title": "chore", "body": ""},
                         _pr(3, state="CLOSED", closedAt="c"),
                         _pr(4, state="MERGED", mergedAt="2026-10-14T09:00:00Z",
                             baseRefName="feature", mergeCommit={"oid": "m"})], broken={3})
        result = collector.collect(gh, 100, 14, log=lambda *_: None)
        assert gh.viewed == [1, 3, 4]
        assert [p["number"] for p in result["prs"]] == [1, 3, 4]
        assert len(result["errors"]) == 1 and "PR #3" in result["errors"][0]
        assert result["not_followed"] == [
            "PR #4: its base branch feature no longer exists, and the merge never reached main"]
        kinds = {(e["artifact"]["pr"], e["kind"]) for e in result["events"]}
        assert (1, "lesson") in kinds and (3, "pr_outcome") in kinds

    def test_the_report_separates_corrections_from_other_commits(self):
        result = collector.collect(FakeGitHub([_pr(1)]), 100, 14, log=lambda *_: None)
        text = collector.report(result, result["events"], dry_run=True)
        corrections = text.split("## People's corrections")[1].split("##")[0]
        others = text.split("## Other commits")[1].split("##")[0]
        assert "`h1`" in corrections and "`h2`" not in corrections
        assert "`h2`" in others
        assert "real failure to reproduce" in text


class TestDiagnosisFeedback:
    def test_a_healing_pr_rejected_as_the_wrong_page_is_a_false_locator_fix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "KNOWN_ISSUES", tmp_path / "known-issues.json")
        who = {"agent": "test-healing-agent", "session": "s1", "target": "local-ShopWebTest"}
        reject = feedback.event("verdict", source="github", key=(1,), explicit=True,
                                agent="test-healing-agent", session="s1",
                                artifact={"pr": 5}, verdict="reject", reasons=["wrong-flow"])
        wrong_locator = {**reject, "id": "x", "reasons": ["wrong-locator"]}
        written = collector.record_diagnosis_feedback([reject, wrong_locator],
                                                      [{"number": 5, "_who": who}])
        assert written == 1
        entry = json.loads((tmp_path / "known-issues.json").read_text())[0]
        assert entry["kind"] == "false_locator_fix" and entry["test_name"] == "local-ShopWebTest"

    def test_other_agents_and_verdicts_say_nothing_about_a_diagnosis(self, tmp_path):
        path = tmp_path / "k.json"
        assert not verdict_feedback.from_review(path, "test-authoring-agent", "reject",
                                                ["wrong-flow"], "t")
        assert not verdict_feedback.from_review(path, "test-healing-agent", "edited",
                                                ["wrong-flow"], "t")
        assert not path.exists()


class TestJudgeProposals:
    @pytest.fixture
    def proposals(self, tmp_path):
        path = tmp_path / "proposals.json"
        for session, item in [("s1", 1), ("s1", 2), ("s1", 1), ("s2", 1)]:
            verdict_feedback.record_proposal(path, session, "Shop", item, "locator", None)
        return path

    def test_judges_the_latest_proposal_of_each_item(self, proposals):
        assert verdict_feedback.judge_proposals(proposals, "s1", "accept") == 2
        entries = json.loads(proposals.read_text())
        assert [e["accepted"] for e in entries] == [None, True, True, None]
        assert entries[2]["verdict"] == "accept" and entries[2]["judged_at"]

    def test_an_edited_proposal_was_not_accepted_verbatim(self, proposals):
        verdict_feedback.judge_proposals(proposals, "s1", "edited", item=2)
        entries = json.loads(proposals.read_text())
        assert entries[1]["accepted"] is False and entries[1]["verdict"] == "edited"
        assert verdict_feedback.proposal_acceptance(proposals)["judged"] == 1

    def test_an_unknown_verdict_is_refused(self, proposals):
        with pytest.raises(ValueError):
            verdict_feedback.judge_proposals(proposals, "s1", "maybe")


class TestRecordVerdict:
    def test_records_the_verdict_and_judges_the_proposals(self, tmp_path, capsys):
        agents = tmp_path / "agents"
        verdict_feedback.record_proposal(
            agents / "test-adaptation-agent" / "feedback" / "proposals.json",
            "s1", "Shop", 1, "locator", None)
        assert record_verdict.main(["--agent", "test-adaptation-agent", "--session", "s1",
                                    "--verdict", "edited", "--reason", "convention,no-reuse",
                                    "--note", "use the helper"], agents_dir=agents) == 0
        event = feedback.read()[-1]
        assert (event["source"], event["verdict"], event["reasons"], event["text"]) == \
            ("cli", "edited", ["convention", "no-reuse"], "use the helper")
        assert "Judged 1 proposal" in capsys.readouterr().out

    def test_a_healing_reject_for_the_wrong_page_reaches_known_issues(self, tmp_path):
        agents = tmp_path / "agents"
        record_verdict.main(["--agent", "test-healing-agent", "--session", "s9",
                             "--verdict", "reject", "--reason", "wrong-flow"], agents_dir=agents)
        entries = json.loads((agents / "test-healing-agent" / "feedback"
                              / "known-issues.json").read_text())
        assert entries[0]["kind"] == "false_locator_fix" and entries[0]["session"] == "s9"

    def test_an_unknown_reason_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            record_verdict.main(["--agent", "test-healing-agent", "--session", "s",
                                 "--verdict", "reject", "--reason", "too-slow"],
                                agents_dir=tmp_path)
