"""Which page object Locate believes a failing selector belongs to.

The bug this pins: `_declaring_field` scanned every page object in the repo and
returned the first one whose declared selector string matched. Selector strings
are not unique — `button[type='submit']` is declared by a login page and by an
unrelated OTP page in the same repo — so the owner was decided by the order
`rglob` happened to walk the tree.

On a real run that sent Locate looking for a baseline for `OtpPage`, a page the
test never opened, on a failure in `NaukriLoginPage`. It refused with
"no recorded good run for OtpPage", which reads as a missing baseline rather than
as the wrong question. Both the failure context and the stack trace named the
right page object, and neither was read.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    import os
    os.environ.setdefault("AUDIT_DIR", str(ROOT / "tests" / "fixtures"))
    os.environ.setdefault("HANDOFF_FILE", str(ROOT / "tests" / "fixtures" / "none.json"))
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_locate.py"
    spec = importlib.util.spec_from_file_location("healing_locate_decl", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_locate_decl"] = mod
    spec.loader.exec_module(mod)
    return mod


loc = pytest.importorskip("bs4") and _load()

SUBMIT = "button[type='submit']"

LOGIN_PAGE = '''
public class NaukriLoginPage extends BasePage {
    private final Locator usernameField = page.locator("#usernameField");
    private final Locator loginButton = page.locator("button[type='submit']");
    public void doLogin(String u, String p) { click(loginButton, "Login button"); }
}
'''

OTP_PAGE = '''
public class OtpPage extends BasePage {
    private final Locator verifyButton = page.locator("button[type='submit']");
    public void verify() { click(verifyButton, "Verify button"); }
}
'''

# Walked before the login page, the way rglob returned it on the failing run.
SOURCES = {"OtpPage": OTP_PAGE, "NaukriLoginPage": LOGIN_PAGE}


class TestDeclaringField:
    def test_the_page_the_failure_names_wins_over_walk_order(self):
        name, field, raw = loc._declaring_field(SOURCES, SUBMIT, prefer="NaukriLoginPage")
        assert (name, field) == ("NaukriLoginPage", "loginButton")
        assert raw == SUBMIT

    def test_walk_order_alone_no_longer_decides(self):
        # Without the hint there is nothing to separate the two candidates, and
        # answering "OtpPage" because it sorted first is the original defect.
        assert loc._declaring_field(SOURCES, SUBMIT) == (None, None, None)

    def test_a_hint_naming_no_candidate_refuses(self):
        assert loc._declaring_field(SOURCES, SUBMIT, prefer="ProfilePage") == (None, None, None)

    def test_a_sole_declaration_needs_no_hint(self):
        name, field, _ = loc._declaring_field(SOURCES, "#usernameField")
        assert (name, field) == ("NaukriLoginPage", "usernameField")

    def test_an_undeclared_selector_is_still_absent(self):
        assert loc._declaring_field(SOURCES, "#nothing") == (None, None, None)


# Both pages call their submit control "Login button" — an element name is no more
# unique across a repo than a selector string is.
COLLIDING = {
    "OtpPage": OTP_PAGE.replace('click(verifyButton, "Verify button")',
                                'click(verifyButton, "Login button")'),
    "NaukriLoginPage": LOGIN_PAGE,
}


class TestFieldByElementName:
    def test_the_named_page_is_searched_first(self):
        name, field, _ = loc._field_by_element_name(
            COLLIDING, "Failed to click on element 'Login button'",
            prefer="NaukriLoginPage")
        assert (name, field) == ("NaukriLoginPage", "loginButton")

    def test_the_other_page_is_still_reachable(self):
        name, field, _ = loc._field_by_element_name(
            COLLIDING, "Failed to click on element 'Login button'", prefer="OtpPage")
        assert (name, field) == ("OtpPage", "verifyButton")

    def test_an_unnamed_element_finds_nothing(self):
        assert loc._field_by_element_name(SOURCES, "no element name here") == (None, None, None)


class TestOwnerHint:
    def test_read_from_the_failure_context(self, tmp_path):
        context = tmp_path / "f.context.json"
        context.write_text(json.dumps({
            "schema": 1, "test": "a.b.C.d", "failedAt": "2026-09-03T23:10:06",
            "failure": {"kind": "ELEMENT_INTERACTION", "pageObject": "NaukriLoginPage",
                        "anchors": [], "elapsedMs": 28, "budgetMs": 30000},
            "page": {}, "pageObjectCoverage": {}, "domVolatility": {},
            "navigation": [], "httpErrors": [], "jsErrors": []}))
        assert loc._owner_hint({"failure_context": str(context)}) == "NaukriLoginPage"

    def test_falls_back_to_the_stack_frame(self):
        issue = {"stack_trace": "at automation.modules.naukari.web.NaukriLoginPage"
                                ".doLogin(NaukriLoginPage.java:36)"}
        assert loc._owner_hint(issue) == "NaukriLoginPage"

    def test_the_context_outranks_the_stack_frame(self, tmp_path):
        context = tmp_path / "f.context.json"
        context.write_text(json.dumps({
            "schema": 1, "test": "a.b.C.d", "failedAt": "2026-09-03T23:10:06",
            "failure": {"kind": "ELEMENT_INTERACTION", "pageObject": "NaukriLoginPage",
                        "anchors": [], "elapsedMs": 28, "budgetMs": 30000},
            "page": {}, "pageObjectCoverage": {}, "domVolatility": {},
            "navigation": [], "httpErrors": [], "jsErrors": []}))
        # The frame is the helper that called through, not the page that failed.
        assert loc._owner_hint({"failure_context": str(context),
                                "stack_trace": "SomeHelper.java:12"}) == "NaukriLoginPage"

    def test_nothing_named_is_empty(self):
        assert loc._owner_hint({}) == ""


class TestSignInPage:
    """The bug this pins: Locate signed in before examining a locator that lives
    on the sign-in page.

    `_login_replay` performs the login and then navigates to the failure URL —
    which is /nlogin/login. A signed-in visitor is redirected away from it, so
    the replay examined the post-login home page, found none of the login page's
    locators, and reported WRONG_STATE about a page it never opened.
    """

    def test_named_by_the_page_object(self):
        assert loc._is_sign_in_page("NaukriLoginPage", "") is True
        assert loc._is_sign_in_page("AuthPage", "") is True
        assert loc._is_sign_in_page("SignInPage", "") is True

    def test_named_by_the_url(self):
        # A repo may call it something else entirely; the route still says so.
        assert loc._is_sign_in_page("EntryPage", "https://www.naukri.com/nlogin/login") is True
        assert loc._is_sign_in_page("EntryPage", "https://x.com/sign-in") is True

    def test_an_ordinary_page_is_not_one(self):
        assert loc._is_sign_in_page("NaukriProfilePage",
                                    "https://www.naukri.com/mnjuser/profile") is False

    def test_a_url_merely_containing_the_word_is_not_enough(self):
        # "/mnjuser/loginhistory" is not the sign-in page; the segment must be.
        assert loc._is_sign_in_page("ProfilePage",
                                    "https://x.com/mnjuser/loginhistory") is False

    def test_nothing_known_is_not_one(self):
        assert loc._is_sign_in_page("", "") is False
