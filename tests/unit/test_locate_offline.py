"""Locate resolves a broken locator from the failure capture, with no browser.

The run this pins: five failing tests, one broken locator (`#login-mukesh` for
`LoginPage#loginButton`), and Locate spent 184 seconds resolving none of them. It
was logging into the application to recreate the failure so it could click the
candidate; the login timed out five times, once per test, on a locator it had
already answered four times over.

Both halves are covered here: the answer comes out of the fingerprint the
framework captured at failure, and one locator is resolved once however many
tests it broke.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pytest.importorskip("bs4")


def _load(env: dict):
    for key, value in env.items():
        os.environ[key] = str(value)
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_locate.py"
    spec = importlib.util.spec_from_file_location("healing_locate_offline", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_locate_offline"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Fixtures: a page object, its baseline, and the DOM captured when it broke ──

LOGIN_PAGE = '''
public class LoginPage extends BasePage {
    private final Locator usernameField;
    private final Locator loginButton;
    public LoginPage(Config config) {
        usernameField = page.locator("#user-name");
        loginButton = page.locator("#login-mukesh");
    }
    public void doLogin(String u, String p) {
        fillText(usernameField, u, "Username field");
        click(loginButton, "Login button");
    }
}
'''

HTML = """<!-- qa-agent-network:dom-snapshot test="loginAndVerifyProductsPage" \
url="https://shop.example.com/" capturedAt="2026-09-23T16:09:03" fingerprints="{sidecar}" -->
<!DOCTYPE html><html lang="en"><head><title>Swag Labs</title></head>
<body class="">
  <form>
    <input id="user-name" name="user-name" type="text" data-test="username" aria-label="Username">
    {button}
  </form>
</body></html>
"""

BUTTON_HTML = ('<input id="{id}" name="login-button" type="submit"{testid} '
               'value="Login">')


def _el(index, tag, **kw):
    """One captured element, with every field the scorer reads."""
    el = {"index": index, "tag": tag, "id": None, "name": None, "type": None,
          "class_list": [], "role": None, "accessible_name": "", "aria_label": None,
          "placeholder": None, "alt": None, "href": None, "title": None,
          "testid": None, "text": "", "attrs": {}, "is_interactive": True,
          "is_visible": True, "is_enabled": True,
          "abs_xpath": f"/html[1]/body[1]/form[1]/input[{index}]",
          "id_xpath": "", "ancestor_chain": [{"tag": "form", "id": None,
                                              "classes": [], "role": "form",
                                              "testid": None}],
          "sibling_index": index, "sibling_count": 2,
          "bbox_norm": {"x": 0.4, "y": 0.2, "w": 0.15, "h": 0.04},
          "area_norm": 0.006, "aspect": 5.9, "neighbor_texts": ["Swag Labs"]}
    el.update(kw)
    return el


USERNAME = _el(1, "input", id="user-name", name="user-name", type="text",
               testid="username", role="textbox", accessible_name="Username",
               aria_label="Username", id_xpath="//*[@id='user-name']",
               attrs={"id": "user-name", "name": "user-name", "type": "text",
                      "data-test": "username", "aria-label": "Username"})
LOGIN_BUTTON = _el(2, "input", id="login-button", name="login-button", type="submit",
                   testid="login-button", role="button", accessible_name="Login",
                   id_xpath="//*[@id='login-button']",
                   attrs={"id": "login-button", "name": "login-button",
                          "type": "submit", "data-test": "login-button",
                          "value": "Login"})

# What it looked like on the last good run. The baseline is recorded from the
# page, not from the source, so it describes the real element even when the
# selector in the page object never matched it — which is the common case: a bad
# edit, a merge, a typo. `#login-mukesh` is nowhere in here, deliberately.
BASELINE_FINGERPRINT = LOGIN_BUTTON

# The other shape of the same break: the application renamed the id. Everything
# that says what the element IS survives; only the handle changed.
# No test id on this one: the id tier is only reached by an element that has
# nothing stronger, which is exactly the element a renamed id matters for.
RENAMED = dict(LOGIN_BUTTON, id="signin-button", testid=None,
               id_xpath="//*[@id='signin-button']",
               attrs={"id": "signin-button", "name": "login-button",
                      "type": "submit", "value": "Login"})

TESTS = ["addProductToCart", "loginAndVerifyProductsPage", "simulateWebLifecycle",
         "simulateHybridApiAndWebLifecycle", "verifyProductAppearsInCart"]


def _capture(tmp_path, name, elements, button_id, testid=True):
    """A failure DOM and its fingerprint sidecar, written as the framework does."""
    sidecar = tmp_path / f"{name}.fingerprints.json"
    sidecar.write_text(json.dumps({"url": "https://shop.example.com/",
                                   "elements": elements}))
    snapshot = tmp_path / f"{name}.html"
    snapshot.write_text(HTML.format(
        sidecar=sidecar,
        button=BUTTON_HTML.format(
            id=button_id,
            testid=' data-test="login-button"' if testid else "") if button_id else ""))
    return snapshot


@pytest.fixture
def world(tmp_path):
    """A workspace, a baseline, a failure capture, and a five-test handoff."""
    repo = tmp_path / "repo"
    java = repo / "src" / "main" / "java" / "automation" / "modules" / "shop" / "web"
    java.mkdir(parents=True)
    (java / "LoginPage.java").write_text(LOGIN_PAGE)

    baselines = tmp_path / "baselines" / "shop"
    baselines.mkdir(parents=True)
    (baselines / "LoginPage.json").write_text(json.dumps({
        "pageObject": "LoginPage", "module": "shop",
        "recordedAt": "2026-09-01T10:00:00",
        "urlShape": "https://shop.example.com/", "title": "Swag Labs", "bodyClass": "",
        "coverage": {"usernameField": 1, "loginButton": 1},
        "landmarks": ["form:Login"],
        "fingerprints": {"usernameField": USERNAME, "loginButton": BASELINE_FINGERPRINT},
    }))

    dom = tmp_path / "dom"
    dom.mkdir()
    sidecar = dom / "login.fingerprints.json"
    sidecar.write_text(json.dumps(
        {"url": "https://shop.example.com/", "elements": [USERNAME, LOGIN_BUTTON]}))
    snapshot = dom / "login.html"
    snapshot.write_text(HTML.format(
        sidecar=sidecar,
        button=BUTTON_HTML.format(id="login-button",
                                  testid=' data-test="login-button"')))

    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps({"build_tag": "local", "automation_issues": [
        {"test_name": f"automation.shop.ShopWebTest.{name}",
         "failed_selector": "#login-mukesh",
         "failure_url": "https://shop.example.com/",
         "error_message": "Failed to click on element 'Login button' with locator: "
                          "Locator@#login-mukesh",
         "stack_trace": "at automation.modules.shop.web.LoginPage.doLogin(LoginPage.java:9)",
         "dom_snapshot": str(snapshot)}
        for name in TESTS]}))

    audit = tmp_path / "audit"
    audit.mkdir()
    mod = _load({"AUDIT_DIR": audit, "HANDOFF_FILE": handoff,
                 "WORKSPACE_DIR": tmp_path, "GITHUB_REPO_AUTOMATION": "repo",
                 "FRAMEWORK_DIR": "", "HEALING_BASELINE_DIR": tmp_path / "baselines",
                 "HEALING_LOCATE_MODE": "enforce"})
    return {"mod": mod, "repo": repo, "audit": audit, "snapshot": snapshot,
            "sidecar": sidecar, "tmp": tmp_path}


def _one(world, **overrides):
    mod = world["mod"]
    sources = overrides.pop("_sources", {"LoginPage": LOGIN_PAGE})
    assertions = overrides.pop("_assertions", {})
    issue = {"test_name": "automation.shop.ShopWebTest.loginAndVerifyProductsPage",
             "failed_selector": "#login-mukesh",
             "failure_url": "https://shop.example.com/",
             "error_message": "Failed to click on element 'Login button' with "
                              "locator: Locator@#login-mukesh",
             "stack_trace": "at automation.modules.shop.web.LoginPage.doLogin"
                            "(LoginPage.java:9)",
             "dom_snapshot": str(world["snapshot"])}
    mode = overrides.pop("_mode", None)
    issue.update(overrides)
    cfg = mod.yaml.safe_load(mod.CONFIG_FILE.read_text())
    if mode:
        cfg["classify"]["heal_assertions"] = mode
    return mod.locate_one(issue, sources, assertions, cfg,
                          mod.Volatility(cfg), world["repo"])


class TestResolvingFromTheCapture:
    def test_the_renamed_element_is_found_without_a_browser(self, world):
        record = _one(world)
        assert record["verdict"] == "HEALED"
        assert record["new_locator"] == "[data-test='login-button']"
        assert record["new_expression"] == (
            'page.locator("[data-test=\'login-button\']")')
        assert record["page_object"] == "LoginPage"
        assert record["field"] == "loginButton"
        assert record["score"] > 0.75

    def test_it_never_claims_to_have_executed_anything(self, world):
        # Fix applies the edit and runs the real test. Claiming a proof this step
        # did not perform is how an unverified fix is reported as a verified one.
        assert "capture" in _one(world)["verification"]

    def test_the_test_id_is_spelled_the_way_the_page_spells_it(self, world):
        # This page writes data-test. Emitting [data-testid=...] — or its Java
        # equivalent getByTestId, which looks for data-testid — matches nothing,
        # and the whole strongest tier silently drops out of the ladder.
        expression = _one(world)["new_expression"]
        assert "data-testid" not in expression
        assert "data-test='login-button'" in expression

    def test_a_renamed_id_is_followed_by_what_the_element_still_is(self, world, tmp_path):
        record = _one(world, dom_snapshot=str(
            _capture(tmp_path, "renamed", [USERNAME, RENAMED], "signin-button",
                     testid=False)))
        assert record["verdict"] == "HEALED"
        assert record["new_locator"] == "#signin-button"

    def test_an_element_that_is_genuinely_gone_is_refused(self, world, tmp_path):
        record = _one(world, dom_snapshot=str(
            _capture(tmp_path, "gone", [USERNAME], "")))
        assert record["verdict"] != "HEALED"
        assert "new_expression" not in record

    def test_no_failing_selector_says_what_is_missing_not_what_it_means(self, world):
        # An absent selector is a gap in the evidence. It used to be reported as
        # "not a locator failure", two lines under a diagnosis of a stale locator.
        record = _one(world, failed_selector="")
        assert record["verdict"] == "SKIPPED"
        assert record["reason"] == "no failing selector recorded — nothing to locate"

    def test_no_capture_is_a_refusal_not_a_crash(self, world):
        record = _one(world, dom_snapshot="")
        assert record["verdict"] == "NO_CAPTURE"

    def test_a_locator_healed_too_often_asks_for_a_test_id(self, world, tmp_path):
        path = tmp_path / "baselines" / "shop" / "LoginPage.json"
        record = json.loads(path.read_text())
        record["healHistory"] = {"loginButton": [
            {"healedAt": "2026-09-2{}T10:00:00+00:00".format(n), "to": "#x", "score": 1}
            for n in range(3)]}
        path.write_text(json.dumps(record))
        assert _one(world)["classification"] == "UNSTABLE_LOCATOR"


SEEN_BY = {"loginButton": 'assertElementVisible(config, loginButton, "Login")'}


class TestALocatorAnAssertionReads:
    """Repaired on proof, reported otherwise — never handed on to be guessed at.

    Pointed at a different element, a presence check passes on anything visible,
    and a caught regression becomes a green build. The fingerprint match does not
    make that mistake, so its answer is accepted; when it has none, Fix must not
    ask the model to pick one.
    """

    def test_it_is_repaired_when_the_fingerprint_proves_the_element(self, world):
        record = _one(world, _assertions=SEEN_BY)
        assert record["verdict"] == "HEALED"
        assert record["new_locator"] == "[data-test='login-button']"
        # Carried to the report, so a reviewer is pointed at the one kind of fix
        # a passing test does not vouch for.
        assert record["assertion_site"] == SEEN_BY["loginButton"]

    def test_it_is_reported_when_nothing_proves_it(self, world, tmp_path):
        record = _one(world, _assertions=SEEN_BY, dom_snapshot=str(
            _capture(tmp_path, "gone", [USERNAME], "")))
        assert record["verdict"] == "NO_HEAL"
        assert record["classification"] == "ASSERTION_LOCATOR"
        assert "could not be proven to be the same element" in record["reason"]
        assert SEEN_BY["loginButton"] in record["reason"]
        assert "new_expression" not in record
        # What Locate actually concluded is kept, not overwritten.
        assert record["unproven_as"] and record["unproven_as"] != "ASSERTION_LOCATOR"

    def test_missing_evidence_is_not_proof_either(self, world):
        record = _one(world, _assertions=SEEN_BY, dom_snapshot="")
        assert record["classification"] == "ASSERTION_LOCATOR"
        assert record["unproven_as"] == "NO_CAPTURE"

    def test_configured_never_it_is_refused_before_anything_is_spent(self, world):
        record = _one(world, _assertions=SEEN_BY, _mode="never")
        assert record["classification"] == "ASSERTION_LOCATOR"
        assert "never auto-healed" in record["reason"]
        assert "new_expression" not in record

    def test_configured_always_it_is_a_locator_like_any_other(self, world, tmp_path):
        record = _one(world, _assertions=SEEN_BY, _mode="always", dom_snapshot=str(
            _capture(tmp_path, "gone", [USERNAME], "")))
        assert record["classification"] != "ASSERTION_LOCATOR"

    def test_a_locator_nothing_asserts_on_is_untouched_by_the_rule(self, world, tmp_path):
        record = _one(world, dom_snapshot=str(_capture(tmp_path, "gone", [USERNAME], "")))
        assert record["classification"] != "ASSERTION_LOCATOR"
        assert "assertion_site" not in record


class TestWhyThereIsNothingToCompareAgainst:
    """Three different gaps used to share one sentence, false for the commonest.

    "It did not resolve on the last good run" was said of a locator the page-load
    count showed matching, whose fingerprint had simply not been captured — a hole
    in the baseline, reported as evidence about the element.
    """

    def _reason(self, world, coverage):
        return world["mod"]._no_fingerprint_reason(
            "CheckoutPage", "orderTotal", {"coverage": coverage})

    def test_matched_but_never_fingerprinted_is_an_incomplete_baseline(self, world):
        reason = self._reason(world, {"orderTotal": 1})
        assert "matched 1 element(s) on the last good run" in reason
        assert "the baseline is incomplete" in reason
        assert "did not resolve" not in reason

    def test_absent_at_load_and_never_used(self, world):
        assert "matched nothing when the page loaded" in self._reason(
            world, {"orderTotal": 0})

    def test_not_evaluable_at_load(self, world):
        assert "could not be evaluated when the page loaded" in self._reason(world, {})

    def test_a_hole_in_a_baseline_is_not_counted_as_a_missing_baseline(
            self, world, tmp_path):
        # Both are missing evidence and both go to Fix, so the verdict is shared.
        # The count is not: "no baseline" says no good run was recorded, and here
        # one was.
        path = tmp_path / "baselines" / "shop" / "LoginPage.json"
        stored = json.loads(path.read_text())
        del stored["fingerprints"]["loginButton"]
        path.write_text(json.dumps(stored))
        record = _one(world)
        assert record["verdict"] == "NO_BASELINE"
        assert record["classification"] == "NO_FINGERPRINT"
        assert world["mod"]._counts([record]) == {"NO_FINGERPRINT": 1}

    def test_no_recorded_run_at_all_keeps_the_plain_name(self, world, tmp_path):
        (tmp_path / "baselines" / "shop" / "LoginPage.json").unlink()
        record = _one(world)
        assert record["verdict"] == "NO_BASELINE"
        assert not record.get("classification")


class TestOneLocatorIsResolvedOnce:
    def test_five_tests_on_one_locator_produce_one_search_and_five_records(self, world):
        searched = []
        original = world["mod"].locate_one
        world["mod"].locate_one = lambda *a, **k: (searched.append(1)
                                                   or original(*a, **k))
        assert world["mod"].main() == 0

        payload = json.loads((world["audit"] / "01-locate.json").read_text())
        assert len(searched) == 1
        assert payload["distinct_locators"] == 1
        assert payload["attempted"] == len(TESTS)
        assert payload["located"] == len(TESTS)
        assert {r["test_name"] for r in payload["resolutions"]} == {
            f"automation.shop.ShopWebTest.{name}" for name in TESTS}
        assert {r["new_locator"] for r in payload["resolutions"]} == {
            "[data-test='login-button']"}

    def test_the_report_lists_the_locator_once(self, world):
        world["mod"].main()
        assert (world["audit"] / "01-locate.md").read_text().count(
            "## LoginPage#loginButton") == 1


class TestNotEveryFailureIsDrift:
    """The guard the live search used to provide: before looking for a
    replacement, it resolved the failing selector on the page. A selector that
    still matches was not what broke — the element was hidden, late or covered —
    and rebinding it papers over a timing bug with a locator edit."""

    def test_a_selector_that_still_matches_is_not_healed(self, world, tmp_path):
        present = dict(LOGIN_BUTTON, id="login-mukesh",
                       id_xpath="//*[@id='login-mukesh']",
                       attrs=dict(LOGIN_BUTTON["attrs"], id="login-mukesh"))
        record = _one(world, dom_snapshot=str(
            _capture(tmp_path, "slow", [USERNAME, present], "login-mukesh")))
        assert record["classification"] == "NOT_LOCATOR"
        assert "new_expression" not in record

    def test_an_unevaluable_selector_still_gets_searched(self, world, tmp_path):
        # "unevaluable is not absent": an XPath nobody can compile must not read
        # as "the selector still matches" and block a genuine heal.
        record = _one(world, failed_selector="//input[@id='login-mukesh']")
        assert record["verdict"] == "HEALED"


# ── The chain: one broken locator hides the next ─────────────────────────────

CART_LINK = _el(3, "a", id="shopping-cart", testid="shopping-cart-link",
                role="link", accessible_name="Cart", text="Cart",
                id_xpath="//*[@id='shopping-cart']",
                attrs={"id": "shopping-cart", "data-test": "shopping-cart-link",
                       "href": "/cart.html"})

PRODUCTS_HTML = """<!-- qa-agent-network:dom-snapshot test="loginAndVerifyProductsPage" \
url="https://shop.example.com/inventory" capturedAt="2026-09-23T16:20:00" fingerprints="{sidecar}" -->
<!DOCTYPE html><html lang="en"><head><title>Products</title></head>
<body class=""><div id="header"><a id="shopping-cart" data-test="shopping-cart-link"
 href="/cart.html">Cart</a></div></body></html>
"""


@pytest.fixture
def chain(world, tmp_path):
    """Attempt 1 repaired the login button; the test now stops at the cart link.

    This is exactly what Fix writes: the edit is kept, the member is recorded as
    `advanced`, and `next_issue` carries the same test rebuilt around the element
    that fails NOW — new selector, new capture, both from the verification run.
    """
    sidecar = tmp_path / "products.fingerprints.json"
    sidecar.write_text(json.dumps({"url": "https://shop.example.com/inventory",
                                   "elements": [CART_LINK]}))
    snapshot = tmp_path / "products.html"
    snapshot.write_text(PRODUCTS_HTML.format(sidecar=sidecar))

    baselines = tmp_path / "baselines" / "shop"
    (baselines / "ProductsPage.json").write_text(json.dumps({
        "pageObject": "ProductsPage", "module": "shop",
        "recordedAt": "2026-09-01T10:00:00",
        "urlShape": "https://shop.example.com/inventory", "title": "Products",
        "coverage": {"cartLink": 1}, "landmarks": [],
        "fingerprints": {"cartLink": dict(CART_LINK, id="cart-link",
                                          id_xpath="//*[@id='cart-link']",
                                          attrs=dict(CART_LINK["attrs"], id="cart-link"))},
    }))
    java = world["repo"] / "src/main/java/automation/modules/shop/web"
    (java / "ProductsPage.java").write_text('''
public class ProductsPage extends BasePage {
    private final Locator cartLink = page.locator("#cart-link");
    public void openCart() { click(cartLink, "Cart link"); }
}
''')

    test = f"automation.shop.ShopWebTest.{TESTS[0]}"
    (world["audit"] / "01-fix.json").write_text(json.dumps({
        "fix_attempt": 1, "stuck_attempts": 0, "failed_fixes": [{
            "test_name": test, "status": "advanced",
            "failed_selector": "#login-mukesh",
            "next_issue": {
                "test_name": test, "failed_selector": "#cart-link",
                "error_message": "Failed to click on element 'Cart link' with "
                                 "locator: Locator@#cart-link",
                "stack_trace": "at automation.modules.shop.web.ProductsPage"
                               ".openCart(ProductsPage.java:3)",
                "dom_snapshot": str(snapshot),
                "baseline_not_after": "2026-09-23T16:09:03"}}]}))
    world["mod"] = _load({"AUDIT_DIR": world["audit"], "HANDOFF_FILE": world["tmp"] / "handoff.json",
                          "WORKSPACE_DIR": world["tmp"], "GITHUB_REPO_AUTOMATION": "repo",
                          "FRAMEWORK_DIR": "", "HEALING_BASELINE_DIR": world["tmp"] / "baselines",
                          "HEALING_LOCATE_MODE": "enforce", "FIX_ATTEMPT": "2"})
    return world


class TestWalkingTheChain:
    def test_the_next_link_is_resolved_from_the_failure_it_just_uncovered(self, chain):
        assert chain["mod"].main() == 0
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        now = [r for r in payload["resolutions"] if r["failed_selector"] == "#cart-link"]
        assert now and now[0]["verdict"] == "HEALED"
        assert now[0]["new_locator"] == "[data-test='shopping-cart-link']"

    def test_only_the_tests_still_failing_are_worked_on(self, chain):
        # Four of the five went green on attempt 1; re-resolving them would be
        # work on failures that no longer exist.
        chain["mod"].main()
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        assert {r["test_name"] for r in payload["resolutions"]} == {
            f"automation.shop.ShopWebTest.{TESTS[0]}"}

    def test_earlier_links_are_kept_in_the_report(self, chain):
        # Seed attempt 1's answer, then check attempt 2 keeps it.
        (chain["audit"] / "01-locate.json").write_text(json.dumps({"resolutions": [{
            "test_name": f"automation.shop.ShopWebTest.{TESTS[0]}",
            "failed_selector": "#login-mukesh", "verdict": "HEALED",
            "new_locator": "#login-button"}]}))
        chain["mod"].main()
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        assert {r["failed_selector"] for r in payload["resolutions"]} == {
            "#login-mukesh", "#cart-link"}
        assert payload["located"] == 2
        assert payload["attempt"] == 2

    def test_an_answer_already_applied_and_failed_is_not_offered_again(self, chain):
        # Fix reverts an edit that helped nobody, so the broken selector is back
        # in the source and this step would propose the identical answer for ever.
        (chain["audit"] / "01-locate.json").write_text(json.dumps({"resolutions": [{
            "test_name": f"automation.shop.ShopWebTest.{TESTS[0]}",
            "failed_selector": "#login-mukesh", "verdict": "HEALED",
            "new_locator": "#login-button"}]}))
        fix_json = json.loads((chain["audit"] / "01-fix.json").read_text())
        fix_json["failed_fixes"][0] = {
            "test_name": f"automation.shop.ShopWebTest.{TESTS[0]}",
            "status": "test_failed", "failed_selector": "#login-mukesh"}
        (chain["audit"] / "01-fix.json").write_text(json.dumps(fix_json))

        chain["mod"].main()
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        again = [r for r in payload["resolutions"]
                 if r["failed_selector"] == "#login-mukesh"]
        assert again[0]["classification"] == "REVERTED"
        assert again[0]["verdict"] != "HEALED"
        assert payload["verdicts"] == {"REVERTED": 1}

    def _reverted(self, chain, earlier):
        (chain["audit"] / "01-locate.json").write_text(json.dumps({"resolutions": [dict(
            {"test_name": f"automation.shop.ShopWebTest.{TESTS[0]}",
             "failed_selector": "#login-mukesh"}, **earlier)]}))
        fix_json = json.loads((chain["audit"] / "01-fix.json").read_text())
        fix_json["failed_fixes"][0] = {
            "test_name": f"automation.shop.ShopWebTest.{TESTS[0]}",
            "status": "test_failed", "failed_selector": "#login-mukesh"}
        (chain["audit"] / "01-fix.json").write_text(json.dumps(fix_json))
        chain["mod"].main()
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        return next(r for r in payload["resolutions"]
                    if r["failed_selector"] == "#login-mukesh")

    def test_the_refusal_names_the_locator_and_what_was_written_for_it(self, chain):
        # It replaces the answer in the file. Naming neither, the only trace of a
        # located-and-reverted repair was a truncated selector and "already tried".
        record = self._reverted(chain, {
            "verdict": "HEALED", "locator_id": "LoginPage#loginButton",
            "page_object": "LoginPage", "field": "loginButton",
            "new_expression": 'page.getByPlaceholder("Sign in")'})
        assert record["locator_id"] == "LoginPage#loginButton"
        assert record["field"] == "loginButton"
        assert record["tried"] == 'page.getByPlaceholder("Sign in")'
        assert 'page.getByPlaceholder("Sign in") was applied' in record["reason"]
        assert "reverted" in record["reason"]

    def test_an_answer_disproved_once_stays_disproved(self, chain):
        # Two attempts on: the file holds the refusal, not the answer, so there
        # is no HEALED record left to recognise the selector by. It was resolved
        # afresh and the same failed answer offered a second time.
        record = self._reverted(chain, {
            "verdict": "NO_HEAL", "classification": "REVERTED",
            "locator_id": "LoginPage#loginButton",
            "tried": 'page.locator("#login-button")'})
        assert record["classification"] == "REVERTED"
        assert record["tried"] == 'page.locator("#login-button")'
        assert "new_expression" not in record

    def test_a_baseline_written_during_the_repair_is_refused(self, chain, tmp_path):
        # A sibling test passing mid-run re-records the page object, so the
        # "last good run" would be the broken page. next_issue pins the cutoff.
        path = tmp_path / "baselines" / "shop" / "ProductsPage.json"
        record = json.loads(path.read_text())
        record["recordedAt"] = "2026-09-23T16:15:00"     # after the first failure
        path.write_text(json.dumps(record))
        chain["mod"].main()
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        now = [r for r in payload["resolutions"] if r["failed_selector"] == "#cart-link"]
        assert now[0]["verdict"] == "NO_BASELINE"

    def test_a_baseline_rewritten_mid_run_does_not_cost_the_chain_its_answer(
            self, chain, tmp_path):
        """The run that repairs link #1 re-records the page link #2 lives on.

        Attempt 1 greened three of five tests; every one of them walked through
        ProductsPage, so the framework rewrote its baseline minutes after the
        failure. Reading the live tree, Locate refuses it — rightly, it is
        younger than the failure — and the whole second link goes to the model.
        The copy taken before the session ran anything is the answer.
        """
        live = tmp_path / "baselines" / "shop" / "ProductsPage.json"
        preserved = chain["audit"] / "baselines"
        assert chain["mod"].baseline_store.preserve(tmp_path / "baselines", preserved) > 0

        record = json.loads(live.read_text())
        record["recordedAt"] = "2026-09-23T16:15:00"     # rewritten by a passing test
        live.write_text(json.dumps(record))

        fix_json = json.loads((chain["audit"] / "01-fix.json").read_text())
        fix_json["failed_fixes"][0]["next_issue"]["healing_baseline_dir"] = str(preserved)
        (chain["audit"] / "01-fix.json").write_text(json.dumps(fix_json))

        chain["mod"].main()
        payload = json.loads((chain["audit"] / "01-locate.json").read_text())
        now = [r for r in payload["resolutions"] if r["failed_selector"] == "#cart-link"]
        assert now[0]["verdict"] == "HEALED"
        assert now[0]["new_locator"] == "[data-test='shopping-cart-link']"


class TestNotEveryAnchorIsWorthHaving:
    """What the emit ladder is allowed to write into a page object.

    The run this pins: a cart link whose badge read "2" at failure time was
    healed to `getByText("2", exact)`. It was unique, it verified, and it broke
    on the next run with a different item count. Two separate faults put it
    there — the test-id candidate above it could never match, and nothing
    checked whether the text was identity or a number that happens to be there.
    """

    def test_a_badge_count_is_never_the_anchor(self, world, tmp_path):
        cart = _el(3, "a", testid=None, role="link", accessible_name="Cart",
                   text="2", class_list=["shopping_cart_link"],
                   attrs={"class": "shopping_cart_link", "href": "/cart.html"})
        html = HTML.format(sidecar="{sidecar}", button="").replace(
            "</form>", '</form><a class="shopping_cart_link" href="/cart.html">2</a>')
        sidecar = tmp_path / "cart.fingerprints.json"
        sidecar.write_text(json.dumps({"url": "https://shop.example.com/",
                                       "elements": [USERNAME, cart]}))
        snapshot = tmp_path / "cart.html"
        snapshot.write_text(html.format(sidecar=sidecar))

        from shared import locator_emit, locator_score
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "locator.yaml").read_text())
        emitted = world["mod"]._emit_offline(
            cart, locator_score.Volatility(cfg),
            world["mod"].page_identity.parse(snapshot.read_text()),
            json.loads(sidecar.read_text()))
        assert emitted is not None
        assert ":text-is" not in emitted["sel"]
        assert emitted["sel"] == "a.shopping_cart_link"

    def test_prose_is_still_allowed(self):
        from shared import locator_emit, locator_score
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "locator.yaml").read_text())
        vol = locator_score.Volatility(cfg)
        link = {"tag": "a", "text": "Continue Shopping", "is_interactive": True,
                "class_list": [], "attrs": {}}
        assert any(c["strategy"] == "text" for c in locator_emit.candidates_for(link, vol))

    def test_a_locator_already_in_the_file_is_not_a_heal(self, world, tmp_path):
        # The failure was read from an artefact older than the source — an
        # earlier attempt, or a PR, already applied this answer. Emitting it
        # again produces an edit that changes nothing.
        java = world["repo"] / "src/main/java/automation/modules/shop/web/LoginPage.java"
        java.write_text(LOGIN_PAGE.replace('"#login-mukesh"',
                                           '"[data-test=\'login-button\']"'))
        record = _one(world, _sources={"LoginPage": java.read_text()})
        assert record["classification"] == "ALREADY_CURRENT"
        assert "new_expression" not in record

    def test_a_reverted_cluster_is_not_sent_back_to_the_original_handoff(self, chain):
        # Attempt 2's edit was reverted, so its record carries the issue it was
        # working on. Attempt 3 must resolve THAT, not the login button the
        # handoff still names and attempt 1 already repaired.
        fix_json = json.loads((chain["audit"] / "01-fix.json").read_text())
        reverted = dict(fix_json["failed_fixes"][0])
        reverted["status"] = "test_failed"
        handoff = json.loads((chain["tmp"] / "handoff.json").read_text())

        issues = chain["mod"]._issues(handoff, {"failed_fixes": [reverted]})
        assert [i["failed_selector"] for i in issues] == ["#cart-link"]


# ── An attribute the page rewrites is not something to anchor a repair on ─────

CARD_PAGE = '''
public class CardPage extends BasePage {
    private final Locator expiryField;
    private final Locator codeField;
    public CardPage(Config config) {
        expiryField = page.locator("#expiry");
        codeField = page.locator("#code-mukesh");
    }
    public void pay(String expiry, String code) {
        fillText(expiryField, expiry, "Expiry field");
        fillText(codeField, code, "Security code field");
    }
}
'''

CARD_HTML = """<!-- qa-agent-network:dom-snapshot test="payByCard" \
url="https://shop.example.com/pay" capturedAt="2026-09-23T16:09:03" fingerprints="{sidecar}" -->
<!DOCTYPE html><html lang="en"><head><title>Pay</title></head>
<body class=""><form>
  <input id="number" type="tel" placeholder="1234 1234 1234 1234">
  <input id="expiry" type="tel" placeholder="{expiry_hint}">
  <input id="code" type="password" placeholder="123">
</form></body></html>
"""

CARD_NUMBER = _el(1, "input", id="number", type="tel",
                  placeholder="1234 1234 1234 1234", id_xpath="//*[@id='number']",
                  attrs={"id": "number", "type": "tel",
                         "placeholder": "1234 1234 1234 1234"})
EXPIRY = _el(2, "input", id="expiry", type="tel", placeholder="MM/YY",
             id_xpath="//*[@id='expiry']",
             attrs={"id": "expiry", "type": "tel", "placeholder": "MM/YY"})
CODE = _el(3, "input", id="code", type="password", placeholder="123",
           id_xpath="//*[@id='code']",
           attrs={"id": "code", "type": "password", "placeholder": "123"})


@pytest.fixture
def card(world, tmp_path):
    """A card form whose security-code locator broke, and one to compare it with.

    The page blanks the hint of whichever field has focus. At the failure the
    test had just typed the expiry, so that is the field showing it.
    """
    (tmp_path / "baselines" / "shop" / "CardPage.json").write_text(json.dumps({
        "pageObject": "CardPage", "module": "shop",
        "recordedAt": "2026-09-01T10:00:00",
        "urlShape": "https://shop.example.com/pay", "title": "Pay", "bodyClass": "",
        "coverage": {"expiryField": 1, "codeField": 1}, "landmarks": [],
        "fingerprints": {"expiryField": EXPIRY, "codeField": CODE},
    }))

    def run(expiry_hint):
        focused = dict(EXPIRY, placeholder=expiry_hint,
                       attrs=dict(EXPIRY["attrs"], placeholder=expiry_hint))
        sidecar = tmp_path / "pay.fingerprints.json"
        sidecar.write_text(json.dumps({"url": "https://shop.example.com/pay",
                                       "elements": [CARD_NUMBER, focused, CODE]}))
        snapshot = tmp_path / "pay.html"
        snapshot.write_text(CARD_HTML.format(sidecar=sidecar, expiry_hint=expiry_hint))
        return _one(
            world, _sources={"CardPage": CARD_PAGE},
            test_name="automation.shop.ShopWebTest.payByCard",
            failed_selector="#code-mukesh",
            failure_url="https://shop.example.com/pay",
            error_message="Failed to enter data in element 'Security code field' "
                          "with locator: Locator@#code-mukesh",
            stack_trace="at automation.modules.shop.web.CardPage.pay(CardPage.java:11)",
            dom_snapshot=str(snapshot))
    return run


class TestAnAttributeThePageRewrites:
    """Proved unique is proved for one moment of the page.

    `[placeholder='123']` matched exactly one field in the capture, was written
    into the page object, and timed out on its first run: clearing the field
    focuses it, the page blanks a focused field's hint, and the locator no
    longer found what it had just found.
    """

    def test_a_hint_the_page_changed_on_a_neighbour_is_not_used(self, card):
        record = card(expiry_hint="")
        assert record["verdict"] == "HEALED"
        assert record["strategy"] == "id"
        assert record["new_locator"] == "#code"

    def test_the_answer_says_what_it_passed_over_and_on_whose_evidence(self, card):
        witness = card(expiry_hint="")["anchors_passed_over"]["placeholder"]
        assert "expiryField still matches" in witness
        assert "'MM/YY' on the last good run" in witness
        assert "'' in the failure capture" in witness

    def test_a_page_that_left_its_hints_alone_still_gets_the_readable_locator(self, card):
        record = card(expiry_hint="MM/YY")
        assert record["strategy"] == "placeholder"
        assert record["new_locator"] == "[placeholder='123']"
        assert "anchors_passed_over" not in record

    def test_the_failing_field_is_never_its_own_witness(self, world):
        # Its locator is the one that broke; what it holds now proves nothing
        # about what the page does to a hint.
        mod = world["mod"]
        soup = mod.page_identity.parse(
            CARD_HTML.format(sidecar="none", expiry_hint="MM/YY").replace(
                'placeholder="123"', 'placeholder=""'))
        declared = [{"name": "codeField", "selector": "#code"}]
        assert mod._unsettled_anchors({"fingerprints": {"codeField": CODE}},
                                      declared, "codeField", soup) == {}
        assert "placeholder" in mod._unsettled_anchors(
            {"fingerprints": {"codeField": CODE}}, declared, "otherField", soup)

    def test_a_neighbour_that_no_longer_resolves_to_one_element_is_no_witness(self, world):
        mod = world["mod"]
        soup = mod.page_identity.parse(CARD_HTML.format(sidecar="none", expiry_hint=""))
        stored = {"fingerprints": {"expiryField": EXPIRY}}
        for selector in ("#gone", "input[type='tel']"):        # none, then two
            assert mod._unsettled_anchors(
                stored, [{"name": "expiryField", "selector": selector}],
                "codeField", soup) == {}


# ── An element that is only on the page after something is done to it ─────────
NOTICE_PAGE = '''
public class LoginPage extends BasePage {
    public LoginPage(Config config) {
        usernameField = page.locator("#user-name");
        loginButton = page.locator("#login-button");
        notice = page.locator(".notice-typo");
    }
    public String read() { return getText(notice, "Notice"); }
}
'''

NOTICE = _el(3, "div", class_list=["notice"], role="alert", accessible_name="Saved",
             text="Saved", is_interactive=False,
             attrs={"class": "notice", "role": "alert"})
RECORDED_AT = "2026-09-01T10:00:00"


def _rebaseline(world, **changes):
    """Rewrite the recorded good run. Everything in it is stamped with one time,
    the way a single passing run writes it."""
    path = world["tmp"] / "baselines" / "shop" / "LoginPage.json"
    record = json.loads(path.read_text())
    for key, value in changes.items():
        record[key] = {**(record.get(key) or {}), **value} if isinstance(value, dict) else value
    record.setdefault("lastSeen", {name: RECORDED_AT for name in record["coverage"]})
    path.write_text(json.dumps(record))


def _notice(world, **baseline):
    _rebaseline(world, coverage={"notice": baseline.pop("at_load", 0)},
                fingerprints={"notice": NOTICE},
                selectors={"notice": baseline.pop("selector", "Locator@.notice")},
                **baseline)
    return _one(world, _sources={"LoginPage": NOTICE_PAGE},
                failed_selector=".notice-typo",
                error_message="Failed to get text from element 'Notice' with "
                              "locator: Locator@.notice-typo")


class TestAnElementTheCaptureCannotHold:
    """The bug this pins: a message that shows after a click and clears itself was
    gone by the time the wait for it gave up. Nothing in the capture resembled it,
    which was reported as low confidence and nothing more — so the step that
    judges a replacement against that capture rejected the right one as a guess.
    """

    def test_it_is_said_that_the_capture_cannot_hold_it(self, world):
        record = _notice(world, selector="Locator@.notice-typo")
        assert record["verdict"] != "HEALED"
        assert "exists only after an action" in record["absent_from_capture"]

    def test_an_element_that_was_there_as_the_page_loaded_is_simply_missing(self, world):
        record = _notice(world, at_load=1)
        assert record["verdict"] != "HEALED"
        assert "absent_from_capture" not in record

    def test_a_sighting_from_an_earlier_run_is_not_this_run_using_it(self, world):
        _rebaseline(world, lastSeen={"usernameField": RECORDED_AT, "loginButton": RECORDED_AT,
                                     "notice": "2026-08-01T10:00:00"})
        assert "absent_from_capture" not in _notice(world, selector="Locator@.notice-typo")


class TestTheSelectorTheLastPassingRunUsed:
    """A locator breaks because the page changed or because the code did. For the
    second the answer was written down on the last run that passed."""

    def test_it_is_put_back_for_an_element_the_capture_cannot_hold(self, world):
        record = _notice(world)
        assert record["verdict"] == "HEALED" and record["tier"] == "T0_recorded"
        assert record["new_locator"] == ".notice"
        assert record["new_expression"] == 'page.locator(".notice")'
        assert "test run is the proof" in record["verification"]
        # Ranked against a fingerprint and passed over; this was not chosen from them.
        assert "rejected" not in record and "score" not in record

    def test_it_is_put_back_when_the_capture_proves_it(self, world):
        # The fingerprint cannot decide, the recorded selector can: it still finds
        # exactly one visible element.
        _rebaseline(world, fingerprints={"loginButton": NOTICE},
                    selectors={"loginButton": "Locator@#login-button"})
        record = _one(world)
        assert record["verdict"] == "HEALED" and record["tier"] == "T0_recorded"
        assert record["new_locator"] == "#login-button"
        assert record["verification"] == "unique in the failure capture"

    def test_not_when_the_source_still_declares_it(self, world):
        # The code did not change, so the page did. That is a search, not a restore.
        assert _notice(world, selector="Locator@.notice-typo")["verdict"] != "HEALED"

    def test_not_without_proof(self, world):
        # In neither the capture nor the class of element a capture cannot hold.
        assert _notice(world, at_load=1)["verdict"] != "HEALED"

    def test_not_a_chain_that_one_call_cannot_spell(self, world):
        _rebaseline(world, fingerprints={"loginButton": NOTICE}, selectors={
            "loginButton": "Locator@#pay >> internal:control=enter-frame >> #login-button"})
        assert _one(world)["verdict"] != "HEALED"

    def test_the_fingerprint_answers_first_when_it_can(self, world):
        _rebaseline(world, selectors={"loginButton": "Locator@#login-button"})
        assert _one(world)["tier"] != "T0_recorded"


# ── Two locators missing from one page ────────────────────────────────────────
TWO_BROKEN = '''
public class LoginPage extends BasePage {
    public LoginPage(Config config) {
        formBox = page.locator("form");
        usernameField = page.locator("%s");
        loginButton = page.locator("%s");
    }
    public void doLogin(String u) {
        fillText(usernameField, u, "Username field");
        click(loginButton, "Login button");
    }
}
'''


def _two(world, username="#user-typo", login="#login-typo", **overrides):
    _rebaseline(world, coverage={"formBox": 1})
    return _one(world, _sources={"LoginPage": TWO_BROKEN % (username, login)},
                failed_selector=login,
                error_message="Failed to click on element 'Login button' with "
                              f"locator: Locator@{login}", **overrides)


class TestWhatElseThePageLost:
    """One broken locator hides the next, and each costs a whole test run to
    reach. Two of them on one page are both in the capture the first one left."""

    def test_the_other_missing_locator_is_resolved_with_the_failing_one(self, world):
        record = _two(world)
        assert record["field"] == "loginButton" and record["verdict"] == "HEALED"
        (sibling,) = record["siblings"]
        assert sibling["locator_id"] == "LoginPage#usernameField"
        assert sibling["new_locator"] == "[data-test='username']"
        assert sibling["verification"] == "unique in the failure capture"

    def test_a_locator_that_still_matches_is_left_alone(self, world):
        assert "siblings" not in _two(world, username="#user-name")

    def test_one_an_assertion_reads_waits_for_its_own_failure(self, world):
        record = _two(world, _assertions={
            "usernameField": 'assertElementVisible(config, usernameField, "Username")'})
        assert "siblings" not in record

    def test_one_that_cannot_be_proved_waits_for_its_own_failure(self, world, tmp_path):
        # The username input is not in this capture at all.
        record = _two(world, dom_snapshot=str(
            _capture(tmp_path, "no-username", [LOGIN_BUTTON], "login-button")))
        assert record["verdict"] == "HEALED" and "siblings" not in record

    def test_a_second_field_declared_with_the_same_broken_selector(self, world):
        # A selector is listed once however many fields declare it, so the second
        # field is found by its name.
        record = _two(world, username="#typo", login="#typo")
        ids = {record["locator_id"]} | {s["locator_id"] for s in record["siblings"]}
        assert ids == {"LoginPage#usernameField", "LoginPage#loginButton"}

    def test_the_report_names_it(self, world):
        mod = world["mod"]
        record = _two(world)
        text = mod._markdown({"mode": "enforce", "resolutions": [record]})
        assert "also missing on this page: `LoginPage#usernameField`" in text
