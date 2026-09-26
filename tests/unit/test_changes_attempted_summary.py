"""The "Changes attempted:" block run.sh prints at the end of a healing run.

One locator edit is recorded once per failing test it covers, so a selector that
three tests shared printed its description and diff three times over — three
blocks that differed only in a test name the summary never showed.
"""

import json
import re
import subprocess

import pytest

from qa_agents_server.paths import REPO_ROOT

RUN_SH = REPO_ROOT / "agents" / "test-healing-agent" / "run.sh"


def _summary(tmp_path, fix_json: dict) -> str:
    """Run the summary printer embedded in run.sh over one 01-fix.json."""
    body = re.search(r"<<'PYSUM'[^\n]*\n(.*?)\nPYSUM", RUN_SH.read_text(), re.S)
    assert body, "the PYSUM summary block moved — update this test"
    path = tmp_path / "01-fix.json"
    path.write_text(json.dumps(fix_json))
    out = subprocess.run(["python3", "-c", body.group(1), str(path)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture
def shared_edit():
    """One LoginPage edit: green for one test, "advanced" for two others."""
    diff = ('-        loginButton = page.locator("#login-mukesh");\n'
            '+        loginButton = page.locator("#login-button");')
    why = "The login button's id was `#login-mukesh` but the element has `login-button`."
    def entry(test, outcome):
        return {"test_name": f"automation.saucedemo.SauceDemoWebTest.{test}",
                "test_names": [], "target_file": "/w/src/main/java/LoginPage.java",
                "fix_description": why, "unfixable_reason": "", "fix_diff": diff,
                "outcome": outcome, "reverted": False}
    kept = "kept — the test now stops at a later locator"
    return {"attempts": [{"attempt": 1, "entries": [
        entry("addProductToCart", "verified"),
        entry("simulateWebLifecycle", kept),
        entry("verifyProductAppearsInCart", kept),
    ]}]}


def test_one_edit_is_printed_once(tmp_path, shared_edit):
    out = _summary(tmp_path, shared_edit)

    assert out.count('+        loginButton = page.locator("#login-button");') == 1
    assert out.count("attempt 1: LoginPage.java") == 1


def test_every_test_and_verdict_survives_the_grouping(tmp_path, shared_edit):
    out = _summary(tmp_path, shared_edit)

    assert "verified: addProductToCart" in out
    assert ("kept — the test now stops at a later locator: "
            "simulateWebLifecycle, verifyProductAppearsInCart") in out


def test_a_single_verdict_keeps_it_on_the_header(tmp_path, shared_edit):
    """The one-test case reads exactly as it did before grouping."""
    entries = shared_edit["attempts"][0]["entries"][:1]
    out = _summary(tmp_path, {"attempts": [{"attempt": 1, "entries": entries}]})

    assert "attempt 1: LoginPage.java — verified" in out
    assert "verified: addProductToCart" not in out


def test_an_edit_covering_several_tests_names_them(tmp_path, shared_edit):
    """test_names is what the fix step records when one edit greens a cluster."""
    entries = shared_edit["attempts"][0]["entries"][:1]
    entries[0]["test_names"] = ["automation.saucedemo.SauceDemoWebTest.addProductToCart",
                                "automation.saucedemo.SauceDemoWebTest.loginAndVerifyProductsPage"]
    out = _summary(tmp_path, {"attempts": [{"attempt": 1, "entries": entries}]})

    assert "addProductToCart, loginAndVerifyProductsPage" in out
