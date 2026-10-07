"""How a page renders a value a test expected — measured, never claimed.

A test case written in English says "the name shown matches the one we filled"
or "the amount is the same as before". That names the two values and leaves the
comparison open, and the page rarely renders a value exactly as it was typed:
`081234567890` comes back as `+6281234567890`, a cart total of `20,000` as
`Rp20.000`, a one-word name as `User_orrju sample_last_name`.

The browser step used to judge those "matching" in prose, and throw the judgment
away; generation then wrote strict string equality and the test failed on a
difference nobody would call a bug. `relation()` makes the judgment in Python,
from the two texts alone, so the comparator a test is generated with — and the
one a fix may move it to — is the one the product was actually seen to satisfy.

The relations, tightest first:

  equal       the same text
  formatting  differs only in whitespace or letter case
  numeric     one amount each, the same number (`Rp20.000` = `20,000`)
  phone       one phone number each, the same digits once a trunk `0` or a
              country code is ignored (`081234567890` = `+6281234567890`)
  words       the expected text appears inside the actual one as whole words

Anything else is None: a different value, which is a finding about the product
and never something to loosen a comparison over.

Two amounts that differ have an order instead (`order()`): `less` or `greater`,
what "the total decreased" claims. An order is never a relation. It is not in
RELATIONS, so it can never sanction loosening an equality.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

RELATIONS = ("equal", "formatting", "numeric", "phone", "words")
# Every relation but `equal` is a looser comparator than string equality.
SANCTIONABLE = frozenset(RELATIONS[1:])

MEANING = {
    "equal": "is exactly the expected text",
    "formatting": "differs from the expected text only in spacing or letter case",
    "numeric": "is the same number, formatted differently",
    "phone": "is the same phone number with a different country-code or trunk prefix",
    "words": "contains the expected text as whole words, plus more",
}

# How a check is asserted once its relation is known. Framework-neutral on
# purpose: the automation repo's own conventions say which helper expresses each.
ASSERT_WITH = {
    "equal": "exact equality",
    "formatting": "equality once whitespace and letter case are ignored",
    "numeric": "equality of the two amounts as numbers — one converter in the "
               "module turns each displayed amount into plain number text (no "
               "currency, no grouping separators, no trailing decimal zeros), the "
               "getter that reads the amount returns it through that converter, and "
               "the two texts are compared with the string equality assertion. Never "
               "assert on a parsed numeric type: the assertion helper may have no "
               "overload for it",
    "phone": "equality of the digits once a leading country code or trunk 0 is "
             "dropped — one converter in the module, which the getter that reads "
             "the phone returns through",
    "words": "the shown text CONTAINS the expected text",
}

_SPACE = re.compile(r"[\s  ]+")
# A number with its grouping and decimal separators. A leading minus belongs to
# it only when it does not follow a word character (`ID-123` is not negative).
_NUMBER = re.compile(r"(?<!\w)-?\d[\d.,]*|\d[\d.,]*")
_PHONE = re.compile(r"\+?[\d\s().\-]+")
_WORD = re.compile(r"\w")


def _flat(text) -> str:
    return _SPACE.sub(" ", str(text or "")).strip().casefold()


def _to_number(token: str) -> Optional[float]:
    """`20.000` and `20,000` → 20000, `19000.00` → 19000.0, `1.234,56` → 1234.56.

    The last separator decides: followed by exactly three digits it groups
    thousands, otherwise it is the decimal point.
    ponytail: a heuristic — `1.500` always reads as fifteen hundred, never as one
    and a half. Pass an explicit locale in if a product ever needs the other one.
    """
    sign = -1.0 if token.startswith("-") else 1.0
    digits = token.lstrip("-").rstrip(".,")
    last = max(digits.rfind("."), digits.rfind(","))
    if last == -1:
        whole, fraction = digits, ""
    elif len(digits) - last - 1 == 3:
        whole, fraction = re.sub(r"[.,]", "", digits), ""
    else:
        whole, fraction = re.sub(r"[.,]", "", digits[:last]), digits[last + 1:]
    try:
        return sign * float(f"{whole}.{fraction}" if fraction else whole)
    except ValueError:
        return None


def _amount(text: str):
    """(number, the text around it) when `text` holds exactly one number."""
    found = _NUMBER.findall(text)
    if len(found) != 1:
        return None
    number = _to_number(found[0])
    if number is None:
        return None
    return number, _SPACE.sub("", _NUMBER.sub("", text, count=1))


def _amount_like(rest: str) -> bool:
    # A currency code or symbol at most: `Rp`, `IDR`, `S$`, `$`.
    return len(rest) <= 4 and not any(c.isdigit() for c in rest)


def _numeric(expected: str, actual: str) -> bool:
    a, b = _amount(expected), _amount(actual)
    if not a or not b or a[0] != b[0]:
        return False
    # `5 items` and `5 orders` hold the same number and say different things.
    return a[1] == b[1] or (_amount_like(a[1]) and _amount_like(b[1]))


def _phone(expected: str, actual: str) -> bool:
    if not (_PHONE.fullmatch(expected) and _PHONE.fullmatch(actual)):
        return False
    a = re.sub(r"\D", "", expected).lstrip("0")
    b = re.sub(r"\D", "", actual).lstrip("0")
    if len(a) < 7 or len(b) < 7:
        return False
    if a == b:
        return True
    (short, _), (long_, long_text) = sorted(((a, expected), (b, actual)),
                                            key=lambda pair: len(pair[0]))
    # A country code is the only difference allowed — up to three leading digits
    # — and only a number written in international form carries one. An order id
    # with extra leading digits is a different order, not a reformatted one.
    return (long_text.startswith("+") and long_.endswith(short)
            and len(long_) - len(short) <= 3)


def _words(expected: str, actual: str) -> bool:
    if len(expected) < 2 or not _WORD.search(expected):
        return False
    return re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", actual) is not None


def relation(expected, actual) -> Optional[str]:
    """The tightest relation under which `actual` renders `expected`, or None."""
    if expected is None or actual is None:
        return None
    expected, actual = str(expected), str(actual)
    if expected == actual:
        return "equal"
    flat_expected, flat_actual = _flat(expected), _flat(actual)
    if not flat_expected or not flat_actual:
        return None
    # Whitespace removed outright, as assertion_graph's own formatting rule has
    # it: `$ 8.99` and `$8.99` are one amount rendered two ways.
    if flat_expected.replace(" ", "") == flat_actual.replace(" ", ""):
        return "formatting"
    if _numeric(flat_expected, flat_actual):
        return "numeric"
    if _phone(expected.strip(), actual.strip()):
        return "phone"
    if _words(flat_expected, flat_actual):
        return "words"
    return None


# What an order check is asserted with, said the same framework-neutral way.
ORDER_ASSERT_WITH = {
    "less": "the shown amount is LESS than the other side's: both read through the "
            "module's one converter as plain number text, parsed as numbers, and "
            "compared with the boolean assertion",
    "greater": "the shown amount is GREATER than the other side's: both read through "
               "the module's one converter as plain number text, parsed as numbers, "
               "and compared with the boolean assertion",
}


def order(expected, actual) -> Optional[str]:
    """`less` or `greater` — how the amount `actual` shows compares with the one
    `expected` shows — when each holds one amount and they differ, else None.

    "The total decreased after the promo" is satisfied by two different amounts,
    so relation() calls it None, and a contract built only from relations dropped
    it. Step 03 then read the amount before the promo at a point step 02 never
    read it, after the card number had already lowered it, and the test compared
    an amount with itself.
    """
    if expected is None or actual is None:
        return None
    a, b = _amount(_flat(str(expected))), _amount(_flat(str(actual)))
    if not a or not b or a[0] == b[0]:
        return None
    if not (a[1] == b[1] or (_amount_like(a[1]) and _amount_like(b[1]))):
        return None
    return "less" if b[0] < a[0] else "greater"


def triage(failure_text: str, messages, parse) -> Optional[Dict[str, str]]:
    """The failed assertion, when all it saw was its expected value rendered differently.

    `{message, expected, actual, relation}`, or None for anything else: no value
    assertion in the text; two values that are simply different, which is a
    finding and never a reason to loosen a check; or no single one of `messages`
    to pin it on. Pinned by message because the message is what a failure prints,
    and the one thing that tells two same-shaped checks apart.

    `parse` is the framework plugin's `value_mismatch`: how a failed assertion
    prints its two sides is the framework's business, not this module's.
    """
    line = next((ln for ln in (failure_text or "").splitlines() if parse(ln)), "")
    pair = parse(line) if line else None
    if not pair:
        return None
    found = relation(*pair)
    if found not in SANCTIONABLE:
        return None
    messages = [m for m in messages if m]
    hits = [m for m in set(messages) if m in line]
    if not hits:
        return None
    message = max(hits, key=len)
    if messages.count(message) != 1:
        return None
    return {"message": message, "expected": pair[0], "actual": pair[1],
            "relation": found}


def appears_in(value, text) -> bool:
    """Whether `text` shows `value` anywhere: whitespace and case aside, or — for
    an amount — as the same number (`Rp 20,000` is in "the total was Rp20.000").

    Deliberately lenient: it answers "could this value have come from there?", so
    a wrong yes only means a made-up value goes unflagged, never that a real one
    is taken out of a test.
    """
    flat_value, flat_text = _flat(value), _flat(text)
    if not flat_value or not flat_text:
        return False
    if flat_value.replace(" ", "") in flat_text.replace(" ", ""):
        return True
    amount = _amount(flat_value)
    if amount and _amount_like(amount[1]):
        return any(_to_number(token) == amount[0] for token in _NUMBER.findall(flat_text))
    return False


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"`":
        return text[1:-1]
    return text


def parse_value_check(payload: str) -> Optional[Dict[str, str]]:
    """One `VALUE_CHECK:` payload — `<check>|<element>|<rendered>|<source>|<expected>`.

    The relation is computed here from the two texts; whatever the model thought
    of them is not an input. Rendered text is read between the second field and
    the last two, so a `|` inside what the page showed does not shift the rest.
    Returns None for a line too short to be one.
    """
    parts = str(payload or "").split("|")
    if len(parts) < 5 or not parts[0].strip():
        return None
    rendered = _unquote("|".join(parts[2:-2]))
    expected = _unquote(parts[-1])
    source = parts[-2].strip()
    kind, _, name = source.partition(":")
    kind = kind.strip().lower()
    if kind in ("input", "element"):
        source = f"{kind}:{name.strip()}"
    elif kind == "literal":
        source = "literal"
    found = relation(expected, rendered) or ""
    return {"check": parts[0].strip(), "element": parts[1].strip(),
            "rendered": rendered, "source": source, "expected": expected,
            "relation": found, "order": "" if found else (order(expected, rendered) or "")}
