"""What the adaptation explorer is told before it opens a browser.

An explorer was sent to walk a flow with no start URL, none of the test's steps and
none of its data, and a line saying it was signed in when it was not. It found the
site by guessing, chose its own order for a flow the test spells out, and typed
values that left a form invalid. Separately, the rule that tells it what to refuse
did not match the checker that judges the walk, and the sandbox assertion reached
neither in a way that let a flow finish.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import flow_map

TEST_ID = "automation.shop.ShopWebTest#payByCard"
WALK = {"test": TEST_ID,
        "steps": ["Add the item to the cart", "Pay by card"],
        "data": [{"file": "src/test/resources/shop/orders.csv",
                  "row": {"amount": "500", "phone": "{randomNumber:10}"}}]}


@pytest.fixture
def explore(tmp_path, monkeypatch):
    """03_explore_web.py loaded by path, sys.path and `lib` left as they were."""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    for name in ("ADAPT_ENVIRONMENT", "ADAPT_COUNTRY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    is_lib = lambda n: n == "lib" or n.startswith("lib.")
    for name in [n for n in sys.modules if is_lib(n)]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location(
        "explore_prompt_03", ROOT / "agents/test-adaptation-agent/actions/03_explore_web.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in [n for n in sys.modules if is_lib(n)]:
        del sys.modules[name]
    return module


class TestTheDestructiveRule:
    def test_without_an_assertion_the_explorer_refuses_and_knows_the_words(self, explore):
        rules = explore.load_explore_rules()
        assert "### 4. Destructive actions — refuse them" in rules
        assert ", ".join(flow_map.DESTRUCTIVE_WORDS) in rules, (
            "the checker flags a control by its name; an explorer not told the words "
            "walks one that only opens a screen and loses the whole run")
        assert "this environment is disposable" not in rules

    def test_with_one_it_is_told_to_finish_and_why_it_may(self, explore):
        rules = explore.load_explore_rules("demo store, test payments only")
        assert "### 4. Destructive actions — this environment is disposable" in rules
        assert "demo store, test payments only" in rules
        assert "refuse them" not in rules
        # Global switch, so it must not become licence to act off the script.
        assert "anything the flow does not call for" in rules

    @pytest.mark.parametrize("note", ["", "demo store"])
    def test_one_variant_is_sent_whole_and_the_rules_around_it_stay(self, explore, note):
        rules = explore.load_explore_rules(note)
        assert "rule-4" not in rules and "{destructive_words}" not in rules
        assert "{sandbox_note}" not in rules
        assert rules.count("### 4. ") == 1
        assert rules.index("### 3. ") < rules.index("### 4. ") < rules.index("### 5. ")


class TestWhereTheWalkStarts:
    PLAN = {"module": "Shop", "note": "n", "items": []}

    def _properties(self, workspace, text):
        path = workspace / "parameters" / "staging-sg.properties"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_the_notes_url_wins(self, explore, tmp_path):
        self._properties(tmp_path, "shop.url=https://configured.example.com\n")
        plan = {**self.PLAN, "web_base_url": "https://note.example.com"}
        assert explore.start_url(plan, {}, tmp_path) == "https://note.example.com"

    def test_else_the_url_the_framework_is_configured_with(self, explore, tmp_path):
        self._properties(tmp_path, "shop.url=https://configured.example.com\n")
        assert explore.start_url(self.PLAN, {}, tmp_path) == "https://configured.example.com"

    def test_the_tests_package_names_the_key_when_the_module_does_not(self, explore, tmp_path):
        self._properties(tmp_path, "shop.url=https://configured.example.com\n")
        plan = {**self.PLAN, "module": "Storefront"}
        assert explore.start_url(plan, {"verify": [TEST_ID]}, tmp_path) == (
            "https://configured.example.com")

    def test_nothing_is_made_up_when_neither_says(self, explore, tmp_path):
        assert explore.start_url(self.PLAN, {"verify": [TEST_ID]}, tmp_path) == ""

    def test_the_prompt_carries_the_url_and_the_true_session_state(self, explore):
        out = explore.build_prompt(self.PLAN, "", "", "", {},
                                   {"url": "https://shop.example.com", "signed_in": False})
        assert "URL: https://shop.example.com" in out
        assert "starts signed out" in out and "already signed in" not in out
        signed = explore.build_prompt(self.PLAN, "", "", "", {},
                                      {"url": "https://shop.example.com", "signed_in": True})
        assert "already signed in through a saved session" in signed

    def test_with_no_url_the_explorer_is_still_pointed_at_the_module(self, explore):
        assert "URL: (use the module entry point)" in explore.build_prompt(
            self.PLAN, "", "", "", {}, {"url": "", "signed_in": False})


class TestWhatTheTestDoes:
    PLAN = {"module": "Shop", "note": "n", "items": []}

    def test_the_tests_steps_and_values_are_in_the_prompt(self, explore):
        out = explore.build_prompt(self.PLAN, "", "", "", {"test_walks": [WALK]})
        steps = out[out.index("## What the test does today"):]
        assert steps.index("1. Add the item to the cart") < steps.index("2. Pay by card")
        assert TEST_ID in steps
        values = out[out.index("## The values the test types"):]
        assert "- amount: 500" in values and "- phone: {randomNumber:10}" in values
        assert out.index("## What the test does today") < out.index(
            "## What the flow is supposed to achieve")

    def test_a_test_with_neither_adds_nothing(self, explore):
        for scope in ({}, {"test_walks": []},
                      {"test_walks": [{"test": TEST_ID, "steps": [], "data": []}]}):
            out = explore.build_prompt(self.PLAN, "", "", "", scope)
            assert "What the test does today" not in out
            assert "The values the test types" not in out

    def test_steps_without_data_and_data_without_steps(self, explore):
        only_steps = explore.build_prompt(
            self.PLAN, "", "", "", {"test_walks": [{**WALK, "data": []}]})
        assert "What the test does today" in only_steps
        assert "The values the test types" not in only_steps
        only_data = explore.build_prompt(
            self.PLAN, "", "", "", {"test_walks": [{**WALK, "steps": []}]})
        assert "The values the test types" in only_data
        assert "What the test does today" not in only_data


class TestBriefScreens:
    """A screen that closes by itself is measured only by the call that records it."""

    def test_the_rules_say_to_name_it_on_that_call(self, explore):
        rules = explore.load_explore_rules()
        assert "page.qa.record(action, ms, { page: '<pageId>' })" in rules
        # A message on the page already open is not a page of its own.
        assert "pass that page's id" in rules
        # The page the screen leads to is not measured by the recording.
        assert "page.qa.step(null, { page: '<pageId>' })" in rules

    def test_the_shared_rule_mentions_the_name_without_requiring_it(self, explore):
        assert "{ page: '<pageId>' }" in explore.CAPTURE_RULES
        assert "Where your instructions have you name pages" in explore.CAPTURE_RULES

    def test_a_second_walk_is_told_both_ways_to_measure_and_to_keep_its_markers(self, explore):
        note = explore.inventory_note(["receipt"])
        assert "receipt" in note
        assert "page.qa.step(action, { page: '<pageId>' })" in note
        assert "page.qa.record(action, ms, { page: '<pageId>' })" in note
        # A second walk once came back with no FLOW_STEP line at all.
        assert "FLOW_STEP for every action" in note


class TestAWalkThatReportedNoSteps:
    """The explorer drove the browser — every page was measured — and wrote no step
    line. The walk ended as "empty" and the run stopped for want of a flow map."""

    STEP = {"index": 1, "page": {"id": "home", "identity_digest": "d"},
            "action": {"verb": "click", "target": {"name": "open"}},
            "result": {"outcome": "ok"}}

    class _Done:
        status, stdout = "ok", ""

        def browser_unavailable(self):
            return ""

    def _flow(self, steps, measured=True):
        return {"steps": list(steps), "pages": {"home": {"id": "home"}},
                "_inventories": {"home": [{"tag": "h1"}]} if measured else {},
                "status": "ok" if steps else "empty"}

    def _run(self, explore, tmp_path, monkeypatch, walks, attempts=1):
        (tmp_path / "01-parse-change.json").write_text(json.dumps(
            {"type": "web", "module": "shop", "items": []}))
        (tmp_path / "02-scope.json").write_text(json.dumps(
            {"workspace": str(tmp_path), "entry_path": {"mode": "none"}}))
        notes_seen = []

        def run_attempt(_plan, _rules, notes, *args, **kwargs):
            notes_seen.append(notes)
            return walks[len(notes_seen) - 1], self._Done()

        monkeypatch.setattr(explore, "run_attempt", run_attempt)
        monkeypatch.setattr(explore, "ATTEMPTS", attempts)
        explore.main()
        return notes_seen, json.loads((tmp_path / "03-explore-web.json").read_text())

    def test_it_is_walked_again_and_told_why(self, explore, tmp_path, monkeypatch):
        notes, written = self._run(explore, tmp_path, monkeypatch,
                                   [self._flow([]), self._flow([self.STEP])])
        assert len(notes) == 2
        assert notes[0] == "" and "You reported no steps" in notes[1]
        assert "FLOW_STEP: {...}" in notes[1] and "summary at the end" in notes[1]
        assert written["attempts"] == 2 and len(written["flow"]["steps"]) == 1

    def test_a_walk_that_measured_nothing_is_not(self, explore, tmp_path, monkeypatch):
        # Nothing was driven: there is no walk to ask for the report of.
        notes, written = self._run(explore, tmp_path, monkeypatch,
                                   [self._flow([], measured=False)])
        assert len(notes) == 1 and written["status"] == "empty"

    def test_a_walk_that_reported_its_steps_is_not(self, explore, tmp_path, monkeypatch):
        notes, _written = self._run(explore, tmp_path, monkeypatch,
                                    [self._flow([self.STEP])])
        assert len(notes) == 1

    def test_no_second_walk_when_none_is_allowed(self, explore, tmp_path, monkeypatch):
        notes, written = self._run(explore, tmp_path, monkeypatch,
                                   [self._flow([])], attempts=0)
        assert len(notes) == 1 and written["status"] == "empty"

    def test_it_is_not_walked_again_when_the_record_shows_a_flagged_click(
            self, explore, tmp_path, monkeypatch):
        # Walked again, it would click the same controls again.
        flow = {**self._flow([]), "status": "unsafe",
                "violations": [{"index": None, "token": "pay", "target": "Pay now",
                                "recorded": True}]}
        notes, written = self._run(explore, tmp_path, monkeypatch, [flow])
        assert len(notes) == 1 and written["status"] == "unsafe"
