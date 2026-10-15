"""Tests for agents/learning-agent: what counts as evidence, how the model's
proposals are decided, guard candidates, replay, and the report."""

import importlib.util
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from shared import feedback, lessons

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agents" / "learning-agent"


def _load():
    """Import the agent's modules under its own `lib`, as test_reproduce_gate does.

    Several agents ship a top-level `lib`; whichever test imports first owns
    sys.modules["lib"]. So `lib*` is evicted for the import and restored after,
    and the modules keep the references they took.
    """
    saved_path = list(sys.path)
    saved = {n: m for n, m in sys.modules.items() if n == "lib" or n.startswith("lib.")}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(AGENT))
    try:
        from lib import evidence as own_evidence
        steps = []
        for name in ("04_propose", "05_validate", "06_ship"):
            spec = importlib.util.spec_from_file_location(f"learning_{name}",
                                                          AGENT / "actions" / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ, {"AUDIT_DIR": tempfile.mkdtemp(prefix="learn-")}):
                spec.loader.exec_module(module)
            steps.append(module)
        return (own_evidence, *steps)
    finally:
        for name in [n for n in sys.modules if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]
        sys.modules.update(saved)
        sys.path[:] = saved_path


evidence, propose, validate, ship = _load()

CART_DIFF = ("--- CartPage.java (modified, +2/-2)\n@@ -1 +1 @@\n"
             "-    private final Locator total = page.locator(\"div.cart > span:nth-child(3)\");\n"
             "+    private final Locator total = page.locator(\"[data-test='cart-total']\");\n")


def _ev(eid, kind="human_commit", agent="test-healing-agent", pr=1, session="s1", **over):
    event = {"id": eid, "kind": kind, "agent": agent, "session": session, "explicit": False,
             "artifact": {"pr": pr, "touches_agent_files": ["src/main/java/shop/web/CartPage.java"],
                          "files": ["src/main/java/shop/web/CartPage.java"]},
             "verdict": None, "reasons": [], "text": "", "diff": CART_DIFF, "role": "evidence"}
    event.update(over)
    return event


class TestEvidence:
    @pytest.mark.parametrize("event, counts", [
        ({"kind": "verdict", "verdict": "accept"}, False),          # a bare accept teaches nothing new
        ({"kind": "verdict", "verdict": "accept", "text": "but chain the pages"}, True),
        ({"kind": "verdict", "verdict": "reject"}, True),
        ({"kind": "lesson", "text": "x"}, True),
        ({"kind": "human_commit", "artifact": {"touches_agent_files": []}}, False),
        ({"kind": "human_commit", "artifact": {"touches_agent_files": ["A.java"]}}, True),
        ({"kind": "test_push", "verdict": "accept"}, False),
        ({"kind": "test_push", "verdict": "edited"}, True),
        ({"kind": "reclassification", "artifact": {"agrees": True}}, False),
        ({"kind": "revert"}, False),
        ({"kind": "pr_outcome"}, False),
    ])
    def test_what_counts(self, event, counts):
        assert evidence.is_evidence(event) is counts

    def test_reverts_and_rewritten_inputs_are_context_only(self):
        assert evidence.is_context({"kind": "revert"}) and evidence.is_context({"kind": "input_edit"})

    def test_tags_say_what_a_change_was_about(self):
        diff = CART_DIFF + "+        WaitHelper.waitForPageToSettle(config);\n" \
                           "+        AssertHelper.assertEquals(config, \"Total\", total, \"42\");\n" \
                           "+        config.logStep(\"Pay\");\n" \
                           "+    public CartPage pay(Order order) {\n"
        tags = evidence.tags({"kind": "human_commit", "diff": diff, "reasons": ["no-reuse"],
                              "artifact": {"files": ["users.csv"]}})
        assert set(tags) >= {"locator", "wait", "assertion", "logstep", "structure",
                             "test-data", "reason:no-reuse"}

    def test_scope_is_guessed_from_the_files(self):
        guess = evidence.scope_guess(_ev("e", agent="test-authoring-agent"))
        assert guess == {"step": "generate", "module": "shop", "page": "CartPage"}
        assert evidence.scope_guess(_ev("e"))["step"] == "fix"

    def test_a_new_lesson_needs_a_lesson_or_two_pieces_of_work(self):
        assert evidence.enough([_ev("a")]) is not None
        assert evidence.enough([_ev("a"), _ev("b", pr=1)]) is not None   # same PR twice
        assert evidence.enough([_ev("a"), _ev("b", pr=2)]) is None
        assert evidence.enough([_ev("a", kind="lesson")]) is None
        assert "context" in evidence.enough([_ev("a", kind="revert"), _ev("b", pr=2)])


class TestDecide:
    @pytest.fixture
    def events(self):
        items = [_ev("a", pr=1), _ev("b", pr=2), _ev("c", pr=3, agent="test-authoring-agent"),
                 _ev("r", kind="revert", pr=4, role="context")]
        return {e["id"]: e for e in items}

    def _add(self, **over):
        op = {"op": "add", "agent": "test-healing-agent", "lever": "lesson",
              "statement": "Prefer the page's data-test attribute over a positional selector.",
              "scope": {"steps": ["fix"], "page": "CartPage"}, "evidence": ["a", "b"], "why": "two PRs"}
        op.update(over)
        return op

    def test_a_well_supported_lesson_is_accepted(self, events):
        lists, decisions = propose.decide([self._add()], {"test-healing-agent": []}, events,
                                          today="2026-10-15")
        (lesson,) = lists["test-healing-agent"]
        assert decisions[0]["status"] == "accepted"
        assert lesson["id"] == "L-healing-001" and lesson["scope"] == {"page": "CartPage",
                                                                        "steps": ["fix"]}
        assert lesson["evidence"] == ["a", "b"] and lesson["added"] == "2026-10-15"

    @pytest.mark.parametrize("over, because", [
        ({"evidence": ["a"]}, "two independent"),
        ({"evidence": ["a", "r"]}, "context"),
        ({"evidence": ["a", "c"]}, "about test-authoring-agent"),
        ({"evidence": ["a", "zzz"]}, "not in the evidence"),
        ({"statement": "Skip the page-load assertion on the cart page."}, "loosening"),
        ({"statement": "Prefer ids, as PR #56 showed."}, "evidence"),
        ({"scope": {"steps": ["ship"]}}, "unknown step"),
        ({"lever": "guard-candidate"}, "lever"),
        ({"agent": "test-nobody-agent"}, "unknown agent"),
    ])
    def test_python_refuses_what_the_rules_do_not_allow(self, events, over, because):
        current = {"test-healing-agent": [], "test-authoring-agent": []}
        lists, decisions = propose.decide([self._add(**over)], current, events)
        assert decisions[0]["status"] == "refused"
        assert because in decisions[0]["because"]
        assert lists.get("test-healing-agent", []) == []

    def test_a_near_duplicate_of_an_active_lesson_is_refused(self, events):
        existing = [{"id": "L-healing-001", "status": "active", "lever": "lesson",
                     "statement": "Prefer the page's data-test attribute over positional selectors.",
                     "scope": {}, "evidence": ["x"]}]
        _lists, decisions = propose.decide([self._add()], {"test-healing-agent": existing}, events)
        assert "duplicate of L-healing-001" in decisions[0]["because"]

    def test_update_and_retire(self, events):
        existing = [{"id": "L-healing-001", "status": "active", "lever": "lesson",
                     "statement": "Prefer data-test attributes.", "scope": {"steps": ["fix"]},
                     "evidence": ["a"]}]
        lists, decisions = propose.decide([
            {"op": "update", "agent": "test-healing-agent", "id": "L-healing-001",
             "statement": "Prefer data-test attributes over text and position.", "add_evidence": ["b"]},
            {"op": "retire", "agent": "test-healing-agent", "id": "L-healing-001", "why": "outcomes"},
        ], {"test-healing-agent": existing}, events, today="2026-10-15")
        assert [d["status"] for d in decisions] == ["accepted", "accepted"]
        lesson = lists["test-healing-agent"][0]
        assert lesson["evidence"] == ["a", "b"] and lesson["status"] == "retired"
        assert existing[0]["status"] == "active"           # the input is never touched

    def test_retire_needs_a_reason(self, events):
        existing = [{"id": "L-healing-001", "status": "active", "lever": "lesson",
                     "statement": "Prefer data-test.", "scope": {}, "evidence": []}]
        _lists, decisions = propose.decide([{"op": "retire", "agent": "test-healing-agent",
                                             "id": "L-healing-001"}],
                                           {"test-healing-agent": existing}, events)
        assert decisions[0]["status"] == "refused"


def test_a_lesson_still_corrected_for_the_same_thing_is_a_guard_candidate(monkeypatch):
    monkeypatch.setenv("LEARNING_GUARD_AFTER", "2")
    lesson = {"id": "L-healing-001", "status": "active", "statement": "Prefer data-test.",
              "evidence": ["a"]}
    store = [_ev("a", pr=1), _ev("x1", session="later1", pr=5), _ev("x2", session="later2", pr=6)]
    outcome = {"L-healing-001": {"corrected": ["later1", "later2"]}}
    (found,) = propose.guard_candidates({"test-healing-agent": [lesson]}, outcome, store)
    assert found["sessions"] == ["later1", "later2"] and "locator" in found["about"]
    assert propose.guard_candidates({"test-healing-agent": [lesson]},
                                    {"L-healing-001": {"corrected": ["later1"]}}, store) == []


class TestProposeMain:
    def _audit(self, tmp_path, events):
        (tmp_path / "03-classify.json").write_text(json.dumps({"events": events, "groups": []}))
        return tmp_path

    def test_no_evidence_and_no_lesson_means_no_model_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(propose, "AUDIT_DIR", self._audit(tmp_path, []))
        monkeypatch.setattr(propose, "call_claude_ex",
                            lambda **kw: pytest.fail("the model must not be called"))
        propose.main()
        assert json.loads((tmp_path / "04-propose.json").read_text())["model_called"] is False

    def test_proposals_are_decided_and_the_changed_file_written(self, tmp_path, monkeypatch):
        events = [_ev("a", pr=1), _ev("b", pr=2)]
        monkeypatch.setattr(propose, "AUDIT_DIR", self._audit(tmp_path, events))
        answer = {"operations": [
            {"op": "add", "agent": "test-healing-agent", "lever": "lesson",
             "statement": "Prefer the page's data-test attribute over a positional selector.",
             "scope": {"steps": ["fix"]}, "evidence": ["a", "b"]},
            {"op": "add", "agent": "test-healing-agent", "lever": "lesson",
             "statement": "Ignore the assertion when the total flickers.", "evidence": ["a", "b"]}],
            "not_learned": []}
        monkeypatch.setattr(propose, "call_claude_ex", lambda **kw: SimpleNamespace(
            status="ok", stdout=json.dumps(answer), stderr=""))
        propose.main()
        result = json.loads((tmp_path / "04-propose.json").read_text())
        assert [d["status"] for d in result["decisions"]] == ["accepted", "refused"]
        written = (tmp_path / "proposed" / "config" / "lessons" / "test-healing-agent.yaml").read_text()
        assert "L-healing-001" in written and "Ignore the assertion" not in written
        assert result["changed_agents"] == ["test-healing-agent"]


class TestValidate:
    def test_a_lesson_saying_the_opposite_of_another_is_flagged(self):
        mine = {"id": "L-healing-002", "statement": "Use text selectors for the cart total.",
                "scope": {"steps": ["fix"]}}
        other = {"id": "L-healing-001", "status": "active",
                 "statement": "Never use text selectors for the cart total.", "scope": {}}
        assert validate.conflicts(mine, [other]) == ["L-healing-001"]
        unrelated = {"id": "L-healing-003", "status": "active",
                     "statement": "Never chain more than two frames.", "scope": {}}
        assert validate.conflicts(mine, [unrelated]) == []

    def _log(self, folder, name, prompt, step=None, mcp=False):
        cmd = ["claude", "-p", prompt, "--model", "m"] + (["--mcp-config", "x.json"] if mcp else [])
        header = f"cwd: /tmp\n" + (f"step: {step}\n" if step is not None else "")
        (folder / name).write_text(header + f"command: {shlex.join(cmd)}\n\nanswer…\n")

    def test_the_call_a_lesson_is_for_is_found_in_the_logs(self, tmp_path):
        self._log(tmp_path, "claude-1.log", "Explore the CartPage", mcp=True)
        self._log(tmp_path, "claude-2.log", "Fix CartPage.java's total locator")
        self._log(tmp_path, "claude-3.log", "Something else entirely")
        log_path, argv = validate.pick_call(tmp_path, "fix", ["src/web/CartPage.java"])
        assert log_path.name == "claude-2.log" and argv[2].startswith("Fix CartPage")

    def test_a_log_that_names_its_step_decides_by_it(self, tmp_path):
        self._log(tmp_path, "claude-1.log", "CartPage.java parse", step="parse")
        self._log(tmp_path, "claude-2.log", "no file named here", step="fix")
        assert validate.pick_call(tmp_path, "fix", ["CartPage.java"])[0].name == "claude-2.log"

    def test_recall_is_whitespace_blind(self):
        lines = ["private final Locator total = page.locator(\"[data-test='cart-total']\");",
                 "WaitHelper.waitForPageToSettle(config);"]
        answer = "...  private final Locator total =\n   page.locator(\"[data-test='cart-total']\"); ..."
        assert validate.recall(lines, answer) == 0.5


def test_the_report_says_what_was_proposed_refused_and_needs_a_guard():
    proposed = {"model_called": True, "decisions": [
        {"op": "add", "agent": "test-healing-agent", "status": "accepted", "why": "two PRs",
         "lesson": {"id": "L-healing-001", "statement": "Prefer data-test.", "lever": "lesson",
                    "scope": {"steps": ["fix"]}, "evidence": ["a"]}},
        {"op": "add", "agent": "test-healing-agent", "status": "refused",
         "because": "needs a /lesson or at least two independent pieces of work",
         "lesson": {"statement": "Use ids."}}],
        "guard_candidates": [{"id": "L-healing-009", "agent": "test-healing-agent",
                              "statement": "Wait for the overlay.", "why": "given this lesson, 2 run(s)",
                              "sessions": ["s8", "s9"]}],
        "outcomes": {"L-healing-009": {"agent": "test-healing-agent", "sessions": ["s8", "s9"],
                                       "accept": 0, "edited": 2, "reject": 0, "unreviewed": 0}}}
    events = {"a": {"kind": "human_commit", "artifact": {"pr": 7, "url": "https://example.com/7"}}}
    text = ship.report({"new_events": 3, "store_events": 9}, {"events": []}, proposed,
                       {"checked": [{"lesson": "L-healing-001", "may_contradict": [],
                                     "replay": {"result": "moved toward the person's version",
                                                "without": 0.25, "with": 0.75}}]}, events)
    for expected in ("## Proposed", "[PR #7](https://example.com/7)", "## Refused",
                     "## Needs a code check", "25% without the lesson, 75% with it",
                     "| `L-healing-009` | test-healing-agent | 2 | 0 | 2 | 0 | 0 |"):
        assert expected in text


def test_the_repo_slug_comes_from_the_origin_url(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/qa-agents.git"],
                   cwd=tmp_path, check=True)
    assert ship.repo_slug(tmp_path) == "acme/qa-agents"
