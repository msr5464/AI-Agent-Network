"""Which locators a test *verifies* with, as opposed to *acts* on.

A locator an assertion reads is repaired only on proof that the replacement is
the same element: pointed at a different one, a presence check passes on
anything visible, and a caught regression becomes a green build.

An assertion that a page has loaded is not one of these. Its anchor is how the
page is recognised, and whether the test is on that page is decided from the
rest of the page, not from the anchor itself.

The prototype carried a hand-set `usage: "assertion"` flag on the baseline. That
works for a fixture and is worthless against real source, where nothing sets it.
This reads the answer out of the code instead, reusing assertion_graph's notion
of what counts as an assertion — deliberately broad, because a project-specific
wrapper nobody told us about still matters.
"""
from __future__ import annotations
import re
from typing import Dict, Iterable, Optional

from shared.assertion_graph import ASSERT_CALL, _call_args
from shared.code_analyzer import without_comments


def _referenced(argument_text: str, field: str) -> bool:
    return re.search(r"\b" + re.escape(field) + r"\b", argument_text) is not None


def assertion_sites(field: str, sources: Iterable[str]) -> list[str]:
    """Every assertion call that reads this locator, as short quoted snippets.

    Comments are stripped first: a field named in a commented-out assertion is
    not verified by anything, and treating it as such would refuse heals for no
    reason.
    """
    from shared.frameworks import get_active_plugin
    page_loads = set(get_active_plugin().code.PAGE_LOAD_ASSERTIONS)
    sites: list[str] = []
    for source in sources:
        if not source or field not in source:
            continue
        text = without_comments(source)
        for match in ASSERT_CALL.finditer(text):
            open_paren = text.find("(", match.start())
            if open_paren < 0:
                continue
            args = _call_args(text, open_paren)
            if _referenced(args, field):
                call = match.group(1) if match.groups() else match.group(0)
                if call.split(".")[-1] in page_loads:
                    continue
                sites.append(f"{call}({args.strip()[:70]})")
    return sites


def is_assertion_locator(field: str, sources: Iterable[str]) -> Optional[str]:
    """The first assertion that reads this locator, or None if it is only acted on."""
    sites = assertion_sites(field, sources)
    return sites[0] if sites else None


def assertion_sites_by_field(sources: Dict[str, str],
                             candidate_fields: Iterable[str]) -> Dict[str, str]:
    """Of these locator fields, the ones some assertion reads — each with the
    first assertion that reads it, which is what a reviewer is pointed at."""
    texts = list(sources.values())
    found = {}
    for field in candidate_fields:
        site = is_assertion_locator(field, texts)
        if site:
            found[field] = site
    return found


def assertion_fields(sources: Dict[str, str], candidate_fields: Iterable[str]) -> set:
    """Of these locator fields, the ones some assertion reads."""
    return set(assertion_sites_by_field(sources, candidate_fields))


# What to do about a locator an assertion reads (`classify.heal_assertions`).
NEVER, PROVEN, ALWAYS = "never", "proven", "always"


def heal_mode(cfg: dict) -> str:
    """never / proven / always. Anything unrecognised reads as never: the one
    answer that cannot repair a locator nobody meant to have repaired."""
    value = ((cfg or {}).get("classify") or {}).get("heal_assertions", NEVER)
    value = str(value).strip().lower()
    return value if value in (NEVER, PROVEN, ALWAYS) else NEVER


def unproven(site: str, why: str) -> str:
    """The reason given when such a locator could not be repaired on proof."""
    return (f"read by {site}, and the replacement could not be proven to be the "
            f"same element ({why}) — reported for review rather than guessed")
