"""What becomes of repairs that were kept when the test never went green.

The bug this pins: a test with a chain of broken locators had seven of them
repaired, each kept because the test got past it, and the run then ran out of
attempts. No test had passed, so by the ship step's measure there were no fixes:
no PR, and seven commits left on a local branch in a worktree that was removed
minutes later. The work existed and nobody could see it.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from qa_agents_server import audit_reader


def _load():
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "02_ship.py"
    spec = importlib.util.spec_from_file_location("healing_ship_partial", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_ship_partial"] = mod
    with mock.patch.dict(os.environ, {"AUDIT_DIR": tempfile.mkdtemp(prefix="ship-partial-")}):
        spec.loader.exec_module(mod)
    return mod


ship = _load()

TEST = "app.checkout.CheckoutWebTest.paysByCard"
# The last attempt repaired the pay button and the test moved on; two earlier
# attempts are on the branch behind it.
STILL_FAILING = {
    "test_name": TEST, "status": "advanced", "fix_attempt": 3,
    "target_file": "/ws/CardPage.java", "fix_description": "pay button",
    "fix_diff": "-pay-typo\n+pay",
    "progressed_to": {"element": "Receipt total"},
    "earlier_edits": [
        {"attempt": 1, "target_file": "/ws/LoginPage.java",
         "fix_description": "login button", "fix_diff": "-login-typo\n+login"},
        {"attempt": 2, "target_file": "/ws/CartPage.java",
         "fix_description": "checkout link", "fix_diff": "-go-typo\n+go"},
    ],
}
FIX_DATA = {"build_tag": "local-CheckoutWebTest", "pr_branch": "healing/a-session",
            "fix_attempt": 3, "fixes": []}


class TestKeptEdits:
    def test_every_edit_on_the_branch_is_listed_oldest_first(self):
        kept = ship.kept_edits([STILL_FAILING])
        assert [e["fix_description"] for e in kept] == [
            "login button", "checkout link", "pay button"]

    def test_an_edit_two_tests_share_is_listed_once(self):
        other = dict(STILL_FAILING, test_name="app.checkout.CheckoutWebTest.paysByWallet")
        assert len(ship.kept_edits([STILL_FAILING, other])) == 3

    def test_an_attempt_that_changed_nothing_adds_nothing_of_its_own(self):
        rejected = dict(STILL_FAILING, status="rejected_unsafe")
        assert [e["attempt"] for e in ship.kept_edits([rejected])] == [1, 2]

    def test_a_run_that_kept_nothing_has_nothing(self):
        assert ship.kept_edits([{"test_name": TEST, "status": "unfixable"}]) == []


@pytest.fixture
def pushed(monkeypatch, tmp_path):
    """Everything outward stubbed: the push succeeds, the PR call is recorded."""
    calls = {}

    def create_pr(**kwargs):
        calls.update(kwargs)
        return "https://example.com/pr/7", ""

    monkeypatch.setattr(ship, "AUTO_PUSH", True)
    monkeypatch.setattr(ship, "GITHUB_ORG", "org")
    monkeypatch.setattr(ship, "GITHUB_REPO_AUTOMATION", "repo")
    monkeypatch.setattr(ship, "get_workspace", lambda: tmp_path)
    monkeypatch.setattr(ship, "run_git", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(ship, "create_pr", create_pr)
    monkeypatch.setattr(ship, "send_slack", lambda channel, text: False)
    monkeypatch.setattr(ship, "AUDIT_DIR", tmp_path)
    return calls


class TestAPartlyHealedRunRaisesADraft:
    def test_it_is_a_draft_and_says_so(self, pushed):
        url = ship.push_and_create_pr(FIX_DATA, [], [STILL_FAILING])
        assert url == "https://example.com/pr/7"
        assert pushed["draft"] is True
        assert "[PARTIAL]" in pushed["title"] and "3 locator edit(s) kept" in pushed["title"]

    def test_the_body_lists_what_was_kept_and_what_is_left(self, pushed):
        ship.push_and_create_pr(FIX_DATA, [], [STILL_FAILING])
        body = pushed["body"]
        for kept in ("login button", "checkout link", "pay button"):
            assert kept in body
        assert "No test passes yet" in body
        assert "the test now stops at Receipt total" in body
        # Each kept edit once: under Changes Applied, not again under the failure.
        assert body.count("login button") == 1

    def test_a_run_with_a_verified_fix_is_not_a_draft(self, pushed):
        fixed = {"test_name": TEST, "test_names": [TEST], "target_file": "/ws/CardPage.java",
                 "fix_description": "pay button"}
        ship.push_and_create_pr(dict(FIX_DATA, fixes=[fixed]), [], [])
        assert pushed["draft"] is False and "[PASSED]" in pushed["title"]

    def test_the_result_file_records_that_it_is_a_draft(self, pushed, tmp_path):
        (tmp_path / "01-fix.json").write_text(json.dumps(
            dict(FIX_DATA, failed_fixes=[STILL_FAILING])))
        (tmp_path / ".fix-passed").write_text("false")
        ship.main()
        result = json.loads((tmp_path / "02-ship.json").read_text())
        assert result["pr_url"] == "https://example.com/pr/7" and result["pr_draft"] is True

    def test_a_run_that_kept_nothing_still_raises_nothing(self, pushed, tmp_path):
        (tmp_path / "01-fix.json").write_text(json.dumps(dict(
            FIX_DATA, pr_branch=None,
            failed_fixes=[{"test_name": TEST, "status": "unfixable"}])))
        (tmp_path / ".fix-passed").write_text("false")
        ship.main()
        assert "title" not in pushed
        assert json.loads((tmp_path / "02-ship.json").read_text())["pr_url"] is None


class TestADraftIsNotASuccess:
    """The run's row links the draft like any PR. It must not read as fixed."""

    COUNTS = {"tests_fixed": 0, "tests_unverified": 0}

    def test_a_draft_leaves_the_run_needing_review(self, tmp_path):
        status = audit_reader._healing_status(
            tmp_path, "false", {"pr_url": "https://example.com/pr/7", "pr_draft": True},
            self.COUNTS)
        assert status == "needs_review"

    def test_an_ordinary_pr_is_reported_as_before(self, tmp_path):
        status = audit_reader._healing_status(
            tmp_path, "false", {"pr_url": "https://example.com/pr/7"}, self.COUNTS)
        assert status == "pr_created"
