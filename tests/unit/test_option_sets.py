"""Tests for shared/option_sets.py — the options a choice offered, read off evidence.

An option enum generated from the model's idea of what a page "probably" offers is
an invented fact with a list attached. These pin the rule that keeps it honest:
options are the elements the browser measured beside the one the flow clicked,
in the same state and frame, differing only in the attribute the selector keys on.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import frames  # noqa: E402
from shared import option_sets  # noqa: E402

PAY = "#pay"
IN_PAY = f"{PAY} >> {frames.ENTER} >> "
SELECTOR = frames.join([PAY], "a[data-option='card']")


def _option(key, label, cls="option", frame=IN_PAY, tag="a", **extra):
    attributes = {"data-testid": "method", "data-option": key}
    attributes.update(extra)
    return {"tag": tag, "class": cls, "text": label, "attributes": attributes, "frame": frame}


def _state(*elements):
    return {"inventory": list(elements)}


def test_siblings_in_the_same_frame_become_options_with_a_template():
    rows = [_state(_option("card", "Card"), _option("bank-transfer", "Bank transfer"),
                   _option("wallet", "Wallet"))]
    found = option_sets.option_set(SELECTOR, rows)
    assert found["attribute"] == "data-option"
    assert found["chosen"] == "card"
    assert [o["key"] for o in found["options"]] == ["card", "bank-transfer", "wallet"]
    assert found["options"][1]["label"] == "Bank transfer"
    assert found["template"] == frames.join([PAY], "a[data-option='{key}']")
    assert found["truncated"] is False


def test_an_element_in_another_frame_is_not_an_option():
    rows = [_state(_option("card", "Card"), _option("wallet", "Wallet"),
                   _option("bank-transfer", "Bank transfer", frame=""))]
    keys = [o["key"] for o in option_sets.option_set(SELECTOR, rows)["options"]]
    assert keys == ["card", "wallet"]


def test_a_different_constant_attribute_is_not_an_option():
    # Same shape, but another list's items: a different data-testid.
    rows = [_state(_option("card", "Card"), _option("wallet", "Wallet"),
                   _option("express", "Express", **{"data-testid": "delivery"}))]
    keys = [o["key"] for o in option_sets.option_set(SELECTOR, rows)["options"]]
    assert keys == ["card", "wallet"]


def test_a_different_tag_or_class_is_not_an_option():
    rows = [_state(_option("card", "Card"), _option("wallet", "Wallet"),
                   _option("bank-transfer", "Bank transfer", tag="button"),
                   _option("cash", "Cash", cls="banner"))]
    keys = [o["key"] for o in option_sets.option_set(SELECTOR, rows)["options"]]
    assert keys == ["card", "wallet"]


def test_the_selected_option_may_carry_one_extra_state_class():
    rows = [_state(_option("card", "Card", cls="option selected"), _option("wallet", "Wallet"))]
    keys = [o["key"] for o in option_sets.option_set(SELECTOR, rows)["options"]]
    assert keys == ["card", "wallet"]


def test_selectors_this_module_cannot_key_have_no_set():
    rows = [_state(_option("card", "Card"), _option("wallet", "Wallet"))]
    assert option_sets.option_set(frames.join([PAY], "a.option"), rows) is None
    assert option_sets.option_set(frames.join([PAY], "a:has-text('Card')"), rows) is None
    assert option_sets.option_set("//a[@data-option='card']", rows) is None


def test_an_id_or_a_name_can_be_the_key():
    plans = [{"tag": "button", "class": "plan", "id": plan, "text": plan.title()}
             for plan in ("basic", "plus", "pro")]
    found = option_sets.option_set("#plus", [_state(*plans)])
    assert found["attribute"] == "id"
    assert [o["key"] for o in found["options"]] == ["plus", "basic", "pro"]

    speeds = [{"tag": "input", "class": "", "name": f"speed-{s}", "text": s}
              for s in ("standard", "express")]
    found = option_sets.option_set("input[name='speed-standard']", [_state(*speeds)])
    assert found["attribute"] == "name"
    assert [o["key"] for o in found["options"]] == ["speed-standard", "speed-express"]


def test_an_option_shown_twice_is_one_option_marked_as_not_unique():
    rows = [_state(_option("card", "Card"), _option("wallet", "Wallet"),
                   _option("wallet", "Wallet"))]
    options = option_sets.option_set(SELECTOR, rows)["options"]
    assert [(o["key"], o["occurrences"]) for o in options] == [("card", 1), ("wallet", 2)]


def test_options_are_merged_across_states_in_first_seen_order():
    rows = [_state(_option("card", "Card"), _option("bank-transfer", "Bank transfer")),
            _state(_option("card", "Card"), _option("wallet", "Wallet"))]
    keys = [o["key"] for o in option_sets.option_set(SELECTOR, rows)["options"]]
    assert keys == ["card", "bank-transfer", "wallet"]


def test_a_long_list_is_capped_and_says_so():
    rows = [_state(_option("card", "Card"), *[_option(f"m{i}", f"M{i}") for i in range(10)])]
    found = option_sets.option_set(SELECTOR, rows, max_options=4)
    assert len(found["options"]) == 4 and found["truncated"] is True


def test_a_state_where_the_selector_is_ambiguous_anchors_nothing():
    rows = [_state(_option("card", "Card"), _option("card", "Card"), _option("wallet", "Wallet"))]
    assert option_sets.option_set(SELECTOR, rows) is None


def test_options_keyed_by_an_absolute_url_are_not_an_enum():
    selector = "a[href='https://shop.example.com/card']"
    rows = [_state(*[{"tag": "a", "class": "pay", "text": k,
                      "attributes": {"href": f"https://shop.example.com/{k}"}}
                     for k in ("card", "wallet")])]
    assert option_sets.option_set(selector, rows) is None


def test_typed_and_read_controls_are_skipped_and_no_rows_means_no_sets():
    rows = [_state(_option("card", "Card"), _option("wallet", "Wallet"))]
    assert set(option_sets.from_selectors({"paymentMethodOption": SELECTOR}, rows)) == \
        {"paymentMethodOption"}
    assert option_sets.from_selectors({"paymentMethodOption": SELECTOR}, rows,
                                      skip={"paymentMethodOption"}) == {}
    assert option_sets.from_selectors({"paymentMethodOption": SELECTOR}, []) == {}
