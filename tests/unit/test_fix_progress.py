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


# A page-load assertion on a locator inside an iframe, then the next page's field.
FRAMED_PAGE_LOAD = ("Failed to load Element Locator@#pay-frame >> internal:control="
                    "enter-frame >> div.order-total in CheckoutPage")
CARD_CODE_FAILURE = (
    "Failed to enter data in element 'Card code field' with locator: "
    "Locator@#pay-frame >> internal:control=enter-frame >> #card-code: Error {\n"
    "\tat shop.web.CardFormPage.fillCard(CardFormPage.java:39)")


class TestProgressPastAPageLoadAnchor:
    """The run this pins found the right fix three times and reverted it each time.

    The first broken locator was a page-load anchor inside an iframe. Its failure
    message could not be read, so the failure the attempt started from was
    unidentifiable; with nothing to compare, a run that had got two pages
    further was scored as "still failing on the same element" and undone.
    """

    def _page_load_member(self, output):
        return _member(output, error_message=FRAMED_PAGE_LOAD, stack="CheckoutPage.java:26")

    def test_reaching_the_next_page_is_progress(self):
        advanced, unchanged = fix.split_by_progress(
            [self._page_load_member(CARD_CODE_FAILURE)])
        assert len(advanced) == 1 and not unchanged
        before, after = advanced[0][4], advanced[0][5]
        assert before["page_object"] == "CheckoutPage"
        assert after["element"] == "Card code field"

    def test_the_page_still_not_loading_is_not(self):
        # The replacement anchor is absent too: a different selector in the
        # message, the same outcome.
        again = FRAMED_PAGE_LOAD.replace("div.order-total", "div.order-sum")
        advanced, unchanged = fix.split_by_progress([self._page_load_member(again)])
        assert not advanced and len(unchanged) == 1

    def test_the_identity_is_read_from_the_record_not_from_its_text(self):
        # After an attempt the record keeps only a slice of the run's output.
        # The identity is worked out while the whole output is in hand, and kept.
        member = {"error_message": "the tail of a build log, naming nothing",
                  "stack_trace": "",
                  "failure_identity": fix.failure_identity.identify(FRAMED_PAGE_LOAD)}
        advanced, unchanged = fix.split_by_progress(
            [("pkg.SomeTest.aCase", CARD_CODE_FAILURE, member, 1000.0)])
        assert len(advanced) == 1 and not unchanged

    def test_the_selector_it_was_filed_under_is_enough_to_compare(self):
        member = {"error_message": "wording nothing here recognises",
                  "stack_trace": "", "failed_selector": "#old-anchor"}
        advanced, unchanged = fix.split_by_progress(
            [("pkg.SomeTest.aCase", CARD_CODE_FAILURE, member, 1000.0)])
        assert len(advanced) == 1 and not unchanged

    def test_the_next_issue_carries_its_identity(self, tmp_path):
        issue = {"test_name": "pkg.SomeTest.aCase", "failed_selector": "#old-anchor"}
        refreshed = fix._refresh_issue(issue, CARD_CODE_FAILURE, tmp_path, 1000.0)
        assert refreshed["failure_identity"]["element"] == "Card code field"
        assert refreshed["failed_selector"].endswith("#card-code")

    def test_an_unreadable_run_carries_no_stale_identity(self, tmp_path):
        issue = {"test_name": "pkg.SomeTest.aCase",
                 "failure_identity": fix.failure_identity.identify(FRAMED_PAGE_LOAD)}
        refreshed = fix._refresh_issue(issue, "BUILD FAILURE", tmp_path, 1000.0)
        assert refreshed["failure_identity"] == {}


class TestRetrySection:
    """What a retry is told about the attempt before it.

    One sentence used to serve every case, and fitted one. After an edit that
    worked and moved the test on, it said "do NOT repeat the previous approach"
    about an element nothing had been tried on. After a revert, it showed the
    output of a run made with code that no longer existed and never said so.
    """

    STALE = {"verdict": "LOCATOR_STALE"}

    @staticmethod
    def _ctx(output="", **outcome):
        return {"fix_attempt": 2, "prev_test_output": output, "prev_outcome": outcome}

    def test_a_first_attempt_is_told_nothing(self):
        assert fix.retry_section({"fix_attempt": 1, "prev_test_output": "",
                                  "prev_outcome": {}}, self.STALE) == ""

    def test_after_progress_it_is_a_first_attempt_at_a_new_element(self):
        text = fix.retry_section(self._ctx("output of the run", status="advanced",
                                           fix_diff="-old\n+new"), self.STALE)
        assert "first attempt at THIS element" in text
        assert "must not be undone" in text
        assert "do NOT repeat" not in text

    def test_after_a_revert_it_shows_the_edit_and_says_it_was_undone(self):
        text = fix.retry_section(self._ctx("boom at the same element",
                                           status="test_failed",
                                           fix_diff="-total = old\n+total = new"),
                                 self.STALE)
        assert "was reverted" in text and "back to what it was" in text
        assert "+total = new" in text
        assert "boom at the same element" in text
        assert "do NOT repeat the previous approach" in text

    def test_a_rejected_edit_gives_the_reason_and_no_unrelated_output(self):
        text = fix.retry_section(
            self._ctx("output of an EARLIER run", status="rejected_unsafe",
                      unfixable_reason="the replacement selector matches nothing",
                      fix_diff="-a\n+b"), self.STALE)
        assert "rejected before any test ran" in text
        assert "the replacement selector matches nothing" in text
        assert "+b" in text
        assert "output of an EARLIER run" not in text

    def test_a_second_failure_on_another_verdict_questions_the_diagnosis(self):
        text = fix.retry_section(self._ctx("boom", status="test_failed",
                                           fix_diff="-a\n+b"), {"verdict": "WRONG_PAGE"})
        assert "evidence the original diagnosis was wrong" in text

    def test_an_outcome_it_has_no_account_of_keeps_the_plain_wording(self):
        text = fix.retry_section(self._ctx("boom"), self.STALE)
        assert "Previous fix did not resolve the test" in text and "boom" in text


class TestCarriedIssueNote:
    def test_progress_is_announced_as_progress(self):
        note = fix.carried_issue_note("pkg.SomeTest.pays", "advanced",
                                      {"failed_selector": "#card-code"})
        assert note == "pays: the previous fix landed — now working on #card-code"

    def test_a_revert_is_not_announced_as_a_fix_that_landed(self):
        note = fix.carried_issue_note("pkg.SomeTest.pays", "test_failed",
                                      {"failed_selector": "#old-anchor"})
        assert "landed" not in note
        assert note == "pays: the previous edit was reverted — trying #old-anchor again"

    def test_anything_else_says_nothing(self):
        assert fix.carried_issue_note("pkg.SomeTest.pays", "rejected_unsafe", {}) == ""


class TestReusableInspection:
    """A retry on the same failure parked a browser to learn what it already knew."""

    LIVE = {"status": "ok (live, parked on the failing page)",
            "selectors": {"div.order-total": "#pay-frame >> div.order-sum"}}

    def _ctx(self, selector="#old-anchor", **outcome):
        outcome.setdefault("failed_selector", "#old-anchor")
        outcome.setdefault("dom_findings", self.LIVE)
        return {"failed_selector": selector, "prev_outcome": outcome}

    def test_it_is_reused_for_the_same_selector_after_a_revert(self):
        assert fix.reusable_inspection(self._ctx(status="test_failed")) == self.LIVE

    def test_and_after_a_rejected_edit(self):
        assert fix.reusable_inspection(self._ctx(status="rejected_unsafe")) == self.LIVE

    def test_not_once_the_test_has_moved_on(self):
        # That inspection was of a page the test has since left.
        assert fix.reusable_inspection(self._ctx(status="advanced")) == {}

    def test_not_for_a_different_selector(self):
        assert fix.reusable_inspection(self._ctx("#card-code", status="test_failed")) == {}

    def test_a_snapshot_is_not_a_live_inspection(self):
        findings = {"status": "not needed — failure-time DOM snapshot available",
                    "selectors": {}}
        assert fix.reusable_inspection(
            self._ctx(status="test_failed", dom_findings=findings)) == {}

    def test_a_first_attempt_has_nothing_to_reuse(self):
        assert fix.reusable_inspection({"failed_selector": "#x", "prev_outcome": {}}) == {}


class TestLocateRefusalForAnAssertionLocator:
    def test_fix_defers_and_passes_on_locates_own_account(self):
        outcome = {"verdict": "NO_HEAL", "classification": "ASSERTION_LOCATOR",
                   "reason": "read by assertElementVisible(badge), and the replacement "
                             "could not be proven to be the same element"}
        route, note = fix.locate_route(outcome)
        assert route == "defer"
        assert note == outcome["reason"]

    def test_the_other_refusals_keep_their_wording(self):
        route, note = fix.locate_route({"verdict": "NO_HEAL",
                                        "classification": "UNSTABLE_LOCATOR",
                                        "reason": "healed 4x in 30d"})
        assert route == "defer" and "stable test id" in note


CHECKOUT_PAGE = '''package shop.web;

public class CheckoutPage extends BasePage {
    private final Locator orderTotal;
    public CheckoutPage(Config config) {
        super(config);
        orderTotal = page.frameLocator("#pay-frame").locator("div.order-total");
        assertPageLoaded(orderTotal);
    }
}
'''


class TestTheOwningPageObjectIsAlwaysInThePrompt:
    """A failure that names its page object gets that page object, matched or not.

    A selector inside an iframe is a literal nowhere in the source, so ranking
    files by the names a failure quotes found nothing: the model was told it had
    no declarations to work from, and an answer Locate had already proved could
    not be applied — that edit goes into a page object listed in the context.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        web = tmp_path / "src" / "main" / "java" / "shop" / "web"
        web.mkdir(parents=True)
        (web / "CheckoutPage.java").write_text(CHECKOUT_PAGE)
        return tmp_path

    def _context(self, repo, **issue):
        base = {"test_name": "shop.ShopTest.pays", "error_message": FRAMED_PAGE_LOAD,
                "root_cause": FRAMED_PAGE_LOAD, "stack_trace": "CheckoutPage.java:8"}
        base.update(issue)
        return fix.build_candidate_context(base, repo, "")

    def test_the_page_object_the_failure_names_is_listed_first(self, repo):
        ctx = self._context(repo)
        assert ctx["page_objects"], "the owning page object was not loaded"
        assert Path(ctx["page_objects"][0]["path"]).stem == "CheckoutPage"
        assert "div.order-total" in ctx["page_objects"][0]["snippet"]

    def test_the_failure_is_named_by_its_selector_not_by_a_caller(self, repo):
        ctx = self._context(repo)
        assert ctx["element_names"][0] == "div.order-total"

    def test_the_identity_travels_with_the_context(self, repo):
        ctx = self._context(repo)
        assert ctx["failure_identity"]["page_object"] == "CheckoutPage"
        assert ctx["failure_identity"]["kind"] == "page_load"

    def test_the_previous_outcome_travels_too(self, repo):
        ctx = fix.build_candidate_context(
            {"test_name": "shop.ShopTest.pays", "error_message": FRAMED_PAGE_LOAD},
            repo, "", prev_outcome={"status": "advanced"})
        assert ctx["prev_outcome"] == {"status": "advanced"}


# What three attempts leave behind when a chain of three locators is repaired.
CHAIN_HISTORY = [
    {"attempt": 1, "entries": [
        {"test_name": "pkg.ShopTest.pays", "status": "advanced",
         "target_file": "/ws/CheckoutPage.java", "fix_description": "order total",
         "fix_diff": "-a\n+b"}]},
    {"attempt": 2, "entries": [
        # Applied, failed on the same element, reverted: not on disk any more.
        {"test_name": "pkg.ShopTest.pays", "status": "test_failed",
         "target_file": "/ws/CardFormPage.java", "fix_description": "a wrong guess",
         "fix_diff": "-c\n+x"}]},
    {"attempt": 3, "entries": [
        {"test_name": "pkg.ShopTest.pays", "status": "advanced",
         "target_file": "/ws/CardFormPage.java", "fix_description": "card code",
         "fix_diff": "-c\n+d"}]},
]


class TestKeptEdits:
    """A run that repaired three locators reported one.

    A chain is repaired one link per attempt. Each edit is kept because the test
    got past it, and only the last one sees the test go green — so the record of
    the fixed test held that last edit alone, while the diff it shipped carried
    all three.
    """

    def test_every_edit_kept_on_the_way_is_found(self):
        kept = fix.kept_edits(CHAIN_HISTORY, ["pkg.ShopTest.pays"], before_attempt=4)
        assert [(e["attempt"], e["fix_description"]) for e in kept] == \
            [(1, "order total"), (3, "card code")]

    def test_a_reverted_edit_is_not_one_of_them(self):
        kept = fix.kept_edits(CHAIN_HISTORY, ["pkg.ShopTest.pays"], before_attempt=4)
        assert "a wrong guess" not in [e["fix_description"] for e in kept]

    def test_only_edits_from_before_the_attempt_in_question(self):
        kept = fix.kept_edits(CHAIN_HISTORY, ["pkg.ShopTest.pays"], before_attempt=3)
        assert [e["attempt"] for e in kept] == [1]

    def test_another_tests_edits_are_left_out(self):
        assert fix.kept_edits(CHAIN_HISTORY, ["pkg.ShopTest.refunds"], 4) == []

    def test_one_edit_covering_several_tests_is_counted_once(self):
        history = [{"attempt": 1, "entries": [
            {"test_name": name, "status": "advanced", "target_file": "/ws/LoginPage.java",
             "fix_description": "login button", "fix_diff": "-a\n+b"}
            for name in ("pkg.ShopTest.pays", "pkg.ShopTest.refunds")]}]
        kept = fix.kept_edits(history, ["pkg.ShopTest.pays", "pkg.ShopTest.refunds"], 2)
        assert len(kept) == 1

    def test_they_are_attached_to_the_test_they_were_kept_for(self):
        result = {"fix_attempt": 4, "attempts": CHAIN_HISTORY,
                  "fixes": [{"test_name": "pkg.ShopTest.pays", "fix_attempt": 4,
                             "fix_description": "the bank code field"}],
                  "unverified_fixes": [],
                  "failed_fixes": [{"test_name": "pkg.ShopTest.refunds", "fix_attempt": 4}]}
        assert fix.attach_kept_edits(result) == 2
        assert len(result["fixes"][0]["earlier_edits"]) == 2
        assert result["failed_fixes"][0]["earlier_edits"] == []

    def test_a_test_still_failing_keeps_the_edits_made_for_it_too(self):
        # They are in the branch either way; a report listing none of them leaves
        # the reviewer to find them in the diff.
        result = {"fix_attempt": 4, "attempts": CHAIN_HISTORY, "fixes": [],
                  "unverified_fixes": [],
                  "failed_fixes": [{"test_name": "pkg.ShopTest.pays", "fix_attempt": 4,
                                    "status": "test_failed"}]}
        assert fix.attach_kept_edits(result) == 0
        assert len(result["failed_fixes"][0]["earlier_edits"]) == 2
