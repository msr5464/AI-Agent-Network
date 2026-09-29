"""What a raw, human-written test case states outright.

Read from the test case itself, never from step 01's plan: the plan is a model's
rewording, and a rewording can drop a value. Step 01 turned "fill dummy data …
Address: 221B Baker Street" into "fill the fields with dummy data", step 02 typed
an address it made up, and step 03 generated test data in that address's shape.
"""

import re

from shared import value_match

# `Label: value` lines a test case gives its data in ("Address: 221B Baker Street"),
# minus its header fields.
_GIVEN = re.compile(r"^\s*([A-Za-z][\w ()/.-]{0,30}?)\s*:\s*(\S.*?)\s*$")
_HEADER_LABELS = {"module", "type", "url", "api url", "base url", "web url", "steps",
                  "web steps", "api steps", "test steps", "actual result",
                  "expected result", "description", "title", "priority"}


def given_values(text: str) -> list:
    """[(label, value)] for each value the test case states, in its order.

    A numbered step line is prose, not a value, and neither is a URL split at
    its scheme or a sentence that happens to hold a colon.
    """
    out = []
    for line in (text or "").splitlines():
        match = _GIVEN.match(line)
        if not match:
            continue
        label, value = match.group(1).strip(), match.group(2)
        if (label.lower() in _HEADER_LABELS or len(label.split()) > 4
                or value.startswith("//")):
            continue
        out.append((label, value))
    return out


def is_given(value: str, text: str) -> bool:
    """Whether `value` is one the test case states, allowing for a field's own
    formatting (`4111111111111111` for `4111 1111 1111 1111`)."""
    letters = lambda s: re.sub(r"\W", "", str(s)).casefold()
    return any(value_match.relation(given, value) in ("equal", "formatting")
               or letters(given) == letters(value) != ""
               for _label, given in given_values(text))
