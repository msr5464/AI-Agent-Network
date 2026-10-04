"""Tests for shared/module_index.py — what code exists, and what a change changed.

Codegen cannot reuse a method it never saw, and a reviewer cannot judge a change
to existing code nobody listed. These pin both halves: the index the model is
shown, and the diffs the review notes and the regression re-run are built from.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import module_index as mi  # noqa: E402

HELPER = '''package automation.modules.shop;

/** The shop's flow API. See {@link ShopEnums}. */
public class ShopHelper extends ApiHelper
{
    public ShopHelper(Config config)
    {
        super(config, config.getRunTimeProperty("shop.api.url"));
    }

    @SafeVarargs
    public final CartPage addToCart(Map<String, String>... products)
    {
        CartPage cart = new CartPage(config);
        for (Map<String, String> product : products) { cart.add(product.get("slug")); }
        return cart;
    }

    public String checkout(ShopData order)
    {
        CartPage cart = new CartPage(config);
        cart.fillDetails(order);
        cart.confirm();
        return cart.submit().getTotal();
    }

    private void wiring() {}

    @Value
    public static class Receipt
    {
        String amount;
        String reference;
    }
}
'''

ENUMS = '''package automation.modules.shop;

public class ShopEnums
{
    public enum PaymentMethod
    {
        CreditCard("card", "Card"), BankTransfer("bank", "Bank, transfer"), Wallet("wallet", "Wallet");

        private final String key;
        private final String label;

        PaymentMethod(String key, String label) { this.key = key; this.label = label; }

        public String getKey() { return key; }
    }
}
'''


def test_public_signatures_skip_private_members_and_keep_varargs_and_generics():
    signatures = mi.public_signatures(HELPER)
    assert signatures["ShopHelper"] == [
        "ShopHelper(Config config)",
        "CartPage addToCart(Map<String, String>... products)",
        "String checkout(ShopData order)",
    ]
    assert signatures["ShopHelper.Receipt"] == []


def test_enum_constants_are_found_in_nested_enums_with_arguments():
    assert mi.enum_constants(ENUMS) == {"ShopEnums.PaymentMethod": ["CreditCard", "BankTransfer", "Wallet"]}


def test_describe_lists_each_class_with_its_members(tmp_path):
    module = tmp_path / "src/main/java/automation/modules/shop"
    module.mkdir(parents=True)
    (module / "ShopHelper.java").write_text(HELPER)
    (module / "ShopEnums.java").write_text(ENUMS)
    text = mi.describe_module(tmp_path, "src/main/java/automation/modules/shop")
    assert "class ShopHelper  (src/main/java/automation/modules/shop/ShopHelper.java)" in text
    assert "    String checkout(ShopData order)" in text
    assert "    fields: amount, reference" in text            # the nested value class
    assert "enum ShopEnums.PaymentMethod: CreditCard, BankTransfer, Wallet" in text
    assert "wiring" not in text


def test_changed_methods_tells_a_changed_body_from_an_added_overload():
    after = HELPER.replace("cart.confirm();", "cart.confirm(true);").replace(
        "    private void wiring() {}",
        "    public String checkout(ShopData order, boolean express) { return checkout(order); }\n"
        "    private void wiring() {}")
    diff = mi.changed_methods(HELPER, after)
    assert diff["changed"] == ["ShopHelper.checkout(ShopData)"]
    assert diff["added"] == ["ShopHelper.checkout(ShopData,boolean)"]
    assert diff["removed"] == []


def test_lost_api_names_removed_public_methods_and_enum_constants():
    after = HELPER.replace("public String checkout(ShopData order)", "public String checkout(ShopData order, int n)")
    assert mi.lost_api(HELPER, after) == ["public String checkout(ShopData order)"]
    assert mi.lost_api(ENUMS, ENUMS.replace(', Wallet("wallet", "Wallet")', "")) == \
        ["ShopEnums.PaymentMethod.Wallet"]


def test_changed_fields_lists_changed_initializers_not_added_fields():
    page = 'public class CartPage { private final Locator pay = page.locator("#pay"); }'
    assert mi.changed_fields(page, page.replace("#pay", "#pay-now")) == ["CartPage.pay"]
    added = page.replace("}", 'private final Locator total = page.locator("#total"); }', 1)
    assert mi.changed_fields(page, added) == []


def test_a_copy_with_only_literals_changed_is_a_near_duplicate():
    copy = HELPER.replace(
        "    private void wiring() {}",
        "    public String checkoutExpress(ShopData order)\n    {\n"
        "        CartPage cart = new CartPage(config);\n        cart.fillDetails(order);\n"
        "        cart.confirm();\n        return cart.submit().getTotal();\n    }\n"
        "    private void wiring() {}")
    found = mi.near_duplicates({"Shop.java": copy}, {"Shop.java": HELPER},
                               only={"Shop.java": ["ShopHelper.checkoutExpress(ShopData)"]})
    assert found == [{"new": "ShopHelper.checkoutExpress(ShopData)",
                      "like": "ShopHelper.checkout(ShopData)", "ratio": 1.0}]


def test_methods_doing_different_things_are_not_duplicates():
    other = HELPER.replace(
        "    private void wiring() {}",
        "    public void refund(ShopData order)\n    {\n        OrdersPage orders = openOrders();\n"
        "        orders.find(order.getId()).requestRefund();\n        WaitHelper.waitForPageLoad(config);\n    }\n"
        "    private void wiring() {}")
    assert mi.near_duplicates({"Shop.java": other}, {"Shop.java": HELPER},
                              only={"Shop.java": ["ShopHelper.refund(ShopData)"]}) == []


def test_a_reuse_claim_is_checked_against_what_exists(tmp_path):
    module = tmp_path / "src/main/java/automation/modules/shop"
    module.mkdir(parents=True)
    (module / "ShopHelper.java").write_text(HELPER)
    known = mi.known_members(tmp_path, ["src/main/java/automation/modules/shop"])
    assert mi.parse_member_reference("static String ShopHelper.checkout(ShopData order)") == \
        ("ShopHelper", "checkout")
    assert mi.is_known(known, "ShopHelper", "checkout")
    assert mi.is_known(known, "ShopHelper.Receipt", "getAmount")   # a Lombok getter
    assert not mi.is_known(known, "ShopHelper", "makePayment")
    assert mi.parse_member_reference("no method named here") is None


def test_page_constructors_and_selections_through_different_enums_are_not_copies():
    pages = '''public class PaymentPage extends BasePage {
    public PaymentPage(Config config) { super(config); total = page.locator("#total"); assertPageLoaded(total); }
    public CardPage choose(PaymentMethod method) {
        click(page.frameLocator("#pay").locator("a[data-option='" + method.getKey() + "']"), method.getLabel());
        return new CardPage(config);
    }
}
'''
    cards = '''public class CardPage extends BasePage {
    public CardPage(Config config) { super(config); total = page.locator("#total"); assertPageLoaded(total); }
    public CardPage applyPromo(Promo promo) {
        click(page.frameLocator("#pay").locator("label[for='" + promo.getKey() + "']"), promo.getLabel());
        return new CardPage(config);
    }
}
'''
    assert mi.near_duplicates({"PaymentPage.java": pages, "CardPage.java": cards}, {}) == []
