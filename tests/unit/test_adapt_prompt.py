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
