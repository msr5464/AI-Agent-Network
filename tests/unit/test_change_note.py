"""Tests for parsing a change note (step 01).

The header grammar is deliberately parsed in Python rather than by the model:
`Module:` and `Affects:` are unambiguous, and handing them to a model only adds
the chance of getting them wrong. These cover that half, plus the two decisions
that gate everything downstream — which kinds may never be applied, and whether
the flow ends in something that cannot be undone.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    """Load the action by path — it is a script, not an importable module."""
    path = ROOT / "agents" / "test-adaptation-agent" / "actions" / "01_parse_change.py"
    import os
    os.environ.setdefault("AUDIT_DIR", "/tmp")
    os.environ.setdefault("INPUT_FILE", str(path))
    spec = importlib.util.spec_from_file_location("parse_change", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pc = _load()

NOTE = """Module: checkout
Type: web
URL: https://app.staging.example.com
Affects: automation.checkout.*, automation.cart.*

What changed:
1. After login a "Choose workspace" screen now appears before the dashboard.
2. The 3-step checkout wizard is now 2 steps —
   shipping and payment share a page.
3. "Place Order" was renamed to "Confirm & Pay".

Expected outcome unchanged: an order is placed and a confirmation number is shown.
"""


class TestHeaders:
    def test_headers_are_parsed_without_a_model(self):
        headers = pc.parse_headers(NOTE)
        assert headers["module"] == "checkout"
        assert headers["type"] == "web"
        assert headers["url"] == "https://app.staging.example.com"

    def test_affects_supports_several_globs(self):
        headers = pc.parse_headers(NOTE)
        globs = [a.strip() for a in headers["affects"].split(",")]
        assert globs == ["automation.checkout.*", "automation.cart.*"]

    def test_missing_affects_is_not_an_error(self):
        headers = pc.parse_headers("Module: checkout\n\n1. Something changed.\n")
        assert headers.get("affects", "") == "", (
            "a human will omit it; scope falls back to Module: and says so")


class TestItems:
    def test_numbered_items_keep_their_order(self):
        items = pc.parse_items(NOTE)
        assert [i["index"] for i in items] == [1, 2, 3]

    def test_a_wrapped_item_is_joined(self):
        items = pc.parse_items(NOTE)
        assert "shipping and payment share a page" in items[1]["text"]

    def test_headers_are_not_swallowed_as_items(self):
        assert all("Module" not in i["text"] for i in pc.parse_items(NOTE))

    def test_a_comment_after_the_last_item_is_not_part_of_it(self):
        """`adaptation_handoff` drafts end with an "observed by" footer.

        It sits after the last numbered item, so the continuation rule used to
        append it to that item's prose — and the classifier then read a healing
        agent timestamp as part of the product change.
        """
        note = (
            "Module: checkout\n\n"
            "What changed:\n"
            "1. The page was rebuilt.\n\n"
            "# Observed by automation.checkout.CheckoutTest#placeOrder on\n"
            "# 2026-08-27T08:12:28Z\n"
        )
        items = pc.parse_items(note)
        assert len(items) == 1
        assert items[0]["text"] == "The page was rebuilt."

    def test_a_leading_banner_is_not_an_item(self):
        note = (
            "# DRAFT — written by test-healing-agent, not yet reviewed.\n"
            "# Confirm this was an intended redesign.\n\n"
            "Module: checkout\n\n"
            "1. The page was rebuilt.\n"
        )
        assert [i["text"] for i in pc.parse_items(note)] == ["The page was rebuilt."]


class TestOutcome:
    def test_expected_outcome_is_extracted(self):
        assert "confirmation number" in pc.expected_outcome(NOTE)

    def test_placing_an_order_is_recognised_as_destructive(self):
        assert pc.looks_destructive(pc.expected_outcome(NOTE)) == "place order", (
            "the note says 'an order is placed'. Phrase matching against the "
            "token 'place order' missed that on word order alone, which would "
            "have let exploration walk straight through a real checkout")

    def test_a_read_only_outcome_is_not_destructive(self):
        assert pc.looks_destructive("the dashboard shows the latest balance") == ""

    WRAPPED = ("Module: SauceDemo\n\nWhat changed:\n1. Remove now saves the item.\n\n"
               "Expected outcome changed: after removing a product it is still\n"
               "listed under \"Saved for later\".\n\n# observed by a human\n")

    def test_a_wrapped_outcome_is_one_outcome(self):
        assert pc.expected_outcome(self.WRAPPED) == (
            'after removing a product it is still listed under "Saved for later".')

    def test_a_wrapped_outcome_is_not_part_of_the_last_item(self):
        assert [i["text"] for i in pc.parse_items(self.WRAPPED)] == [
            "Remove now saves the item."], (
            "the second line of the outcome used to be appended to item 1 and "
            "classified as part of the change")

    def test_item_prose_that_mentions_an_outcome_is_not_the_outcome(self):
        note = "1. The expected outcome of checkout: unchanged screens.\n"
        assert pc.expected_outcome(note) == ""


class TestKinds:
    def test_the_kind_vocabulary_is_closed(self):
        assert "outcome_changed" in pc.KINDS and "step_insert" in pc.KINDS

    def test_only_an_unclassified_item_escalates(self):
        assert not hasattr(pc, "ESCALATE_ONLY"), (
            "outcome_changed and content_changed are actionable now — they may "
            "change a check, but only one the agent declares")
        assert pc.UNCLASSIFIED not in pc.KINDS, (
            "the classifier is never offered 'unclassified'; it is what an item "
            "becomes when the classifier could not place it")

    def test_a_change_to_the_test_itself_has_a_kind(self):
        assert "coverage_changed" in pc.KINDS and "coverage_added" in pc.KINDS
        prompt = pc.classify_prompt("saucedemo", [{"index": 1, "text": "drop step 7"}], "")
        assert "coverage_changed" in prompt

    def test_added_coverage_is_still_not_an_outcome_change(self):
        prompt = pc.classify_prompt("saucedemo", [{"index": 1, "text": "also verify the price"}], "")
        assert "is `coverage_added`, not this" in prompt
