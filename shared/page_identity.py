"""Decide which page a failure DOM actually shows, and how much of a page object
still matches it.

The healing agent's default hypothesis is that a missing element means a stale
locator. That is only true when the test really reached the page its page object
describes. Nothing checked that, so a session that silently expired, a navigation
that never happened, or an error page served in place of the real one all arrived
at the fixer wearing the costume of a broken selector.

The discriminator here is *relational*, so it needs no knowledge of the
application under test: run the page object's own declared locators against the
DOM captured at the moment of failure.

    none of them match      -> we are not on that page at all
    most match, one doesn't -> that one is genuinely stale

`shared/dom_snapshot.py` ranks elements by name similarity to propose
replacements. That list is never empty, which is precisely why it cannot be used
to decide whether a replacement should be proposed at all.

Everything here is best-effort: a selector that cannot be evaluated is reported
as *unevaluable*, never as *unmatched*. Confusing the two would invent evidence.
"""

import re
from typing import Dict, List, Optional

from shared.dom_snapshot import parse_header

# Playwright suffixes that narrow an already-valid CSS selector. Dropping them
# widens the match, which is safe for a coverage count: we only ever ask "does
# this page contain anything shaped like that element".
_NARROWING_SUFFIX = re.compile(
    r"\s*>>\s*(nth=-?\d+|first|last|visible=(true|false))\s*$", re.IGNORECASE)

# Selector syntaxes soupsieve cannot evaluate. These are unevaluable, not absent.
_UNEVALUABLE_PREFIX = ("xpath=", "text=", "//", "(//", "..", "id=", "data-testid=")
_UNEVALUABLE_TOKEN = (">>", ":has-text(", ":text(", ":text-is(", ":visible",
                      ":near(", ":right-of(", ":left-of(", ":above(", ":below(")

# How a Java page object declares a locator. Field name is captured when the
# declaration is an assignment, so coverage can be reported per element.
_LOCATOR_PATTERNS = (
    # avatarWidget = page.locator("img[class*='avatar']").first();
    re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*locator\s*\(\s*"""
               r"""(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    # loginField = By.cssSelector("#login") / By.id("login")
    re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?By\s*\.\s*(?:cssSelector|id)\s*\(\s*"""
               r"""(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    # @FindBy(css = "...") / @FindBy(id = "...") — field name follows the annotation
    re.compile(r"""@FindBy\s*\(\s*(?:css|id)\s*=\s*"""
               r"""(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
)
# The @FindBy form carries no assignment, so its field name is read from the
# declaration that FOLLOWS the annotation.
_FINDBY_FIELD = re.compile(r"\b(?:WebElement|MobileElement|Locator|By)\s+(\w+)")

# An accessor declares its name BEFORE the selector:
#     public Locator loginButton() { return page.locator("#login"); }
# Searching forward here (as @FindBy must) reads the *next* accessor's name and
# pairs every selector with the wrong field — which is worse than no name at all,
# because a caller cannot tell it is wrong.
_ACCESSOR_NAME = re.compile(
    r"\b(?:Locator|WebElement|MobileElement|By)\s+(\w+)\s*\([^)]*\)\s*\{"
    r"(?:[^{}]|\{[^{}]*\})*$")


def _unescape(selector: str) -> str:
    """Java string escapes are not part of the selector.

    `page.locator("[name=\\"user\\"]")` reads out of the source with its
    backslashes still attached, and that string matches nothing and compiles as
    nothing.
    """
    return selector.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")

# Playwright's semantic builders. This framework uses getByRole heavily, so a
# page object made of them must not look like a page object with no locators at
# all — that is indistinguishable from "this file is not a page object", and it
# would silently disable the coverage signal for whole modules.
#
# Role and name are matched approximately: role maps to the tags that carry it,
# and the name is compared against text / aria-label / title / alt the way
# Playwright's default (substring, case-insensitive) does. Approximate matches
# are flagged so the diagnosis can weight them below exact CSS.
_GETBY_PATTERNS = (
    (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByRole\s*\(\s*"""
                r"""(?:[\w.]*AriaRole\s*\.\s*)?(?P<role>\w+)"""), "role"),
    (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByTestId\s*\(\s*"""
                r"""(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "testid"),
    (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByPlaceholder\s*\(\s*"""
                r"""(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "placeholder"),
    (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByText\s*\(\s*"""
                r"""(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "text"),
    (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByLabel\s*\(\s*"""
                r"""(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "label"),
)
# setName(...) usually sits on the line after getByRole(, so it is read from a
# window following the match rather than from the same expression.
_SET_NAME = re.compile(r"""setName\s*\(\s*(["'])(?P<val>(?:\\.|(?!\1).)*)\1""")

_ROLE_TAGS = {
    "LINK": "a[href], [role='link']",
    "BUTTON": "button, [role='button'], input[type='submit'], input[type='button']",
    "TEXTBOX": "input[type='text'], input[type='email'], input[type='search'], "
               "input[type='password'], input:not([type]), textarea, [role='textbox']",
    "CHECKBOX": "input[type='checkbox'], [role='checkbox']",
    "RADIO": "input[type='radio'], [role='radio']",
    "HEADING": "h1, h2, h3, h4, h5, h6, [role='heading']",
    "IMG": "img, [role='img']",
    "LISTITEM": "li, [role='listitem']",
    "COMBOBOX": "select, [role='combobox']",
    "TAB": "[role='tab']",
    "DIALOG": "[role='dialog'], dialog",
    "ALERT": "[role='alert']",
}

# Body-class tokens and page text that suggest a state the test did not intend.
# LAST RESORT ONLY: these are app-specific guesses, unlike coverage, which is
# structural. They break ties; they never decide on their own.
_STATE_CLASS_TOKENS = ("logged-out", "loggedout", "anonymous", "guest",
                       "unauthenticated", "signed-out", "error", "notfound",
                       "not-found", "maintenance")
_STATE_TEXT_MARKERS = (
    "sign in", "log in", "session expired", "session has expired",
    "access denied", "permission denied", "forbidden", "unauthorized",
    "page not found", "404", "500", "service unavailable",
    "something went wrong", "under maintenance", "try again later",
    "verify your identity", "two-factor",
)

# A count is only ever used as "zero / non-zero / a few", so stop early.
_MATCH_LIMIT = 25


# Playwright-MCP snapshot handles. `browser_snapshot` labels every node with an
# ephemeral ref (`e71`, `f2e585`) that means nothing outside that one snapshot.
# Recorded as a selector it compiles fine and matches nothing, forever — which is
# how a generated page object ends up polling `[ref='f2e585']` for 30 seconds.
_ARIA_REF = re.compile(r"(?:^|[\[\s])(?:aria-)?ref\s*=", re.I)
_BARE_REF = re.compile(r"^(?=.*\d)[a-f][0-9a-f]{2,}$", re.I)

# Playwright has no `text` attribute — whoever writes button[text='Save'] means
# :has-text('Save'). As CSS it is syntactically valid and matches nothing, so it
# survives every check that only asks "does this parse?".
_TEXT_ATTR = re.compile(r"\[\s*text\s*=", re.I)


def is_dom_selector(raw: str) -> bool:
    """Whether a recorded locator can actually match in a real browser run.

    Deliberately distinct from normalize_selector(), which asks whether a selector
    can be evaluated against a *parsed snapshot*: `button:has-text("Login")` is a
    perfectly good runtime selector that BeautifulSoup cannot evaluate, and
    `[ref=e71]` is the reverse — evaluable-looking and runtime-useless. Callers
    recording selectors for later code generation want this question, not that one.
    """
    from shared.frameworks import get_active_plugin
    return get_active_plugin().code.is_dom_selector(raw)


def normalize_selector(raw: str) -> Optional[str]:
    """Reduce a recorded locator to plain CSS, or None if it cannot be evaluated.

    None means "we cannot tell", and every caller must keep that distinct from a
    match count of zero.
    """
    from shared.frameworks import get_active_plugin
    return get_active_plugin().code.normalize_selector(raw)


def _compile_ok(soup, selector: str) -> bool:
    try:
        soup.select(selector, limit=1)
        return True
    except Exception:
        return False


def page_facts(snapshot_text: str, soup=None) -> Dict:
    """The identity of the page in a snapshot: what it says it is.

    None of these fields is decisive alone. Together they are what a human reads
    first when asked "wait, what page is this?".
    """
    facts: Dict = {
        "url": "", "captured_at": "", "title": "", "lang": "",
        "body_class": [], "headings": [], "meta_description": "",
        "error": "",
    }
    header = parse_header(snapshot_text or "")
    facts["url"] = header.get("url", "")
    facts["captured_at"] = header.get("capturedAt", "")

    if soup is None:
        soup = parse(snapshot_text)
    if soup is None:
        facts["error"] = "could not parse the DOM snapshot"
        return facts

    try:
        if soup.title and soup.title.string:
            facts["title"] = soup.title.string.strip()[:200]
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            facts["lang"] = str(html_tag.get("lang"))[:20]
        if soup.body is not None:
            body_class = soup.body.get("class") or []
            if isinstance(body_class, str):
                body_class = body_class.split()
            facts["body_class"] = [str(c) for c in body_class][:20]
        facts["headings"] = [
            h.get_text(" ", strip=True)[:100]
            for h in soup.find_all(["h1", "h2"], limit=6)
            if h.get_text(strip=True)
        ]
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            facts["meta_description"] = str(meta["content"])[:200]
    except Exception as exc:
        facts["error"] = f"could not read page identity: {exc}"
    return facts


def parse(snapshot_text: str):
    """Parse a snapshot once so callers can share the tree. None on failure."""
    if not snapshot_text:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(snapshot_text, parser)
        except Exception:
            continue
    return None


def extract_locators(source: str) -> List[Dict[str, str]]:
    """Every locator a page object declares, with its field name where known.

    Reads the page-object source the fix step already loads, so no extra file
    access is needed. Duplicates are collapsed on the raw selector string.
    """
    from shared.frameworks import get_active_plugin
    return get_active_plugin().code.extract_locators(source)


def locator_coverage(locators: List[Dict[str, str]], soup) -> Dict:
    """How many of these locators are present in this DOM.

    `evaluable` counts only the selectors we could genuinely test. A page object
    made entirely of XPath yields evaluable=0, and the caller must abstain rather
    than read that as "nothing matched".
    """
    result = {"matched": 0, "evaluable": 0, "total": len(locators), "details": []}
    if soup is None:
        return result

    for locator in locators:
        selector = locator.get("selector") or ""
        entry = {"name": locator.get("name", ""), "raw": locator.get("raw", ""),
                 "selector": selector, "kind": locator.get("kind", "css"),
                 "approx": bool(locator.get("approx")), "count": None}
        wanted = (locator.get("accessible_name") or "").strip().lower()

        if selector and _compile_ok(soup, selector):
            try:
                nodes = soup.select(selector, limit=_MATCH_LIMIT * 8)
                if wanted:
                    nodes = [n for n in nodes if _has_accessible_name(n, wanted)]
                entry["count"] = len(nodes[:_MATCH_LIMIT])
                result["evaluable"] += 1
                if entry["count"] > 0:
                    result["matched"] += 1
            except Exception:
                entry["count"] = None
        elif locator.get("kind") == "text" and wanted:
            try:
                entry["count"] = len(_nodes_with_text(soup, wanted))
                result["evaluable"] += 1
                if entry["count"] > 0:
                    result["matched"] += 1
            except Exception:
                entry["count"] = None
        result["details"].append(entry)
    return result



def _has_accessible_name(node, wanted: str) -> bool:
    """Approximate Playwright's default name match: substring, case-insensitive."""
    for attr in ("aria-label", "title", "alt", "value", "placeholder"):
        value = node.get(attr)
        if value and wanted in str(value).strip().lower():
            return True
    try:
        return wanted in node.get_text(" ", strip=True).lower()
    except Exception:
        return False


def _nodes_with_text(soup, wanted: str, limit: int = _MATCH_LIMIT):
    """Leaf-ish elements whose own text contains `wanted`."""
    hits = []
    for node in soup.find_all(True, limit=3000):
        if node.find(True) is not None:      # skip containers; keep the leaf
            continue
        if wanted in node.get_text(" ", strip=True).lower():
            hits.append(node)
            if len(hits) >= limit:
                break
    return hits


def page_object_coverage(page_objects: List[Dict], soup) -> List[Dict]:
    """Coverage for each page object supplied, best match first.

    `page_objects` are the dicts the fix step already builds: {path, snippet}.
    The one the test expected is identified by the caller, not here — this only
    reports the numbers.
    """
    reports = []
    for page_object in page_objects or []:
        source = page_object.get("snippet") or page_object.get("source") or ""
        path = page_object.get("path", "")
        coverage = locator_coverage(extract_locators(source), soup)
        coverage["path"] = path
        coverage["name"] = _class_name(path)
        coverage["ratio"] = (coverage["matched"] / coverage["evaluable"]
                             if coverage["evaluable"] else None)
        reports.append(coverage)
    reports.sort(key=lambda c: (c["ratio"] if c["ratio"] is not None else -1,
                                c["evaluable"]), reverse=True)
    return reports


def _class_name(path: str) -> str:
    if not path:
        return ""
    return re.sub(r"\.(java|kt|ts|tsx)$", "", path.replace("\\", "/").split("/")[-1])


def state_markers(facts: Dict, soup) -> List[Dict[str, str]]:
    """Text and class hints that the page is in an unintended state.

    Deliberately the weakest signal in this module. Anything decided from these
    alone would be a guess about someone else's application.
    """
    markers: List[Dict[str, str]] = []

    for token in facts.get("body_class") or []:
        if str(token).lower() in _STATE_CLASS_TOKENS:
            markers.append({"marker": str(token), "where": "body class"})

    haystacks = [("title", facts.get("title", "")),
                 ("meta description", facts.get("meta_description", ""))]
    haystacks += [("heading", h) for h in facts.get("headings") or []]
    for where, text in haystacks:
        lowered = (text or "").lower()
        for marker in _STATE_TEXT_MARKERS:
            if marker in lowered:
                markers.append({"marker": marker, "where": where})

    # Visible sign-in affordances are a strong hint the session is not active,
    # but only when they are links/buttons rather than incidental prose.
    if soup is not None:
        try:
            for node in soup.find_all(["a", "button"], limit=400):
                label = node.get_text(" ", strip=True).lower()
                if label in ("sign in", "log in", "login", "sign up"):
                    markers.append({"marker": label, "where": "link/button"})
                    break
        except Exception:
            pass

    deduped, seen = [], set()
    for marker in markers:
        key = (marker["marker"], marker["where"])
        if key not in seen:
            seen.add(key)
            deduped.append(marker)
    return deduped
