"""Tests for shared/feedback.py and the two places that write it into agent output:
the `Agent:` commit trailer (shared/git.py) and the PR verdict block
(shared/github.py).
"""

import json
import subprocess

import pytest

from shared import feedback, github
from shared.git import with_agent_trailer


def _tick(body: str, *labels: str) -> str:
    for label in labels:
        body = body.replace(f"- [ ] {label}", f"- [x] {label}", 1)
    return body


class TestVerdictBlock:
    def test_an_unticked_block_is_not_reviewed(self):
        parsed = feedback.parse_verdict("Summary\n\n" + feedback.verdict_block())
        assert parsed == {"verdict": None, "reasons": [], "ambiguous": False}

    def test_reads_the_verdict_and_the_reasons_ticked(self):
        body = _tick(feedback.verdict_block(), feedback.VERDICTS["edited"],
                     "Convention or style", "Didn't reuse existing code")
        parsed = feedback.parse_verdict(body)
        assert parsed["verdict"] == "edited"
        assert parsed["reasons"] == ["convention", "no-reuse"]

    def test_github_writes_an_upper_case_x(self):
        body = feedback.verdict_block().replace("- [ ] Reject", "- [X] Reject")
        assert feedback.parse_verdict(body)["verdict"] == "reject"

    def test_two_verdicts_are_ambiguous_not_averaged(self):
        body = _tick(feedback.verdict_block(), "Accept as is", "Reject")
        parsed = feedback.parse_verdict(body)
        assert parsed["verdict"] is None and parsed["ambiguous"] is True

    def test_a_body_without_the_block_has_no_verdict(self):
        assert feedback.parse_verdict("- [x] Accept as is") is None

    def test_ticks_outside_the_block_do_not_count(self):
        body = "- [x] Reject\n\n" + feedback.verdict_block()
        assert feedback.parse_verdict(body)["verdict"] is None


class TestLesson:
    def test_reads_the_lesson(self):
        assert feedback.parse_lesson("/lesson Keep test data in the module's CSV") \
            == "Keep test data in the module's CSV"

    def test_keeps_a_lesson_that_runs_over_several_lines(self):
        text = feedback.parse_lesson("/lesson First line\nsecond line")
        assert text == "First line\nsecond line"

    def test_case_and_leading_space_do_not_matter(self):
        assert feedback.parse_lesson("  /LESSON chain page objects") == "chain page objects"

    @pytest.mark.parametrize("comment", ["Looks good", "/lessons learned", "/lesson", ""])
    def test_anything_else_is_not_a_lesson(self, comment):
        assert feedback.parse_lesson(comment) is None


class TestCommits:
    def test_the_trailer_names_the_agent(self):
        message = ("healing: repair locators\n\nFixed: CartTest\n\n"
                   "Agent: test-healing-agent\nSession: 20261014-1-fix-shop")
        assert feedback.agent_of_commit(message) == "test-healing-agent"
        assert feedback.session_of_commit(message) == "20261014-1-fix-shop"

    @pytest.mark.parametrize("subject, agent", [
        ("authoring: generate initial test draft for shop", "test-authoring-agent"),
        ("healing: repair locators for 1 test(s) (local)", "test-healing-agent"),
        ("adaptation: item 1 — locator (shop)", "test-adaptation-agent"),
        ("[Authoring Agent]: First draft for shop", "test-authoring-agent"),
        ("feat(automation): add ShopWebTest test automation — passed", "test-authoring-agent"),
        ("fix(automation): update locators for 1 test(s)", "test-healing-agent"),
        ("chore(baselines): refresh 3 locator fingerprint(s)", feedback.BASELINE_JOB),
    ])
    def test_older_commits_are_known_by_their_subject(self, subject, agent):
        assert feedback.agent_of_commit(subject) == agent

    @pytest.mark.parametrize("message", [
        "object chaining",
        "Merge branch 'main' into authoring/shop",
        'Revert "authoring: generate initial test draft for shop"',
        "Signed-off-by: A Person <person@example.com>",
        "fix flaky wait\n\nCo-Authored-By: A Person <person@example.com>",
    ])
    def test_a_person_s_commit_has_no_agent(self, message):
        assert feedback.agent_of_commit(message) is None

    def test_a_last_paragraph_that_is_not_all_trailers_has_none(self):
        assert feedback.trailers("subject\n\nAgent: test-healing-agent\nnot a trailer") == {}


class TestAgentTrailer:
    def test_joins_the_session_line(self):
        signed = with_agent_trailer("healing: repair locators\n\nFixed: CartTest\n\n"
                                    "Session: s1", "test-healing-agent")
        assert signed.endswith("\n\nAgent: test-healing-agent\nSession: s1")
        assert feedback.trailers(signed) == {"Agent": "test-healing-agent", "Session": "s1"}

    def test_a_message_with_no_session_gets_its_own_paragraph(self):
        signed = with_agent_trailer("adaptation: item 2", "test-adaptation-agent")
        assert signed == "adaptation: item 2\n\nAgent: test-adaptation-agent"
        assert feedback.agent_of_commit(signed) == "test-adaptation-agent"

    def test_signing_twice_changes_nothing(self):
        once = with_agent_trailer("authoring: x\n\nSession: s1", "test-authoring-agent")
        assert with_agent_trailer(once, "test-authoring-agent") == once

    def test_git_reads_the_trailers_the_same_way(self):
        signed = with_agent_trailer("authoring: x\n\nbody\n\nSession: s1", "test-authoring-agent")
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=signed,
                                capture_output=True, text=True, check=True).stdout
        assert "Agent: test-authoring-agent" in parsed and "Session: s1" in parsed


class TestVerdictBlockInPRs:
    def test_every_body_ends_with_the_block(self):
        body = github.with_verdict_block("## Summary\n\nok\n")
        assert body.startswith("## Summary\n\nok\n\n")
        assert body.rstrip().endswith(feedback.BLOCK_END)

    def test_a_body_too_long_for_github_is_cut_before_the_block(self):
        body = github.with_verdict_block("x" * (github.MAX_BODY_CHARS + 500))
        assert len(body) <= github.MAX_BODY_CHARS
        assert "truncated" in body
        assert feedback.parse_verdict(body) == {"verdict": None, "reasons": [],
                                                "ambiguous": False}

    def test_create_pr_sends_the_block(self, monkeypatch, tmp_path):
        sent = {}

        def run(cmd, **kwargs):
            sent["body"] = cmd[cmd.index("--body") + 1]
            return subprocess.CompletedProcess(cmd, 0, "https://example.com/pr/1\n", "")

        monkeypatch.setattr(github.subprocess, "run", run)
        url, err = github.create_pr(tmp_path, "acme/automation", "t", "the body", "b", "main")
        assert url == "https://example.com/pr/1" and err == ""
        assert sent["body"].startswith("the body") and feedback.BLOCK_BEGIN in sent["body"]


class TestStore:
    def _event(self, n, **kw):
        return feedback.event("comment", source="github", key=("acme/automation", 7, n),
                              explicit=True, agent="test-healing-agent", text=f"c{n}", **kw)

    def test_the_same_record_gets_the_same_id(self):
        assert self._event(1)["id"] == self._event(1)["id"] != self._event(2)["id"]

    def test_append_writes_only_what_is_new(self, tmp_path):
        path = tmp_path / "store.jsonl"
        assert len(feedback.append([self._event(1), self._event(2)], path)) == 2
        assert [e["text"] for e in feedback.append([self._event(1), self._event(3)], path)] == ["c3"]
        assert [e["text"] for e in feedback.read(path)] == ["c1", "c2", "c3"]
        assert all(e["recorded_at"] for e in feedback.read(path))

    def test_a_truncated_line_is_skipped(self, tmp_path):
        path = tmp_path / "store.jsonl"
        feedback.append([self._event(1)], path)
        with path.open("a") as handle:
            handle.write('{"id": "half')
        assert len(feedback.read(path)) == 1

    def test_the_store_follows_FEEDBACK_FILE(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FEEDBACK_FILE", str(tmp_path / "elsewhere.jsonl"))
        feedback.append([self._event(1)])
        assert json.loads((tmp_path / "elsewhere.jsonl").read_text())["text"] == "c1"
