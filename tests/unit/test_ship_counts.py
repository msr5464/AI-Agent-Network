"""What a healing PR claims it fixed.

The bug this pins: one locator edit greens every test that walked past it — that
is the entire point of clustering — so `fixes` holds one entry per EDIT, with the
tests it repaired in `test_names`. The ship step counted the entries. A run that
turned five tests green raised a PR titled "Fixed 2/2", a summary table reading
2, and a Slack message to match, while 01-fix.md next to it correctly said 5.
The clustering was working; only the reporting hid it.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "02_ship.py"
    spec = importlib.util.spec_from_file_location("healing_ship", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_ship"] = mod
    # Set only for the import (the step reads them into module constants); left in
    # os.environ they leaked into every later test in the session.
    with mock.patch.dict(os.environ, {"AUDIT_DIR": tempfile.mkdtemp(prefix="ship-counts-")}):
        spec.loader.exec_module(mod)
    return mod


ship = _load()

# The real shape of session 20260923-123904: two edits, five tests.
FIXES = [
    {"target_file": "/ws/ProductsPage.java", "fix_description": "cart link",
     "test_name": "automation.saucedemo.SauceDemoWebTest.simulateWebLifecycle",
     "test_names": ["automation.saucedemo.SauceDemoWebTest.simulateWebLifecycle",
                    "automation.saucedemo.SauceDemoWebTest.verifyProductAppearsInCart"]},
    {"target_file": "/ws/LoginPage.java", "fix_description": "login button",
     "test_name": "automation.saucedemo.SauceDemoWebTest.addProductToCart",
     "test_names": ["automation.saucedemo.SauceDemoWebTest.addProductToCart",
                    "automation.saucedemo.SauceDemoWebTest.loginAndVerifyProductsPage",
                    "automation.saucedemo.SauceDemoWebTest.simulateHybridApiAndWebLifecycle"]},
]


class TestTestCount:
    def test_one_edit_that_greened_three_tests_counts_as_three(self):
        assert ship.test_count(FIXES) == 5

    def test_a_failure_is_always_one_test(self):
        assert ship.test_count([{"test_name": "pkg.T.one"},
                                {"test_name": "pkg.T.two"}]) == 2

    def test_an_entry_without_the_list_falls_back_to_its_single_name(self):
        """Older 01-fix.json files predate test_names."""
        assert ship.test_count([{"test_name": "pkg.T.only"}]) == 1

    def test_nothing_is_zero(self):
        assert ship.test_count([]) == 0


class TestTestsIn:
    def test_every_test_an_edit_repaired_is_named(self):
        assert ship.tests_in(FIXES[1]) == [
            "automation.saucedemo.SauceDemoWebTest.addProductToCart",
            "automation.saucedemo.SauceDemoWebTest.loginAndVerifyProductsPage",
            "automation.saucedemo.SauceDemoWebTest.simulateHybridApiAndWebLifecycle"]

    def test_the_named_test_leads(self):
        """The PR headline uses the first, so it must stay the entry's own."""
        assert ship.tests_in(FIXES[0])[0] == FIXES[0]["test_name"]

    def test_an_entry_with_no_list_still_yields_its_test(self):
        assert ship.tests_in({"test_name": "pkg.T.only"}) == ["pkg.T.only"]


# One test, three edits: two kept by earlier attempts, the third greened it.
CHAIN_FIX = {
    "target_file": "/ws/BankPage.java", "fix_description": "the bank code field",
    "test_name": "automation.shop.ShopWebTest.pays",
    "earlier_edits": [
        {"attempt": 1, "target_file": "/ws/CheckoutPage.java",
         "fix_description": "the order total", "fix_diff": "-a\n+b"},
        {"attempt": 3, "target_file": "/ws/CardFormPage.java",
         "fix_description": "the card code field", "fix_diff": "-c\n+d"},
    ],
}


class TestEditsKeptByEarlierAttempts:
    """A chain of broken locators takes several edits to green one test.

    The record of the fixed test holds the last of them. The report listed that
    one, so a PR whose diff changed three page objects described a single edit.
    """

    def test_every_edit_behind_a_fix_is_counted(self):
        assert ship.edit_count([CHAIN_FIX]) == 3
        assert ship.edit_count(FIXES) == 2

    def test_each_kept_edit_gets_a_line_naming_its_file_and_attempt(self):
        lines = ship.earlier_edit_lines(CHAIN_FIX)
        assert len(lines) == 2
        assert "kept from attempt 1: the order total (`CheckoutPage.java`)" in lines[0]
        assert "kept from attempt 3: the card code field (`CardFormPage.java`)" in lines[1]

    def test_a_fix_with_none_adds_nothing(self):
        assert ship.earlier_edit_lines(FIXES[0]) == []

    def test_the_slack_message_says_how_many_more(self):
        _channel, text = ship._build_slack_message(
            "local-ShopWebTest", [CHAIN_FIX], [], [], None, "true", 4)
        assert "+2 edit(s) kept from earlier attempts" in text
