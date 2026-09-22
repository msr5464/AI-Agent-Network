"""What the adapt step checks before a human is shown a diff, and who gets told.

Three findings from one review of a live run:

  * propose-only — the default — stopped at the diff-shaped guards, so the diff a
    human was asked to trust had never been compiled and never been checked
    against the frozen contracts;
  * `matches_negative` was in the guard table of two agents and had never had
    anything to compare against (healing passes it an empty list), while the flow
    map carried the logged-out page's inventory the whole time;
  * a run whose every item was rejected posted "0 change item(s) applied" to the
    success Slack channel.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AGENT = ROOT / "agents" / "test-adaptation-agent"


def _load(name: str, relative: str, tmp_path, monkeypatch):
    """Load an action by path, leaving sys.path and `lib` as the session had them."""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    sys.path.insert(0, str(AGENT))
    is_lib = lambda n: n == "lib" or n.startswith("lib.")
    for mod in [n for n in sys.modules if is_lib(n)]:
        monkeypatch.delitem(sys.modules, mod)
    spec = importlib.util.spec_from_file_location(name, AGENT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for mod in [n for n in sys.modules if is_lib(n)]:
        del sys.modules[mod]
    return module


class _Txn:
    """Just enough Transaction for verify_proposal: it only compiles."""

    def __init__(self, result):
        self._result = result
        self.compiled = False

    def compile(self, workspace, command, timeout_s=600):
        self.compiled = True
        return self._result


class TestVerifyProposal:
    def test_a_proposal_that_does_not_compile_is_rejected(self, tmp_path, monkeypatch):
        adapt = _load("adapt_compile", "actions/04_adapt.py", tmp_path, monkeypatch)
        record = {}
        ok, why = adapt.verify_proposal(_Txn((False, "javac: cannot find symbol")),
                                        {}, tmp_path, record)
        assert ok is False and "does not compile" in why, (
            "propose-only is still the agent's recommendation — it has to build")

    def test_a_compiler_that_cannot_run_is_infra_not_a_bad_proposal(self, tmp_path,
                                                                    monkeypatch):
        adapt = _load("adapt_nomvn", "actions/04_adapt.py", tmp_path, monkeypatch)
        record = {}
        ok, _ = adapt.verify_proposal(
            _Txn((False, "could not run the compiler: [Errno 2] no mvn")),
            {}, tmp_path, record)
        assert ok is True
        assert "not compiled" in record["compile_status"]

    def test_a_clean_proposal_passes_and_records_that_it_compiled(self, tmp_path,
                                                                  monkeypatch):
        adapt = _load("adapt_clean", "actions/04_adapt.py", tmp_path, monkeypatch)
        record = {}
        ok, why = adapt.verify_proposal(_Txn((True, "")), {}, tmp_path, record)
        assert (ok, why) == (True, "")
        assert record["compile_status"] == "compiles"
        assert record["check_changes"] == [], "no contracts in scope, no check changed"


class TestNegativeDocuments:
    FLOW = {
        "pages": {"login-page": {"url": "https://www.saucedemo.com/", "title": "Swag Labs"},
                  "cart-page": {"url": "https://www.saucedemo.com/cart.html"}},
        "_inventories": {
            "login-page": [{"tag": "input", "id": "user-name"},
                           {"tag": "input", "id": "login-button"}],
            "cart-page": [{"tag": "div", "class": "cart_quantity"}],
        },
    }

    def test_the_logged_out_page_becomes_a_negative(self, tmp_path, monkeypatch):
        adapt = _load("adapt_neg", "actions/04_adapt.py", tmp_path, monkeypatch)
        docs = adapt.negative_documents(self.FLOW)
        assert len(docs) == 1, "only the login page is a page the flow must not end on"

    def test_an_anchor_that_also_matches_the_login_page_is_rejected(self, tmp_path,
                                                                    monkeypatch):
        adapt = _load("adapt_neg2", "actions/04_adapt.py", tmp_path, monkeypatch)
        from shared.edit_guards import matches_negative
        docs = adapt.negative_documents(self.FLOW)
        assert matches_negative(["#login-button"], docs)[0] is False
        assert matches_negative([".cart_quantity"], docs)[0] is True

    def test_no_negative_page_means_no_opinion(self, tmp_path, monkeypatch):
        adapt = _load("adapt_neg3", "actions/04_adapt.py", tmp_path, monkeypatch)
        assert adapt.negative_documents({"pages": {}, "_inventories": {}}) == []


class TestCoveringItem:
    """`covered_by` is the model's claim; only a claim Python can check lands.

    Anything unchecked must fall through to the normal handling — the claim may
    never discard real edits, and in propose-only mode `done` is always empty.
    """
    DONE = [{"index": 1, "summary": "s", "diff": "d", "verified": ["T#m"]}]

    def test_a_claim_naming_a_verified_item_holds(self, tmp_path, monkeypatch):
        adapt = _load("adapt_cov", "actions/04_adapt.py", tmp_path, monkeypatch)
        for value in (1, "1", " 1 "):
            assert adapt.covering_item({"covered_by": value}, self.DONE) is self.DONE[0]

    @pytest.mark.parametrize("value", [2, 0, None, "null", "", True, 1.5, "item 1", [1], "²"])
    def test_anything_else_is_ignored_without_raising(self, value, tmp_path, monkeypatch):
        adapt = _load("adapt_cov_bad", "actions/04_adapt.py", tmp_path, monkeypatch)
        assert adapt.covering_item({"covered_by": value}, self.DONE) is None

    def test_edits_always_win_over_the_claim(self, tmp_path, monkeypatch):
        adapt = _load("adapt_cov_edits", "actions/04_adapt.py", tmp_path, monkeypatch)
        payload = {"covered_by": 1, "edits": [{"file": "X.java"}]}
        assert adapt.covering_item(payload, self.DONE) is None

    def test_nothing_done_means_nothing_can_be_covered(self, tmp_path, monkeypatch):
        adapt = _load("adapt_cov_empty", "actions/04_adapt.py", tmp_path, monkeypatch)
        assert adapt.covering_item({"covered_by": 1}, []) is None
        assert adapt.done_section([]) == ""

    def test_the_done_section_carries_the_diff_and_marks_truncation(self, tmp_path,
                                                                   monkeypatch):
        adapt = _load("adapt_cov_note", "actions/04_adapt.py", tmp_path, monkeypatch)
        out = adapt.done_section([{**self.DONE[0], "diff": "+x" * 1500}])
        assert "Item 1" in out and "T#m" in out and "… (truncated)" in out


class TestNeedsAHuman:
    @pytest.fixture
    def ship(self, tmp_path, monkeypatch):
        return _load("ship_alert", "actions/05_ship.py", tmp_path, monkeypatch)

    def test_every_item_rejected_reaches_a_person(self, ship):
        alert, detail = ship.needs_a_human(
            "", [], [{"status": "rejected", "reason": "assertion conservation failed"}], [])
        assert alert is True
        assert "could not be adapted" in detail and "conservation" in detail

    def test_a_run_that_applied_something_is_not_an_alert(self, ship):
        applied = [{"status": "applied"}]
        assert ship.needs_a_human("", [], applied, applied) == (False, "")

    def test_an_explicit_escalation_still_wins(self, ship):
        alert, detail = ship.needs_a_human(
            "escalate", [{"what": "item 1", "why": "the specification moved"}], [], [])
        assert alert is True and "item 1" in detail

    def test_tests_that_still_fail_reach_a_person(self, ship):
        applied = [{"status": "partial"}]
        alert, detail = ship.needs_a_human("", [], applied, applied, ["T#b"])
        assert alert is True and "still fail" in detail

    def test_being_stuck_is_escalating(self, ship):
        assert "stuck" in ship.ESCALATING, (
            "the stop rule writes this skip reason; it means a human has to look")


class TestAlreadyApplied:
    """The "nothing to change" skip, per kind of item."""

    @pytest.fixture
    def adapt(self, tmp_path, monkeypatch):
        return _load("adapt_skip", "actions/04_adapt.py", tmp_path, monkeypatch)

    @pytest.mark.parametrize("kind", ["step_insert", "locator", "route", "test_data"])
    def test_items_that_only_add_or_retarget_steps_are_skipped_when_seen(self, adapt, kind):
        assert adapt.already_applied({"added": []}, [{"kind": kind}]) is True

    @pytest.mark.parametrize("kind", ["step_merge", "coverage_changed", "outcome_changed",
                                      "content_changed", "coverage_added"])
    def test_items_that_remove_or_check_are_never_skipped(self, adapt, kind):
        # None of these adds an interaction, so each would always look applied.
        assert adapt.already_applied({"added": []}, [{"kind": kind}]) is False

    def test_a_new_step_means_not_applied(self, adapt):
        assert adapt.already_applied({"added": [{"flow_index": 1}]}, [{"kind": "step_insert"}]) is False

    def test_a_note_of_only_unclassified_items_goes_on_to_escalate(self, adapt):
        assert adapt.already_applied({"added": []}, [{"kind": "unclassified"}]) is False


class TestCheckChangesGuard:
    BEFORE = ("public class T {\n    public void flow() {\n"
              '        AssertHelper.assertEquals(config, a.count(), "1", "Badge shows 1");\n'
              '        AssertHelper.assertTrue(config, a.shown(), "Page is shown");\n'
              "    }\n}\n")
    AFTER = BEFORE.replace('        AssertHelper.assertTrue(config, a.shown(), "Page is shown");\n', "")

    @pytest.fixture
    def adapt(self, tmp_path, monkeypatch):
        module = _load("adapt_guard", "actions/04_adapt.py", tmp_path, monkeypatch)
        cc = module.check_changes
        after = {"checks": cc.file_checks({"T.java": self.AFTER}), "unresolved": []}
        monkeypatch.setattr(cc, "measure", lambda scope, workspace: after)
        monkeypatch.setattr(cc, "enabled_tests", lambda workspace: [])
        return module

    def _judge(self, adapt, tmp_path, declared, kind="coverage_changed"):
        from types import SimpleNamespace
        cc = adapt.check_changes
        before = {"checks": cc.file_checks({"T.java": self.BEFORE}), "unresolved": [],
                  "index": {}}
        txn = SimpleNamespace(snapshots={"/ws/T.java": self.BEFORE},
                              staged={"/ws/T.java": self.AFTER})
        record = {"guards": []}
        ok, why = adapt.judge_checks({"index": 1, "kind": kind}, {"check_changes": declared},
                                     txn, {"intent_contracts": {}}, tmp_path, {}, before, record)
        return ok, why, record, before

    def test_an_undeclared_removal_is_refused_and_recorded_as_a_guard(self, adapt, tmp_path):
        ok, why, record, _ = self._judge(adapt, tmp_path, [])
        assert not ok and "without declaring" in why
        assert record["guards"][-1] == {"guard": "check_changes", "ok": False, "reason": why}

    def test_the_declared_removal_passes_and_is_listed(self, adapt, tmp_path):
        before = adapt.check_changes.file_checks({"T.java": self.BEFORE})
        cid = next(c["id"] for c in before if c["message"] == "Page is shown")
        ok, why, record, _ = self._judge(adapt, tmp_path,
                                         [{"check": cid, "action": "remove", "why": "item 1"}])
        assert (ok, why) == (True, "")
        assert [r["message"] for r in record["check_changes"]] == ["Page is shown"]

    def test_a_refusing_judge_rejects_the_proposal(self, adapt, tmp_path):
        ok, why = adapt.verify_proposal(_Txn((True, "")), {}, tmp_path, {},
                                        judge=lambda: (False, "changes a check without declaring it"))
        assert (ok, why) == (False, "changes a check without declaring it")

    def test_outside_means_not_re_run(self, adapt, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(adapt.check_changes, "reached_outside",
                            lambda entries, tests, in_scope, fp: seen.setdefault("s", in_scope) and {})
        from types import SimpleNamespace
        cc = adapt.check_changes
        before = {"checks": cc.file_checks({"T.java": self.BEFORE}), "unresolved": [], "index": {}}
        txn = SimpleNamespace(snapshots={"/ws/T.java": self.BEFORE}, staged={"/ws/T.java": self.AFTER})
        adapt.judge_checks({"index": 1, "kind": "coverage_changed"}, {"check_changes": []}, txn,
                           {"intent_contracts": {"a.T#flow": {}, "a.T#sibling": {}},
                            "verify": ["a.T#flow"]}, tmp_path, {}, before, {"guards": []})
        assert seen["s"] == {"a.T#flow"}, (
            "a sibling that is measured but not re-run must count as outside")

    def test_a_measurement_that_fails_is_not_a_pass(self, adapt, tmp_path, monkeypatch):
        def broken(scope, workspace):
            raise RuntimeError("index failed")
        monkeypatch.setattr(adapt.check_changes, "measure", broken)
        with pytest.raises(RuntimeError):
            self._judge(adapt, tmp_path, [])


class TestChecksInThePullRequest:
    @pytest.fixture
    def ship(self, tmp_path, monkeypatch):
        return _load("ship_body", "actions/05_ship.py", tmp_path, monkeypatch)

    ROW = {"id": "c1", "site": "T#flow", "message": "Page is shown", "action": "remove",
           "before": [], "after": [], "why": "item 1 drops the step", "evidence": "test-only",
           "saw": ""}

    def test_the_table_leads_the_body(self, ship):
        body = ship.build_body({}, {}, {}, {}, "", [self.ROW], "Checks this PR changes")
        assert body.index("Checks this PR changes") < body.index("Overview")
        assert "Page is shown" in body and "item 1 drops the step" in body

    def test_an_unmeasurable_table_says_so(self, ship):
        body = ship.build_body({}, {}, {}, {}, "", None, "Checks this PR changes")
        assert "Could not be measured" in body

    def test_no_changes_no_section(self, ship):
        assert "Checks this" not in ship.build_body({}, {}, {}, {}, "")

    def test_without_a_pr_the_rows_are_the_items_own(self, ship):
        adapt = {"items": [{"status": "proposed", "check_changes": [self.ROW]},
                           {"status": "rejected", "check_changes": [self.ROW]}]}
        assert ship.declared_rows(adapt) == [self.ROW]


class TestCarryForward:
    """A retry must not drop what an earlier attempt applied: its edit is still
    on disk, and ship commits only what 04-adapt.json lists."""

    ROW = {"id": "c1", "site": "T#flow", "message": "Badge shows 1", "before": ["1"],
           "after": [], "why": "step 5 dropped", "evidence": "test_only"}
    APPLIED = {"index": 1, "kind": "step_merge", "status": "applied", "files": ["A.java"],
               "check_changes": [{**ROW, "action": "remove"}], "summary": "drop step 5"}

    @pytest.fixture
    def adapt(self, tmp_path, monkeypatch):
        module = _load("adapt_carry", "actions/04_adapt.py", tmp_path, monkeypatch)
        monkeypatch.setattr(module, "ATTEMPT", 2)
        return module

    def _earlier(self, tmp_path, items, applied_mode=True):
        (tmp_path / "04-adapt.json").write_text(json.dumps(
            {"attempt": 1, "applied_mode": applied_mode, "items": items,
             "verified": ["T#a"], "failed": ["T#b"]}))

    def _result(self, items=(), verified=()):
        return {"attempt": 2, "applied_mode": True, "items": list(items), "escalations": [],
                "verified": list(verified), "failed": [], "proposed": []}

    def test_the_first_attempt_carries_nothing(self, adapt, tmp_path, monkeypatch):
        monkeypatch.setattr(adapt, "ATTEMPT", 1)
        self._earlier(tmp_path, [self.APPLIED])
        result = self._result()
        adapt.carry_forward(result)
        assert result["items"] == []

    def test_an_early_stop_keeps_what_the_earlier_attempt_applied(self, adapt, tmp_path):
        self._earlier(tmp_path, [self.APPLIED, {"index": 2, "status": "rolled_back"}])
        result = self._result()
        adapt.finish(result, "skipped", "stuck")
        written = json.loads((tmp_path / "04-adapt.json").read_text())
        assert [(i["index"], i["status"], i["attempt"]) for i in written["items"]] == \
            [(1, "applied", 1)], "otherwise ship sees nothing applied and raises no PR"
        assert (written["verified"], written["failed"]) == (["T#a"], ["T#b"])
        assert "(from attempt 1)" in (tmp_path / "04-adapt.md").read_text()

    def test_a_reapplied_item_keeps_both_attempts_files_and_checks(self, adapt, tmp_path):
        self._earlier(tmp_path, [self.APPLIED])
        result = self._result([{"index": 1, "status": "applied", "files": ["B.java"],
                                "check_changes": [{**self.ROW, "action": "change"}]}],
                              verified=["T#c"])
        adapt.carry_forward(result)
        item = result["items"][0]
        assert item["files"] == ["A.java", "B.java"]
        assert [r["action"] for r in item["check_changes"]] == ["remove", "change"]
        assert result["verified"] == ["T#c"] and "attempt" not in item

    def test_a_retry_that_did_not_land_keeps_the_earlier_record(self, adapt, tmp_path):
        self._earlier(tmp_path, [self.APPLIED])
        result = self._result([{"index": 1, "status": "failed",
                                "reason": "the model proposed no edits"}])
        adapt.carry_forward(result)
        item = result["items"][0]
        assert (item["status"], item["files"]) == ("applied", ["A.java"])
        assert item["retry"] == {"attempt": 2, "status": "failed",
                                 "reason": "the model proposed no edits"}

    def test_a_covered_retry_never_replaces_an_applied_record(self, adapt, tmp_path):
        self._earlier(tmp_path, [self.APPLIED])
        result = self._result([{"index": 1, "status": "covered", "covered_by": 2}])
        adapt.carry_forward(result)
        assert result["items"][0]["status"] == "applied", "a covered item commits nothing"

    def test_covered_items_come_along(self, adapt, tmp_path):
        self._earlier(tmp_path, [self.APPLIED, {"index": 2, "status": "covered", "covered_by": 1}])
        result = self._result()
        adapt.carry_forward(result)
        assert [i["status"] for i in result["items"]] == ["applied", "covered"]

    def test_a_propose_only_earlier_run_is_ignored(self, adapt, tmp_path):
        self._earlier(tmp_path, [self.APPLIED], applied_mode=False)
        result = self._result()
        adapt.carry_forward(result)
        assert result["items"] == []
