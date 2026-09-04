"""Generate the v2 drifted-page corpus from v1/app.html.

Every element carries a `data-gt` ground-truth marker that survives mutation.
The healer never sees it (capture.py strips `data-gt*`); eval.py uses it to ask
"did you land on the element that actually is the old one?".

Each case declares the verdict the pipeline is *supposed* to reach. The negative
cases matter more than the positive ones: they are what proves the thing is
trustworthy rather than merely enthusiastic.
"""
import json, pathlib, re, sys
from bs4 import BeautifulSoup

HERE = pathlib.Path(__file__).parent
V1 = HERE / "v1" / "app.html"
V2 = HERE / "v2"

# The "page object" under test. Deliberately mixes strategies — real page
# objects do, and each strategy fails differently.
LOCATORS = {
    "LoginPage#usernameField":     {"raw": "#user-name",                    "gt": "username",     "action": "fill", "post": {"js": "(() => { const l = [...document.querySelectorAll('label')].find(x => /user|benutzer/i.test(x.textContent)); if (!l) return null; const el = (l.htmlFor && document.getElementById(l.htmlFor)) || l.parentElement.querySelector('input'); return el ? el.value : null; })()", "equals": "healcheck"}},
    "LoginPage#usernameByName":    {"raw": '[name="user-name"]',            "gt": "username",     "action": "fill", "post": {"js": "(() => { const l = [...document.querySelectorAll('label')].find(x => /user|benutzer/i.test(x.textContent)); if (!l) return null; const el = (l.htmlFor && document.getElementById(l.htmlFor)) || l.parentElement.querySelector('input'); return el ? el.value : null; })()", "equals": "healcheck"}},
    "LoginPage#usernameNested":    {"raw": '#login_button_container .form_group > input[type="text"]',
                                                                            "gt": "username",     "action": "fill", "post": {"js": "(() => { const l = [...document.querySelectorAll('label')].find(x => /user|benutzer/i.test(x.textContent)); if (!l) return null; const el = (l.htmlFor && document.getElementById(l.htmlFor)) || l.parentElement.querySelector('input'); return el ? el.value : null; })()", "equals": "healcheck"}},
    "LoginPage#loginButton":       {"raw": "button#login-button",           "gt": "login",        "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Login attempted"}},
    "LoginPage#loginByText":       {"raw": 'button:has-text("Login")',      "gt": "login",        "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Login attempted"}},
    "LoginPage#loginNested":       {"raw": "#login_button_container form button", "gt": "login",  "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Login attempted"}},
    "ProductsPage#addBackpack":    {"raw": '[data-testid="add-backpack"]',  "gt": "add_backpack", "action": "click", "post": {"js": "(document.querySelector('[data-cart]')||{dataset:{}}).dataset.lastAdded", "equals": "Sauce Labs Backpack"}},
    "ProductsPage#sortDropdown":   {"raw": ".product_sort_container",       "gt": "sort",         "action": "select", "post": {"js": "document.getElementById('app-status').textContent", "contains": "Sorted:"}},
    "HeaderPage#cartLink":         {"raw": ".shopping_cart_link",           "gt": "cart",         "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Cart opened"}},
    "ProfilePage#saveButton":      {"raw": "#save-btn",                     "gt": "save",         "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Profile saved"}},
    "ProfilePage#saveByText":      {"raw": 'button:text-is("Save changes")', "gt": "save",        "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Profile saved"}},
    "ProfilePage#saveNested":      {"raw": "main > section#profile_section > button", "gt": "save", "action": "click", "post": {"js": "document.getElementById('app-status').textContent", "equals": "Profile saved"}},
    # Used by a VERIFY step, not an interaction. Must never be healed: rewriting
    # what a test checks is how a healer hides the bug it was meant to catch.
    "HeaderPage#cartCount":        {"raw": "[data-cart]", "gt": "cart", "action": "assert",
                                    "usage": "assertion"},
    # Pinned to utility classes -- the exact shape of the real failing locator.
    "SummaryPage#editButton":      {"raw": 'div.rounded-2xl button[type="submit"]',
                                    "gt": "edit_summary", "action": "click",
                                    "post": {"js": "document.getElementById('app-status').textContent", "equals": "Summary editor opened"}},
    "ProductsPage#firstProduct":   {"raw": ".grid > .inventory_item:nth-child(1) button", "gt": "add_backpack", "action": "click", "post": {"js": "(document.querySelector('[data-cart]')||{dataset:{}}).dataset.lastAdded", "equals": "Sauce Labs Backpack"}},
}

def soup():
    return BeautifulSoup(V1.read_text(), "html.parser")

def gt(s, marker):
    el = s.find(attrs={"data-gt": marker})
    if el is None:
        raise SystemExit(f"fixture bug: no element with data-gt={marker}")
    return el

# ---------------------------------------------------------------- mutations
# Each returns the mutated soup. Signature: (soup) -> None (mutates in place).

def m_id_renamed(s):
    gt(s, "username")["id"] = "username-field"
    lbl = s.find("label", attrs={"for": "user-name"}); lbl["for"] = "username-field"

def m_id_hashed(s):
    gt(s, "username")["id"] = "user-name-a3f9c2e1"
    s.find("label", attrs={"for": "user-name"})["for"] = "user-name-a3f9c2e1"

def m_class_renamed(s):
    gt(s, "sort")["class"] = ["sort-select"]

def m_class_hashed(s):
    """Styled-components rebuild: every class becomes a content hash."""
    gt(s, "cart")["class"] = ["css-1qx9v2f"]
    for i, el in enumerate(s.select(".btn_primary, .btn_secondary")):
        el["class"] = [f"css-{i}k2n9df"]

def m_tag_swapped(s):
    """<button> becomes <a role=button> — the classic component-library migration."""
    b = gt(s, "login")
    a = s.new_tag("a", href="#", attrs={"role": "button"})
    a["class"] = b.get("class"); a["id"] = b.get("id")
    a["data-gt"] = "login"; a.string = b.get_text()
    b.replace_with(a)

def m_text_reworded(s):
    gt(s, "login").string = "Sign in"

def m_text_recased(s):
    gt(s, "save").string = "SAVE CHANGES"

def m_dom_moved(s):
    """Login button leaves the <form> for a new sibling container."""
    b = gt(s, "login").extract()
    holder = s.new_tag("div"); holder["class"] = ["form_actions"]
    holder.append(b)
    s.find("section", id="login_button_container").append(holder)

def m_wrapper_inserted(s):
    """An extra div per field — breaks `>` chains and nth-child indexes."""
    for grp in s.select(".form_group"):
        wrap = s.new_tag("div"); wrap["class"] = ["field-shell"]
        for child in list(grp.children):
            wrap.append(child.extract())
        grp.append(wrap)

def m_attr_removed(s):
    del gt(s, "username")["name"]

def m_testid_removed(s):
    del gt(s, "add_backpack")["data-testid"]

def m_moved_into_modal(s):
    """Profile moves inside a dialog wrapper — new ancestry, same element."""
    sec = s.find("section", id="profile_section").extract()
    dlg = s.new_tag("div", attrs={"role": "dialog", "aria-label": "Edit profile"})
    inner = s.new_tag("div"); inner["class"] = ["modal_body"]
    inner.append(sec); dlg.append(inner)
    s.find("main").append(dlg)

# ------------------------------------------------------------ negative cases

def m_element_removed(s):
    """Whole Profile feature deleted. Correct answer: don't heal, report removal."""
    s.find("section", id="profile_section").decompose()

def m_element_replaced(s):
    """THE TRAP. Same slot, same styling, same shape — opposite meaning.
    A position/structure-driven healer binds the Save test to Delete account."""
    b = gt(s, "save")
    new = s.new_tag("button", id="delete-account-btn", attrs={"name": "delete"})
    new["class"] = b.get("class"); new.string = "Delete account"
    b.replace_with(new)

def m_wrong_page(s):
    """App bounced us to a session-expired screen. Nothing here is healable."""
    s.find("main").decompose()
    for el in s.select("header a, header button"):
        el.decompose()
    main = s.new_tag("main")
    main.append(BeautifulSoup(
        '<section class="panel"><h2>Session expired</h2>'
        '<p>Please sign in again to continue.</p>'
        '<button class="btn_primary" id="relogin">Sign in again</button></section>',
        "html.parser"))
    s.find("header").insert_after(main)

def m_element_disabled(s):
    """Element is right there, just disabled and covered. Not a locator problem."""
    b = gt(s, "save"); b["disabled"] = "disabled"
    overlay = BeautifulSoup(
        '<div style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:9"></div>',
        "html.parser")
    s.find("main").append(overlay)

def m_element_duplicated(s):
    """Two identical Add-to-cart buttons for the same product. Ambiguity, not drift."""
    b = gt(s, "add_backpack")
    twin = BeautifulSoup(str(b), "html.parser").find("button")
    del twin["data-gt"]
    b.parent.append(twin)

# ---------------------------------------------------------------- holdout set
# Written AFTER the weights and thresholds were fixed, and never tuned against.
# A corpus you calibrate on cannot tell you whether the thing generalises.

def h_compound_drift(s):
    """Three changes at once: id renamed, classes hashed, wrapper inserted."""
    m_id_renamed(s)
    for i, el in enumerate(s.select(".form_input")):
        el["class"] = [f"css-{i}f8k2m1"]
    m_wrapper_inserted(s)

def h_icon_relabelled(s):
    """Icon-only link: aria-label reworded and class hashed. Only href survives."""
    c = gt(s, "cart")
    c["aria-label"] = "Cart (0 items)"
    c["class"] = ["css-9kd21f"]

def h_siblings_reordered(s):
    """Products reordered. Position now lies; only content tells the truth."""
    grid = s.select_one(".grid")
    items = grid.find_all("div", class_="inventory_item")
    for it in reversed(items):
        grid.append(it.extract())

def h_translated(s):
    """UI translated to German. Text and accessible name change; id/name hold."""
    gt(s, "login").string = "Anmelden"
    s.find("label", attrs={"for": "user-name"}).string = "Benutzername"
    gt(s, "username")["placeholder"] = "Benutzername"

def h_deep_restructure(s):
    """Semantic HTML rewritten as divs. Every ancestor changes; ids/labels hold."""
    sec = s.find("section", id="login_button_container")
    form = sec.find("form")
    form.name = "div"
    for grp in sec.select(".form_group"):
        grp.name = "span"
        grp["class"] = ["fld"]
    sec.name = "article"

def h_assertion_target(s):
    """The element an assertion reads is renamed. Healable in principle --
    and refused on purpose."""
    c = gt(s, "cart")
    c["data-cart-count"] = c.get("data-cart", "0")
    del c["data-cart"]

def h_decoy_added(s):
    """A pixel-identical twin of the Profile panel appears and the original's id
    is renamed. The enclosing section id survives, so the twin is excludable by
    structure -- this should heal, and heal to the original."""
    import copy
    sec = s.find("section", id="profile_section")
    twin = copy.copy(sec)
    twin_html = str(sec).replace('id="profile_section"', 'id="profile_section_2"') \
                        .replace('id="save-btn"', 'id="save-btn-2"') \
                        .replace('id="display-name"', 'id="display-name-2"') \
                        .replace('for="display-name"', 'for="display-name-2"') \
                        .replace('data-gt="save"', '') \
                        .replace('data-gt="displayname"', '')
    from bs4 import BeautifulSoup as BS
    sec.insert_after(BS(twin_html, "html.parser"))
    gt(s, "save")["id"] = "persist-btn"

def h_utility_class_churn(s):
    """A restyle rewrites the utility classes. The element is untouched -- only
    the styling decision the locator was pinned to has changed. This is the
    shape of the real NaukriProfilePage failure."""
    box = gt(s, "summary_box")
    box["class"] = ["rounded-3xl", "bg-gray-100", "px-5", "py-3"]
    btn = gt(s, "edit_summary")
    btn["class"] = ["bg-neutral-900", "text-gray-50", "px-4", "py-2"]


def h_decoy_no_anchor(s):
    """Same twin, but every enclosing id is renamed too, so no ancestor from the
    green run survives. Nothing structural separates the two candidates and
    nothing textual does either -- refusing is the only honest answer."""
    h_decoy_added(s)
    s.find("section", id="profile_section")["id"] = "panel_a"
    s.find("section", id="profile_section_2")["id"] = "panel_b"


HOLDOUT = [
    ("compound_drift",    h_compound_drift,    "LoginPage#usernameField",   "HEAL", None),
    ("icon_relabelled",   h_icon_relabelled,   "HeaderPage#cartLink",       "HEAL", None),
    ("siblings_reordered", h_siblings_reordered, "ProductsPage#firstProduct", "NO_HEAL", "MISBOUND"),
    ("translated",        h_translated,        "LoginPage#loginByText",     "HEAL", None),
    ("deep_restructure",  h_deep_restructure,  "LoginPage#usernameNested",  "HEAL", None),
    ("utility_class_churn", h_utility_class_churn, "SummaryPage#editButton",  "HEAL", None),
    ("decoy_added",       h_decoy_added,       "ProfilePage#saveButton",    "HEAL", None),
    ("decoy_no_anchor",   h_decoy_no_anchor,   "ProfilePage#saveButton",    "NO_HEAL", "LOW_CONFIDENCE"),
    ("assertion_target",  h_assertion_target,  "HeaderPage#cartCount",      "NO_HEAL", "ASSERTION_LOCATOR"),
]


CASES = [
    # name, mutation, target locator, expected verdict, expected reason
    ("id_renamed",         m_id_renamed,         "LoginPage#usernameField",   "HEAL", None),
    ("id_hashed",          m_id_hashed,          "LoginPage#usernameField",   "HEAL", None),
    ("class_renamed",      m_class_renamed,      "ProductsPage#sortDropdown", "HEAL", None),
    ("class_hashed",       m_class_hashed,       "HeaderPage#cartLink",       "HEAL", None),
    ("tag_swapped",        m_tag_swapped,        "LoginPage#loginButton",     "HEAL", None),
    ("text_reworded",      m_text_reworded,      "LoginPage#loginByText",     "HEAL", None),
    ("text_recased",       m_text_recased,       "ProfilePage#saveByText",    "HEAL", None),
    ("dom_moved",          m_dom_moved,          "LoginPage#loginNested",     "HEAL", None),
    ("wrapper_inserted",   m_wrapper_inserted,   "LoginPage#usernameNested",  "HEAL", None),
    ("attr_removed",       m_attr_removed,       "LoginPage#usernameByName",  "HEAL", None),
    ("testid_removed",     m_testid_removed,     "ProductsPage#addBackpack",  "HEAL", None),
    ("moved_into_modal",   m_moved_into_modal,   "ProfilePage#saveNested",    "HEAL", None),

    ("element_removed",    m_element_removed,    "ProfilePage#saveButton",    "NO_HEAL", "FEATURE_REMOVED"),
    ("element_replaced",   m_element_replaced,   "ProfilePage#saveButton",    "NO_HEAL", "LOW_CONFIDENCE"),
    ("wrong_page",         m_wrong_page,         "ProfilePage#saveButton",    "NO_HEAL", "WRONG_STATE"),
    ("element_disabled",   m_element_disabled,   "ProfilePage#saveButton",    "NO_HEAL", "NOT_LOCATOR"),
    ("element_duplicated", m_element_duplicated, "ProductsPage#addBackpack",  "NO_HEAL", "AMBIGUOUS"),
]

CASES += HOLDOUT

PO_HEADER = """package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/** Generated fixture page object -- the file the healer patches. */
public class %s {

    private final Page page;

    public %s(Page page) {
        this.page = page;
    }
"""


def write_page_objects(out: pathlib.Path) -> int:
    """Emit Java page objects mirroring LOCATORS, so Phase G has real files to
    edit rather than a simulated string buffer."""
    out.mkdir(parents=True, exist_ok=True)
    by_class: dict[str, list[tuple[str, dict]]] = {}
    for lid, spec in LOCATORS.items():
        cls, field = lid.split("#")
        by_class.setdefault(cls, []).append((field, spec))
    for cls, fields in by_class.items():
        body = [PO_HEADER % (cls, cls)]
        for field, spec in fields:
            java = spec["raw"].replace("\\", "\\\\").replace('"', '\\"')
            body.append(f'\n    public Locator {field}() {{\n'
                        f'        return page.locator("{java}");\n'
                        f'    }}\n')
        body.append("}\n")
        (out / f"{cls}.java").write_text("".join(body))
    return len(by_class)


def main():
    V2.mkdir(exist_ok=True)
    for f in V2.glob("*.html"):
        f.unlink()
    cases = []
    for name, mut, target, expect, reason in CASES:
        s = soup()
        mut(s)
        (V2 / f"{name}.html").write_text(str(s))
        cases.append({
            "name": name, "file": f"v2/{name}.html", "target": target,
            "expect": expect, "expect_reason": reason,
            "expect_gt": LOCATORS[target]["gt"] if expect == "HEAL" else None,
            "doc": (mut.__doc__ or "").strip().split("\n")[0],
            "holdout": name in {h[0] for h in HOLDOUT},
        })
    (HERE / "manifest.json").write_text(json.dumps(
        {"baseline": "v1/app.html", "locators": LOCATORS, "cases": cases}, indent=2))
    n_po = write_page_objects(HERE / "pageobjects")
    print(f"wrote {n_po} java page objects -> {HERE / 'pageobjects'}")
    pos = sum(1 for c in cases if c["expect"] == "HEAL")
    print(f"generated {len(cases)} cases -> {V2}  ({pos} positive, {len(cases)-pos} negative)")

if __name__ == "__main__":
    main()
