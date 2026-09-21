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
        assert record["conservation"] == [], "no contracts in scope, nothing to conserve"


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

    def test_being_stuck_is_escalating(self, ship):
        assert "stuck" in ship.ESCALATING, (
            "the stop rule writes this skip reason; it means a human has to look")
