"""How a page renders a value a test expected — shared/value_match.py.

The pairs are the real ones from session 20260927-144044-create-payment-gateway-
checkout: a one-word name the demo checkout rendered with a default last name, a
phone number normalised to +62, a cart total of 20,000 shown as Rp20.000, a bank
page's 19000.00 against the payment page's Rp19.000 — and the invented
Rp 490.909 that none of them should ever be mistaken for.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import value_match as vm  # noqa: E402
from shared.frameworks.base import DiagnosticEngine  # noqa: E402

FAIL_LINE = ("java.lang.AssertionError: ✘ FAIL: Customer name in order details overlay "
             "should match the name entered in the checkout form | Expected: 'User_orrju' "
             "| Actual: 'User_orrju sample_last_name'")
NAME_MESSAGE = ("Customer name in order details overlay should match the name entered "
                "in the checkout form")


class _Diagnostics(DiagnosticEngine):
    """Only the concrete default under test; the abstract ones are not."""
    def is_ambiguous_locator(self, error_message):
        return False

    def is_locator_resolution_failure(self, error_message):
        return False


PARSE = _Diagnostics().value_mismatch


class TestRelation:
    @pytest.mark.parametrize("expected, actual, want", [
        ("Test User", "Test User", "equal"),
        ("PENDING", "Pending", "formatting"),
        ("$ 8.99", "$8.99", "formatting"),
        ("20,000", "Rp20.000", "numeric"),
        ("19000.00", "Rp19.000", "numeric"),
        ("Rp19.000", "19000.00", "numeric"),
        ("Total: 20,000", "Total: 20.000", "numeric"),
        ("081234567890", "+6281234567890", "phone"),
        ("0812-3456-7890", "081234567890", "phone"),
        ("User_orrju", "User_orrju sample_last_name", "words"),
        ("Thank you for your purchase.",
         "✓ Thank you for your purchase. Get a nice sleep.", "words"),
    ])
    def test_the_tightest_relation_that_holds(self, expected, actual, want):
        assert vm.relation(expected, actual) == want

    @pytest.mark.parametrize("expected, actual", [
        ("Rp 490.909", "Rp20.000"),     # the invented amount: a different number
        ("Pending", "Shipped"),
        ("John", "Johnson"),            # not a whole word
        ("5 items", "5 orders"),        # one number, two different claims
        ("1234567", "991234567"),       # extra leading digits, no + — another id
        ("-5", "5"),
    ])
    def test_a_different_value_has_no_relation(self, expected, actual):
        assert vm.relation(expected, actual) is None

    def test_equal_is_the_only_relation_a_sanction_never_needs(self):
        assert "equal" not in vm.SANCTIONABLE
        assert vm.SANCTIONABLE == set(vm.RELATIONS) - {"equal"}
        assert set(vm.ASSERT_WITH) == set(vm.MEANING) == set(vm.RELATIONS)


class TestParseValueCheck:
    def test_the_relation_is_computed_not_taken_from_the_model(self):
        check = vm.parse_value_check(
            "Validate the customer name|customerNameLabel|Test User sample_last_name|"
            "input:nameField|Test User")
        assert check == {"check": "Validate the customer name",
                         "element": "customerNameLabel",
                         "rendered": "Test User sample_last_name",
                         "source": "input:nameField", "expected": "Test User",
                         "relation": "words"}

    def test_quotes_and_source_case_are_normalised(self):
        check = vm.parse_value_check(
            "Amount same as earlier|PaymentPage.amountDisplay|'Rp20.000'|Element: cartTotal|20,000")
        assert (check["rendered"], check["source"], check["relation"]) == (
            "Rp20.000", "element:cartTotal", "numeric")

    def test_a_pipe_inside_the_rendered_text_does_not_shift_the_fields(self):
        check = vm.parse_value_check("c1|el|Order | Paid|literal|Paid")
        assert (check["rendered"], check["source"], check["expected"], check["relation"]) == (
            "Order | Paid", "literal", "Paid", "words")

    def test_no_relation_is_recorded_as_empty(self):
        assert vm.parse_value_check("c|el|Rp20.000|literal|Rp 490.909")["relation"] == ""

    def test_a_short_line_is_not_a_check(self):
        assert vm.parse_value_check("too|short") is None


class TestValueMismatch:
    def test_the_repo_assertion_line(self):
        assert PARSE("[15:11:57] " + FAIL_LINE) == ("User_orrju", "User_orrju sample_last_name")

    def test_the_int_form_and_testng(self):
        assert PARSE("✘ FAIL: count | Expected: 3 | Actual: 4") == ("3", "4")
        assert PARSE("AssertionError: title expected [Products] but found [Items]") == (
            "Products", "Items")

    def test_a_locator_failure_is_not_one(self):
        assert PARSE("Failed to load Element Locator@#snap-midtrans >> .header-amount") is None


class TestTriage:
    def test_pins_the_failure_on_its_one_assertion(self):
        hit = vm.triage("noise\n" + FAIL_LINE + "\nmore", [NAME_MESSAGE, "phone should match"],
                        PARSE)
        assert hit == {"message": NAME_MESSAGE, "expected": "User_orrju",
                       "actual": "User_orrju sample_last_name", "relation": "words"}

    def test_a_different_value_is_a_finding_not_a_triage(self):
        line = "✘ FAIL: amount | Expected: 'Rp 490.909' | Actual: 'Rp20.000'"
        assert vm.triage(line, ["amount"], PARSE) is None

    def test_two_assertions_with_that_message_cannot_be_told_apart(self):
        assert vm.triage(FAIL_LINE, [NAME_MESSAGE, NAME_MESSAGE], PARSE) is None

    def test_no_assertion_with_that_message(self):
        assert vm.triage(FAIL_LINE, ["something else"], PARSE) is None


class TestAppearsIn:
    def test_text_and_amounts(self):
        assert vm.appears_in("Thank you for your purchase.", "…Thank you for your purchase. Get…")
        assert vm.appears_in("20,000", "Validate amount — Rp20.000 → Rp19.000")
        assert not vm.appears_in("Rp 490.909", "Rp20.000 then Rp19.000 and 19000.00")
