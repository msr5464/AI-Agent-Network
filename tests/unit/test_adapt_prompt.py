"""What build_adapt_prompt tells the model it may edit.

A SauceDemo run declined a two-step insert as "not in the editable files list":
three API files sorted ahead of ProductsPage and the cut of six dropped it, and
the test class the insert belongs in was never offered at all.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_adapt(tmp_path, monkeypatch):
    """Load 04_adapt.py by path, leaving sys.path and `lib` as the session had them.

    Three agents ship a `lib` package (see test_adapt_transaction.py), and 04_adapt
    puts its own agent dir on sys.path when imported — either leak breaks whichever
    later test imports another agent's `lib`.
    """
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    is_lib = lambda n: n == "lib" or n.startswith("lib.")
    for name in [n for n in sys.modules if is_lib(n)]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location(
        "adapt_04", ROOT / "agents/test-adaptation-agent/actions/04_adapt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in [n for n in sys.modules if is_lib(n)]:
        del sys.modules[name]              # monkeypatch then restores the originals
    return module


def test_page_objects_and_the_web_test_are_offered(tmp_path, monkeypatch):
    adapt = _load_adapt(tmp_path, monkeypatch)
    workspace = tmp_path / "ws"
    # This run's seven candidates, in the order that used to cut ProductsPage.
    roles = {"PostBuilder": "builder", "PostData": "data", "SauceDemoHelper": "helper",
             "api/PostApi": "api", "web/CartPage": "page_object",
             "web/LoginPage": "page_object", "web/ProductsPage": "page_object"}
    candidates = [{"path": f"src/main/java/m/{name}.java", "role": role}
                  for name, role in roles.items()]
    named = [{"test": "m.ApiTest#a", "path": "src/test/java/m/ApiTest.java", "is_web": False},
             {"test": "m.WebTest#b", "path": "src/test/java/m/WebTest.java", "is_web": True}]
    for rel in [c["path"] for c in candidates] + [r["path"] for r in named]:
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text("class X {}")

    prompt = adapt.build_adapt_prompt(
        {"index": 1, "kind": "step_insert", "text": "sort, then add a second product"},
        {"type": "web"}, {"edit_candidates": candidates, "tiers": {"named": named}},
        {"steps": [], "pages": {}}, workspace, rules="", retry_note="")

    offered = prompt.split("## Files you may edit", 1)[1]
    assert "ProductsPage.java" in offered
    assert "WebTest.java  (test)" in offered
    assert "ApiTest.java" not in offered        # a web change is not edited into API tests


def test_the_checks_are_listed_with_what_the_browser_saw(tmp_path, monkeypatch):
    adapt = _load_adapt(tmp_path, monkeypatch)
    check = {"id": "c1038d38", "message": "Cart badge should display 2 items",
             "site": "WebTest#b", "callee": "AssertHelper.assertEquals", "display": ['"2"'],
             "via": ""}
    flow = {"steps": [], "pages": {},
            "outcomes": [{"invariant": "c1038d38", "observed": "fail|badge shows 3"}]}
    changing = adapt.build_adapt_prompt(
        {"index": 1, "kind": "coverage_changed", "text": "add a third product"},
        {"type": "web"}, {}, flow, tmp_path, rules="", retry_note="", checks=[check])
    assert "[c1038d38]" in changing and "badge shows 3" in changing
    assert "untrusted page text" in changing
    assert "may remove or change the checks" in changing

    locked = adapt.build_adapt_prompt(
        {"index": 1, "kind": "locator", "text": "button renamed"},
        {"type": "web"}, {}, {"steps": [], "pages": {}}, tmp_path, rules="", retry_note="",
        checks=[check])
    assert "may not remove or change any check" in locked


def test_a_prompt_with_no_contracts_still_builds(tmp_path, monkeypatch):
    adapt = _load_adapt(tmp_path, monkeypatch)
    prompt = adapt.build_adapt_prompt(
        {"index": 1, "kind": "locator", "text": "x"}, {"type": "web"}, {},
        {"steps": [], "pages": {}}, tmp_path, rules="", retry_note="")
    assert "_No checks measured._" in prompt


def test_the_repos_own_conventions_reach_the_model(tmp_path, monkeypatch):
    """The adapt call used to see no framework conventions at all."""
    adapt = _load_adapt(tmp_path, monkeypatch)
    (tmp_path / "CLAUDE.md").write_text("Use click(locator, name), never locator.click().")
    prompt = adapt.build_adapt_prompt(
        {"index": 1, "kind": "locator", "text": "x"}, {"type": "web"}, {},
        {"steps": [], "pages": {}}, tmp_path, rules="", retry_note="")
    assert "PROJECT CONVENTIONS" in prompt
    assert "never locator.click()" in prompt


def test_a_measured_relation_is_offered_as_a_relax(tmp_path, monkeypatch):
    adapt = _load_adapt(tmp_path, monkeypatch)
    check = {"id": "c1", "message": "Customer name should match", "site": "WebTest#b",
             "callee": "AssertHelper.assertEquals", "display": [], "via": ""}
    flow = {"steps": [], "pages": {},
            "value_checks": [{"check": "c1", "element": "name", "source": "input:name",
                              "rendered": "User_x sample_last_name", "expected": "User_x",
                              "relation": "words"}]}
    prompt = adapt.build_adapt_prompt(
        {"index": 1, "kind": "outcome_changed", "text": "the overlay shows the full name"},
        {"type": "web"}, {}, flow, tmp_path, rules="", retry_note="", checks=[check])
    assert "relation **words**" in prompt and "User_x sample_last_name" in prompt
    assert "`relax` one listed with a relation" in prompt


def test_a_verify_failure_that_is_only_rendering_is_named(tmp_path, monkeypatch):
    adapt = _load_adapt(tmp_path, monkeypatch)
    checks = [{"id": "c1", "message": "Customer name should match"},
              {"id": "c2", "message": "Phone should match"}]
    failed = [("m.WebTest#b", "failed",
               "✘ FAIL: Customer name should match | Expected: 'User_x' | "
               "Actual: 'User_x sample_last_name'"),
              ("m.WebTest#c", "failed",
               "✘ FAIL: Phone should match | Expected: '0811' | Actual: 'Budi'")]
    assert adapt.value_mismatches(failed, checks) == [
        {"check": "c1", "message": "Customer name should match", "expected": "User_x",
         "actual": "User_x sample_last_name", "relation": "words"}]


def test_the_explorer_counts_the_page_objects_exact_locators(tmp_path, monkeypatch):
    """Every exact locator of every page object in scope is handed to the browser
    helpers to count live; one assembled at runtime is left out, because counting
    its approximation would report a working locator as broken."""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    is_lib = lambda n: n == "lib" or n.startswith("lib.")
    for name in [n for n in sys.modules if is_lib(n)]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location(
        "explore_03", ROOT / "agents/test-adaptation-agent/actions/03_explore_web.py")
    explore = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(explore)
    for name in [n for n in sys.modules if is_lib(n)]:
        del sys.modules[name]
    source = ('public class ProductsPage extends BasePage {\n'
              '  private final Locator title = page.locator(".title");\n'
              '  private final Locator cart = page.locator("[data-test=\'shopping-cart-link\']");\n'
              '  Locator item(String n) { return page.locator("#item-" + n); }\n}\n')
    known = explore.known_locators([{"path": "src/web/ProductsPage.java", "snippet": source}])
    assert {(k["owner"], k["name"], k["selector"]) for k in known} >= {
        ("ProductsPage", "title", ".title"),
        ("ProductsPage", "cart", "[data-test='shopping-cart-link']")}
    assert all(k["path"] == "src/web/ProductsPage.java" for k in known)
    assert not any(k["selector"].startswith("#item-") for k in known)
