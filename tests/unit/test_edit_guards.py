"""Tests for the guards that bound an edit larger than a locator.

Everything here is a way to make a red test green without changing the product.
The existing size and lost-method guards cannot see any of it, because a flow
change is legitimately large and none of these removes a method.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import edit_guards as g

BEFORE = """\
    public void placeOrder() {
        Element.click(testConfig, submitButton, "Submit");
        AssertHelper.assertEquals(testConfig, "Total", total, "42.00");
    }
"""


def _after(body: str) -> str:
    return "    public void placeOrder() {\n" + body + "    }\n"


class TestNoNewSwallowing:
    @pytest.mark.parametrize("added,expected", [
        ('        Thread.sleep(5000);\n', "Thread.sleep"),
        ('        @Ignore\n', "@Ignore"),
        ('        assumeTrue(isReady);\n', "assumeTrue"),
        ('        throw new SkipException("later");\n', "SkipException"),
    ])
    def test_rejects_survivable_failure(self, added, expected):
        ok, reason = g.no_new_swallowing(BEFORE, BEFORE + added)
        assert ok is False and expected in reason

    def test_rejects_empty_catch(self):
        after = BEFORE + "        try { risky(); } catch (Exception e) { }\n"
        ok, reason = g.no_new_swallowing(BEFORE, after)
        assert ok is False and "catch" in reason

    def test_allows_catch_that_records_the_failure(self):
        after = BEFORE + ('        try { risky(); } '
                          'catch (Exception e) { logFail(testConfig, e); }\n')
        ok, _ = g.no_new_swallowing(BEFORE, after)
        assert ok is True, "a catch that reports the failure is not swallowing it"

    def test_allows_an_ordinary_added_step(self):
        after = BEFORE + '        Element.click(testConfig, confirm, "Confirm");\n'
        assert g.no_new_swallowing(BEFORE, after)[0] is True


class TestWrapperCompliance:
    @pytest.mark.parametrize("added", [
        '        driver.findElement(By.id("x")).click();\n',
        '        field.sendKeys("hello");\n',
        '        new WebDriverWait(driver, 10);\n',
    ])
    def test_rejects_raw_driver(self, added):
        ok, reason = g.wrapper_compliance(BEFORE, BEFORE + added)
        assert ok is False and "CONVENTIONS" in reason

    def test_allows_framework_wrappers(self):
        after = BEFORE + '        Element.enterData(testConfig, f, "x", "Field");\n'
        assert g.wrapper_compliance(BEFORE, after)[0] is True


class TestLogStep:
    def test_added_interaction_in_a_test_class_needs_a_logstep(self):
        after = BEFORE + '        Element.click(testConfig, next, "Next");\n'
        ok, reason = g.logstep_present(BEFORE, after, is_test_class=True)
        assert ok is False and "logStep" in reason

    def test_with_a_logstep_it_passes(self):
        after = BEFORE + ('        logStep(testConfig, "Choose a workspace");\n'
                          '        Element.click(testConfig, next, "Next");\n')
        assert g.logstep_present(BEFORE, after, is_test_class=True)[0] is True

    def test_page_objects_are_exempt(self):
        after = BEFORE + '        Element.click(testConfig, next, "Next");\n'
        assert g.logstep_present(BEFORE, after, is_test_class=False)[0] is True


class TestMatchesNegative:
    def test_anchor_matching_a_failure_page_is_rejected(self):
        logged_out = ('<html><body class="logged-out">'
                      '<a href="/login" class="brand">Home</a></body></html>')
        ok, reason = g.matches_negative([".brand"], [logged_out])
        assert ok is False and "proves nothing" in reason

    def test_anchor_absent_from_the_failure_page_is_fine(self):
        logged_out = '<html><body class="logged-out"><a href="/login">In</a></body></html>'
        assert g.matches_negative(["#dashboard-widget"], [logged_out])[0] is True

    def test_no_negatives_means_no_opinion(self):
        assert g.matches_negative([".anything"], [])[0] is True


def _step(name, unique=True, kind="new"):
    return {"action": {"target": {"name": name, "accessible_name": name}},
            "selector_check": {"unique": unique},
            "maps_to_test": {"kind": kind}}


class TestStepsJustified:
    def test_added_step_matching_an_observation_is_allowed(self):
        after = BEFORE + '        Element.click(testConfig, workspaceCard, "Acme");\n'
        ok, reason = g.steps_justified(BEFORE, after, [_step("workspaceCard")])
        assert ok is True, reason

    def test_invented_step_is_rejected(self):
        after = BEFORE + '        Element.click(testConfig, mysteryButton, "?");\n'
        ok, reason = g.steps_justified(BEFORE, after, [_step("workspaceCard")])
        assert ok is False and "observed" in reason

    def test_unverifiable_selector_justifies_nothing(self):
        after = BEFORE + '        Element.click(testConfig, workspaceCard, "Acme");\n'
        ok, reason = g.steps_justified(BEFORE, after,
                                       [_step("workspaceCard", unique=None)])
        assert ok is False, (
            "an observation whose selector could not be verified unique is not "
            "an observation")

    def test_more_steps_than_were_observed_is_rejected(self):
        after = BEFORE + ('        Element.click(testConfig, workspaceCard, "A");\n'
                          '        Element.click(testConfig, workspaceCard, "B");\n'
                          '        Element.click(testConfig, workspaceCard, "C");\n')
        ok, reason = g.steps_justified(BEFORE, after, [_step("workspaceCard")])
        assert ok is False and "more steps than were seen" in reason

    def test_no_added_interactions_is_vacuously_fine(self):
        assert g.steps_justified(BEFORE, BEFORE + "        // note\n", [])[0] is True


class TestVisibilityPreflight:
    """A replacement that matches only hidden elements cannot work.

    The run this guards against swapped a missing edit icon for a button that
    existed but was not visible. The click timed out identically, 90 seconds
    later. `:has-text()` is the shape that slipped through: BeautifulSoup cannot
    compile it, so the old match-count rule skipped the selector and passed.
    """

    LINE = '    private final Locator edit = page.locator("%s");\n'
    ORIGINAL = LINE % "#summary img[alt='mukesh']"

    BODY = ('<div id="summary"><button>Profile summary</button>'
            '<span><img alt="PencilSimple"></span></div>')
    PRINTS = {"elements": [
        {"tag": "button", "id": None, "testid": None, "alt": None,
         "aria_label": None, "text": "Profile summary", "is_visible": False},
        {"tag": "img", "id": None, "testid": None, "alt": "PencilSimple",
         "aria_label": None, "text": "", "is_visible": True},
    ]}

    @pytest.fixture
    def soup(self):
        bs4 = pytest.importorskip("bs4")
        return bs4.BeautifulSoup(self.BODY, "html.parser")

    def _check(self, soup, selector, prints):
        return g.validate_diagnosis_fit(
            self.ORIGINAL, self.LINE % selector, "LOCATOR_STALE", soup, prints)

    def test_rejects_a_selector_matching_only_hidden_elements(self, soup):
        ok, why = self._check(soup, "#summary button:has-text('Profile summary')",
                              self.PRINTS)
        assert not ok
        assert "not visible" in why

    def test_accepts_a_selector_matching_a_visible_element(self, soup):
        assert self._check(soup, '#summary img[alt="PencilSimple"]', self.PRINTS)[0]

    def test_accepts_the_java_escaped_form(self, soup):
        """Locate emits page.locator("...img[alt=\\"PencilSimple\\"]")."""
        assert self._check(soup, '#summary img[alt=\\"PencilSimple\\"]', self.PRINTS)[0]

    def test_still_rejects_a_selector_matching_nothing(self, soup):
        ok, why = self._check(soup, "#edit-summary", self.PRINTS)
        assert not ok
        assert "matches nothing" in why

    @pytest.mark.parametrize("prints", [None, {}, {"elements": []}])
    def test_without_fingerprints_behaviour_is_unchanged(self, soup, prints):
        """No sidecar must degrade to the match-count rule, never to 'reject'."""
        assert self._check(soup, "#summary button:has-text('Profile summary')",
                           prints)[0]

    def test_without_a_snapshot_the_rule_does_not_run(self):
        assert g.validate_diagnosis_fit(
            self.ORIGINAL, self.LINE % "#anything", "LOCATOR_STALE",
            None, self.PRINTS)[0]

    def test_only_applies_to_stale_locator_verdicts(self, soup):
        assert g.validate_diagnosis_fit(
            self.ORIGINAL, self.LINE % "#summary button:has-text('Profile summary')",
            "TOO_SLOW", soup, self.PRINTS)[0]


class TestStaleCaptureIsNotEvidence:
    """A capture is evidence only for the failure it was captured for.

    The bug this pins: attempt 1 repaired the login button and three tests went
    green; two reached the cart page and failed there. The retry still carried
    the login page's capture, so when the model answered with the cart locator
    the match-count rule found nothing, called a correct fix "a guess" and
    reverted it — before any test ran.
    """

    LINE = '    private final Locator cart = page.locator("%s");\n'
    # The file no longer mentions the selector the capture was taken for: an
    # earlier edit already repaired it.
    ORIGINAL = LINE % ".mukesh"

    BODY = '<div id="login-box"><input id="login-button"></div>'

    @pytest.fixture
    def soup(self):
        bs4 = pytest.importorskip("bs4")
        return bs4.BeautifulSoup(self.BODY, "html.parser")

    def test_a_capture_for_an_already_repaired_selector_does_not_rule(self, soup):
        ok, _why = g.validate_diagnosis_fit(
            self.ORIGINAL, self.LINE % "#shopping_cart_container", "LOCATOR_STALE",
            soup, {}, failing_selector="#login-mukesh")
        assert ok, "a capture of a page the flow has moved past must not reject"

    def test_a_capture_for_the_failure_in_hand_still_rules(self, soup):
        ok, why = g.validate_diagnosis_fit(
            self.LINE % "#login-mukesh", self.LINE % "#nowhere", "LOCATOR_STALE",
            soup, {}, failing_selector="#login-mukesh")
        assert not ok and "matches nothing" in why

    def test_callers_that_name_no_selector_are_unchanged(self, soup):
        ok, why = g.validate_diagnosis_fit(
            self.ORIGINAL, self.LINE % "#nowhere", "LOCATOR_STALE", soup, {})
        assert not ok and "matches nothing" in why


class TestAmbiguousLocatorGuard:
    """A fix for an ambiguous locator has to be unambiguous.

    The bug this pins: the model answered `#loginForm button[type='submit']` for a
    strict-mode violation. Both buttons were inside that form, so it still matched
    two and failed identically — discovered by running the test for 27 seconds and
    then reverting, when the DOM captured at failure said so for free.
    """

    PAGE = ('<html><body><form id="loginForm">'
            '<button type="submit" class="blue-btn">Login</button>'
            '<button type="submit" class="otpButton">Use OTP to Login</button>'
            '</form></body></html>')

    def _soup(self):
        from bs4 import BeautifulSoup
        return BeautifulSoup(self.PAGE, "html.parser")

    def _edit(self, selector):
        return ('    Locator loginButton = page.locator("button[type=\'submit\']");',
                f'    Locator loginButton = page.locator("{selector}");')

    def test_a_still_ambiguous_selector_is_rejected(self):
        original, updated = self._edit("#loginForm button[type='submit']")
        ok, reason = g.validate_diagnosis_fit(original, updated, "AMBIGUOUS_LOCATOR",
                                            self._soup(), {})
        assert not ok
        assert "still matches 2 elements" in reason

    def test_a_unique_selector_is_accepted(self):
        original, updated = self._edit("button[type='submit'].blue-btn")
        ok, _ = g.validate_diagnosis_fit(original, updated, "AMBIGUOUS_LOCATOR",
                                       self._soup(), {})
        assert ok

    def test_a_selector_matching_nothing_is_rejected(self):
        original, updated = self._edit("button.does-not-exist")
        ok, reason = g.validate_diagnosis_fit(original, updated, "AMBIGUOUS_LOCATOR",
                                            self._soup(), {})
        assert not ok
        assert "matches nothing" in reason

    def test_the_rule_only_applies_to_this_verdict(self):
        # A stale locator has its own rule; two matches is not a defect there.
        original, updated = self._edit("#loginForm button[type='submit']")
        ok, _ = g.validate_diagnosis_fit(original, updated, "LOCATOR_STALE",
                                       self._soup(), {})
        assert ok


class TestStaleLocatorActedOn:
    """A stale locator the test clicks must be replaced by one matching one element.

    Both attempts on the run this pins matched Login and Use OTP to Login —
    `#loginForm button:has-text('Login')`, then `button[type='submit']` — and each
    was found out by a 40-second Maven run and a revert.
    """

    def _fit(self, selector, extra=""):
        from bs4 import BeautifulSoup
        original, updated = TestAmbiguousLocatorGuard()._edit(selector)
        soup = BeautifulSoup(TestAmbiguousLocatorGuard.PAGE, "html.parser")
        return g.validate_diagnosis_fit(original + extra, updated + extra, "LOCATOR_STALE",
                                        soup, {}, require_unique=True)

    def test_a_replacement_matching_two_is_rejected(self):
        ok, reason = self._fit("#loginForm button[type='submit']:has-text('Login')")
        assert not ok and "matches 2 elements" in reason

    def test_exact_text_is_unique(self):
        assert self._fit("#loginForm button[type='submit']:text-is('Login')")[0]

    def test_a_field_used_as_a_list_may_match_several(self):
        assert self._fit("#loginForm button[type='submit']",
                         "\n    loginButton.first().click();")[0]

    def test_positional_narrowing_in_the_selector_is_one_element(self):
        assert self._fit("#loginForm button[type='submit'] >> nth=0")[0]


class TestPlaywrightShapes:
    """The guards were written for Selenium; a Playwright repo slipped past them."""

    @pytest.mark.parametrize("added", [
        '        loginButton.click();\n',
        '        page.locator("#user").fill("standard_user");\n',
        '        cartBadge.hover();\n',
        '        page.waitForTimeout(2000);\n',
    ])
    def test_rejects_raw_playwright_calls(self, added):
        ok, reason = g.wrapper_compliance(BEFORE, BEFORE + added)
        assert ok is False and "CONVENTIONS" in reason

    @pytest.mark.parametrize("added", [
        '        click(checkoutButton, "Checkout");\n',
        '        fillText(zipField, zip, "Zip code");\n',
        '        Element.click(config, next, "Next");\n',
        '        workspace = page.locator("[data-test=\'workspace\']");\n',
        '        items.clear();\n',
    ])
    def test_allows_wrappers_and_locator_declarations(self, added):
        assert g.wrapper_compliance(BEFORE, BEFORE + added)[0] is True

    def test_a_helper_step_in_a_playwright_test_needs_a_logstep(self):
        after = BEFORE + '        products.addToCart("Sauce Labs Backpack");\n'
        ok, reason = g.logstep_present(BEFORE, after, is_test_class=True)
        assert ok is False and "logStep" in reason

    def test_config_logstep_satisfies_it(self):
        after = BEFORE + ('        config.logStep("Add the backpack to the cart");\n'
                          '        products.addToCart("Sauce Labs Backpack");\n')
        assert g.logstep_present(BEFORE, after, is_test_class=True)[0] is True

    def test_reading_data_is_not_a_step(self):
        after = BEFORE + '        String user = helper.getUsername();\n'
        assert g.logstep_present(BEFORE, after, is_test_class=True)[0] is True

    def test_an_added_assertion_is_not_a_new_step(self):
        after = BEFORE + '        AssertHelper.assertEquals(config, badge, "2", "Cart badge");\n'
        assert g.logstep_present(BEFORE, after, is_test_class=True)[0] is True
