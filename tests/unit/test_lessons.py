"""Tests for shared/lessons.py: which lessons are usable, which fit a step, and what
a step is given under each LESSONS_MODE."""

import json

import pytest

from shared import feedback, lessons


def _lesson(n=1, statement="Prefer a data-test attribute when the page object already uses them.",
            **over):
    item = {"id": f"L-healing-{n:03d}", "status": "active", "lever": "lesson",
            "statement": statement, "scope": {"steps": ["fix"]}, "evidence": ["e1", "e2"]}
    item.update(over)
    return item


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LESSONS_DIR", str(tmp_path / "lessons"))
    (tmp_path / "lessons").mkdir()
    lessons._ASKED.clear()
    yield tmp_path / "lessons"
    lessons._ASKED.clear()


def _write(folder, agent, items):
    (folder / f"{agent}.yaml").write_text(lessons.file_text(agent, items))


class TestProblems:
    def test_a_good_lesson_has_none(self):
        assert lessons.problems(_lesson(), "test-healing-agent") == []

    @pytest.mark.parametrize("statement", [
        "Skip the page-load check when the page is slow.",
        "Remove the assertion on the total if it flakes.",
        "Disable the locator guard for this module.",
    ])
    def test_a_lesson_that_loosens_a_check_is_refused(self, statement):
        assert any("loosening" in p for p in lessons.problems(_lesson(statement=statement),
                                                              "test-healing-agent"))

    @pytest.mark.parametrize("statement", [
        "Never remove an assertion to make a test pass.",
        "Do not weaken a check; add a wait for the element instead.",
    ])
    def test_a_negated_one_is_a_constraint_and_allowed(self, statement):
        assert lessons.problems(_lesson(statement=statement), "test-healing-agent") == []

    @pytest.mark.parametrize("statement", [
        "As seen in 20261012-080125-fix-ShopTest, prefer ids.",
        "Prefer ids, as PR #56 showed.",
        "See https://example.com/pr/1 and prefer ids.",
    ])
    def test_session_ids_pr_numbers_and_links_stay_in_evidence(self, statement):
        assert any("evidence" in p for p in lessons.problems(_lesson(statement=statement),
                                                             "test-healing-agent"))

    def test_shape_errors(self):
        bad = _lesson(id="lesson-1", status="maybe", lever="hint",
                      scope={"steps": ["ship"], "colour": "red"})
        found = " | ".join(lessons.problems(bad, "test-healing-agent"))
        for word in ("id must", "status", "lever", "unknown step", "unknown scope key"):
            assert word in found

    def test_a_statement_too_long_is_refused(self):
        assert lessons.problems(_lesson(statement="x " * 300), "test-healing-agent")


class TestSelect:
    def test_only_active_lessons_and_only_the_prompt_lever(self, folder):
        _write(folder, "test-healing-agent", [
            _lesson(1), _lesson(2, status="retired"), _lesson(3, lever="convention"),
            _lesson(4, statement="Skip the check when slow.")])
        usable, refused = lessons.load("test-healing-agent")
        assert [l["id"] for l in usable] == ["L-healing-001"]
        assert [r[0] for r in refused] == ["L-healing-004"]

    def test_scope_must_fit_and_the_most_specific_comes_first(self, folder):
        _write(folder, "test-healing-agent", [
            _lesson(1, statement="General advice for every locator fix in any module.",
                    scope={"steps": ["fix"]}),
            _lesson(2, statement="On the cart page, the total lives in a data-test attribute.",
                    scope={"steps": ["fix"], "page": "CartPage"}),
            _lesson(3, statement="Only for the other repo, use its own selectors scheme here.",
                    scope={"repo": "other-repo"}),
        ])
        chosen = lessons.select("test-healing-agent", "fix",
                                {"repo": "shop-automation", "page": "CartPage"})
        assert [l["id"] for l in chosen] == ["L-healing-002", "L-healing-001"]
        assert [l["id"] for l in lessons.select("test-healing-agent", "fix",
                                                {"repo": "shop-automation", "page": "LoginPage"})] \
            == ["L-healing-001"]

    def test_the_size_cap_leaves_out_what_does_not_fit(self, folder):
        _write(folder, "test-healing-agent", [_lesson(i, statement=f"Lesson number {i} " + "x" * 80)
                                              for i in range(1, 6)])
        assert len(lessons.select("test-healing-agent", "fix", {}, max_chars=250)) == 2


class TestBlock:
    @pytest.fixture
    def session(self, folder, tmp_path, monkeypatch):
        _write(folder, "test-healing-agent", [_lesson(1)])
        audit = tmp_path / "audit"
        audit.mkdir()
        monkeypatch.setenv("AUDIT_DIR", str(audit))
        monkeypatch.setenv("STEP_KEY", "fix")
        return audit

    def test_off_gives_nothing_and_records_nothing(self, session, monkeypatch):
        monkeypatch.setenv("LESSONS_MODE", "off")
        assert lessons.block("test-healing-agent") == ""
        assert not (session / "00-lessons.json").exists()

    def test_shadow_records_without_injecting(self, session, monkeypatch):
        monkeypatch.setenv("LESSONS_MODE", "shadow")
        said = []
        assert lessons.block("test-healing-agent", log=said.append) == ""
        recorded = json.loads((session / "00-lessons.json").read_text())
        assert recorded["steps"]["fix"] == {"mode": "shadow", "ids": ["L-healing-001"], "refused": []}
        assert "shadow" in said[0]
        assert lessons.applied_ids(session) == []         # shadow is not "given"

    def test_on_injects_records_and_says_so_once(self, session, monkeypatch):
        monkeypatch.setenv("LESSONS_MODE", "on")
        said = []
        text = lessons.block("test-healing-agent", log=said.append)
        assert "## Lessons from past reviews" in text and "[L-healing-001]" in text
        assert lessons.block("test-healing-agent", log=said.append) == text   # a loop asking again
        assert len(said) == 1
        assert lessons.applied_ids(session) == ["L-healing-001"]

    def test_a_step_asked_with_two_contexts_records_both(self, folder, session, monkeypatch):
        monkeypatch.setenv("LESSONS_MODE", "on")
        _write(folder, "test-healing-agent", [
            _lesson(1, statement="On the cart page, read totals from data-test.",
                    scope={"steps": ["fix"], "page": "CartPage"}),
            _lesson(2, statement="On the login page, the button keeps its id.",
                    scope={"steps": ["fix"], "page": "LoginPage"})])
        lessons.block("test-healing-agent", page="CartPage")
        lessons.block("test-healing-agent", page="LoginPage")
        assert lessons.applied_ids(session) == ["L-healing-001", "L-healing-002"]


class TestFileAndIds:
    def test_the_file_round_trips(self, folder):
        _write(folder, "test-healing-agent", [_lesson(1)])
        assert lessons.read_file("test-healing-agent")[0]["id"] == "L-healing-001"

    def test_next_id(self):
        assert lessons.next_id("test-healing-agent", ["L-healing-001", "L-healing-007"]) == "L-healing-008"
        assert lessons.next_id("test-design-agent", []) == "L-design-001"


def test_outcomes_join_the_runs_given_a_lesson_to_their_verdicts(tmp_path):
    agents = tmp_path / "agents"
    for session, verdict in (("s1", "accept"), ("s2", "edited"), ("s3", None)):
        audit = agents / "test-healing-agent" / "audit" / session
        audit.mkdir(parents=True)
        (audit / "00-lessons.json").write_text(json.dumps(
            {"steps": {"fix": {"mode": "on", "ids": ["L-healing-001"]}}}))
        if verdict:
            feedback.person_verdict("test-healing-agent", session, verdict, agents_dir=agents)
    row = lessons.outcomes(agents)["L-healing-001"]
    assert (row["accept"], row["edited"], row["unreviewed"]) == (1, 1, 1)
    assert row["corrected"] == ["s2"]
