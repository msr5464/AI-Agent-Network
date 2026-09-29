"""Locate and distil the DOM captured at the moment a test failed.

**Agent instrument, not target-repo code.** This drives a browser the AGENTS
open to inspect a page — reading the DOM, counting how many elements a candidate
selector matches, trying a corrected locator before any code is edited. It is
deliberately always Playwright, whatever framework the repository under test
uses, because it speaks CDP and a browser does not know or care what drove it
there. (Selenium 4 exposes the same DevTools port, which is what lets a
Selenium-launched browser be inspected this way.)

So the direct `sync_playwright` / Playwright-selector usage below is correct and
must NOT be routed through shared/frameworks. What IS pluggable is the syntax
written into the target repo — see shared/frameworks/base.py CodeEngine.

The automation framework writes the page's rendered HTML to
`{resultsDirectory}/dom/{testcaseName}_{HHmmss}.html` when a test fails
(`BrowserHelper.captureDomSnapshot`). That file is the only artefact that shows
the page exactly as the test saw it — right session, right test data, right step
of the flow — which is what makes a broken locator diagnosable without replaying
the flow to get back there.

test-triaging-agent finds the file and copies it into its audit session;
test-healing-agent distils it into the handful of elements worth putting in a
prompt. Both use this module so the two halves cannot drift.
"""

import html
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from shared import frames

_HEADER_RE = re.compile(r'<!--\s*qa-agent-network:dom-snapshot(.*?)-->', re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# Playwright text pseudo-classes, which BeautifulSoup cannot compile.
_HAS_TEXT_RE = re.compile(r""":(?:has-)?text(?:-is)?\(\s*(["'])(.*?)\1\s*\)""")

# How much text identifies an element. Long enough to separate siblings, short
# enough to survive the capture's own truncation.
_SIG_TEXT = 60

# Attributes worth showing: enough to build a selector from, nothing else.
_IDENTIFYING_ATTRS = (
    "data-testid", "data-test", "data-cy", "id", "name", "aria-label",
    "placeholder", "type", "role", "href", "title",
)

_INTERACTIVE_TAGS = ("input", "button", "a", "select", "textarea", "label",
                     "option", "form", "summary")

# Tokens too generic to carry signal when matching an element name.
_STOPWORDS = {"the", "and", "for", "with", "page", "element", "field", "button",
              "link", "text", "input", "web", "popup", "pop", "header", "label"}


def find_snapshot(report_dir: Path, method_name: str,
                  not_before: Optional[float] = None) -> Optional[Path]:
    """Newest DOM snapshot for this test method under report_dir, if any.

    `not_before` is an epoch-seconds floor, normally when the run started. The
    name alone cannot distinguish this run's snapshot from any earlier run's, so
    without it a run that produced none quietly inherits an old one.
    """
    if not method_name or not report_dir or not Path(report_dir).exists():
        return None
    matches = [p for p in Path(report_dir).rglob(f"dom/{method_name}_*.html") if p.is_file()]
    if not matches:
        # Some CI layouts flatten the directory — fall back to a name match.
        matches = [p for p in Path(report_dir).rglob(f"{method_name}_*.html") if p.is_file()]
    if not_before is not None:
        matches = [p for p in matches if p.stat().st_mtime >= not_before]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def parse_header(text: str) -> Dict[str, str]:
    """Read the url / test / capturedAt written into the snapshot's header."""
    match = _HEADER_RE.search(text[:2000])
    if not match:
        return {}
    return {k: v for k, v in _ATTR_RE.findall(match.group(1))}


def load_fingerprints(snapshot_path) -> Dict:
    """The element fingerprints captured beside a DOM snapshot, or {}.

    `BrowserHelper.captureDomSnapshot` runs LocatorCapture over the live page and
    writes the result to a `.fingerprints.json` sidecar, naming it in the
    snapshot's header. Those records carry what the saved HTML cannot: computed
    visibility, bounding boxes and ARIA roles. Re-deriving them from the markup is
    not merely harder, it is impossible — BeautifulSoup has no layout engine.
    """
    if not snapshot_path:
        return {}
    snapshot = Path(snapshot_path)
    if not snapshot.exists():
        return {}
    try:
        header = parse_header(snapshot.read_text(encoding="utf-8", errors="ignore")[:2000])
    except OSError:
        return {}
    sidecar = header.get("fingerprints") or ""
    if not sidecar or not Path(sidecar).exists():
        return {}
    try:
        return json.loads(Path(sidecar).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _norm_text(value: str) -> str:
    return " ".join((value or "").split())[:_SIG_TEXT]


def _fp_signature(element: Dict) -> tuple:
    return (element.get("tag") or "", element.get("id") or "",
            element.get("testid") or "", element.get("alt") or "",
            element.get("aria_label") or "", _norm_text(element.get("text")))


def _node_signature(node) -> tuple:
    return (node.name or "", node.get("id") or "",
            node.get("data-testid") or node.get("data-test") or "",
            node.get("alt") or "", node.get("aria-label") or "",
            _norm_text(node.get_text(" ", strip=True)))


def selector_visibility(selector: str, soup, fingerprints: Dict) -> Optional[tuple]:
    """(matches, visible_matches) for a selector, or None when undecidable.

    None and (0, 0) are different answers and callers must keep them apart: the
    first means we could not evaluate the selector, the second that we evaluated
    it and it matched nothing.

    `:has-text()` is why this exists rather than a plain `soup.select()`. It is a
    Playwright pseudo-class BeautifulSoup cannot compile, so `normalize_selector`
    returns None for it and every check downstream skipped such a selector
    silently — which is how a fix pointing at an invisible button reached a
    90-second Maven run. Splitting the text clause off and matching it against the
    captured text restores the check.
    """
    found = select_nodes(selector, soup)
    if found is None:
        return None
    nodes, doc = found
    if not nodes:
        return 0, 0
    if doc is not soup:
        fingerprints = getattr(doc, "qa_prints", None) or {}

    # Only a positive visibility record counts. An element the capture never saw
    # is unknown, not hidden, and rejecting on unknown would block correct fixes
    # whenever the sidecar and the markup disagree.
    visible = {_fp_signature(e) for e in (fingerprints.get("elements") or [])
               if e.get("is_visible")}
    if doc is not soup and not fingerprints.get("elements"):
        # A frame whose own capture failed says nothing about visibility, and the
        # page's capture cannot stand in for it: unknown, so not held against it.
        return len(nodes), len(nodes)
    return len(nodes), sum(1 for n in nodes if _node_signature(n) in visible)


def select_nodes(selector: str, soup) -> Optional[tuple]:
    """(nodes, doc) a selector matches in a snapshot, in the frame document it
    names; None when undecidable. `doc` is None when the frame is not there.
    """
    if not selector or soup is None:
        return None
    # Java string escapes are not part of the selector. Read straight out of the
    # source a locator arrives as [alt=\\"PencilSimple\\"], which compiles as
    # nothing — so an escaped selector used to be skipped as unevaluable, exactly
    # like :has-text() was.
    from shared.page_identity import _unescape, normalize_selector as _normalize_selector
    selector = _unescape(selector)

    text_clause, exact = None, False
    match = _HAS_TEXT_RE.search(selector)
    if match:
        # :text-is() is Playwright's exact, case-sensitive match; :has-text() and
        # :text() match a substring. Counting all three as a substring made
        # `button:text-is('Login')` two buttons ("Use OTP to Login") instead of one.
        exact = "text-is" in match.group(0)
        text_clause = (" ".join(match.group(2).split()) if exact
                       else _norm_text(match.group(2)).lower())
        selector = _HAS_TEXT_RE.sub("", selector).strip()
        if not selector:
            return None

    normalized = _normalize_selector(selector)
    if not normalized:
        return None
    # Inside an iframe, the element is looked for in that frame's own document and
    # judged visible by that frame's own capture.
    from shared.page_identity import in_frame
    try:
        doc, inner = in_frame(soup, normalized)
        if doc is False:
            return [], None
        if doc is None:
            return None
        nodes = doc.select(inner)
    except Exception:
        return None

    if text_clause is not None and exact:
        # get_text() with no separator is textContent, which is what Playwright compares.
        nodes = [n for n in nodes if " ".join(n.get_text().split()) == text_clause]
    elif text_clause is not None:
        nodes = [n for n in nodes
                 if text_clause in _norm_text(n.get_text(" ", strip=True)).lower()]
    return nodes, doc


# Input types that say what a field holds. `text` and `number` say nothing.
_TELLING_TYPES = ("email", "tel", "password", "url", "date", "search")
_FIELD_BOX = ("tr", "li", "fieldset")


def field_context(node, doc) -> str:
    """What a form field says about itself, the way a user reads it: its label,
    aria-label, placeholder, name, id, a telling type, the row or group it sits in
    and, inside a table, its column header.

    The column header is not optional. A cart's amount input sat in a row naming
    only the product; "Amount" was its column's header.
    """
    parts = [node.get(a) or "" for a in ("aria-label", "placeholder", "name", "id", "title")]
    if (node.get("type") or "").lower() in _TELLING_TYPES:
        parts.append(node["type"])
    if node.get("id") and doc is not None:
        label = doc.find("label", attrs={"for": node["id"]})
        if label is not None:
            parts.append(label.get_text(" ", strip=True))
    wrapping = node.find_parent("label")
    if wrapping is not None:
        parts.append(wrapping.get_text(" ", strip=True))
    box = node.find_parent(_FIELD_BOX) or node.find_parent(
        class_=re.compile(r"field|form-group", re.I))
    if box is not None:
        parts.append(box.get_text(" ", strip=True))
    cell, row, table = node.find_parent(["td", "th"]), node.find_parent("tr"), node.find_parent("table")
    if cell is not None and row is not None and table is not None:
        cells = row.find_all(["td", "th"], recursive=False)
        heads = next((r.find_all(["td", "th"], recursive=False) for r in table.find_all("tr")
                      if r.find("th", recursive=False) is not None), [])
        if cell in cells and cells.index(cell) < len(heads):
            parts.append(heads[cells.index(cell)].get_text(" ", strip=True))
    return " ".join(p for p in parts if p).strip()


# Containers named in a selector, used to scope candidates to the region the
# broken locator was pointing at.
_SCOPE_RE = re.compile(r'#([A-Za-z][\w-]*)|\[data-testid=["\']?([^"\'\]]+)')


def _scopes_in(selector: str) -> List[str]:
    """Container ids / testids the failing selector was scoped to."""
    found = []
    for cid, testid in _SCOPE_RE.findall(selector or ""):
        if cid or testid:
            found.append(cid or testid)
    return found


def _in_scope(element: Dict, scopes: List[str]) -> bool:
    if not scopes:
        return False
    for ancestor in element.get("ancestor_chain") or []:
        if ancestor.get("id") in scopes or ancestor.get("testid") in scopes:
            return True
    return element.get("id") in scopes or element.get("testid") in scopes


def _css_value(value: str) -> str:
    """Quote a CSS value as locator_emit._css does: single quotes unless the value
    holds one. The model copies these suggestions into a Java string, where double
    quotes would arrive escaped."""
    from shared.frameworks import get_active_plugin
    return get_active_plugin().code.quote_css_value(value)


def _fp_selector(element: Dict, scopes: List[str]) -> str:
    """A selector for a captured element, scoped to a surviving container.

    Scoping is what makes it unique: this page carries seven identical pencil
    icons, and only one of them is inside the profile-summary section.
    """
    tag = element.get("tag") or "*"
    if element.get("testid"):
        core = f'[data-testid={_css_value(element["testid"])}]'
    elif element.get("id"):
        core = f'#{element["id"]}'
    elif element.get("name"):
        core = f'{tag}[name={_css_value(element["name"])}]'
    elif element.get("aria_label"):
        core = f'{tag}[aria-label={_css_value(element["aria_label"])}]'
    elif element.get("alt"):
        core = f'{tag}[alt={_css_value(element["alt"])}]'
    elif element.get("placeholder"):
        core = f'{tag}[placeholder={_css_value(element["placeholder"])}]'
    elif element.get("text"):
        text = " ".join(element["text"].split())
        # Exact text is what tells "Login" from "Use OTP to Login"; has-text is a
        # substring match and picks up both. Too long to quote whole → a prefix.
        core = (f'{tag}:text-is({_css_value(text)})' if len(text) <= 40
                else f'{tag}:has-text({_css_value(text[:40].rstrip())})')
    else:
        core = tag
    if core.startswith("#") or core.startswith("[data-testid"):
        return core                      # already unique on its own
    # Only a container this element actually sits in. The failing selector's
    # own id is often the thing that vanished (`#random-text-locator`), and
    # prefixing it made every candidate match nothing. Failing that, the
    # element's nearest id'd ancestor: it exists, because it was captured.
    chain = [a["id"] for a in element.get("ancestor_chain") or []
             if a.get("id") and _SAFE_ID.fullmatch(a["id"])]
    scope = next((s for s in scopes if s in chain), "") or next(iter(chain), "")
    return f"#{scope} {core}" if scope else core


_SAFE_ID = re.compile(r"[A-Za-z][\w-]*")

# Last element type named in a selector: "#box img[alt='x']" -> img
_TARGET_TAG = re.compile(r"([a-zA-Z][\w-]*)\s*(?:\[[^\]]*\])*\s*$")


def candidates_from_fingerprints(fingerprints: Dict, element_names: Optional[List[str]] = None,
                                 failed_selector: str = "", max_elements: int = 30,
                                 soup=None) -> Dict:
    """Candidate elements taken from the capture rather than the saved markup.

    `distill()` reads the HTML, which cannot say what was visible and describes
    elements only by the attributes it happens to look for. On the page this was
    written against it offered three candidates, all of them wrong, and could not
    represent the right one at all: the target was an <img> carrying only an
    `alt`, and neither `img` nor `alt` is in its lists. The capture has every
    element with its computed visibility, so the pool is both truthful and
    complete.

    Ranking here is ordering for a prompt, not a search: in the region the broken
    selector pointed at, then name overlap, then interactive. Finding *which*
    element the selector meant is the Locate step's job and is not repeated here.
    """
    result: Dict = {"url": fingerprints.get("url", ""), "captured_at": "", "test": "",
                    "elements": [], "likely_matches": [], "total_elements": 0,
                    "error": "", "source": "fingerprints"}
    # A selector that failed inside an iframe is replaced by an element of that
    # iframe, so the candidates are that frame's capture, and every suggestion
    # keeps the way in.
    frame_path, failed_inner = frames.split(failed_selector)
    captured = fingerprints
    if frame_path and soup is not None:
        from shared.page_identity import in_frame
        try:
            doc = in_frame(soup, failed_selector)[0]
        except Exception:
            doc = None
        if doc in (None, False):
            return {**result, "error": "the iframe the selector points into was not captured"}
        captured, failed_selector = getattr(doc, "qa_prints", None) or {}, failed_inner
    pool = [e for e in (captured.get("elements") or [])
            if e.get("is_visible") and (e.get("area_norm") or 0) > 0
            and e.get("tag") not in ("body", "main", "html")]
    if not pool:
        return {**result, "error": "no visible elements were captured"}
    result["total_elements"] = len(pool)

    scopes = _scopes_in(failed_selector)
    tokens: List[str] = []
    for name in (element_names or []):
        tokens.extend(_tokenize(name))
    tokens = list(dict.fromkeys(tokens))

    def overlap(element: Dict) -> int:
        hay = " ".join(str(v) for k, v in element.items()
                       if k in ("tag", "id", "testid", "alt", "aria_label", "role",
                                "accessible_name", "text", "name")).lower()
        return sum(1 for t in tokens if t in hay)

    # The tag the broken selector was aiming at. A redesign usually renames or
    # restyles an element without changing what kind of thing it is, so an <img>
    # that stopped matching is far more likely replaced by another <img> than by
    # the <div> that happens to contain the same words.
    want_tag = _TARGET_TAG.search(failed_selector or "")
    want_tag = want_tag.group(1).lower() if want_tag else ""

    def rank(element: Dict) -> tuple:
        return (0 if _in_scope(element, scopes) else 1,
                0 if want_tag and (element.get("tag") or "").lower() == want_tag else 1,
                -overlap(element),
                0 if element.get("is_interactive") else 1,
                # Prefer the icon over the container that wraps it: a click lands
                # on the smallest thing that carries the affordance.
                element.get("area_norm") or 0)

    ranked = sorted(pool, key=rank)

    def render(element: Dict) -> Dict:
        out = {"tag": element.get("tag") or "", "visible": True,
               "suggested_selector": frames.join(frame_path, _fp_selector(element, scopes))}
        if frame_path:
            out["inside_iframe"] = " > ".join(frame_path)
        for src, dst in (("id", "id"), ("testid", "data-testid"), ("alt", "alt"),
                         ("aria_label", "aria-label"), ("role", "role"),
                         ("name", "name"), ("placeholder", "placeholder"),
                         ("accessible_name", "accessible-name")):
            if element.get(src):
                out[dst] = str(element[src])[:120]
        if element.get("text"):
            out["text"] = _norm_text(element["text"])
        if _in_scope(element, scopes):
            out["in_failing_scope"] = True
        # The same counter the guard uses, so the model sees up front which
        # suggestions the guard would refuse for matching more than one element.
        counted = selector_visibility(out["suggested_selector"], soup, fingerprints)
        if counted:
            out["matches"] = counted[0]
        return out

    in_scope = [e for e in ranked if _in_scope(e, scopes)]
    result["likely_matches"] = [render(e) for e in (in_scope or ranked)[:8]]
    result["elements"] = [render(e) for e in ranked[:max_elements]]
    return result


def _tokenize(name: str) -> List[str]:
    """Split an element name into comparable tokens.

    Handles both of the shapes failures report: "Block Reason PopUp Header" and
    "blockReasonPopUpHeader".
    """
    bare = name.split(":")[-1]
    words = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+', bare)
    return [w.lower() for w in words if len(w) >= 3 and w.lower() not in _STOPWORDS]


def suggest_selector(element: Dict[str, str]) -> str:
    """Propose a selector for an element, following the project's priority order."""
    for attr in ("data-testid", "data-test", "data-cy"):
        if element.get(attr):
            return f"[{attr}={_css_value(element[attr])}]"
    if element.get("id"):
        return f"#{element['id']}" if re.fullmatch(r'[A-Za-z][\w-]*', element["id"]) \
            else f"[id={_css_value(element['id'])}]"
    if element.get("name"):
        return f"[name={_css_value(element['name'])}]"
    if element.get("aria-label"):
        return f"[aria-label={_css_value(element['aria-label'])}]"
    if element.get("placeholder"):
        return f"[placeholder={_css_value(element['placeholder'])}]"
    if element.get("text"):
        return f"{element['tag']}:has-text({_css_value(element['text'][:40])})"
    return element.get("tag", "")


def _collect_elements(soup) -> List[Dict[str, str]]:
    seen = set()
    collected: List[Dict[str, str]] = []
    candidates = list(soup.find_all(_INTERACTIVE_TAGS))
    candidates += soup.find_all(attrs={"role": True})
    candidates += soup.find_all(attrs={"data-testid": True})
    candidates += soup.find_all(attrs={"data-test": True})
    candidates += soup.find_all(attrs={"data-cy": True})

    for node in candidates:
        if id(node) in seen:
            continue
        seen.add(id(node))
        element = {"tag": node.name}
        for attr in _IDENTIFYING_ATTRS:
            value = node.get(attr)
            if isinstance(value, list):
                value = " ".join(value)
            if value:
                element[attr] = str(value)[:120]
        text = node.get_text(" ", strip=True)
        if text:
            element["text"] = text[:60]
        # An element with nothing identifying about it cannot be located anyway.
        if len(element) > 1:
            collected.append(element)
    return collected


def distill(snapshot_text: str, element_names: Optional[List[str]] = None,
            max_elements: int = 30) -> Dict:
    """Reduce a full page DOM to the elements worth showing a fixer.

    Elements matching the failing element's name are ranked first, so the most
    likely replacement is visible even when the page has hundreds of nodes.
    """
    header = parse_header(snapshot_text)
    result: Dict = {
        "url": header.get("url", ""),
        "captured_at": header.get("capturedAt", ""),
        "test": header.get("test", ""),
        "elements": [],
        "likely_matches": [],
        "total_elements": 0,
        "error": "",
    }

    from shared.page_identity import parse
    soup = parse(snapshot_text)
    if soup is None:
        result["error"] = "could not parse the DOM snapshot (is beautifulsoup4 installed?)"
        return result

    elements = _collect_elements(soup)
    # The iframes captured beside the page. Their elements are as clickable as the
    # page's own, and a fixer shown only the top document cannot see them at all.
    for path, doc in _frame_docs(soup):
        for element in _collect_elements(doc):
            element["inside_iframe"] = " > ".join(h or "iframe (no stable selector)"
                                                  for h in path)
            element["_frame_path"] = None if None in path else path
            elements.append(element)
    result["total_elements"] = len(elements)

    tokens: List[str] = []
    for name in (element_names or []):
        tokens.extend(_tokenize(name))
    tokens = list(dict.fromkeys(tokens))

    def score(element: Dict[str, str]) -> int:
        haystack = " ".join(str(v) for v in element.values()).lower()
        return sum(1 for token in tokens if token in haystack)

    if tokens:
        ranked = sorted(elements, key=score, reverse=True)
        result["likely_matches"] = [_suggested(el) for el in ranked if score(el) > 0][:8]
    else:
        ranked = elements

    result["elements"] = [_suggested(el) for el in ranked[:max_elements]]
    return result


def _suggested(element: Dict) -> Dict:
    out = {k: v for k, v in element.items() if k != "_frame_path"}
    selector = suggest_selector(element)
    path = element.get("_frame_path")
    if element.get("inside_iframe") and not path:
        # Its iframe cannot be told apart, so no selector reaches it reliably, and a
        # bare one would search the top document.
        selector = "(none — its iframe has no stable unique selector)"
    out["suggested_selector"] = frames.join(path, selector) if path and selector else selector
    return out


_VOLATILE = re.compile(r"\d{4,}|[0-9a-f]{8,}", re.I)


def _host_selector(host, doc) -> Optional[str]:
    """A plain unique selector for a captured <iframe>, or None.

    For showing a fixer the way in, not for proving anything: it tries only the
    attributes that are stable by nature, where shared/frames.py LINK_JS — which
    needs a live page — tries more.
    """
    tried = []
    for attr in ("id", "title", "name"):
        value = host.get(attr)
        if value and not _VOLATILE.search(value):
            tried.append(f"#{value}" if attr == "id" and re.fullmatch(r"[A-Za-z][\w-]*", value)
                         else f"{host.name}[{attr}={_css_value(value)}]")
    # A per-load name keeps its stem: popup_1790441440177 -> [name^='popup_'].
    stem = re.match(r"\D{3,}", host.get("name") or "")
    if stem and _VOLATILE.search(host.get("name")):
        tried.append(f"{host.name}[name^={_css_value(stem.group(0))}]")
    for selector in tried:
        try:
            if len(doc.select(selector)) == 1:
                return selector
        except Exception:
            continue
    return None


def _frame_docs(doc, path=()):
    """(hop selectors, document) for every captured iframe below `doc`, depth first."""
    for host in doc.find_all(["iframe", "frame"]):
        child = getattr(host, "qa_frame", None)
        if child is not None:
            hops = path + (_host_selector(host, doc),)
            yield list(hops), child
            yield from _frame_docs(child, hops)


def format_for_prompt(distilled: Dict, max_chars: int = 6000) -> str:
    """Render the distilled snapshot as the prompt section a fixer reads."""
    if distilled.get("error"):
        return f"(DOM snapshot could not be read: {distilled['error']})"

    lines: List[str] = []
    if distilled.get("url"):
        lines.append(f"Page URL at failure: {distilled['url']}")
    if distilled.get("captured_at"):
        lines.append(f"Captured at: {distilled['captured_at']}")
    from_capture = distilled.get("source") == "fingerprints"
    lines.append(
        f"Visible elements captured on the page: {distilled.get('total_elements', 0)}"
        if from_capture else
        f"Interactive elements on the page: {distilled.get('total_elements', 0)}")
    lines.append("")

    def key(element: Dict[str, str]) -> tuple:
        return tuple(sorted((k, v) for k, v in element.items()
                            if k != "suggested_selector"))

    def render(element: Dict[str, str]) -> str:
        attrs = " ".join(
            f'{k}="{html.escape(str(v), quote=False)}"'
            for k, v in element.items()
            if k not in ("tag", "text", "suggested_selector", "matches")
        )
        open_tag = f'<{element["tag"]} {attrs}>' if attrs else f'<{element["tag"]}>'
        text = f' — text: "{element["text"]}"' if element.get("text") else ""
        matches = element.get("matches")
        count = ("" if matches is None else "  (unique)" if matches == 1 else
                 f"  (matches {matches} elements — NOT unique; Playwright will refuse it)")
        return (f'  {open_tag}{text}\n'
                f'      suggested selector: {element["suggested_selector"]}{count}')

    shown = set()
    if distilled.get("likely_matches"):
        lines.append(
            "Candidates from the page as the browser saw it at the moment of "
            "failure. Every one was VISIBLE — anything hidden has already been "
            "removed, because a hidden element times out exactly as the original "
            "did. Ordered by the region the broken selector pointed at, then by "
            "matching its element type, then by name. Order is a hint, not "
            "evidence: check the page identity above before treating any of "
            "these as a replacement:"
            if from_capture else
            "Candidate elements, ranked by name similarity to the missing "
            "one. Similarity is not evidence that this is the right page — "
            "check the page identity above before treating any of these as "
            "a replacement:")
        for element in distilled["likely_matches"]:
            lines.append(render(element))
            shown.add(key(element))
        lines.append("")

    # The two lists are built from separate dict copies, so dedupe on content —
    # identity would let every ranked match appear twice.
    others = [el for el in distilled.get("elements", []) if key(el) not in shown]
    if others:
        lines.append("Other interactive elements present:")
        lines.extend(render(el) for el in others)

    text = "\n".join(lines)
    return text[:max_chars] + ("\n  … (truncated)" if len(text) > max_chars else "")
