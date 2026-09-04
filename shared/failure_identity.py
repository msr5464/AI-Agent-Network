"""Which locator a test failure is about, and whether two failures share one.

The question this answers is the difference between throwing a good fix away and
keeping it. A verification run that still fails says nothing on its own: the
repaired element may be fine and the flow may simply have advanced to the *next*
broken locator, which is progress and must not be reverted. Only the identity of
the failing element separates the two, and it is recoverable from the framework's
own wording:

    Failed to click on element 'Login button' with locator: Locator@button[type='submit']
        at automation.modules.naukari.web.NaukriLoginPage.doLogin(NaukriLoginPage.java:36)

Comparison is by selector first and element name second. A fix changes the
selector by definition, so a selector that no longer appears is expected; what
makes two failures the same is the *element* — the page object and field being
reached for — which survives the repair.
"""

import re
from typing import Dict, List

from shared import page_identity

# "Failed to click on element 'X' with locator: Locator@sel: Error {" — the
# framework's wording for every interaction failure, and the only line that
# names the element and its selector together.
_INTERACTION = re.compile(
    r"Failed to [\w ]{2,30}?element\s+'([^']+)'\s+with locator:\s*"
    r"(?:Locator@)?(.+?)(?=:\s*Error|\s*$)", re.MULTILINE)

# "Failed to load Element Locator@sel in DashboardPage" — the page-load assertion.
_PAGE_LOAD = re.compile(r"Failed to load Element\s+(?:Locator@)?(\S+)\s+in\s+(\w+)")

# The first frame that is a page object, not the test or a helper.
_FRAME = re.compile(r"\bat\s+[\w.]*?\.(\w*Page)\.\w+\(\1\.java:(\d+)\)")


def identify(output: str) -> Dict:
    """What failed, per the test output. Empty strings where it cannot be told.

    Reads the FIRST interaction failure in the text, not the last: maven repeats
    the same failure in its summary, and a cascading run reports the consequence
    after the cause.
    """
    result = {"element": "", "selector": "", "page_object": "", "available": False}
    if not output:
        return result

    match = _INTERACTION.search(output)
    if match:
        result["element"] = match.group(1).strip()
        result["selector"] = match.group(2).strip()
        result["available"] = True
    else:
        match = _PAGE_LOAD.search(output)
        if match:
            result["selector"] = match.group(1).strip()
            result["page_object"] = match.group(2).strip()
            result["available"] = True

    if not result["page_object"]:
        frame = _FRAME.search(output)
        if frame:
            result["page_object"] = frame.group(1)
    return result


def _norm(selector: str) -> str:
    return page_identity.normalize_selector(selector or "") or (selector or "").strip()


def same_locator(before: Dict, after: Dict) -> bool:
    """Whether two failures are about the same element.

    Element name is the primary key and selector the fallback, deliberately: a
    successful repair changes the selector while keeping the element, so matching
    on the selector alone would call every repaired locator "different" and keep
    edits that fixed nothing.
    """
    if not before.get("available") or not after.get("available"):
        # Nothing identifiable on one side. Treat as the same failure, which is
        # the conservative answer: it reverts, which is the old behaviour.
        return True
    before_element = (before.get("element") or "").strip().lower()
    after_element = (after.get("element") or "").strip().lower()
    if before_element and after_element:
        return before_element == after_element
    return bool(_norm(before.get("selector"))) and \
        _norm(before.get("selector")) == _norm(after.get("selector"))


def describe(failure: Dict) -> str:
    """One phrase naming the failing element, for a log line."""
    if not failure.get("available"):
        return "an unidentifiable failure"
    name = failure.get("element") or failure.get("selector") or "?"
    owner = failure.get("page_object")
    return f"{name!r} in {owner}" if owner else repr(name)


def is_locator_shaped(output: str) -> bool:
    """Whether the failure is an element the test could not act on.

    A compile error, an assertion about a value, or a crash is not a locator to
    heal, and continuing to edit selectors in the face of one only churns.
    """
    return identify(output).get("available", False)


def selectors_of(failures: List[Dict]) -> List[str]:
    """The distinct selectors in a list of failures, in order."""
    seen, out = set(), []
    for failure in failures:
        selector = _norm(failure.get("selector"))
        if selector and selector not in seen:
            seen.add(selector)
            out.append(selector)
    return out
