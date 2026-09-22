"""Tests for shared/assertion_graph.py.

Once an agent may add and remove steps, "the test went green" stops being
evidence of anything — the cheapest way to make an assertion pass is to stop
running it. These cases are the ways that happens in practice, and each one is
invisible to a diff of the test file alone.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import assertion_graph as ag
from shared import blast_radius as br


def _write(root: Path, package: str, name: str, body: str, imports=()):
    parts = package.split(".")
    path = root / "src" / "main" / "java" / Path(*parts) / f"{name}.java"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(f"import {i};\n" for i in imports)
    path.write_text(f"package {package};\n\n{head}public class {name} {{\n{body}\n}}\n")
    return path


def _index(tmp_path):
    br._cache.clear()
    return ag.member_index(str(tmp_path))


TEST_BODY = """\
    CheckoutHelper helper;

    public void placeOrder() {
        logStep(testConfig, "Place an order and verify the confirmation number");
        helper.completeCheckout();
    }
"""

HELPER_STRONG = """\
    ConfirmPage confirm;

    public void completeCheckout() {
        confirm.verifyTotal();
    }
"""

PAGE_STRONG = """\
    public void verifyTotal() {
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
    }
"""


def _repo(tmp_path, page_body=PAGE_STRONG, helper_body=HELPER_STRONG):
    _write(tmp_path, "automation.checkout", "CheckoutTest", TEST_BODY)
    _write(tmp_path, "automation.checkout", "CheckoutHelper", helper_body)
    _write(tmp_path, "automation.checkout", "ConfirmPage", page_body)
    return _index(tmp_path)


class TestReachability:
    def test_assertion_two_hops_down_is_found(self, tmp_path):
        index = _repo(tmp_path)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        sites = {i["site"] for i in fps["asserts"].values()}
        assert "ConfirmPage#verifyTotal" in sites, (
            "the assertion lives in a page object the test never names; "
            "comparing the test file's own diff would prove nothing")
        # Both the project wrapper (confirm.verifyTotal()) and the
        # AssertHelper call inside it are recorded. Redundant on purpose:
        # each is independently a check, and removing either has to be caught.
        callees = {i["callee"] for i in fps["asserts"].values()}
        assert {"verifyTotal", "AssertHelper.assertEquals"} <= callees

    def test_log_steps_are_collected_for_the_intent_contract(self, tmp_path):
        index = _repo(tmp_path)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        assert fps["log_steps"] == [
            "Place an order and verify the confirmation number"]

    def test_brace_inside_a_selector_string_does_not_tear_a_member(self, tmp_path):
        page = '''\
    public void verifyTotal() {
        String sel = "div[data-x='}'] .total";
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
    }
'''
        index = _repo(tmp_path, page_body=page)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        found = [i for i in fps["asserts"].values()
                 if i["callee"] == "AssertHelper.assertEquals"]
        assert found, (
            "a } inside a CSS selector string is common in this codebase; a "
            "regex splitter truncates the method and reports a phantom loss")
        assert '"42.00"' in found[0]["literals"], (
            "the expected value must survive into the fingerprint, or a "
            "weakened assertion is indistinguishable from an intact one")


class TestConservation:
    def test_unchanged_code_conserves(self, tmp_path):
        index = _repo(tmp_path)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        report = ag.conserved(fps, fps)
        assert report["ok"] is True
        assert "OK" in ag.describe(report)

    def test_assertion_deleted_two_hops_down_is_caught(self, tmp_path, tmp_path_factory):
        before = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        after_root = tmp_path_factory.mktemp("after")
        index = _repo(after_root, page_body="    public void verifyTotal() {\n    }\n")
        after = ag.fingerprints("CheckoutTest", "placeOrder", index)
        report = ag.conserved(before, after)
        assert report["ok"] is False
        assert report["lost"], "a deleted assertion must be named, not merely counted"
        assert "42.00" in report["lost"][0]

    def test_weakened_assertion_is_caught(self, tmp_path, tmp_path_factory):
        before = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        weaker = '''\
    public void verifyTotal() {
        AssertHelper.assertNotNull(testConfig, "Order total", total);
    }
'''
        after_root = tmp_path_factory.mktemp("weak")
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                _repo(after_root, page_body=weaker))
        report = ag.conserved(before, after)
        assert report["ok"] is False
        assert report["weakened"], (
            "assertEquals -> assertNotNull still asserts something, which is "
            "exactly why a call-site count would wave it through")

    def test_assertion_made_conditional_is_caught(self, tmp_path, tmp_path_factory):
        before = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        guarded = '''\
    public void verifyTotal() {
        if (Element.isElementDisplayed(testConfig, total)) {
            AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
        }
    }
'''
        after_root = tmp_path_factory.mktemp("cond")
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                _repo(after_root, page_body=guarded))
        report = ag.conserved(before, after)
        assert report["ok"] is False
        assert report["conditionalised"], (
            "an assertion that only runs when it would pass is a deleted "
            "assertion wearing a disguise")

    def test_duplicate_assertions_are_counted_separately(self, tmp_path, tmp_path_factory):
        # Cart total and checkout total, both "42.00": one fingerprint used to hold
        # both, so deleting either of them was invisible.
        twice = '''\
    public void verifyTotal() {
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
    }
'''
        before = ag.fingerprints("CheckoutTest", "placeOrder",
                                 _repo(tmp_path, page_body=twice))
        after_root = tmp_path_factory.mktemp("once")
        after = ag.fingerprints("CheckoutTest", "placeOrder", _repo(after_root))
        report = ag.conserved(before, after)
        assert len(before["asserts"]) == len(after["asserts"]) + 1
        assert report["ok"] is False
        assert report["lost"], "deleting one of two identical assertions must be caught"

    def test_a_contract_frozen_before_occurrence_suffixes_still_conserves(self, tmp_path):
        # A session frozen by the previous release stores bare hashes and no
        # skeleton. Resuming it must not read every assertion as removed.
        fps = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        legacy = {"asserts": {fp.rsplit("_", 1)[0]: {k: v for k, v in info.items()
                                                     if k != "skeleton"}
                              for fp, info in fps["asserts"].items()},
                  "unresolved": fps["unresolved"], "log_steps": []}
        report = ag.conserved(legacy, fps)
        assert report["ok"] is True, ag.describe(report)
        assert all("_matched" not in info for info in legacy["asserts"].values()), (
            "the frozen contract is reused across items and attempts; comparing "
            "against it must not write into it")


class TestHoles:
    def test_unresolvable_receiver_is_reported_not_dropped(self, tmp_path):
        helper = '''\
    public void completeCheckout() {
        somethingUnknown.doTheThing();
    }
'''
        index = _repo(tmp_path, helper_body=helper)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        assert any("somethingUnknown" in u for u in fps["unresolved"]), (
            "a call we could not follow is a hole in the guarantee; ignoring it "
            "turns 'no assertion was lost' into 'none that I looked at'")

    def test_new_holes_downgrade_to_plausible_rather_than_failing(self, tmp_path):
        index = _repo(tmp_path)
        before = ag.fingerprints("CheckoutTest", "placeOrder", index)
        after = {"asserts": dict(before["asserts"]),
                 "unresolved": ["CheckoutHelper#completeCheckout -> x.y()"],
                 "log_steps": []}
        report = ag.conserved(before, after)
        assert report["ok"] is True
        assert report["verdict"] == "PLAUSIBLE"
        assert "could not be resolved" in ag.describe(report)


class TestMessageVersusExpectedValue:
    """The distinction that decides whether this guard is usable at all.

    An assertion's last argument is its human-readable failure message. Including
    it in the fingerprint meant improving the wording of a message registered as a
    *weakened assertion* — and a guard that cries wolf over a copy edit is one
    people learn to override, which costs far more than it saves.
    """

    PAGE = ('    public void verifyTotal() {\n'
            '        AssertHelper.assertEquals(testConfig, total, "42.00", '
            '"Order total should be 42.00");\n'
            '    }\n')

    # The checkout run's real shape: several amounts asserted side by side, all
    # the same call shape, and the page renders each as `$ 175.00`, not `$175.00`.
    MONEY = ('    public void verifyTotal() {\n'
             '        AssertHelper.assertEquals(testConfig, price, "$175.00", "Price");\n'
             '        AssertHelper.assertEquals(testConfig, shipping, "$8.99", "Shipping");\n'
             '        AssertHelper.assertEquals(testConfig, total, "$183.99", "Total");\n'
             '    }\n')

    def _report(self, tmp_path, factory, mutated, page=None):
        before = ag.fingerprints("CheckoutTest", "placeOrder",
                                 _repo(tmp_path, page_body=page or self.PAGE))
        after_root = factory.mktemp("after")
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                _repo(after_root, page_body=mutated))
        return ag.conserved(before, after)

    def test_rewording_the_message_is_allowed(self, tmp_path, tmp_path_factory):
        reworded = self.PAGE.replace("Order total should be 42.00",
                                     "Order grand total should be 42.00")
        assert self._report(tmp_path, tmp_path_factory, reworded)["ok"] is True

    def test_changing_the_expected_value_is_blocked(self, tmp_path, tmp_path_factory):
        changed = self.PAGE.replace('"42.00", "Order total', '"43.00", "Order total')
        report = self._report(tmp_path, tmp_path_factory, changed)
        assert report["ok"] is False, (
            "the expected value is the whole point of the assertion; only the "
            "message is cosmetic")

    def test_reformatting_the_expected_value_is_allowed(self, tmp_path, tmp_path_factory):
        spaced = self.MONEY.replace('"$', '"$ ')
        report = self._report(tmp_path, tmp_path_factory, spaced, self.MONEY)
        assert report["ok"] is True, (
            "$175.00 -> $ 175.00 is the page's formatting, not a new expectation; "
            "pairing price with total because they share a call shape is a false "
            "rejection of the fix that was actually right")

    def test_a_different_amount_is_blocked_and_named(self, tmp_path, tmp_path_factory):
        zeroed = self.MONEY.replace('"$', '"$ ').replace('"$ 175.00"', '"$ 0.00"')
        report = self._report(tmp_path, tmp_path_factory, zeroed, self.MONEY)
        assert report["ok"] is False, (
            "same call, same place, same shape — only the amount differs, and "
            "the amount is what a checkout test exists to prove")
        assert '"$175.00" -> "$ 0.00"' in report["reason"], (
            "the reviewer has to see which value became which, not a mispairing")


class TestWhatCountsAsAHole:
    """`unresolved` is what downgrades conservation to PLAUSIBLE, so it has to
    mean something. It used to fire on ordinary framework calls in every single
    test, which trains people to skim past it — and a warning nobody reads is
    worse than no warning, because it still claims to be a guarantee.
    """

    def _write_pair(self, tmp_path, body):
        _write(tmp_path, "automation.checkout", "CheckoutTest", body)
        return _index(tmp_path)

    def test_a_call_on_a_parameter_of_a_framework_type_is_not_a_hole(self, tmp_path):
        # `Config` is not defined in this repo. A parameter declares its type
        # just as firmly as an assignment, but the local-declaration regex needs
        # an `=` and so never saw one.
        index = self._write_pair(tmp_path, textwrap.dedent("""\
            public void placeOrder(Config config) {
                config.logStep("Place an order");
            }
        """))
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        assert fps["unresolved"] == []
        assert fps["log_steps"] == ["Place an order"]

    def test_a_call_on_a_field_inherited_from_an_unreadable_base_is_not_a_hole(self, tmp_path):
        path = _write(tmp_path, "automation.checkout", "CheckoutTest", "")
        path.write_text(textwrap.dedent("""\
            package automation.checkout;

            public class CheckoutTest extends TestBase {
                public void placeOrder() {
                    page.locator("#buy").click();
                }
            }
        """))
        index = _index(tmp_path)
        assert ag.fingerprints("CheckoutTest", "placeOrder", index)["unresolved"] == []

    def test_a_genuinely_missing_method_is_still_reported(self, tmp_path):
        index = self._write_pair(tmp_path, textwrap.dedent("""\
            public void placeOrder() {
                logStep(testConfig, "Place an order");
            }
        """))
        fps = ag.fingerprints("CheckoutTest", "noSuchMethod", index)
        assert any("noSuchMethod" in u for u in fps["unresolved"]), (
            "over-reporting was the bug; under-reporting would be worse")


class TestCommentsAreNotCode:
    def test_a_commented_out_assertion_is_not_counted_as_live(self, tmp_path):
        """Commenting a check out is the cheapest disguise of all.

        This module already refuses an assertion hidden behind an `if`. Scanning
        raw text meant `// assertEquals(...)` fingerprinted identically to the
        real thing, so conservation compared equal and approved an edit that had
        stopped the test proving anything.
        """
        _write(tmp_path, "automation.checkout", "CheckoutTest", textwrap.dedent("""\
            public void placeOrder() {
                // AssertHelper.assertEquals(testConfig, total, "42.00", "total is right");
            }
        """))
        index = _index(tmp_path)
        assert ag.fingerprints("CheckoutTest", "placeOrder", index)["asserts"] == {}

    def test_a_javadoc_example_is_not_a_call(self, tmp_path):
        _write(tmp_path, "automation.checkout", "CheckoutTest", textwrap.dedent("""\
            /**
             * Usage:
             *   PostData created = api.execute(PostApi.CreatePost, post);
             */
            public void placeOrder() {
                logStep(testConfig, "Place an order");
            }
        """))
        index = _index(tmp_path)
        assert ag.fingerprints("CheckoutTest", "placeOrder", index)["unresolved"] == []


# ── delta: per-item comparison for the adaptation agent ─────────────────────

def _flow_checks(root, body, extra=None, follow=True):
    """The merged checks of `ShopTest#flow` in a throwaway repo."""
    _write(root, "automation.shop", "ShopTest", body)
    for name, page in (extra or {}).items():
        _write(root, "automation.shop", name, page)
    fps = ag.fingerprints("ShopTest", "flow", _index(root), follow_constructors=follow)
    return ag.merge({"automation.shop.ShopTest#flow": fps})["checks"]


TITLES = """\
    ProductsPage products;
    public void flow() {
        AssertHelper.assertEquals(config, products.getPageTitle(), "Products", "User should be on Products page");
        AssertHelper.assertEquals(config, products.getCartCount(), "1", "Cart badge should display 1 item");
        AssertHelper.assertEquals(config, products.getCartItemCount(), 1, "Cart should contain exactly 1 item");
        AssertHelper.assertEquals(config, products.getPageTitle(), "Products", "User should be returned to Products page");
    }
"""


def _edit(old, new):
    assert old in TITLES
    return TITLES.replace(old, new)


class TestDelta:
    def _delta(self, tmp_path, tmp_path_factory, after_body):
        before = _flow_checks(tmp_path, TITLES)
        after = _flow_checks(tmp_path_factory.mktemp("after"), after_body)
        return ag.delta(before, after)

    def test_unchanged_code_has_no_delta(self, tmp_path, tmp_path_factory):
        d = self._delta(tmp_path, tmp_path_factory, TITLES)
        assert not any(d.values())

    def test_the_removed_one_of_two_identical_checks_is_the_one_named(
            self, tmp_path, tmp_path_factory):
        # Same call, same expected value; only the message differs. conserved()
        # pairs these by occurrence number and names the wrong one.
        d = self._delta(tmp_path, tmp_path_factory, _edit(
            '        AssertHelper.assertEquals(config, products.getPageTitle(), "Products", '
            '"User should be on Products page");\n', ""))
        assert [c["message"] for c in d["removed"]] == ["User should be on Products page"]
        assert not d["changed"] and not d["moved"]

    def test_a_changed_string_value_is_a_change(self, tmp_path, tmp_path_factory):
        d = self._delta(tmp_path, tmp_path_factory, _edit('"1", "Cart badge', '"2", "Cart badge'))
        assert [(b["values"], a["values"]) for b, a in d["changed"]] == [(["1"], ["2"])]
        assert not d["removed"] and not d["added"]

    def test_a_changed_number_is_a_change_not_a_loss(self, tmp_path, tmp_path_factory):
        d = self._delta(tmp_path, tmp_path_factory, _edit("getCartItemCount(), 1,",
                                                          "getCartItemCount(), 2,"))
        assert [(b["values"], a["values"]) for b, a in d["changed"]] == [(["1"], ["2"])]
        assert not d["removed"]

    def test_a_reworded_message_is_reworded(self, tmp_path, tmp_path_factory):
        d = self._delta(tmp_path, tmp_path_factory, _edit(
            "Cart badge should display 1 item", "Badge shows one item"))
        assert [a["message"] for _, a in d["reworded"]] == ["Badge shows one item"]
        assert not d["removed"] and not d["changed"]

    def test_a_check_moved_to_another_method_is_a_move(self, tmp_path, tmp_path_factory):
        # Not `verifyBadge`: ASSERT_CALL counts any verifyX(...) call as an assertion.
        moved = TITLES.replace(
            '        AssertHelper.assertEquals(config, products.getCartCount(), "1", '
            '"Cart badge should display 1 item");\n', "        showBadge();\n"
        ).rstrip() + ('\n    public void showBadge() {\n        AssertHelper.assertEquals('
                      'config, products.getCartCount(), "1", "Cart badge should display 1 '
                      'item");\n    }\n')
        d = self._delta(tmp_path, tmp_path_factory, moved)
        assert [(b["site"], a["site"]) for b, a in d["moved"]] == [
            ("ShopTest#flow", "ShopTest#showBadge")]
        assert not d["removed"] and not d["added"]

    def test_wrapping_a_check_in_an_if_is_conditional(self, tmp_path, tmp_path_factory):
        d = self._delta(tmp_path, tmp_path_factory, _edit(
            '        AssertHelper.assertEquals(config, products.getCartCount(), "1", '
            '"Cart badge should display 1 item");',
            '        if (products.isShown()) {\n            AssertHelper.assertEquals(config, '
            'products.getCartCount(), "1", "Cart badge should display 1 item");\n        }'))
        assert [b["message"] for b, _ in d["conditional"]] == ["Cart badge should display 1 item"]

    def test_unwrapping_a_check_is_not_conditional(self, tmp_path, tmp_path_factory):
        wrapped = _edit(
            '        AssertHelper.assertEquals(config, products.getCartCount(), "1", '
            '"Cart badge should display 1 item");',
            '        if (products.isShown()) {\n            AssertHelper.assertEquals(config, '
            'products.getCartCount(), "1", "Cart badge should display 1 item");\n        }')
        before = _flow_checks(tmp_path, wrapped)
        after = _flow_checks(tmp_path_factory.mktemp("after"), TITLES)
        assert not ag.delta(before, after)["conditional"]

    def test_a_weaker_assertion_in_the_same_place_is_weakened(self, tmp_path, tmp_path_factory):
        d = self._delta(tmp_path, tmp_path_factory, _edit(
            'AssertHelper.assertEquals(config, products.getCartCount(), "1",',
            'AssertHelper.assertContains(config, products.getCartCount(), "1",'))
        assert len(d["weakened"]) == 1 and not d["removed"]

    def test_ids_are_stable_while_a_check_is_unchanged(self, tmp_path, tmp_path_factory):
        before = {c["message"]: c["id"] for c in _flow_checks(tmp_path, TITLES)}
        after = {c["message"]: c["id"] for c in _flow_checks(
            tmp_path_factory.mktemp("after"), _edit('"1", "Cart badge', '"2", "Cart badge'))}
        assert before["User should be on Products page"] == after["User should be on Products page"]
        assert before["Cart badge should display 1 item"] != after["Cart badge should display 1 item"]


class TestCheckParts:
    @pytest.mark.parametrize("raw, canonical", [
        ('"$ 1.50"', "$1.50"), ("1_000", "1000"), ("10L", "10"), ("0.50f", "0.5"),
        ("0x1F", "31"), ("-3", "-3"), ("1.0", "1"), ('" Products "', "products"),
    ])
    def test_values_are_compared_however_they_are_written(self, raw, canonical):
        assert ag.canonical_value(raw) == canonical

    def test_a_value_nested_in_a_call_is_not_a_top_level_argument(self):
        parts = ag.check_parts({"literals": ['"title"', '"Second product in cart"'],
                                "skeleton": "_,_._(_._(@)),@"})
        assert parts["values"] == ["title"] and parts["top"] == [False]
        assert parts["message"] == "Second product in cart"

    def test_subtraction_is_not_a_negative_number(self):
        parts = ag.check_parts({"literals": [], "skeleton": "_,-1,_-1"})
        assert parts["values"] == ["-1", "1"] and parts["top"] == [True, False]


CART_PAGE = """\
    public CartPage(Config config) {
        assertPageLoaded(cartList);
    }
"""

NEW_PAGES = """\
    public void flow() {
        CartPage cart = new CartPage(config);
        List<String> names = new ArrayList<>();
        Helper helper = new Helper(config);
    }
"""


class TestConstructors:
    def test_a_page_objects_constructor_check_is_followed_when_asked(self, tmp_path):
        checks = _flow_checks(tmp_path, NEW_PAGES,
                              {"CartPage": CART_PAGE, "Helper": "    int x;\n"})
        assert [(c["site"], c["via"]) for c in checks] == [("CartPage#CartPage", "new CartPage()")]

    def test_off_by_default_so_other_agents_measure_as_before(self, tmp_path):
        assert _flow_checks(tmp_path, NEW_PAGES, {"CartPage": CART_PAGE}, follow=False) == []

    def test_library_types_and_classes_without_a_constructor_add_no_holes(self, tmp_path):
        _write(tmp_path, "automation.shop", "ShopTest", NEW_PAGES)
        _write(tmp_path, "automation.shop", "CartPage", CART_PAGE)
        _write(tmp_path, "automation.shop", "Helper", "    int x;\n")
        fps = ag.fingerprints("ShopTest", "flow", _index(tmp_path), follow_constructors=True)
        assert not [u for u in fps["unresolved"] if "new " in u]

    def test_an_ambiguous_class_name_is_reported_not_guessed(self, tmp_path):
        _write(tmp_path, "automation.shop", "ShopTest",
               "    public void flow() {\n        LoginPage login = new LoginPage(config);\n    }\n")
        ctor = "    public LoginPage(Config config) {\n        assertPageLoaded(user);\n    }\n"
        _write(tmp_path, "automation.github", "LoginPage", ctor)
        _write(tmp_path, "automation.shop.web", "LoginPage", ctor)
        fps = ag.fingerprints("ShopTest", "flow", _index(tmp_path), follow_constructors=True)
        assert fps["asserts"] == {}
        assert any("new LoginPage() (ambiguous class name)" in u for u in fps["unresolved"])

    def test_via_names_the_call_in_the_test_that_reaches_a_helper_check(self, tmp_path):
        fps = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        assert {info["via"] for info in fps["asserts"].values()} == {"helper.completeCheckout()"}


class TestSameNamedClasses:
    """GitHub's LoginPage and SauceDemo's: resolved as Java does, by import and
    then package, never by whichever the index happened to keep."""

    GITHUB, SHOP = "automation.github.web.LoginPage", "automation.shop.web.LoginPage"

    def _repo(self, root, test_body, imports):
        ctor = "    public LoginPage(Config config) {\n        assertPageLoaded(user);\n    }\n"
        _write(root, "automation.github.web", "LoginPage", ctor
               + "    public void open() {\n        this.check();\n    }\n"
               + '    public void check() {\n        assertTrue(config, gh(), "GitHub login");\n    }\n')
        _write(root, "automation.shop.web", "LoginPage", ctor
               + '    public void check() {\n        assertTrue(config, shop(), "Shop login");\n    }\n')
        _write(root, "automation.shop.web", "ProductsPage",
               "    public LoginPage logout() {\n        return new LoginPage(config);\n    }\n")
        _write(root, "automation.flows", "FlowTest", test_body, imports)
        return _index(root)

    def _checks(self, index, test="FlowTest"):
        fps = ag.fingerprints(test, "flow", index, follow_constructors=True)
        return fps, ag.merge({"t": fps})["checks"]

    def test_a_local_of_an_imported_type_is_measured_against_that_class(self, tmp_path):
        index = self._repo(tmp_path, "    public void flow() {\n        LoginPage login = null;\n"
                                     "        login.check();\n    }\n", [self.SHOP])
        _, checks = self._checks(index)
        assert [(c["message"], c["owner"]) for c in checks] == [("Shop login", self.SHOP)]

    def test_this_inside_one_of_them_stays_in_it(self, tmp_path):
        index = self._repo(tmp_path, "    public void flow() {\n        LoginPage login = null;\n"
                                     "        login.open();\n    }\n", [self.GITHUB])
        _, checks = self._checks(index)
        assert [c["message"] for c in checks] == ["GitHub login"]

    def test_a_constructor_is_resolved_by_the_callers_package(self, tmp_path):
        index = self._repo(tmp_path, "    ProductsPage products;\n    public void flow() {\n"
                                     "        products.logout();\n    }\n",
                           ["automation.shop.web.ProductsPage"])
        fps, checks = self._checks(index)
        assert [(c["site"], c["owner"]) for c in checks] == [("LoginPage#LoginPage", self.SHOP)]
        assert not any("ambiguous" in u for u in fps["unresolved"])

    def test_neither_import_nor_package_is_reported_not_guessed(self, tmp_path):
        index = self._repo(tmp_path, "    public void flow() {\n        LoginPage login = null;\n"
                                     "        login.check();\n    }\n", [])
        fps, checks = self._checks(index)
        assert checks == []
        assert any("login.check() (ambiguous class name)" in u for u in fps["unresolved"])

    def test_a_full_test_name_is_accepted(self, tmp_path):
        index = self._repo(tmp_path, "    public void flow() {\n        LoginPage login = null;\n"
                                     "        login.check();\n    }\n", [self.SHOP])
        _, checks = self._checks(index, "automation.flows.FlowTest")
        assert [c["message"] for c in checks] == ["Shop login"]

    def test_the_same_check_in_both_stays_two_checks(self, tmp_path):
        self._repo(tmp_path, "", [])
        _write(tmp_path, "automation.github.web", "HomePage",
               "    public LoginPage signIn() {\n        return new LoginPage(config);\n    }\n")
        _write(tmp_path, "automation.flows", "BothTest",
               "    ProductsPage products;\n    HomePage home;\n    public void flow() {\n"
               "        products.logout();\n        home.signIn();\n    }\n",
               ["automation.shop.web.ProductsPage", "automation.github.web.HomePage"])
        index = _index(tmp_path)
        fps = ag.fingerprints("BothTest", "flow", index, follow_constructors=True)
        checks = ag.merge({"t": fps})["checks"]
        assert sorted(c["owner"] for c in checks) == [self.GITHUB, self.SHOP]
        assert len({c["id"] for c in checks}) == 2
        assert ag.delta(checks, checks[:1])["removed"] == checks[1:], (
            "dropping one page's check must not read as the other's")
