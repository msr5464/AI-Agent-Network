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

    def test_backtick_wrapped_markers_are_parsed(self):
        # Models often render markers as inline code; losing them emptied a whole run.
        flow = fm.build(f"`FLOW_STEP: {_step(0, 'p')}`\n")
        assert [s["index"] for s in flow["steps"]] == [0]

    def test_bold_markdown_markers_are_parsed(self):
        # An observed retry wrote every marker this way and parsed as an empty walk.
        flow = fm.build(f"**PAGE_STATE:** p|https://x/|{INVENTORY}\n"
                        f"**FLOW_STEP:** `{_step(0, 'p')}`\n")
        assert [s["index"] for s in flow["steps"]] == [0]
        assert fm.pages_without_inventory(flow) == []

    def test_pretty_printed_inventory_is_parsed(self):
        # An observed run wrote every PAGE_STATE one element per line; the parser
        # saw only "[" and every selector downstream stayed unverified.
        pretty = json.dumps(json.loads(INVENTORY), indent=2)
        flow = fm.build(f"PAGE_STATE: workspace|https://x/|{pretty}\n"
                        f"FLOW_STEP: {_step(0, 'workspace')}\n")
        assert len(flow["_inventories"]["workspace"]) == 3
        assert flow["steps"][0]["selector_check"]["unique"] is True

    def test_an_unterminated_inventory_does_not_swallow_the_next_marker(self):
        flow = fm.build(f"PAGE_STATE: p|https://x/|[\n  {{\"tag\": \"div\"}},\n"
                        f"FLOW_STEP: {_step(0, 'p')}\n")
        assert [s["index"] for s in flow["steps"]] == [0]

    def test_a_restated_walk_is_not_counted_twice(self):
        flow = fm.build(f"FLOW_STEP: {_step(1, 'p')}\nFLOW_STEP: {_step(2, 'p')}\n"
                        "## Summary\n"
                        f"FLOW_STEP: {_step(1, 'p')}\nFLOW_STEP: {_step(2, 'p')}\n")
        assert [s["index"] for s in flow["steps"]] == [1, 2]

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

    def test_a_dot_inside_an_attribute_value_is_not_a_class(self):
        """SauceDemo's T-shirt button recounted as zero — `.allthethings` read as a
        class — and "Unique? NO" then made the adapt step decline the whole change."""
        button = {"tag": "button", "attributes": {
            "data-test": "add-to-cart-test.allthethings()-t-shirt-(red)"}}
        assert fm.count_in_inventory(
            "[data-test='add-to-cart-test.allthethings()-t-shirt-(red)']", [button]) == 1


class TestStepsWrittenWithoutTheirMarker:
    """A walk put every step in one fenced block at the end, each a complete object
    on its own line and none with `FLOW_STEP:` before it. It had walked the whole
    flow, and read as empty."""

    BLOCK = ("PAGE_ENTER: p|https://x/|T\nHere is the complete flow walk:\n```json\n"
             + _step(1, "p", name="open") + "\n" + _step(2, "p", name="save") + "\n```\n")

    def test_the_steps_are_read(self):
        flow = fm.build(self.BLOCK)
        assert [s["action"]["target"]["name"] for s in flow["steps"]] == ["open", "save"]
        assert flow["status"] == "ok"

    def test_a_marked_step_is_never_replaced_by_a_recap_of_it(self):
        marked = "FLOW_STEP: " + _step(1, "p", name="as walked") + "\n"
        flow = fm.build(marked + self.BLOCK)
        assert [s["action"]["target"]["name"] for s in flow["steps"]] == ["as walked", "save"]

    @pytest.mark.parametrize("line", [
        '{"index": 3, "action": {"verb": "click|fill|select"}}',     # the rules' own example
        '{"index": "3", "action": {"verb": "click"}}',               # not a step's index
        '{"index": 3, "note": "no action"}',
        '{"tag": "button", "text": "Save"}',                         # an element, not a step
        '[{"index": 3, "action": {"verb": "click"}}]',
        "{not json",
    ])
    def test_only_an_object_shaped_like_a_step_is_one(self, line):
        assert fm.build("PAGE_ENTER: p|https://x/|T\n" + line + "\n")["steps"] == []


class TestTheBrowsersOwnRecord:
    """The step list is typed by the explorer. The helpers also record every click
    and keystroke as the browser dispatches it, and a walk's step list has left out
    one action, most of them, and all of them."""

    PAGE = "PAGE_ENTER: cart|https://shop.test/|Shop\n"

    @staticmethod
    def _click(sel, text="", uid=None, total=1, visible=1):
        return {"clicked": {"sel": sel, "text": text, "uid": uid, "total": total,
                            "visible": visible}}

    @staticmethod
    def _typed(sel, value, uid=None, password=False):
        return {"typed": {"sel": sel, "value": None if password else value, "uid": uid,
                          "password": password, "total": 1, "visible": 1}}

    @staticmethod
    def _state(page="cart", checks=None):
        return {"page": page, "url": "https://shop.test/", "checks": checks or {},
                "inventory": [{"tag": "h1", "text": page}]}

    def _flow(self, steps, rows, sandbox=False):
        return fm.build(self.PAGE + "".join("FLOW_STEP: " + s + "\n" for s in steps),
                        rows, sandbox=sandbox)

    def test_actions_are_read_in_order_one_per_control_under_the_page_they_led_to(self):
        rows = [self._click("#open", "Open"), self._state("list"),
                self._typed("#name", "A"), self._typed("#name", "Ab"),
                self._click("#save", "Save"), self._state("detail")]
        assert [(a["kind"], a["sel"], a["page"]) for a in fm.recorded_actions(rows)] == [
            ("click", "#open", "list"), ("fill", "#name", "detail"),
            ("click", "#save", "detail")]

    def test_a_fields_late_change_event_is_not_a_second_typing(self):
        # A field reports its change when focus leaves it, steps after the typing.
        rows = [self._typed("#card", "4111"), self._state(),
                self._typed("#card", "4111"),                       # the same value
                self._typed("#pin", "", password=True), self._state(),
                self._typed("#pin", "", password=True),             # never recorded
                self._typed("#card", "4000")]                       # typed again
        assert [a["sel"] for a in fm.recorded_actions(rows)] == ["#card", "#pin", "#card"]

    def test_an_action_no_step_reports_is_listed(self):
        rows = [self._click("#open", "Open"), self._state(),
                self._click("a.tab", "Card"), self._state("card")]
        flow = self._flow([_step(1, "cart", name="open", selector="#open")], rows)
        assert flow["unreported_actions"] == [
            {"kind": "click", "name": "Card", "selector": "a.tab", "page": "card"}]
        assert len(flow["steps"]) == 1, "listed for a reader; never made a step"

    def test_a_step_that_spelled_the_selector_differently_still_accounts_for_it(self):
        # Matched by the element: the helpers counted the step's selector at the
        # same one the click landed on.
        rows = [self._click("a.tab", "Card", uid="e7"),
                self._state(checks={"text=Card": {"total": 1, "visible": 1, "uid": "e7"}})]
        flow = self._flow([_step(1, "cart", name="card", selector="text=Card")], rows)
        assert flow["unreported_actions"] == []

    def test_the_hidden_input_behind_a_label_is_not_a_second_action(self):
        rows = [self._click("label[for='opt']", "No coupon", uid="e1"),
                self._click("#opt", "", uid="e2", visible=0),
                self._typed("#opt", "opt", uid="e2"), self._state()]
        flow = self._flow([], rows)
        assert [a["selector"] for a in flow["unreported_actions"]] == ["label[for='opt']"]

    def test_a_walk_with_no_record_reads_exactly_as_before(self):
        flow = self._flow([_step(1, "cart", name="Checkout")], [])
        assert flow["unreported_actions"] == []
        assert [v["target"] for v in flow["violations"]] == ["Checkout"]
        assert "not reported as a step" not in fm.describe(flow)

    def test_a_click_nobody_reported_is_still_judged(self):
        # A walk clicked through to a completed payment, reported no step, and
        # read as empty rather than unsafe: the check had only the step list.
        rows = [self._click("#buy", "BUY NOW"), self._state(),
                self._click("#pay", "Pay now"), self._state("bank")]
        flow = self._flow([], rows)
        assert [(v["target"], v["token"], v["index"]) for v in flow["violations"]] == [
            ("BUY NOW", "buy", None), ("Pay now", "pay", None)]
        assert flow["status"] == "unsafe"
        assert "BUY NOW (its name contains 'buy') — clicked in the browser, not " \
               "reported as a step" in fm.describe(flow)

    def test_a_step_reported_as_refused_or_only_looked_at_does_not_cover_a_click(self):
        rows = [self._click("#pay", "Pay now"), self._state()]
        for verb, outcome in (("click", "refused"), ("observe", "ok")):
            flow = self._flow([_step(4, "cart", verb=verb, name="Pay now",
                                     selector="#pay", outcome=outcome)], rows)
            assert [(v["index"], v.get("recorded")) for v in flow["violations"]] == [(4, True)]
            assert "the browser recorded a click on it" in fm.describe(flow)

    def test_a_reported_click_is_one_finding_and_so_is_one_made_twice(self):
        rows = [self._click("#buy", "BUY NOW"), self._state(),
                self._click("#pay", "Pay now"), self._state(),
                self._click("#pay", "Pay now"), self._state()]
        flow = self._flow([_step(1, "cart", name="BUY NOW", selector="#buy")], rows)
        assert [(v["target"], v["index"]) for v in flow["violations"]] == [
            ("BUY NOW", 1), ("Pay now", None)]

    def test_under_a_sandbox_assertion_the_record_raises_nothing(self):
        rows = [self._click("#pay", "Pay now"), self._state()]
        flow = self._flow([], rows, sandbox=True)
        assert flow["violations"] == []
        assert [a["name"] for a in flow["unreported_actions"]] == ["Pay now"]

    def test_typing_a_flagged_word_into_a_field_is_not_a_click(self):
        rows = [self._typed("#note", "pay later"), self._state()]
        assert self._flow([], rows)["violations"] == []

    def test_a_long_list_is_cut_and_counted(self):
        rows = [row for n in range(fm.UNREPORTED_SHOWN + 3)
                for row in (self._click(f"#b{n}", f"Item {n}"), self._state())]
        described = fm.describe(self._flow([], rows))
        assert described.count("- click: Item") == fm.UNREPORTED_SHOWN
        assert "- … 3 more" in described


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

    WALK = (f"PAGE_ENTER: p|https://x/|T\nPAGE_STATE: p|https://x/|{INVENTORY}\n"
            + "FLOW_STEP: " + _step(0, "p", name="BUY NOW") + "\n"
            + "FLOW_STEP: " + _step(1, "p", name="Checkout") + "\n")

    def test_a_violation_names_the_control_and_the_word_that_flagged_it(self):
        # "2 destructive action(s) were performed" sent the reader to the JSON to
        # find out which two, and neither had done anything but open a screen.
        flow = fm.build(self.WALK)
        assert [(v["target"], v["token"]) for v in flow["violations"]] == [
            ("BUY NOW", "buy"), ("Checkout", "checkout")]
        assert fm.violations_text(flow) == "BUY NOW ('buy'), Checkout ('checkout')"
        described = fm.describe(flow)
        assert "step 0: BUY NOW (its name contains 'buy')" in described
        assert "step 1: Checkout (its name contains 'checkout')" in described

    def test_under_a_sandbox_assertion_a_walked_control_is_not_a_violation(self):
        # The explorer was told to finish the flow, so walking a flagged control
        # is what it was asked to do. The word stays on the step for the report.
        flow = fm.build(self.WALK, sandbox=True)
        assert flow["violations"] == [] and flow["status"] == "ok"
        assert [s["result"]["destructive_token"] for s in flow["steps"]] == [
            "buy", "checkout"]
        described = fm.describe(flow)
        assert "Walked under the sandbox assertion" in described
        assert "cannot be treated as side-effect free" not in described

    def test_the_words_the_explorer_is_told_are_the_words_the_checker_flags(self):
        # Told a word the checker ignores, it refuses for nothing; not told one
        # the checker flags, it walks it and loses the run.
        for word in fm.DESTRUCTIVE_WORDS:
            assert fm.destructive_token(word), word
        for token in fm.DESTRUCTIVE:
            assert token in fm.DESTRUCTIVE_WORDS, token

    def test_looking_at_a_destructive_control_is_not_performing_it(self):
        # A change note asking the explorer to verify a Checkout button exists
        # escalated the whole run: the step's target matched "checkout", it
        # succeeded, and that counted as a destructive action performed.
        stdout = (f"PAGE_ENTER: p|https://x/|T\nPAGE_STATE: p|https://x/|{INVENTORY}\n"
                  + "FLOW_STEP: " + _step(0, "p", verb="observe", name="Checkout",
                                          outcome="ok") + "\n")
        flow = fm.build(stdout)
        assert flow["violations"] == []
        assert flow["status"] == "ok"
        assert flow["steps"][0]["result"]["destructive_token"] == "checkout", (
            "the token still belongs in the flow map — it just is not a violation")


class TestUninventoriedPages:
    """A page walked without a PAGE_STATE is unusable downstream.

    An observed run entered three pages, inventoried none, and still reported
    `ok`: no page object matched anything, every selector stayed unverified, and
    the adapt step was told the repo had no page object for pages it has had for
    months.
    """

    WALKED = {"pages": {"login": {}, "cart": {}},
              "_inventories": {"login": [{"tag": "input", "id": "user-name"}]},
              "steps": []}

    def test_a_page_with_no_inventory_is_named(self):
        assert fm.pages_without_inventory(self.WALKED) == ["cart"]

    def test_a_fully_inventoried_flow_names_nothing(self):
        flow = {"pages": {"login": {}}, "_inventories": {"login": [{"tag": "input"}]}}
        assert fm.pages_without_inventory(flow) == []

    def test_a_page_only_passed_through_is_not_acted_on(self):
        flow = {"pages": {"login": {}, "products": {}},
                "steps": [{"page": {"id": "products"}}, {"page": "products"}]}
        assert fm.pages_acted_on(flow) == {"products"}, (
            "the start page opened with a saved session has no step, so a missing "
            "inventory there is no reason for a whole second attempt")

    def test_validate_reports_it(self):
        ok, problems = fm.validate(self.WALKED)
        assert ok is False
        assert any("PAGE_STATE" in p for p in problems)

    def test_an_inventoried_attempt_outranks_a_longer_blind_one(self):
        blind = {"pages": {"a": {}, "b": {}}, "_inventories": {},
                 "steps": [{"result": {"outcome": "ok"}} for _ in range(9)]}
        seeing = {"pages": {"a": {}}, "_inventories": {"a": [{"tag": "div"}]},
                  "steps": [{"result": {"outcome": "ok"}} for _ in range(3)]}
        assert fm.score(seeing, "ok") > fm.score(blind, "ok"), (
            "more steps across pages nobody described is less usable than fewer "
            "steps that can actually be verified")

    def test_an_empty_walk_never_outranks_one_with_steps(self):
        blind = {"pages": {"a": {}, "b": {}}, "_inventories": {},
                 "steps": [{"result": {"outcome": "ok"}} for _ in range(5)]}
        empty = {"pages": {}, "_inventories": {}, "steps": []}
        assert fm.score(blind, "ok") > fm.score(empty, "ok"), (
            "an empty walk has no uninventoried pages; that must not let it "
            "replace a walk that has steps")


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


class TestValueChecks:
    def test_both_sides_are_kept_and_the_relation_is_measured_here(self):
        flow = fm.build("OUTCOME_OBSERVED: c1|pass|User_orrju sample_last_name\n"
                        "**VALUE_CHECK:** c1|customerName|User_orrju sample_last_name"
                        "|input:nameField|User_orrju\n"
                        "VALUE_CHECK: new: amount repeats the cart total|amount|Rp20.000"
                        "|element:cartTotal|20,000\n"
                        "VALUE_CHECK: too|short\n")
        assert [(v["check"], v["relation"]) for v in flow["value_checks"]] == [
            ("c1", "words"), ("new: amount repeats the cart total", "numeric")]


class TestMeasuredEvidence:
    """What the browser helpers measured, folded in: 30 of 87 explorer selectors were
    unverifiable only because no PAGE_STATE was ever typed for their page."""

    MARKERS = "\n".join([
        "PAGE_ENTER: products|https://shop.test/inventory.html|Shop",
        'FLOW_STEP: {"index": 0, "page": "products", "action": {"verb": "click", '
        '"target": {"name": "cart", "selector": "a.cart"}}, "result": {"outcome": "ok"}}',
        'FLOW_STEP: {"index": 1, "page": "products", "action": {"verb": "click", '
        '"target": {"name": "checkout", "selector": "button:has-text(\'Checkout\')"}}, '
        '"result": {"outcome": "ok"}}',
    ])
    ROWS = [
        {"page": "products", "url": "https://shop.test/inventory.html",
         "inventory": [{"tag": "a", "class": "cart"}, {"tag": "span", "class": "title",
                                                       "text": "Products"}],
         "checks": {"button:has-text('Checkout')": {"total": 1, "visible": 1}},
         "known": [{"owner": "ProductsPage", "path": "p/ProductsPage.java", "name": "title",
                    "selector": ".title", "total": 1, "visible": 1},
                   {"owner": "ProductsPage", "path": "p/ProductsPage.java", "name": "sort",
                    "selector": "#sort", "total": 0, "visible": 0},
                   {"owner": "CartPage", "path": "p/CartPage.java", "name": "checkout",
                    "selector": "#checkout", "total": 0, "visible": 0}]},
        # A later state of the same page, where the checkout button is gone.
        {"page": "products", "url": "https://shop.test/inventory.html",
         "checks": {"button:has-text('Checkout')": {"total": 0, "visible": 0}}},
    ]

    def test_without_evidence_nothing_is_verified(self):
        flow = fm.build(self.MARKERS)
        assert fm.pages_without_inventory(flow) == ["products"]
        assert {s["selector_check"]["counted_against"] for s in flow["steps"]} == {"none"}

    def test_evidence_inventories_the_page_and_verifies_live(self):
        flow = fm.build(self.MARKERS, self.ROWS)
        assert fm.pages_without_inventory(flow) == []
        cart, checkout = (s["selector_check"] for s in flow["steps"])
        assert (cart["counted_against"], cart["unique"]) == ("page_inventory", True)
        assert (checkout["counted_against"], checkout["unique"]) == ("live", True), (
            "a :has-text() selector the inventory cannot evaluate was counted live, and "
            "a later state where it is gone does not undo that")

    def test_known_locators_rank_page_objects_and_name_the_broken_ones(self):
        flow = fm.build(self.MARKERS, self.ROWS)
        fm.measure_page_objects(flow, [{"path": "p/ProductsPage.java", "snippet": ""}])
        best = flow["pages"]["products"]["best_page_object"]
        assert (best["name"], best["matched"], best["evaluable"], best["broken"]) == (
            "ProductsPage", 1, 2, ["sort"])
        table = fm.describe_page_objects(flow)
        assert "resolve live; broken: sort" in table and "counted on the live page" in table

    def test_a_locator_seen_in_an_earlier_state_is_not_broken(self):
        toast = {"owner": "ProductsPage", "path": "p/ProductsPage.java", "name": "toast",
                 "selector": ".toast"}
        rows = [{"page": "products", "url": "u", "known": [{**toast, "total": 1, "visible": 1}]},
                {"page": "products", "url": "u", "known": [{**toast, "total": 0, "visible": 0}]}]
        flow = fm.build(self.MARKERS, rows)
        fm.measure_page_objects(flow, [{"path": "p/ProductsPage.java", "snippet": ""}])
        best = flow["pages"]["products"]["best_page_object"]
        assert (best["matched"], best["broken"]) == (1, []), (
            "a toast counted while it showed is not broken because a later state came "
            "after it closed")

    @staticmethod
    def _counted(owner, name, total, visible=None):
        return {"owner": owner, "path": f"p/{owner}.java", "name": name,
                "selector": f".{name}", "total": total,
                "visible": total if visible is None else visible}

    def _overlay_flow(self):
        """A cart form, then a payment sheet opened over it at the same URL: the
        form's locators all still show behind the sheet."""
        c = self._counted
        cart = [c("CartPage", "email", 1), c("CartPage", "total", 1),
                c("CartPage", "submit", 1)]
        markers = "\n".join(f"PAGE_ENTER: {p}|https://shop.test/|Shop"
                            for p in ("home", "cart", "payment"))
        rows = [
            {"page": "home", "url": "https://shop.test/", "known": [
                c("HomePage", "buy", 1), c("HomePage", "thanks", 0),
                # In the DOM already, off-screen until the cart opens.
                c("CartPage", "email", 1), c("CartPage", "total", 0),
                c("CartPage", "submit", 1, visible=0),
                c("PaymentPage", "card", 0), c("PaymentPage", "expiry", 0),
                c("PaymentPage", "cvv", 0), c("PaymentPage", "amount", 0),
                c("PaymentPage", "pay", 0)]},
            {"page": "cart", "url": "https://shop.test/", "known": [
                c("HomePage", "buy", 1), c("HomePage", "thanks", 0), *cart,
                c("PaymentPage", "card", 0), c("PaymentPage", "expiry", 0),
                c("PaymentPage", "cvv", 0), c("PaymentPage", "amount", 0),
                c("PaymentPage", "pay", 0)]},
            {"page": "payment", "url": "https://shop.test/", "known": [
                c("HomePage", "buy", 1), c("HomePage", "thanks", 0), *cart,
                c("PaymentPage", "card", 1), c("PaymentPage", "expiry", 1),
                c("PaymentPage", "cvv", 1), c("PaymentPage", "amount", 1),
                c("PaymentPage", "pay", 0)]},
        ]
        flow = fm.build(markers, rows)
        fm.measure_page_objects(flow, [{"path": "p/CartPage.java", "snippet": ""}])
        return flow

    def test_a_page_object_showing_behind_an_overlay_does_not_claim_the_overlay(self):
        # By share alone the form behind won 3/3 against the sheet's own 4/5, and
        # the adapt step was told the payment sheet was the cart page.
        flow = self._overlay_flow()
        best = {page: flow["pages"][page]["best_page_object"]["name"]
                for page in ("home", "cart", "payment")}
        assert best == {"home": "HomePage", "cart": "CartPage", "payment": "PaymentPage"}
        payment = flow["pages"]["payment"]["best_page_object"]
        assert (payment["matched"], payment["evaluable"], payment["broken"],
                payment["appeared"]) == (4, 5, ["pay"], 4)
        assert "4 first shown on this page" in fm.describe_page_objects(flow)

    def test_a_row_placed_by_url_does_not_set_the_order_pages_were_reached(self):
        # Where every page shares one URL, a row with no page id cannot say which
        # of them it saw, so it must not move a page to the front of the walk.
        markers = "\n".join(f"PAGE_ENTER: {p}|https://shop.test/|Shop"
                            for p in ("home", "cart"))
        rows = [{"url": "https://shop.test/", "inventory": [{"tag": "a"}]},
                {"page": "home", "url": "https://shop.test/", "known": []},
                {"page": "cart", "url": "https://shop.test/", "known": []}]
        assert fm.build(markers, rows)["_page_order"] == ["home", "cart"]

    WALK = "\n".join([
        "PAGE_ENTER: list|https://shop.test/|Shop",
        "PAGE_ENTER: detail|https://shop.test/|Shop",
        'FLOW_STEP: {"index": 0, "page": "detail", "action": {"verb": "click", '
        '"target": {"name": "tab", "selector": "a.tab"}}, "result": {"outcome": "ok"}}',
    ])

    @staticmethod
    def _state(page, checks=None):
        return {"page": page, "url": "https://shop.test/", "checks": checks or {},
                "inventory": [{"tag": "h1", "text": page}]}

    def _unique(self, rows):
        check = fm.build(self.WALK, rows)["steps"][0]["selector_check"]
        return check["counted_against"], check["unique"]

    def test_a_control_the_click_removes_is_counted_where_it_was_clicked(self):
        # A tab is gone once clicked, so the step's own count read 0 on the page it
        # landed on and the flow map called a unique selector "not unique". The
        # helpers had counted it at the moment of the click.
        click = {"clicked": {"sel": "a.tab", "total": 1, "visible": 1, "text": "Card"}}
        rows = [self._state("list"), click,
                self._state("detail", {"a.tab": {"total": 0, "visible": 0}})]
        assert self._unique(rows) == ("live", True)
        assert self._unique([rows[0], rows[2]]) == ("live", False), (
            "without the click's own count the step reads as it used to")

    def test_a_keystroke_counts_the_field_it_went_into(self):
        typed = {"typed": {"sel": "a.tab", "value": "x", "total": 1, "visible": 1}}
        assert self._unique([typed, self._state("detail")]) == ("live", True)

    @pytest.mark.parametrize("action", [
        {"sel": "a.tab", "total": 2, "visible": 2},        # ambiguous when used
        {"sel": "a.tab", "total": 1, "visible": 0},        # hidden when used
        {"sel": None, "total": 1, "visible": 1},           # frame with no selector
    ])
    def test_only_a_use_on_one_visible_element_verifies_anything(self, action):
        rows = [{"clicked": action},
                self._state("detail", {"a.tab": {"total": 0, "visible": 0}})]
        assert self._unique(rows) == ("live", False)

    def test_a_use_belongs_to_the_next_state_a_step_named_and_no_later_one(self):
        click = {"clicked": {"sel": "a.tab", "total": 1, "visible": 1}}
        unplaced = {"url": "https://elsewhere.test/", "checks": {}}
        # The state measured after the click named no page, so the click cannot be
        # filed: handed on to a later step's page it would vouch for the wrong one.
        assert self._unique([click, unplaced, self._state("detail")]) == ("page_inventory", False)

    def test_a_brief_screens_rows_are_a_record_like_any_other(self):
        c = self._counted
        rows = [
            {"page": "list", "url": "https://shop.test/", "inventory": [{"tag": "h1"}],
             "known": [c("ListPage", "tab", 1), c("DetailPage", "total", 0)]},
            {"clicked": {"sel": "a.tab", "total": 1, "visible": 1}},
            {"page": "detail", "url": "https://shop.test/", "brief": True,
             "inventory": [{"tag": "h2", "text": "Paid"}],
             "known": [c("ListPage", "tab", 0), c("DetailPage", "total", 1)]},
        ]
        flow = fm.build(self.WALK, rows)
        assert fm.pages_without_inventory(flow) == []
        fm.measure_page_objects(flow, [{"path": "p/DetailPage.java", "snippet": ""}])
        assert flow["pages"]["detail"]["best_page_object"]["name"] == "DetailPage"
        assert flow["steps"][0]["selector_check"]["unique"] is True

    def test_evidence_file_tolerates_a_torn_last_line(self, tmp_path):
        path = tmp_path / "evidence.jsonl"
        path.write_text(json.dumps(self.ROWS[0]) + "\n{\"page\": \"prod")
        assert len(fm.read_evidence(path)) == 1
        assert fm.read_evidence(tmp_path / "missing.jsonl") == []


def test_elements_matching_returns_the_matched_elements_or_none():
    from shared import flow_map
    inventory = [{"tag": "a", "class": "option", "attributes": {"data-option": "card"}},
                 {"tag": "a", "class": "option", "attributes": {"data-option": "wallet"}}]
    matched = flow_map.elements_matching("a[data-option='card']", inventory)
    assert matched == [inventory[0]]
    assert flow_map.elements_matching("a:has-text('Card')", inventory) is None
    assert flow_map.count_in_inventory("a.option", inventory) == 2
