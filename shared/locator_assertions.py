"""Which locators a test *verifies* with, as opposed to *acts* on.

Heal how a test finds an element; never what it checks. A healed assertion
locator turns a caught regression into a green build, which is the exact failure
this whole system exists to avoid.

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
                sites.append(f"{call}({args.strip()[:70]})")
    return sites


def is_assertion_locator(field: str, sources: Iterable[str]) -> Optional[str]:
    """The first assertion that reads this locator, or None if it is only acted on."""
    sites = assertion_sites(field, sources)
    return sites[0] if sites else None


def assertion_fields(sources: Dict[str, str], candidate_fields: Iterable[str]) -> set:
    """Of these locator fields, the ones some assertion reads."""
    texts = list(sources.values())
    return {field for field in candidate_fields if is_assertion_locator(field, texts)}
