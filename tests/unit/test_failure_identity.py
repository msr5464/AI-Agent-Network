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
