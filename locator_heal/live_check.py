"""Golden end-to-end against a real site (saucedemo.com).

The fixture corpus is synthetic and I wrote both halves of it. This runs the same
pipeline against a real application with real markup, real CSS frameworks and a
real accessibility tree, mutating its live DOM to simulate drift. Ground truth is
stamped on the elements BEFORE mutation, and capture.py strips `data-gt*`, so the
healer cannot see the answer.

Also the only place we measure latency on a page that is not 37 elements.
"""
from __future__ import annotations
import pathlib, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import yaml
from playwright.sync_api import sync_playwright

from shared import browser_mode
from shared import locator_capture as capture
from shared import locator_resolve as heal_mod
from shared import locator_verify as verify_mod

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE.parent / "config" / "locator.yaml"
BASE = HERE / "baselines"
SITE = "https://www.saucedemo.com/"
OK, BAD, MEH, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

USER_VALUE_JS = ("[...document.querySelectorAll('input')]"
                 ".find(i => i.placeholder === 'Username')?.value")
ERROR_JS = "document.querySelector('[data-test=\"error\"]')?.textContent || ''"
CART_JS = "document.querySelector('.shopping_cart_badge')?.textContent || ''"

LOGIN_TARGETS = {
    "LoginPage#username":  {"raw": "#user-name",    "gt": "username", "action": "fill",
                            "post": {"js": USER_VALUE_JS, "equals": "healcheck"}},
    "LoginPage#password":  {"raw": "#password",     "gt": "password", "action": "fill",
                            "post": {"js": "document.querySelector('#password,[type=password]')?.value",
                                     "equals": "healcheck"}},
    "LoginPage#loginBtn":  {"raw": "#login-button", "gt": "login",    "action": "click",
                            "post": {"js": ERROR_JS, "contains": "required"}},
}
INVENTORY_TARGETS = {
    "Inventory#addBackpack": {"raw": '[data-test="add-to-cart-sauce-labs-backpack"]',
                              "gt": "add_backpack", "action": "click",
                              "post": {"js": CART_JS, "equals": "1"}},
    "Inventory#sortSelect":  {"raw": ".product_sort_container", "gt": "sort", "action": "select",
                              "post": {"js": "document.querySelector('.product_sort_container')?.value",
                                       "contains": ""}},
    "Inventory#cartLink":    {"raw": ".shopping_cart_link", "gt": "cart", "action": "click",
                              "post": {"js": "location.pathname", "contains": "cart"}},
}

STAMP_GT = """(pairs) => {
  for (const [sel, gt] of pairs) {
    const el = document.querySelector(sel);
    if (el) el.setAttribute('data-gt', gt);
  }
}"""

# Drift applied to the live DOM. Each is something a real release does.
MUTATE_LOGIN = """() => {
  const u = document.querySelector('#user-name');
  if (u) { u.id = 'user-name-a91f3c'; }                       // build-hashed id
  const p = document.querySelector('#password');
  if (p) { p.id = 'passwd_field';                             // renamed
           const w = document.createElement('div');           // + wrapper inserted
           p.parentNode.insertBefore(w, p); w.appendChild(p); }
  const b = document.querySelector('#login-button');
  if (b) { b.id = 'signin-submit';                            // renamed + reworded
           b.className = 'css-1x9f2kd';
           if (b.value !== undefined) b.value = 'Sign In'; }
}"""
# Renaming a class in a real build swaps the NAME but keeps the styles. Dropping
# the class outright collapses the element's box, which is a different failure
# (an unclickable element) and not the drift we mean to simulate.
MUTATE_INVENTORY = """() => {
  const rename = (el, cls) => {
    if (!el) return;
    // getComputedStyle returns a LIVE object -- snapshot the values as strings
    // before touching className, or we read back the post-change styles.
    const r = el.getBoundingClientRect();
    const display = String(getComputedStyle(el).display);
    el.className = cls;
    el.style.display = display;
    el.style.width = r.width + 'px';
    el.style.height = r.height + 'px';
  };
  const a = document.querySelector('[data-test="add-to-cart-sauce-labs-backpack"]');
  if (a) a.removeAttribute('data-test');                      // testid dropped
  rename(document.querySelector('.product_sort_container'), 'css-7ka91z');
  rename(document.querySelector('.shopping_cart_link'), 'css-2mn81a');
}"""


def run_page(browser, cfg, name, url, targets, mutate, login=False):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(url, wait_until="domcontentloaded")
    if login:
        page.fill("#user-name", "standard_user")
        page.fill("#password", "secret_sauce")
        page.click("#login-button")
        page.wait_for_url("**/inventory.html", timeout=15000)

    # Ground truth first, then baselines, then drift.
    page.evaluate(STAMP_GT, [[t["raw"], t["gt"]] for t in targets.values()])
    heal_mod.record_baselines(page, page.url, targets, BASE, app="live", cfg=cfg)
    n_elements = len(capture.scorable(capture.snapshot(page)["elements"]))

    def replay(p):
        p.goto(url, wait_until="domcontentloaded")
        if login:
            p.fill("#user-name", "standard_user")
            p.fill("#password", "secret_sauce")
            p.click("#login-button")
            p.wait_for_url("**/inventory.html", timeout=15000)
        p.evaluate(STAMP_GT, [[t["raw"], t["gt"]] for t in targets.values()])
        p.evaluate(mutate)

    print(f"\n{name}  ({n_elements} scorable elements)")
    print("-" * 96)
    results = []
    for lid, spec in targets.items():
        baseline = heal_mod.load_baseline(BASE, "live", lid)
        work = browser.new_page(viewport={"width": 1280, "height": 900})
        replay(work)
        t0 = time.time()
        res = heal_mod.heal(work, baseline, cfg, url, browser=browser, replay=replay,
                            post=verify_mod.post_from_spec(spec.get("post")))
        elapsed = int((time.time() - t0) * 1000)
        work.close()

        good = res.verdict == heal_mod.HEALED and res.picked_gt == spec["gt"]
        col = OK if good else (BAD if res.verdict == heal_mod.HEALED else MEH)
        label = "heal" if good else ("WRONG" if res.verdict == heal_mod.HEALED else "miss")
        detail = (res.emitted.get("java") or res.emitted["sel"])[:46] if res.emitted else res.reason[:46]
        print(f"{col}  {label:<6}{RST}{lid:<26}{res.score:.2f}  {res.tier or res.classification:<13}"
              f"{elapsed:>5}ms  {DIM}{detail}{RST}")
        results.append(good)
    page.close()
    return results, n_elements


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text())
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        try:
            a, n1 = run_page(browser, cfg, "saucedemo /  (login)", SITE,
                             LOGIN_TARGETS, MUTATE_LOGIN)
            b, n2 = run_page(browser, cfg, "saucedemo /inventory.html", SITE,
                             INVENTORY_TARGETS, MUTATE_INVENTORY, login=True)
        finally:
            browser.close()
    good, total = sum(a + b), len(a + b)
    print(f"\n{'=' * 60}")
    col = OK if good == total else BAD
    print(f"{col}  live golden case: {good}/{total} healed to the correct element{RST}")
    print(f"  real-DOM size: {n1} and {n2} scorable elements")
    print("=" * 60)
    return 0 if good == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
