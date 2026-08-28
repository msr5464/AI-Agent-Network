"""Tests for shared/flow_map.py.

The flow map is the evidence every later guard rests on, so the properties that
matter are about honesty rather than richness: an unverifiable selector must not
look verified, a malformed line must not destroy the steps around it, and a
destructive action that succeeded must be visible as a violation.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import flow_map as fm


def _step(index, page, verb="click", name="submitButton", selector="#submit",
          outcome="ok"):
    return json.dumps({
        "index": index, "page": page,
        "action": {"verb": verb,
                   "target": {"name": name, "selector": selector,
                              "accessible_name": name}},
        "result": {"outcome": outcome},
    })


INVENTORY = json.dumps([
    {"tag": "h1", "text": "Choose a workspace"},
    {"tag": "button", "id": "submit", "class": "btn primary", "text": "Continue"},
    {"tag": "button", "id": "cancel", "class": "btn", "text": "Cancel"},
])

STDOUT = f"""\
PAGE_ENTER: workspace|https://app.example.com/workspaces|Choose workspace
PAGE_STATE: workspace|https://app.example.com/workspaces|{INVENTORY}
FLOW_STEP: {_step(0, "workspace")}
some prose the model emitted that is not a marker
FLOW_STEP: {{not valid json
FLOW_STEP: {_step(1, "workspace", name="cancelButton", selector=".btn")}
"""


class TestParsing:
    def test_markers_become_ordered_steps(self):
        flow = fm.build(STDOUT)
        assert [s["index"] for s in flow["steps"]] == [0, 1]

    def test_a_malformed_line_does_not_lose_the_others(self):
        flow = fm.build(STDOUT)
        assert len(flow["steps"]) == 2, (
            "emit-as-you-go exists so a killed run keeps a valid prefix; one bad "
            "line must not cost the steps around it")
        assert any(n["kind"] == "unparsed" for n in flow["notes"])

    def test_page_identity_is_attached(self):
        flow = fm.build(STDOUT)
        page = flow["steps"][0]["page"]
        assert page["identity_digest"].startswith("sha1:")
        assert page["title"] == "Choose workspace"
        assert page["headings"] == ["Choose a workspace"]

    def test_same_page_twice_has_one_digest(self):
        flow = fm.build(STDOUT)
        digests = {s["page"]["identity_digest"] for s in flow["steps"]}
        assert len(digests) == 1, (
            "'the 3-step wizard is now 2 steps' is only mechanically visible if "
            "one page yields one digest however many steps happen on it")


class TestSelectorVerification:
    def test_unique_selector_is_measured_not_claimed(self):
        flow = fm.build(STDOUT)
        check = flow["steps"][0]["selector_check"]
        assert check["match_count"] == 1
        assert check["unique"] is True
        assert check["counted_against"] == "page_inventory"

    def test_ambiguous_selector_is_rejected(self):
        flow = fm.build(STDOUT)
        check = flow["steps"][1]["selector_check"]
        assert check["match_count"] == 2 and check["unique"] is False

    def test_unevaluable_selector_is_none_not_zero(self):
        stdout = (f"PAGE_ENTER: p|https://x/|T\nPAGE_STATE: p|https://x/|{INVENTORY}\n"
                  + "FLOW_STEP: " + _step(0, "p", selector="xpath=//button[1]") + "\n")
        check = fm.build(stdout)["steps"][0]["selector_check"]
        assert check["match_count"] is None and check["unique"] is None, (
            "'I could not check' is not 'it matches nothing' — collapsing them "
            "records a guess as a verified absence")

    def test_model_claim_is_kept_but_never_used(self):
        stdout = (f"PAGE_ENTER: p|https://x/|T\nPAGE_STATE: p|https://x/|{INVENTORY}\n"
                  'SELECTOR_COUNT: p|.btn|1\n'
                  + "FLOW_STEP: " + _step(0, "p", selector=".btn") + "\n")
        flow = fm.build(stdout)
        assert flow["steps"][0]["selector_check"]["match_count"] == 2
        assert any(n["kind"] == "claimed_count" for n in flow["notes"])


class TestCountInInventory:
    ELEMENTS = [
        {"tag": "button", "id": "submit", "class": "btn primary"},
        {"tag": "a", "class": "btn", "attributes": {"data-cy": "go"}},
    ]

    @pytest.mark.parametrize("selector,expected", [
        ("#submit", 1), ("button", 1), (".btn", 2), ("button.btn.primary", 1),
        ("[data-cy='go']", 1), ("#nope", 0), ("//div", None),
    ])
    def test_counts(self, selector, expected):
        assert fm.count_in_inventory(selector, self.ELEMENTS) == expected


class TestDestructive:
    def test_refused_destructive_action_is_a_normal_outcome(self):
        stdout = (f"PAGE_ENTER: p|https://x/|T\nPAGE_STATE: p|https://x/|{INVENTORY}\n"
                  + "FLOW_STEP: " + _step(0, "p", name="Place Order",
                                          outcome="refused") + "\n"
                  "REFUSED: 0|Place Order|destructive_verb\n")
        flow = fm.build(stdout)
        assert flow["violations"] == []
        assert flow["refusals"][0]["target"] == "Place Order"

    def test_destructive_action_that_succeeded_is_a_violation(self):
        stdout = (f"PAGE_ENTER: p|https://x/|T\nPAGE_STATE: p|https://x/|{INVENTORY}\n"
                  + "FLOW_STEP: " + _step(0, "p", name="Place Order",
                                          outcome="ok") + "\n")
        flow = fm.build(stdout)
        assert flow["violations"], (
            "refusing is fine; quietly complying is what we must be able to see")
        assert flow["status"] == "unsafe"
        assert "cannot be treated as side-effect free" in fm.describe(flow)


class TestScoreAndValidate:
    def test_clean_run_beats_a_crash_with_fewer_failures(self):
        rich = fm.build(STDOUT)
        thin = fm.build("PAGE_ENTER: p|https://x/|T\n")
        assert fm.score(rich, "ok") > fm.score(thin, "timeout")

    def test_validate_flags_a_step_without_identity(self):
        flow = {"steps": [{"index": 0, "action": {"verb": "click"}, "page": {}}]}
        ok, problems = fm.validate(flow)
        assert ok is False and any("identity" in p for p in problems)


class TestDiff:
    def test_unmatched_observed_step_is_reported_as_added(self):
        flow = fm.build(STDOUT)
        diff = fm.diff_against_test(flow, [{"index": 0, "target": "submitButton"}])
        assert flow["steps"][0]["maps_to_test"]["kind"] == "existing"
        assert any(a["flow_index"] == 1 for a in diff["added"])


class TestDestructiveMatching:
    """What counts as an action that cannot be undone.

    This is a safety guard, so the asymmetry is deliberate and stated in the
    module: a false refusal costs one escalation, a false permission costs real
    data. The cases below are the ones that actually came up.
    """

    @pytest.mark.parametrize("text,expected", [
        # The example change note's own wording. Matching the phrase "place
        # order" as a substring missed this entirely.
        ("an order is placed and a confirmation number is shown", "place order"),
        ("Place Order", "place order"),
        ("Confirm & Pay", "confirm & pay"),
        ("Delete this record", "delete"),
        ("cancel the subscription", "cancel subscription"),
        ("submit the payment now", "submit payment"),
    ])
    def test_destructive_text_is_caught(self, text, expected):
        assert fm.destructive_token(text) == expected

    @pytest.mark.parametrize("text", [
        "the dashboard shows the latest balance",
        "in order to continue, click next",
        # "payload" contains "pay". Substring matching made every page with a
        # payload on it un-explorable.
        "the payload is parsed",
        "Save the profile",
        "Add to cart",
        "verify the products page loads",
    ])
    def test_ordinary_text_is_not_destructive(self, text):
        assert fm.destructive_token(text) == ""


class TestMeasuredPageObjects:
    """Guess → measure → edit.

    Step 02 nominates page objects by name similarity. That guess is cheap and
    often right, and it is still a guess: two modules can both have a
    `LoginPage`, and a renamed page object stops matching its own name long
    before it stops being the right file. Once exploration has reported what is
    actually on each page, the mapping can be measured.
    """

    INVENTORY = [
        {"tag": "span", "class": "title", "text": "Products"},
        {"tag": "a", "class": "shopping_cart_link"},
        {"tag": "button", "id": "react-burger-menu-btn"},
    ]
    PRODUCTS = {"path": "web/ProductsPage.java", "snippet":
                'a = page.locator(".title"); b = page.locator(".shopping_cart_link");'
                ' c = page.locator("#react-burger-menu-btn");'}
    LOGIN = {"path": "web/LoginPage.java", "snippet":
             'u = page.locator("#user-name"); p = page.locator("#password");'}

    def _flow(self, inventory=None):
        return {"pages": {"products": {"id": "products"}},
                "_inventories": {"products": inventory if inventory is not None
                                 else self.INVENTORY},
                "steps": [{"index": 0, "page": {"id": "products"}}]}

    def test_the_right_page_object_is_measured_not_guessed(self):
        flow = fm.measure_page_objects(self._flow(), [self.LOGIN, self.PRODUCTS])
        best = flow["pages"]["products"]["best_page_object"]
        assert best["name"] == "ProductsPage" and best["matched"] == 3, (
            "LoginPage was offered first; only measurement can rule it out")

    def test_the_measurement_reaches_each_step(self):
        flow = fm.measure_page_objects(self._flow(), [self.PRODUCTS])
        assert flow["steps"][0]["page"]["best_page_object"]["name"] == "ProductsPage", (
            "step 04 should not have to join two structures to know which page "
            "object a step happened on")

    def test_an_empty_inventory_yields_no_match_rather_than_a_wrong_one(self):
        flow = fm.measure_page_objects(self._flow(inventory=[]),
                                       [self.LOGIN, self.PRODUCTS])
        assert flow["pages"]["products"]["best_page_object"] is None

    def test_nothing_matching_is_reported_as_nothing_not_as_the_first_candidate(self):
        unrelated = {"path": "web/CartPage.java",
                     "snippet": 'x = page.locator("#nothing-here");'}
        flow = fm.measure_page_objects(self._flow(), [unrelated])
        assert flow["pages"]["products"]["best_page_object"] is None, (
            "ranking alone would hand back whichever candidate sorted first")

    def test_every_report_is_marked_sampled(self):
        """An inventory is a bounded sample, so a zero here is 'not observed',
        not 'absent' — and the call site must not have to remember that."""
        flow = fm.measure_page_objects(self._flow(), [self.PRODUCTS])
        page = flow["pages"]["products"]
        assert page["measured_sampled"] is True
        assert all(r["sampled"] for r in page["page_objects"])
        assert page["best_page_object"]["sampled"] is True

    def test_describe_says_the_measurement_is_a_sample(self):
        flow = fm.measure_page_objects(self._flow(), [self.PRODUCTS])
        text = fm.describe_page_objects(flow)
        assert "ProductsPage" in text and "bounded sample" in text


class TestDocumentFromInventory:
    def test_attributes_and_text_survive(self):
        doc = fm.document_from_inventory([
            {"tag": "button", "id": "go", "class": "btn primary", "text": "Continue",
             "attributes": {"data-cy": "submit"}}])
        for fragment in ('id="go"', 'class="btn primary"', 'data-cy="submit"',
                         ">Continue<"):
            assert fragment in doc

    def test_hostile_content_cannot_break_the_document(self):
        doc = fm.document_from_inventory([
            {"tag": "div", "text": '<script>x</script>', "id": 'a" onload="y'}])
        assert "<script>" not in doc, "element text must not become markup"
        from shared.page_identity import parse
        assert parse(doc) is not None
