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
import re
from pathlib import Path
from typing import Dict, List, Optional

_HEADER_RE = re.compile(r'<!--\s*qa-agent-network:dom-snapshot(.*?)-->', re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

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


def find_snapshot(report_dir: Path, method_name: str) -> Optional[Path]:
    """Newest DOM snapshot for this test method under report_dir, if any."""
    if not method_name or not report_dir or not Path(report_dir).exists():
        return None
    matches = [p for p in Path(report_dir).rglob(f"dom/{method_name}_*.html") if p.is_file()]
    if not matches:
        # Some CI layouts flatten the directory — fall back to a name match.
        matches = [p for p in Path(report_dir).rglob(f"{method_name}_*.html") if p.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def parse_header(text: str) -> Dict[str, str]:
    """Read the url / test / capturedAt written into the snapshot's header."""
    match = _HEADER_RE.search(text[:2000])
    if not match:
        return {}
    return {k: v for k, v in _ATTR_RE.findall(match.group(1))}


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
    lines.append(f"Interactive elements on the page: {distilled.get('total_elements', 0)}")
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
        lines.append("MOST LIKELY REPLACEMENTS for the missing element "
                     "(ranked by name similarity):")
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
