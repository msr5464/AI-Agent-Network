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
from typing import Dict, List, Optional

from shared import page_identity

INTERACTION, PAGE_LOAD = "interaction", "page_load"

# A locator printed through its own toString: "Locator@#save". The selector as it
# appears in source is what follows the `<Type>@`.
_TO_STRING = re.compile(r"^[A-Z][A-Za-z]*@")

# "Failed to click on element 'X' with locator: Locator@sel: Error {" — the
# framework's wording for every interaction failure, and the only line that
# names the element and its selector together.
_INTERACTION = re.compile(
    r"Failed to [\w ]{2,30}?element\s+'([^']+)'\s+with locator:\s*"
    r"(?:Locator@)?(.+?)(?=:\s*Error|\s*$)", re.MULTILINE)

# "Failed to load Element Locator@sel in DashboardPage" — the page-load assertion.
# The selector is everything up to the LAST " in <Class>" on the line, not one
# whitespace-free token: a descendant selector has spaces in it, and so does a
# locator inside an iframe, which prints as a chain of hops. Reading only up to
# the first space matched none of those, and a failure nothing could identify is
# one the fix loop cannot tell from the next.
_PAGE_LOAD = re.compile(
    r"Failed to load Element[ \t]+(.+)[ \t]+in[ \t]+(\w+)[ \t]*$", re.MULTILINE)

# Several anchors print as "Locator@a, Locator@b": split where the next one starts.
_NEXT_ANCHOR = re.compile(r",\s+(?=[A-Z][A-Za-z]*@)")

# The first frame that is a page object, not the test or a helper. A page-load
# assertion is raised from a constructor, whose frame names no method: `<init>`.
_FRAME = re.compile(r"\bat\s+[\w.]*?\.(\w*Page)\.[\w<>$]+\(\1\.java:(\d+)\)")

# What the framework's failure context calls the two shapes.
_CONTEXT_KINDS = {"PAGE_NOT_LOADED": PAGE_LOAD, "ELEMENT_INTERACTION": INTERACTION}


def bare_selector(text: str) -> str:
    """A selector without the `<Type>@` its locator's toString puts in front."""
    return _TO_STRING.sub("", (text or "").strip())


def page_load_anchors(text: str) -> tuple:
    """([selectors], page object) from the first page-load assertion in the text.

    ([], "") when there is none. A page may be waited for on several anchors, so
    the selectors are a list; the first is the one a single-anchor reader wants.
    """
    match = _PAGE_LOAD.search(text or "")
    if not match:
        return [], ""
    anchors = [bare_selector(part) for part in _NEXT_ANCHOR.split(match.group(1).strip())]
    return [a for a in anchors if a], match.group(2).strip()


def identify(output: str) -> Dict:
    """What failed, per the test output. Empty strings where it cannot be told.

    Reads the FIRST interaction failure in the text, not the last: maven repeats
    the same failure in its summary, and a cascading run reports the consequence
    after the cause.
    """
    result = {"element": "", "selector": "", "page_object": "", "kind": "",
              "available": False}
    if not output:
        return result

    match = _INTERACTION.search(output)
    if match:
        result["element"] = match.group(1).strip()
        result["selector"] = match.group(2).strip()
        result["kind"] = INTERACTION
        result["available"] = True
    else:
        anchors, owner = page_load_anchors(output)
        if anchors:
            result["selector"] = anchors[0]
            result["page_object"] = owner
            result["kind"] = PAGE_LOAD
            result["available"] = True

    if not result["page_object"]:
        frame = _FRAME.search(output)
        if frame:
            result["page_object"] = frame.group(1)
    return result


def failing_selector(text: str, context: Optional[Dict] = None) -> str:
    """The selector a failure is about, from the evidence that names one.

    The framework's own message first: it quotes the locator it was acting on.
    Failing that, the anchors it recorded while the element was failing
    (`failure_context.load`). Empty when neither names one, which is an honest
    answer — every later step looks the failure up by this value, so a guess here
    would be worse than a blank.
    """
    found = identify(text).get("selector") or ""
    if found:
        return found
    for anchor in (context or {}).get("anchors") or []:
        selector = bare_selector(str(anchor.get("selector") or ""))
        if selector:
            return selector
    return ""


def of_issue(issue: Dict, context: Optional[Dict] = None) -> Dict:
    """Which element a failure RECORD is about.

    An identity worked out from a run's whole output and carried on the record
    wins: re-reading it from the slice of text a record keeps is how a failure
    that was identified once became unidentifiable later. Then the record's own
    text. Then its structured evidence — the selector it was filed under and what
    the framework's failure context says about it — for a message whose wording
    nothing here recognises.
    """
    carried = issue.get("failure_identity")
    if isinstance(carried, dict) and carried.get("available"):
        return carried
    found = identify(f"{issue.get('error_message') or ''}\n{issue.get('stack_trace') or ''}")
    if found["available"]:
        return found
    selector = (issue.get("failed_selector") or "").strip()
    if not selector:
        return found
    context = context or {}
    return {**found, "selector": selector,
            "page_object": found["page_object"] or context.get("page_object") or "",
            "kind": _CONTEXT_KINDS.get(context.get("kind") or "", ""),
            "available": True}


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
    # A page-load assertion names no element, only the page it was waiting for.
    # Two of them on one page object are the same failure whatever the selector:
    # a replacement anchor that is also absent changes the text of the message
    # and nothing about the outcome, and must not read as the flow moving on.
    if (before.get("kind") == PAGE_LOAD and after.get("kind") == PAGE_LOAD
            and before.get("page_object")
            and before["page_object"] == after.get("page_object")):
        return True
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
