"""Locator synthesis.

The element we matched is not the locator we write. Emitting the raw XPath of
the winning node would "work" and be unmaintainable — and would break again on
the next reshuffle. Walk a preference ladder from most durable to least, and
take the first rung that uniquely identifies the element on the live page.
"""
from __future__ import annotations
import re

from shared import locator_capture as capture
from shared.locator_score import Volatility

ROLE_TO_JAVA = {
    "button": "BUTTON", "link": "LINK", "textbox": "TEXTBOX", "checkbox": "CHECKBOX",
    "radio": "RADIO", "combobox": "COMBOBOX", "listbox": "LISTBOX", "heading": "HEADING",
    "img": "IMG", "option": "OPTION", "tab": "TAB", "dialog": "DIALOG", "list": "LIST",
    "listitem": "LISTITEM", "searchbox": "SEARCHBOX", "slider": "SLIDER",
    "spinbutton": "SPINBUTTON", "table": "TABLE", "menuitem": "MENUITEM",
}
# Text that tends to change on its own: prices, counts, dates, badges. Anchoring
# a locator on any of it trades one kind of brittleness for another.
VOLATILE_TEXT = re.compile(
    r"([$£€¥]\s*[\d,.]+|\b\d[\d,.]*\s*(items?|results?|unread|new)\b|\(\s*\d+\s*\)"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d+\s*(min|sec|hour|day)s?\b)", re.I)


def text_stability(t: str) -> float:
    """Higher is safer to anchor on. Prefers distinctive prose over numbers."""
    if not t:
        return -1.0
    letters = sum(c.isalpha() for c in t)
    digits = sum(c.isdigit() for c in t)
    s = letters / max(len(t), 1)                      # alphabetic is good
    if VOLATILE_TEXT.search(t):
        s -= 1.0                                      # prices/counts/dates: avoid
    if digits > letters:
        s -= 0.5
    if len(t) < 4:
        s -= 0.3                                      # too short to be distinctive
    return s


VOLATILE_SELECTOR = re.compile(
    r"(nth-child|nth-of-type|/html\[|\[\d+\]|css-[0-9a-z]{5,}|jss\d+|sc-[0-9a-z]{6,})", re.I)


def _q(s: str) -> str:
    """Quote for a Java string literal (method arguments)."""
    return '"' + (s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'


def _css(value: str) -> str:
    """Quote an attribute VALUE inside a CSS selector — single quotes by default.

    The selector ends up inside a Java double-quoted string, so double quotes
    here would have to be escaped: `img[alt=\\"PencilSimple\\"]`. That is valid
    Java and valid CSS, and it silently breaks the safety guard in edit_guards,
    which reads selectors back out of the source text and gets the backslashes
    with them — the selector then matches nothing and a correct fix is rejected
    as "a guess". Single quotes need no escaping, and match the convention the
    repo already uses (img[alt='mukesh']).
    """
    from shared.frameworks import get_active_plugin
    return get_active_plugin().code.quote_css_value(value)


def _unique(ctx, sel: str, expect_index: int | None = None, snap: dict | None = None) -> bool:
    """Exactly one match — and, when we know which node we mean, THAT node.

    count()==1 alone is not enough: on a page with three identical product
    buttons, a selector matching exactly one *sibling* passes the count check
    while pointing at the wrong element. That is a silent wrong-heal generator.
    """
    try:
        if ctx.locator(sel).count() != 1:
            return False
    except Exception:
        return False
    if expect_index is None:
        return True
    try:
        n, fp = capture.find_by_locator(ctx, sel, snap=snap)
    except Exception:
        return False
    return n == 1 and fp is not None and fp["index"] == expect_index


def scoped_by_context(ctx, el: dict, expect_index: int, snap: dict | None) -> dict | None:
    """Anchor on a surviving ancestor when the element alone is not unique.

    Real pages repeat a control per section — seven identical edit pencils, one
    per profile block — so nothing about the element itself distinguishes it, and
    the thing that does is where it sits. `#profile-section-profile-summary
    img[alt="PencilSimple"]` is exactly what a human writes here, and it stays
    readable, which a positional XPath does not.

    Tried in order of how much the reader learns from the result: the element's
    own identifying attribute inside a stable ancestor first, then a nearby
    distinguishing text, and only then the bare tag.
    """
    tag = el["tag"]

    anchors: list[str] = []
    for anc in (el.get("ancestor_chain") or [])[:4]:
        if anc.get("testid"):
            anchors.append(f'[data-testid={_css(anc["testid"])}]')
        # An ancestor id is usually the most stable thing in reach and was
        # missing here: without it, a section-scoped control had no anchor at all
        # once its utility classes were (correctly) rejected as volatile.
        if anc.get("id"):
            anchors.append(f'#{anc["id"]}')
        for klass in anc.get("classes") or []:
            anchors.append(f".{klass}")
        if anc.get("tag") and anc["tag"] not in ("div", "span", "body", "main"):
            anchors.append(anc["tag"])
    if not anchors:
        return None

    inners: list[str] = []
    if el.get("testid"):
        inners.append(f'[data-testid={_css(el["testid"])}]')
    for attribute in ("alt", "aria_label", "name", "placeholder", "title"):
        value = el.get(attribute)
        if value:
            inners.append(f'{tag}[{attribute.replace("_", "-")}={_css(value)}]')
    inners.append(tag)

    seen: set[str] = set()
    for anchor in anchors:
        for inner in inners:
            selector = f"{anchor} {inner}"
            if selector in seen:
                continue
            seen.add(selector)
            if _unique(ctx, selector, expect_index, snap):
                return {"strategy": "scoped-by-ancestor", "sel": selector,
                        "python": f"page.locator({_q(selector)})",
                        "java": f"page.locator({_q(selector)})"}

    # Still ambiguous: bring in a nearby text that tells the sections apart.
    texts = sorted((t for t in (el.get("neighbor_texts") or []) if t and len(t) <= 60),
                   key=lambda t: -text_stability(t))[:4]
    from shared.frameworks import get_active_plugin
    for anchor in anchors:
        for text in texts:
            if text_stability(text) < 0:
                continue
            selector = get_active_plugin().code.build_has_text_selector(anchor, text, tag)
            if selector in seen:
                continue
            seen.add(selector)
            if _unique(ctx, selector, expect_index, snap):
                code_snippet = get_active_plugin().code.emit_locator(selector=selector)
                return {"strategy": "scoped-by-neighbor", "sel": selector,
                        "python": code_snippet.get("python", ""),
                        "java": code_snippet.get("java", "")}
    return None


def candidates_for(el: dict, vol: Volatility) -> list[dict]:
    """The preference ladder, most maintainable first."""
    out: list[dict] = []
    tag = el["tag"]
    role, acc = el.get("role"), el.get("accessible_name")

    if el.get("testid"):
        t = el["testid"]
        out.append({"strategy": "testid", "sel": f'[data-testid={_css(t)}]',
                    "python": f"page.get_by_test_id({_q(t)})",
                    "java": f"page.getByTestId({_q(t)})"})

    if role and acc and role not in ("generic", "presentation"):
        jrole = ROLE_TO_JAVA.get(role)
        out.append({
            "strategy": "role+name",
            "sel": f'internal:role={role}[name={_q(acc)}s]',
            "python": f"page.get_by_role({_q(role)}, name={_q(acc)}, exact=True)",
            "java": (f"page.getByRole(AriaRole.{jrole}, new Page.GetByRoleOptions()"
                     f".setName({_q(acc)}).setExact(true))") if jrole else None,
        })

    if el.get("placeholder"):
        v = el["placeholder"]
        out.append({"strategy": "placeholder", "sel": f'[placeholder={_css(v)}]',
                    "python": f"page.get_by_placeholder({_q(v)})",
                    "java": f"page.getByPlaceholder({_q(v)})"})

    if el.get("alt"):
        v = el["alt"]
        out.append({"strategy": "alt", "sel": f'[alt={_css(v)}]',
                    "python": f"page.get_by_alt_text({_q(v)})",
                    "java": f"page.getByAltText({_q(v)})"})

    if el.get("title"):
        v = el["title"]
        out.append({"strategy": "title", "sel": f'[title={_css(v)}]',
                    "python": f"page.get_by_title({_q(v)})",
                    "java": f"page.getByTitle({_q(v)})"})

    _id = el.get("id")
    if _id and not vol.id_is_generated(_id):
        out.append({"strategy": "id", "sel": f"#{_id}",
                    "python": f"page.locator({_q('#' + _id)})",
                    "java": f"page.locator({_q('#' + _id)})"})

    if el.get("name"):
        n = el["name"]
        sel = f'{tag}[name={_css(n)}]'
        out.append({"strategy": "name", "sel": sel,
                    "python": f"page.locator({_q(sel)})",
                    "java": f"page.locator({_q(sel)})"})

    text = (el.get("text") or "").strip()
    if text and len(text) <= 60 and el["is_interactive"]:
        out.append({"strategy": "text", "sel": f'{tag}:text-is({_css(text)})',
                    "python": f"page.get_by_text({_q(text)}, exact=True)",
                    "java": f"page.getByText({_q(text)}, new Page.GetByTextOptions().setExact(true))"})

    stable = vol.stable_classes(el.get("class_list"))
    if stable:
        sel = tag + "".join(f".{c}" for c in stable)
        out.append({"strategy": "css-class", "sel": sel,
                    "python": f"page.locator({_q(sel)})", "java": f"page.locator({_q(sel)})"})

    return out


def robula_xpath(ctx, el: dict, vol: Volatility, expect_index: int | None = None,
                 snap: dict | None = None) -> str | None:
    """Robula+-flavoured minimal XPath: start at //*, add the most durable
    predicate available, climb one ancestor at a time until unique. Last resort —
    an XPath tells the next reader nothing about intent."""
    def predicates(d: dict) -> list[str]:
        p = []
        if d.get("testid"): p.append(f'@data-testid={_css(d["testid"])}')
        if d.get("id") and not vol.id_is_generated(d["id"]): p.append(f'@id={_css(d["id"])}')
        if d.get("name"): p.append(f'@name={_css(d["name"])}')
        if d.get("aria_label"): p.append(f'@aria-label={_css(d["aria_label"])}')
        if d.get("type"): p.append(f'@type={_css(d["type"])}')
        for c in vol.stable_classes(d.get("class_list"))[:1]:
            p.append(f'contains(@class,{_css(c)})')
        return p

    base = f'//{el["tag"]}'
    for pred in predicates(el):
        xp = f"{base}[{pred}]"
        if _unique(ctx, f"xpath={xp}", expect_index, snap):
            return xp
    text = (el.get("text") or "").strip()
    if text and len(text) <= 40:
        xp = f'{base}[normalize-space(.)={_q(text)}]'
        if _unique(ctx, f"xpath={xp}", expect_index, snap):
            return xp
    for anc in el.get("ancestor_chain", [])[:3]:
        for pred in predicates({"id": anc.get("id"), "testid": anc.get("testid"),
                                "class_list": anc.get("classes")}):
            xp = f'//{anc["tag"]}[{pred}]{base}'
            if _unique(ctx, f"xpath={xp}", expect_index, snap):
                return xp
    return None


def alternates(ctx, el: dict, vol: Volatility, snap: dict | None = None,
               n: int = 2, skip: str | None = None) -> list[dict]:
    """The next-best locators that also uniquely identify this element.

    Stored on the fingerprint so the next drift starts from a richer prior than a
    single string: if the primary breaks, these are tried before scoring.
    """
    out, idx = [], el.get("index")
    for cand in candidates_for(el, vol):
        if len(out) >= n:
            break
        if cand["sel"] == skip or VOLATILE_SELECTOR.search(cand["sel"]):
            continue
        if _unique(ctx, cand["sel"], idx, snap):
            out.append(_flag(cand))
    return out


def _flag(cand: dict) -> dict:
    """Mark a locator that matches today but embeds self-changing text.

    Emitting it is still better than the broken one, but the reviewer should see
    that `getByRole(LINK, "Cart (0 items)")` breaks the next time the cart is not
    empty.
    """
    hit = VOLATILE_TEXT.search(cand["sel"])
    if hit:
        cand["fragile"] = (f"embeds self-changing text {hit.group(0)!r} — "
                           f"consider a stable test id here")
    return cand


def emit(ctx, el: dict, vol: Volatility, snap: dict | None = None) -> dict | None:
    """First rung of the ladder that uniquely identifies THIS element."""
    idx = el.get("index")
    for cand in candidates_for(el, vol):
        if VOLATILE_SELECTOR.search(cand["sel"]):
            continue
        if _unique(ctx, cand["sel"], idx, snap):
            return _flag(cand)
    scoped = scoped_by_context(ctx, el, idx, snap) if idx is not None else None
    if scoped:
        return _flag(scoped)
    xp = robula_xpath(ctx, el, vol, idx, snap)
    if xp:
        # Reaching XPath means nothing semantic identified this element. Usually
        # that is an accessibility gap in the app, and saying so is more useful
        # than silently emitting a structural locator.
        hint = None
        if not el.get("accessible_name") and not el.get("testid"):
            hint = (f"<{el['tag']}> has no accessible name and no test id, so only a "
                    f"structural locator was possible — an aria-label would fix both "
                    f"this and the screen-reader experience")
        return {"strategy": "xpath", "sel": f"xpath={xp}", "fragile": hint,
                "python": f"page.locator({_q('xpath=' + xp)})",
                "java": f"page.locator({_q('xpath=' + xp)})"}
    return None


def synthesize(ctx, el: dict, expect_index: int | None = None, snap: dict | None = None) -> dict | None:
    """The best selector for this element. None if it could not be uniquely identified.

    `expect_index` checks the generated selector actually resolves to the exact
    node we started with (identified by index in the flat snapshot).
    """
    from shared.frameworks import get_active_plugin
    code_engine = get_active_plugin().code
    
    if el.get("testid") and _unique(ctx, f'[data-testid={_css(el["testid"])}]', expect_index, snap):
        code_snippet = code_engine.emit_locator(testid=el["testid"])
        return {"strategy": "testid", "sel": f'[data-testid={_css(el["testid"])}]',
                "python": code_snippet.get("python", ""),
                "java": code_snippet.get("java", "")}

    tag = el.get("tag", "")
    role = el.get("role", "")
    text = el.get("inner_text") or el.get("value") or ""
    
    jrole = code_engine.map_role(role)
    if role and jrole and text and len(text) < 40 and text_stability(text) >= 0:
        # getByRole allows a role filter and a text filter in one call, which is
        # the single most durable way to identify an element.
        test_sel = f"{tag}:has-text({_css(text)})" if text else tag
        if _unique(ctx, test_sel, expect_index, snap):
            code_snippet = code_engine.emit_locator(role=jrole, name=text, exact=True)
            return {"strategy": "role-name", "sel": test_sel,
                    "python": code_snippet.get("python", ""),
                    "java": code_snippet.get("java", "")}

    for attribute in ("placeholder", "alt", "aria_label", "title", "name"):
        v = el.get(attribute)
        if not v or len(v) > 60:
            continue
        sel = f"{tag}[{attribute.replace('_', '-')}={_css(v)}]"
        if _unique(ctx, sel, expect_index, snap):
            if attribute == "placeholder":
                code_snippet = code_engine.emit_locator(placeholder=v)
                return {"strategy": "placeholder", "sel": sel,
                        "python": code_snippet.get("python", ""),
                        "java": code_snippet.get("java", "")}
            elif attribute in ("aria_label", "title", "alt"):
                code_snippet = code_engine.emit_locator(label=v)
                return {"strategy": "label", "sel": sel,
                        "python": code_snippet.get("python", ""),
                        "java": code_snippet.get("java", "")}
            code_snippet = code_engine.emit_locator(selector=sel)
            return {"strategy": "attribute", "sel": sel,
                    "python": code_snippet.get("python", ""),
                    "java": code_snippet.get("java", "")}

    if text and len(text) < 40 and text_stability(text) >= 0:
        sel = f"{tag}:has-text({_css(text)})"
        if _unique(ctx, sel, expect_index, snap):
            code_snippet = code_engine.emit_locator(text=text, exact=True)
            return {"strategy": "text", "sel": sel,
                    "python": code_snippet.get("python", ""),
                    "java": code_snippet.get("java", "")}

    if el.get("id") and not VOLATILE_SELECTOR.search(el["id"]) and _unique(ctx, f'#{el["id"]}', expect_index, snap):
        code_snippet = code_engine.emit_locator(selector=f'#{el["id"]}')
        return {"strategy": "id", "sel": f'#{el["id"]}',
                "python": code_snippet.get("python", ""),
                "java": code_snippet.get("java", "")}

    classes = [c for c in (el.get("classes") or []) if not VOLATILE_SELECTOR.search(c)]
    if classes:
        sel = tag + "".join(f".{c}" for c in classes)
        if _unique(ctx, sel, expect_index, snap):
            code_snippet = code_engine.emit_locator(selector=sel)
            return {"strategy": "class", "sel": sel,
                    "python": code_snippet.get("python", ""),
                    "java": code_snippet.get("java", "")}

    scoped = scoped_by_context(ctx, el, expect_index, snap) if expect_index is not None else None
    if scoped:
        return scoped

    return None
