"""Tests for shared/intent.py.

The property that matters most here is not what a contract contains but *when*
it is computed. A contract derived after an edit describes the edited code, so a
conservation guard comparing against it approves whatever the edit did.
"""

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import assertion_graph as ag
from shared import code_analyzer
from shared import blast_radius as br
from shared import intent


def _write(root: Path, package: str, name: str, body: str):
    path = root / "src" / "main" / "java" / Path(*package.split(".")) / f"{name}.java"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"package {package};\n\npublic class {name} {{\n{body}\n}}\n")


STRONG = """\
    public void placeOrder() {
        logStep(testConfig, "Place an order and verify the confirmation number");
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
    }
"""

WEAK = """\
    public void placeOrder() {
        logStep(testConfig, "Place an order");
    }
"""


@pytest.fixture
def repo(tmp_path):
    _write(tmp_path, "automation.checkout", "CheckoutTest", STRONG)
    br._cache.clear()
    return tmp_path


class TestDerive:
    def test_logsteps_become_the_prose_of_the_contract(self, repo):
        contract = intent.derive(repo, "automation.checkout.CheckoutTest#placeOrder")
        assert contract["proves"] == [
            "Place an order and verify the confirmation number"]
        assert contract["source"] == "derived"

    def test_assertions_become_invariants(self, repo):
        contract = intent.derive(repo, "automation.checkout.CheckoutTest#placeOrder")
        assert contract["invariants"], "an assertion must become a must_remain invariant"
        assert all(i["must_remain"] for i in contract["invariants"])

    def test_never_list_is_always_present(self, repo):
        contract = intent.derive(repo, "automation.checkout.CheckoutTest#placeOrder")
        assert any("weaken" in rule for rule in contract["never"])


class TestAuthored:
    def test_authored_contract_wins_but_keeps_measured_fingerprints(self, repo):
        test = "automation.checkout.CheckoutTest#placeOrder"
        path = intent.path_for(repo, test)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"test": test, "proves": ["hand written"]}))
        contract = intent.for_test(repo, test)
        assert contract["source"] == "authored"
        assert contract["proves"] == ["hand written"]
        assert contract["_fingerprints"]["asserts"], (
            "prose is what a human wrote; fingerprints are what the guards "
            "compare — an authored contract needs both")

    def test_missing_contract_never_blocks(self, repo):
        contract = intent.for_test(repo, "automation.checkout.CheckoutTest#placeOrder")
        assert contract is not None and contract["source"] == "derived"


class TestFreeze:
    def test_frozen_contract_is_json_serialisable(self, repo):
        test = "automation.checkout.CheckoutTest#placeOrder"
        frozen = intent.freeze({test: intent.for_test(repo, test)})
        json.dumps(frozen)                       # must not raise
        assert "_fingerprints" not in frozen[test]
        assert frozen[test]["_asserts"]

    def test_frozen_before_edit_still_catches_a_deletion_after_it(self, repo):
        """The reason freezing exists, stated as a test.

        Deriving the contract again after the edit would produce one that expects
        nothing, and conservation would pass.
        """
        test = "automation.checkout.CheckoutTest#placeOrder"
        frozen = intent.freeze({test: intent.for_test(repo, test)})

        _write(repo, "automation.checkout", "CheckoutTest", WEAK)   # assertion deleted
        # Both caches, not just one: code_analyzer memoises file contents for the
        # duration of a run, so the adapt step must invalidate after it writes or
        # it will re-measure the file it edited and see the old text.
        br._cache.clear()
        code_analyzer.reset_caches()
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                ag.member_index(str(repo)))

        report = ag.conserved(intent.thaw(frozen[test]), after)
        assert report["ok"] is False and report["lost"]

        br._cache.clear()
        code_analyzer.reset_caches()
        re_derived = intent.derive(repo, test)
        assert not re_derived["invariants"], (
            "re-deriving after the edit yields a contract expecting nothing — "
            "which is exactly how a guard ends up approving its own violation")


class TestVerifies:
    """The assertion messages, pulled out for a human to read.

    This feeds the adaptation panel's "how this test works today" pane, so the
    bar is legibility: an expected *value* is not a sentence and does not belong
    in a bulleted list under "Verifies".
    """

    def test_the_message_argument_is_kept_without_its_quotes(self):
        contract = {"invariants": [
            {"literals": ['"1"', '"Cart badge should show 1 after adding a product"']}]}
        assert intent.verifies(contract) == [
            "Cart badge should show 1 after adding a product"]

    def test_a_bare_expected_value_is_not_a_message(self):
        assert intent.verifies({"invariants": [{"literals": ['"Products"']}]}) == []

    def test_an_assertion_with_no_literals_is_skipped(self):
        assert intent.verifies({"invariants": [{"literals": []}, {}]}) == []

    def test_the_same_message_twice_is_listed_once(self):
        contract = {"invariants": [
            {"literals": ['"a b"']}, {"literals": ['"a b"']}, {"literals": ['"c d"']}]}
        assert intent.verifies(contract) == ["a b", "c d"]

    def test_a_contract_with_no_invariants_yields_nothing(self):
        assert intent.verifies({}) == []

    def test_it_reads_a_real_derived_contract(self, repo):
        contract = intent.derive(repo, "automation.checkout.CheckoutTest#placeOrder")
        # STRONG asserts `assertEquals(testConfig, "Order total", total, "42.00")`
        # — the last literal is the value, so there is no sentence to show.
        assert intent.verifies(contract) == []
