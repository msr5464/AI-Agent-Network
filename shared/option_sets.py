"""The other options a page offered at a choice the flow made.

A test that picks one payment method from a list proves that one path. The page
also offered the others, and a module helper that knows only the one it was
written for makes the next test copy the whole method under another name. The
browser already measured every one of those options: step 02's evidence file
lists each element of every settled page state, iframes included. So the
alternatives a choice offers are a fact read off that inventory, never a guess
the model makes about what a page probably has.

An option set is built from a confirmed selector of the control the flow used:

  * **The key.** Only exact `tag[attr='v']`, `[attr='v']` and `#id` forms can
    name an option, because only they say which attribute tells one option from
    the next. `:has-text`, combinators and xpath are skipped — a set read from a
    selector this module cannot evaluate would be a guess with a list attached.
  * **The anchor.** A state counts only where the selector matches exactly one
    inventory element. Matching two means the state cannot say which one the flow
    used, so it says nothing about that element's neighbours.
  * **Siblings.** Elements in the same state and the same frame, with the same
    tag, the same attribute names, equal values for every attribute but the key,
    and a different key. Class sets must be equal or one must contain the other:
    the selected option usually carries one extra state class.
  * **Merged across states** in first-seen order. `occurrences` is the most
    times one key appeared within a single state — a list that shows an option
    under both "recommended" and "all" lists it twice, and a locator built from
    that key alone is not unique there.

Blind spots, which the enum generated from a set has to admit rather than paper
over: options inside a collapsed group (they carry no attributes until expanded),
a native `<select>`'s `<option>`s (the inventory records the select, not its
options), anything past the inventory's per-frame sample, and lists whose items
carry a per-element generated id — those never compare equal, so no set is found
and the enum lists only the value the flow used.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from shared import frames
from shared.flow_map import element_value, elements_matching, simple_parts

# A list longer than this is a catalogue, not a choice — keep the head and say so.
MAX_OPTIONS = 30

# Top-level inventory keys that describe an element rather than identify it.
_NOT_ATTRIBUTES = {"tag", "class", "text", "frame", "attributes"}

# Rule 16 forbids URLs in generated Java, so a choice keyed by one cannot become
# an enum value. A fragment or relative path is not a URL in that sense.
_ABSOLUTE_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)


def _attributes(element: Dict) -> Dict[str, str]:
    """Every identifying attribute of an inventory element, top-level and nested."""
    found = {k: str(v) for k, v in element.items()
             if k not in _NOT_ATTRIBUTES and v is not None}
    for name, value in (element.get("attributes") or {}).items():
        if value is not None:
            found.setdefault(name, str(value))
    return found


def _classes(element: Dict) -> set:
    return set(str(element.get("class") or "").split())


def _key_candidates(parts: Dict) -> List[str]:
    """Attributes the selector pins to one exact value — the ones that can vary."""
    names = [name for name, op, value in parts["attrs"] if op == "" and value is not None]
    if parts["ids"] and not names:
        names.append("id")
    return names


def _siblings(anchor: Dict, state: List[Dict], key: str) -> List[Dict]:
    """Elements of the same state that differ from the anchor only in `key`."""
    frame = frames.prefix_path(anchor.get("frame") or "")
    tag = str(anchor.get("tag") or "").lower()
    own = _attributes(anchor)
    classes = _classes(anchor)
    found = []
    for element in state:
        if element is anchor:
            continue
        if frames.prefix_path(element.get("frame") or "") != frame:
            continue
        if str(element.get("tag") or "").lower() != tag:
            continue
        other_classes = _classes(element)
        if not (classes <= other_classes or other_classes <= classes):
            continue
        attrs = _attributes(element)
        if set(attrs) != set(own):
            continue
        if attrs.get(key) == own.get(key):
            continue
        if any(attrs[name] != value for name, value in own.items() if name != key):
            continue
        found.append(element)
    return found


def _template(selector: str, key_value: str) -> str:
    """The selector with its key replaced by `{key}`, or "" if that is ambiguous."""
    path, inner = frames.split(selector)
    if inner.count(key_value) != 1:
        return ""
    return frames.join(path, inner.replace(key_value, "{key}"))


def option_set(selector: str, rows: Iterable[Dict],
               max_options: int = MAX_OPTIONS) -> Optional[Dict]:
    """The options offered alongside the element `selector` names, or None.

    None when the selector is not one this module can key, when no state matched
    it exactly once, when nothing in those states looked like a sibling, or when
    an option's key is an absolute URL.
    """
    parts = simple_parts(selector)
    if parts is None:
        return None
    keys = _key_candidates(parts)
    if not keys:
        return None

    best: Optional[Dict] = None
    for key in keys:
        options: Dict[str, Dict] = {}
        chosen = ""
        for row in rows or []:
            state = row.get("inventory") or []
            matched = elements_matching(selector, state)
            if matched is None:
                return None
            if len(matched) != 1:
                continue
            anchor = matched[0]
            siblings = _siblings(anchor, state, key)
            if not siblings:
                continue
            chosen = chosen or (element_value(anchor, key) or "")
            seen_here: Dict[str, int] = {}
            for element in [anchor] + siblings:
                value = element_value(element, key)
                if value is None:
                    continue
                seen_here[value] = seen_here.get(value, 0) + 1
                entry = options.setdefault(value, {"key": value, "label": "", "occurrences": 0})
                if not entry["label"]:
                    entry["label"] = " ".join(str(element.get("text") or "").split())
            for value, count in seen_here.items():
                options[value]["occurrences"] = max(options[value]["occurrences"], count)
        if len(options) < 2:
            continue
        if any(_ABSOLUTE_URL.match(value) for value in options):
            return None
        if best is None or len(options) > len(best["options"]):
            ordered = list(options.values())
            best = {
                "selector": selector,
                "attribute": key,
                "template": _template(selector, chosen) if chosen else "",
                "chosen": chosen,
                "options": ordered[:max_options],
                "truncated": len(ordered) > max_options,
            }
    return best


def from_selectors(selectors: Dict[str, str], rows: List[Dict],
                   skip: Iterable[str] = ()) -> Dict[str, Dict]:
    """{locator name: option set} for every confirmed selector that has one.

    `skip` names the fields the flow typed into and the elements it only read —
    neither is a choice, and an input row beside other inputs would otherwise
    read as one.
    """
    skipped = set(skip or ())
    found = {}
    for name, selector in (selectors or {}).items():
        if name in skipped or not selector:
            continue
        result = option_set(selector, rows)
        if result:
            found[name] = result
    return found
