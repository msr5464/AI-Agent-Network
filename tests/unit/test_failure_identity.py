"""Telling a repaired locator apart from the next broken one.

The bug this pins: a fix that worked was reverted. `button[type='submit']` was
repaired, the login succeeded, the flow reached a page it had never reached, and
the test then failed on a different element in a different page object. The gate
was whole-test pass/fail, so the run scored that as a failure and put the file
back — guaranteeing the next attempt would fix the same locator again.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import failure_identity as fi

LOGIN = """java.lang.AssertionError:
Failed to click on element 'Login button' with locator: Locator@button[type='submit']: Error {
  message='Timeout 1ms exceeded.
}
	at automation.modules.naukari.web.NaukriLoginPage.doLogin(NaukriLoginPage.java:36)
	at automation.naukari.NaukriProfileSummaryWebTest.toggle(NaukriProfileSummaryWebTest.java:23)
"""

PROFILE = """java.lang.AssertionError:
Failed to click on element 'Edit Profile Summary button' with locator: Locator@#profile-section-profile-summary img[alt='mukesh']: Error {
  message='Timeout 1ms exceeded.
}
	at automation.modules.naukari.web.NaukriProfilePage.edit(NaukriProfilePage.java:20)
"""

# The same element, after a repair changed how it is found.
LOGIN_REPAIRED = LOGIN.replace("button[type='submit']", "button:text-is('Login')")

PAGE_LOAD = "Failed to load Element Locator@img.avatar in DashboardPage"


class TestIdentify:
    def test_reads_the_element_selector_and_owner(self):
        found = fi.identify(LOGIN)
        assert found["element"] == "Login button"
        assert found["selector"] == "button[type='submit']"
        assert found["page_object"] == "NaukriLoginPage"

    def test_reads_a_page_load_assertion(self):
        found = fi.identify(PAGE_LOAD)
        assert found["selector"] == "img.avatar"
        assert found["page_object"] == "DashboardPage"

    def test_nothing_identifiable_is_marked_unavailable(self):
        assert fi.identify("BUILD FAILURE\nCannot find symbol")["available"] is False

    def test_empty_output_is_unavailable(self):
        assert fi.identify("")["available"] is False


class TestSameLocator:
    def test_a_different_element_is_progress(self):
        assert fi.same_locator(fi.identify(LOGIN), fi.identify(PROFILE)) is False

    def test_the_same_element_is_not_progress(self):
        assert fi.same_locator(fi.identify(LOGIN), fi.identify(LOGIN)) is True

    def test_a_repaired_selector_on_the_same_element_is_not_progress(self):
        # The whole point of matching on element name: the fix changes the
        # selector, so a selector comparison would call this "different" and keep
        # an edit that changed nothing about the outcome.
        assert fi.same_locator(fi.identify(LOGIN), fi.identify(LOGIN_REPAIRED)) is True

    def test_an_unreadable_failure_is_treated_as_the_same(self):
        # Conservative on purpose: unknown means revert, which is the old behaviour.
        assert fi.same_locator(fi.identify(LOGIN), fi.identify("BUILD FAILURE")) is True


class TestDescribe:
    def test_names_the_element_and_its_page(self):
        assert fi.describe(fi.identify(PROFILE)) == \
            "'Edit Profile Summary button' in NaukriProfilePage"

    def test_says_so_when_nothing_is_identifiable(self):
        assert fi.describe(fi.identify("")) == "an unidentifiable failure"


class TestLocatorShaped:
    def test_an_element_failure_is_locator_shaped(self):
        assert fi.is_locator_shaped(LOGIN) is True

    def test_a_compile_error_is_not(self):
        assert fi.is_locator_shaped("[ERROR] cannot find symbol: method foo()") is False


# A locator inside an iframe prints as a chain of hops, with spaces in it.
FRAMED_PAGE_LOAD = ("java.lang.AssertionError: Failed to load Element "
                    "Locator@#pay-frame >> internal:control=enter-frame >> div.order-total "
                    "in CheckoutPage\n"
                    "\tat shop.web.CheckoutPage.<init>(CheckoutPage.java:26)\n"
                    "\tat shop.web.CartPage.checkout(CartPage.java:42)")
FRAMED_CHAIN = "#pay-frame >> internal:control=enter-frame >> div.order-total"

CARD_CODE = ("Failed to enter data in element 'Card code field' with locator: "
             "Locator@#pay-frame >> internal:control=enter-frame >> #card-code: Error {\n"
             "\tat shop.web.CardFormPage.fillCard(CardFormPage.java:39)")


class TestAPageLoadSelectorWithSpaces:
    """The page-load reader took one whitespace-free token as the selector.

    A descendant selector has spaces in it, and so does a locator inside an
    iframe. Neither matched, the failure came back unidentifiable, and an edit
    that had moved the test two pages further was reverted as having changed
    nothing — once per attempt, until the retry budget ran out.
    """

    def test_an_iframe_chain_is_read_whole(self):
        found = fi.identify(FRAMED_PAGE_LOAD)
        assert found["available"] is True
        assert found["selector"] == FRAMED_CHAIN
        assert found["page_object"] == "CheckoutPage"
        assert found["kind"] == fi.PAGE_LOAD

    def test_a_descendant_selector_is_read_whole(self):
        found = fi.identify("Failed to load Element Locator@div.cart tr td.total in CartPage")
        assert found["selector"] == "div.cart tr td.total"
        assert found["page_object"] == "CartPage"

    def test_the_owner_is_the_last_word_not_the_first_in(self):
        found = fi.identify(
            "Failed to load Element Locator@button:has-text('Sign in here') in LoginPage")
        assert found["selector"] == "button:has-text('Sign in here')"
        assert found["page_object"] == "LoginPage"

    def test_several_anchors_are_split_and_the_first_stands_for_them(self):
        anchors, owner = fi.page_load_anchors(
            "Failed to load Element Locator@img.avatar, Locator@div.welcome banner "
            "in HomePage")
        assert anchors == ["img.avatar", "div.welcome banner"]
        assert owner == "HomePage"

    def test_getting_past_the_page_is_progress(self):
        assert fi.same_locator(fi.identify(FRAMED_PAGE_LOAD), fi.identify(CARD_CODE)) is False

    def test_the_same_page_failing_to_load_on_another_anchor_is_not_progress(self):
        # A replacement anchor that is also absent changes the message and
        # nothing about the outcome.
        again = FRAMED_PAGE_LOAD.replace("div.order-total", "div.order-sum")
        assert fi.same_locator(fi.identify(FRAMED_PAGE_LOAD), fi.identify(again)) is True

    def test_a_different_page_failing_to_load_is_progress(self):
        nxt = "Failed to load Element Locator@#receipt in ReceiptPage"
        assert fi.same_locator(fi.identify(FRAMED_PAGE_LOAD), fi.identify(nxt)) is False


class TestConstructorFrames:
    def test_a_constructor_frame_names_its_page_object(self):
        # A page-load assertion is raised from a constructor: `<init>`, no method.
        found = fi.identify("Failed to click on element 'Pay' with locator: Locator@#pay\n"
                            "\tat shop.web.CheckoutPage.<init>(CheckoutPage.java:26)\n"
                            "\tat shop.web.CartPage.checkout(CartPage.java:42)")
        assert found["page_object"] == "CheckoutPage"


class TestFailingSelector:
    """The selector a failure is filed under, when no trace named one."""

    def test_the_message_names_it(self):
        assert fi.failing_selector(FRAMED_PAGE_LOAD) == FRAMED_CHAIN

    def test_the_recorded_anchor_names_it_when_the_message_does_not(self):
        context = {"anchors": [{"selector": "Locator@div.cart tr", "count": 0}]}
        assert fi.failing_selector("BUILD FAILURE", context) == "div.cart tr"

    def test_the_message_wins_over_the_context(self):
        context = {"anchors": [{"selector": "Locator@#other", "count": 0}]}
        assert fi.failing_selector(CARD_CODE, context) == \
            "#pay-frame >> internal:control=enter-frame >> #card-code"

    def test_nothing_named_is_an_empty_answer_not_a_guess(self):
        assert fi.failing_selector("BUILD FAILURE", {"anchors": []}) == ""
        assert fi.failing_selector("") == ""


class TestIdentityOfARecord:
    def test_a_carried_identity_outlives_the_text_it_came_from(self):
        carried = fi.identify(FRAMED_PAGE_LOAD)
        record = {"error_message": "the tail of a build log", "failure_identity": carried}
        assert fi.of_issue(record) == carried

    def test_the_text_is_read_when_nothing_was_carried(self):
        assert fi.of_issue({"error_message": CARD_CODE})["element"] == "Card code field"

    def test_the_selector_it_was_filed_under_is_the_last_resort(self):
        record = {"error_message": "wording nothing recognises", "failed_selector": "#old"}
        found = fi.of_issue(record, {"page_object": "CheckoutPage",
                                     "kind": "PAGE_NOT_LOADED"})
        assert found["available"] is True
        assert found["selector"] == "#old"
        assert found["page_object"] == "CheckoutPage"
        assert found["kind"] == fi.PAGE_LOAD

    def test_a_record_with_nothing_stays_unidentified(self):
        assert fi.of_issue({"error_message": "BUILD FAILURE"})["available"] is False
