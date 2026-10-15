"""Which locators count as read by an assertion, and what is done about them.

A locator an assertion reads is repaired only on proof that the replacement is
the same element. An assertion that a PAGE has loaded is not one of those: its
anchor is how the page is recognised. Counting it as one made every page-load
anchor in a repository unrepairable — which went unnoticed only because, until
the failing selector of a page-load failure was recorded at all, the offline
locate never got as far as asking.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import locator_assertions

CART_PAGE = '''
public class CartPage extends BasePage {
    private final Locator cartTable;
    private final Locator cartBadge;
    private final Locator checkoutButton;
    public CartPage(Config config) {
        cartTable = page.locator("table.cart");
        cartBadge = page.locator(".badge");
        checkoutButton = page.locator("#checkout");
        assertPageLoaded(cartTable);
    }
    public void verifyBadge() {
        AssertHelper.assertElementText(config, cartBadge, "1", "badge shows one");
    }
    public void checkout() { click(checkoutButton, "Checkout button"); }
}
'''
FIELDS = ["cartTable", "cartBadge", "checkoutButton"]


class TestWhatCountsAsReadByAnAssertion:
    def test_a_page_load_anchor_does_not(self):
        found = locator_assertions.assertion_fields({"CartPage": CART_PAGE}, FIELDS)
        assert "cartTable" not in found

    def test_a_locator_handed_to_a_value_assertion_does(self):
        found = locator_assertions.assertion_fields({"CartPage": CART_PAGE}, FIELDS)
        assert found == {"cartBadge"}

    def test_the_assertion_is_named_for_the_reviewer(self):
        sites = locator_assertions.assertion_sites_by_field({"CartPage": CART_PAGE}, FIELDS)
        assert list(sites) == ["cartBadge"]
        assert sites["cartBadge"].startswith("AssertHelper.assertElementText(")

    def test_an_anchor_that_a_value_assertion_also_reads_still_counts(self):
        source = CART_PAGE.replace("config, cartBadge,", "config, cartTable,")
        assert locator_assertions.assertion_fields({"CartPage": source}, FIELDS) == \
            {"cartTable"}

    def test_a_framework_that_names_no_page_load_assertion_keeps_them_all(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_FRAMEWORK", "selenium")
        monkeypatch.setenv("FRAMEWORK_DIR", "")
        found = locator_assertions.assertion_fields({"CartPage": CART_PAGE}, FIELDS)
        assert found == {"cartTable", "cartBadge"}


class TestTheSwitch:
    def test_the_three_values(self):
        for value in ("never", "proven", "always"):
            assert locator_assertions.heal_mode(
                {"classify": {"heal_assertions": value}}) == value

    def test_anything_else_reads_as_never(self):
        # The one answer that cannot repair a locator nobody meant to have repaired.
        for value in (True, False, "yes", "", None):
            assert locator_assertions.heal_mode(
                {"classify": {"heal_assertions": value}}) == "never"
        assert locator_assertions.heal_mode({}) == "never"

    def test_the_shipped_configuration_repairs_on_proof(self):
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parents[2]
                              / "config" / "locator.yaml").read_text())
        assert locator_assertions.heal_mode(cfg) == "proven"
