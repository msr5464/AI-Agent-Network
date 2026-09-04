"""Locate and distil the DOM captured at the moment a test failed.

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
    if not selector or soup is None:
        return None
    # Java string escapes are not part of the selector. Read straight out of the
    # source a locator arrives as [alt=\\"PencilSimple\\"], which compiles as
    # nothing — so an escaped selector used to be skipped as unevaluable, exactly
    # like :has-text() was.
    from shared.page_identity import _unescape, normalize_selector as _normalize_selector
    selector = _unescape(selector)

    text_clause = None
    match = _HAS_TEXT_RE.search(selector)
    if match:
        text_clause = _norm_text(match.group(2)).lower()
        selector = _HAS_TEXT_RE.sub("", selector).strip()
        if not selector:
            return None

    normalized = _normalize_selector(selector)
    if not normalized:
        return None
    try:
        nodes = soup.select(normalized)
    except Exception:
        return None

    if text_clause is not None:
        nodes = [n for n in nodes
                 if text_clause in _norm_text(n.get_text(" ", strip=True)).lower()]
    if not nodes:
        return 0, 0

    # Only a positive visibility record counts. An element the capture never saw
    # is unknown, not hidden, and rejecting on unknown would block correct fixes
    # whenever the sidecar and the markup disagree.
    visible = {_fp_signature(e) for e in (fingerprints.get("elements") or [])
               if e.get("is_visible")}
    return len(nodes), sum(1 for n in nodes if _node_signature(n) in visible)


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


def _fp_selector(element: Dict, scopes: List[str]) -> str:
    """A selector for a captured element, scoped to a surviving container.

    Scoping is what makes it unique: this page carries seven identical pencil
    icons, and only one of them is inside the profile-summary section.
    """
    tag = element.get("tag") or "*"
    if element.get("testid"):
        core = f'[data-testid="{element["testid"]}"]'
    elif element.get("id"):
        core = f'#{element["id"]}'
    elif element.get("name"):
        core = f'{tag}[name="{element["name"]}"]'
    elif element.get("aria_label"):
        core = f'{tag}[aria-label="{element["aria_label"]}"]'
    elif element.get("alt"):
        core = f'{tag}[alt="{element["alt"]}"]'
    elif element.get("placeholder"):
        core = f'{tag}[placeholder="{element["placeholder"]}"]'
    elif element.get("text"):
        core = f'{tag}:has-text("{_norm_text(element["text"])[:40]}")'
    else:
        core = tag
    if core.startswith("#") or core.startswith("[data-testid"):
        return core                      # already unique on its own
    scope = next((s for s in scopes if _SAFE_ID.fullmatch(s)), "")
    return f"#{scope} {core}" if scope else core


_SAFE_ID = re.compile(r"[A-Za-z][\w-]*")

# Last element type named in a selector: "#box img[alt='x']" -> img
_TARGET_TAG = re.compile(r"([a-zA-Z][\w-]*)\s*(?:\[[^\]]*\])*\s*$")


def candidates_from_fingerprints(fingerprints: Dict, element_names: Optional[List[str]] = None,
                                 failed_selector: str = "", max_elements: int = 30) -> Dict:
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
    pool = [e for e in (fingerprints.get("elements") or [])
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
               "suggested_selector": _fp_selector(element, scopes)}
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
            return f"[{attr}='{element[attr]}']"
    if element.get("id"):
        return f"#{element['id']}" if re.fullmatch(r'[A-Za-z][\w-]*', element["id"]) \
            else f"[id='{element['id']}']"
    if element.get("name"):
        return f"[name='{element['name']}']"
    if element.get("aria-label"):
        return f"[aria-label='{element['aria-label']}']"
    if element.get("placeholder"):
        return f"[placeholder='{element['placeholder']}']"
    if element.get("text"):
        return f"{element['tag']}:has-text(\"{element['text'][:40]}\")"
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

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        result["error"] = "beautifulsoup4 not installed — cannot distil the DOM snapshot"
        return result

    try:
        soup = BeautifulSoup(snapshot_text, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(snapshot_text, "html.parser")
        except Exception as exc:
            result["error"] = f"could not parse the DOM snapshot: {exc}"
            return result

    elements = _collect_elements(soup)
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
        result["likely_matches"] = [
            {**el, "suggested_selector": suggest_selector(el)}
            for el in ranked if score(el) > 0
        ][:8]
    else:
        ranked = elements

    result["elements"] = [
        {**el, "suggested_selector": suggest_selector(el)} for el in ranked[:max_elements]
    ]
    return result


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
            if k not in ("tag", "text", "suggested_selector")
        )
        open_tag = f'<{element["tag"]} {attrs}>' if attrs else f'<{element["tag"]}>'
        text = f' — text: "{element["text"]}"' if element.get("text") else ""
        return (f'  {open_tag}{text}\n'
                f'      suggested selector: {element["suggested_selector"]}')

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
