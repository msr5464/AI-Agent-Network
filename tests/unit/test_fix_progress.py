"""Whether a fix is kept when the test still fails.

The bug this pins: the healing agent repaired `button[type='submit']`, the login
succeeded, the flow reached a page it had never reached before, and the test then
failed on a *different* element in a *different* page object. The gate was
whole-test pass/fail, so that scored as a failure, the file was reverted, and the
next attempt started over on the locator that was already fixed.
"""

import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_fix.py"
    spec = importlib.util.spec_from_file_location("healing_fix_progress", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_fix_progress"] = mod
    # Set only for the import (the step reads them into module constants); left in
    # os.environ they leaked into every later test in the session.
    with mock.patch.dict(os.environ, {"AUDIT_DIR": tempfile.mkdtemp(prefix="fix-progress-"),
                                      "HANDOFF_FILE": str(ROOT / "tests" / "fixtures" / "none.json")}):
        spec.loader.exec_module(mod)
    return mod


fix = pytest.importorskip("bs4") and _load()

LOGIN_FAILURE = (
    "Failed to click on element 'Login button' with locator: "
    "Locator@button[type='submit']: Error {")
PROFILE_FAILURE = (
    "Failed to click on element 'Edit Profile Summary button' with locator: "
    "Locator@#profile-section-profile-summary img[alt='mukesh']: Error {\n"
    "\tat automation.modules.naukari.web.NaukriProfilePage.edit(NaukriProfilePage.java:20)")


def _member(output, error_message=LOGIN_FAILURE,
            stack="NaukriLoginPage.java:36", name="pkg.SomeTest.aCase"):
    return (name, output,
            {"error_message": error_message, "stack_trace": stack}, 1000.0)


class TestSplitByProgress:
    def test_a_later_element_counts_as_progress(self):
        advanced, unchanged = fix.split_by_progress([_member(PROFILE_FAILURE)])
        assert len(advanced) == 1 and not unchanged
        _name, _out, _member_ctx, _started, before, after = advanced[0]
        assert before["element"] == "Login button"
        assert after["element"] == "Edit Profile Summary button"

    def test_the_same_element_is_not_progress(self):
        advanced, unchanged = fix.split_by_progress([_member(LOGIN_FAILURE)])
        assert not advanced and len(unchanged) == 1

    def test_an_unreadable_failure_is_not_progress(self):
        # Conservative: unknown means revert, which is the pre-existing behaviour.
        advanced, unchanged = fix.split_by_progress([_member("BUILD FAILURE")])
        assert not advanced and len(unchanged) == 1

    def test_members_are_judged_independently(self):
        advanced, unchanged = fix.split_by_progress([
            _member(PROFILE_FAILURE, name="pkg.SomeTest.moved"),
            _member(LOGIN_FAILURE, name="pkg.SomeTest.stuck"),
        ])
        assert [a[0] for a in advanced] == ["pkg.SomeTest.moved"]
        assert [u[0] for u in unchanged] == ["pkg.SomeTest.stuck"]

    def test_nothing_failing_splits_to_nothing(self):
        assert fix.split_by_progress([]) == ([], [])


class TestAdvancedRecord:
    """The bookkeeping that makes progress usable by the NEXT attempt.

    The bug this pins: `split_by_progress` was only consulted when the whole
    cluster still failed. A cluster that greened three of five tests recorded the
    other two as a plain `test_failed` with no refreshed issue, so attempts 2, 3
    and 4 re-read the original handoff and spent themselves re-investigating the
    locator attempt 1 had already repaired.
    """

    @staticmethod
    def _cluster(member):
        return SimpleNamespace(
            contexts=[member],
            issues=[{"test_name": "pkg.SomeTest.aCase",
                     "failed_selector": "button[type='submit']",
                     "dom_snapshot": "/gone/login.html",
                     "failure_url": "https://app.example.com/login"}])

    def test_it_carries_the_next_failure_not_the_repaired_one(self, tmp_path):
        entry = fix.split_by_progress([_member(PROFILE_FAILURE)])[0][0]
        member = entry[2]
        record = fix._advanced_record(entry, self._cluster(member),
                                      tmp_path / "LoginPage.java",
                                      "fixed the login button", "--- a/x", tmp_path)

        assert record["status"] == "advanced"
        nxt = record["next_issue"]
        # The element that fails NOW, not the one this edit repaired.
        assert nxt["failed_selector"] == "#profile-section-profile-summary img[alt='mukesh']"
        # Evidence for the repaired failure is dropped rather than carried: a
        # stale path that survives reads as this failure's evidence.
        assert nxt["dom_snapshot"] == "" and nxt["failure_url"] == ""

    def test_a_test_stuck_on_the_same_element_is_not_recorded_as_advanced(self):
        advanced, unchanged = fix.split_by_progress([_member(LOGIN_FAILURE)])
        assert not advanced and len(unchanged) == 1
        # unchanged entries are 3-tuples — the partial-success path unpacks them
        # as such, so a shape change here would break it loudly.
        assert len(unchanged[0]) == 3


class TestFixBranchName:
    """Two runs of the same test must not want the same branch.

    The bug this pins: the name came from the build tag alone, so a re-run
    pushed a branch the remote already had from an earlier session. Cut from the
    same base, the two were siblings, and `--force-with-lease` on a ref this
    worktree had never fetched fails with "stale info" — the run ended NO_PR
    with two verified fixes stranded in a /tmp worktree that is then removed.
    """

    SESSION = "20260923-111221-fix-SauceDemoWebTest"

    def test_the_session_names_the_branch(self):
        assert fix.fix_branch_name("healing", "local-SauceDemoWebTest",
                                   self.SESSION) == \
            "healing/20260923-111221-fix-saucedemowebtest"

    def test_two_sessions_never_collide(self):
        assert (fix.fix_branch_name("healing", "local-SauceDemoWebTest", self.SESSION)
                != fix.fix_branch_name("healing", "local-SauceDemoWebTest",
                                       "20260923-103931-fix-SauceDemoWebTest"))

    def test_two_runs_in_the_same_second_still_differ(self):
        # Session ids are stamped to the second, so the GUI disambiguates with a
        # "-2" suffix. It has to survive into the branch name, or the pair wants
        # one branch again — the failure this change exists to remove.
        assert (fix.fix_branch_name("healing", "t", self.SESSION)
                != fix.fix_branch_name("healing", "t", self.SESSION + "-2"))

    def test_every_attempt_of_one_run_shares_a_branch(self):
        # run.sh exports SESSION_ID once, so the retry must land on the same
        # branch — otherwise attempt 2 cuts a new one and attempt 1's commits
        # never reach the PR.
        assert (fix.fix_branch_name("healing", "t", self.SESSION)
                == fix.fix_branch_name("healing", "t", self.SESSION))

    def test_without_a_session_the_build_tag_stands(self):
        assert fix.fix_branch_name("healing", "ProdSanity-All-Tests-541", "") == \
            "healing/prodsanity-all-tests-541"

    def test_the_name_is_a_usable_git_ref(self):
        name = fix.fix_branch_name("healing", "Prod Sanity/All#541",
                                   "20260923-111221-fix-Prod Sanity/All#541")
        assert re.fullmatch(r"healing/[a-z0-9_-]+", name), name


class TestBaselineCutoffIsPinned:
    """A baseline this session wrote is not "the last good run".

    The bug this pins: attempt 1 greened a sibling test that passes through the
    products page without touching the cart. Promoting that run recorded every
    declared locator, so the broken-but-unused `cartLink` went in as absent —
    and attempt 2 read it as ELEMENT_GONE, "removed from the product".
    """

    def test_the_first_capture_is_carried_not_the_latest(self, tmp_path):
        snap = tmp_path / "aCase_1.html"
        snap.write_text('<!-- qa-agent-network:dom-snapshot test="aCase" url="x" '
                        'capturedAt="2026-09-23T11:15:46" -->\n<html></html>')
        first = fix._refresh_issue({"test_name": "pkg.T.aCase",
                                    "dom_snapshot": str(snap)},
                                   PROFILE_FAILURE, tmp_path, 0.0)
        assert first["baseline_not_after"] == "2026-09-23T11:15:46"

        # Refreshed again on a later attempt: the pin must not move forward, or
        # a baseline written in between counts as evidence about the run before.
        second = fix._refresh_issue(first, LOGIN_FAILURE, tmp_path, 0.0)
        assert second["baseline_not_after"] == "2026-09-23T11:15:46"

    def test_no_capture_means_no_pin(self, tmp_path):
        refreshed = fix._refresh_issue({"test_name": "pkg.T.aCase"},
                                       PROFILE_FAILURE, tmp_path, 0.0)
        assert refreshed["baseline_not_after"] == ""


class TestAGreenTestIsNotWorkForTheModel:
    """A failure that does not reproduce is not a locator to repair.

    The bug this pins: from attempt 2 on, the fix step re-runs the failing test
    with a browser parked so it can inspect the live page. When that re-run
    PASSED, the helper logged "nothing to inspect" and returned {} — the same
    value it returns when repair mode is simply unavailable. The caller could not
    tell the two apart and asked the model to repair a green test anyway, three
    attempts running, then reported it as unfixable. The test had failed once on
    a dropped connection and passed every time since.
    """

    def test_a_passing_re_run_says_so(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fix, "repair_possible", lambda w: (True, ""))
        monkeypatch.setattr(fix, "run_test",
                            lambda *a, **k: ("passed", "BUILD SUCCESS"))
        assert fix.park_browser_for_repair(tmp_path, "pkg.T.aCase") == {"passed": True}

    def test_an_unavailable_repair_mode_is_not_mistaken_for_a_pass(self, monkeypatch,
                                                                   tmp_path):
        monkeypatch.setattr(fix, "repair_possible", lambda w: (False, "CI is set"))
        session = fix.park_browser_for_repair(tmp_path, "pkg.T.aCase")
        assert session == {}
        assert not session.get("passed"), \
            "the caller keys on this to decide whether to skip the fix entirely"

    def test_a_still_failing_re_run_goes_on_to_look_for_the_parked_browser(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(fix, "repair_possible", lambda w: (True, ""))
        monkeypatch.setattr(fix, "run_test", lambda *a, **k: ("failed", "boom"))
        monkeypatch.setattr(fix, "find_repair_session",
                            lambda w, t, u="": {"endpoint": "http://localhost:9222"})
        session = fix.park_browser_for_repair(tmp_path, "pkg.T.aCase")
        assert session == {"endpoint": "http://localhost:9222"}
        assert not session.get("passed")


class TestRevertedRecord:
    """What a reverted cluster hands the next attempt.

    The bug this pins: a reverted cluster recorded no `next_issue` at all, so the
    next attempt's `refreshed` map was empty and both steps fell back to the
    ORIGINAL handoff. On a real run that meant attempt 3 went back to resolving
    `#login-mukesh` — a locator attempt 1 had already repaired and committed —
    while the cart link it was actually stuck on went unexamined.
    """

    CART = {"test_name": "pkg.SomeTest.aCase", "failed_selector": ".mukesh",
            "dom_snapshot": "/audit/dom/cart.html",
            "healing_baseline_dir": "/audit/baselines",
            "baseline_not_after": "2026-09-23T17:41:40"}

    def _cluster(self, member):
        return SimpleNamespace(contexts=[member], issues=[self.CART])

    def test_it_carries_the_issue_the_attempt_worked_on(self, tmp_path):
        member = {"test_name": "pkg.SomeTest.aCase", "failed_selector": ".mukesh",
                  "repo_conventions": "bulky"}
        record = fix._reverted_record(member, "still red", self._cluster(member),
                                      tmp_path / "ProductsPage.java",
                                      "swapped the cart link", "--- a/x")
        assert record["status"] == "test_failed"
        assert record["next_issue"]["failed_selector"] == ".mukesh"
        # Evidence kept, not cleared: after a revert the state is exactly what it
        # was before the attempt, so the capture it was resolved from still holds.
        assert record["next_issue"]["dom_snapshot"] == "/audit/dom/cart.html"
        assert record["next_issue"]["baseline_not_after"] == "2026-09-23T17:41:40"

    def test_the_conventions_blob_is_not_dragged_into_the_report(self, tmp_path):
        member = {"test_name": "pkg.SomeTest.aCase", "repo_conventions": "bulky"}
        record = fix._reverted_record(member, "", self._cluster(member),
                                      tmp_path / "x.java", "", "")
        assert "repo_conventions" not in record
