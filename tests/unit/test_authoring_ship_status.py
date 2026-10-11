"""What the authoring ship step reports for a dry run.

The bug this pins: with AUTO_PUSH=false, create_branch_and_commit writes the
files to the working tree and returns no branch on purpose. main() read that as
"Branch creation failed" and recorded push_failed, so every dry run — the run a
user asked for — came out NEEDS-REVIEW, failed in the Studio's history, and
uncredited in Analytics.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    path = ROOT / "agents" / "test-authoring-agent" / "actions" / "05_ship.py"
    spec = importlib.util.spec_from_file_location("authoring_ship", path)
    mod = importlib.util.module_from_spec(spec)
    # Set only for the import (the step reads them into module constants).
    with mock.patch.dict(os.environ, {"AUDIT_DIR": tempfile.mkdtemp(prefix="authoring-ship-")}):
        spec.loader.exec_module(mod)
    return mod


ship = _load()


@pytest.mark.parametrize("auto_push,status,verdict", [
    (False, "dry_run", "APPROVED"),        # no branch by design
    (True, "push_failed", "NEEDS-REVIEW"),  # no branch because git failed
])
def test_no_branch_is_a_dry_run_only_when_no_push_was_asked_for(
        tmp_path, monkeypatch, auto_push, status, verdict):
    (tmp_path / "03-generate.json").write_text(json.dumps(
        {"files_written": ["src/test/java/CartTest.java"]}))
    (tmp_path / "04-run-and-fix.json").write_text(json.dumps({"passed": True}))
    (tmp_path / ".fix-passed").write_text("true")
    monkeypatch.setattr(ship, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ship, "AUTO_PUSH", auto_push)
    monkeypatch.setattr(ship, "create_branch_and_commit", lambda *a, **k: (None, None))

    ship.main()

    assert json.loads((tmp_path / "05-ship.json").read_text())["ship_status"] == status
    assert (tmp_path / ".verdict").read_text() == verdict


def test_review_notes_show_reuse_changes_reruns_and_length():
    gen = {
        "reuse": [{"existing": "WaitHelper.waitForUrl(Config config, String url)", "how": "as_is"},
                  {"existing": "ShopHelper.choose(PaymentMethod method)", "how": "extend",
                   "change": "add the wallet case"}],
        "modified_existing": {"src/main/java/automation/modules/shop/ShopHelper.java": {
            "changed": ["ShopHelper.choose(PaymentMethod)"], "added": [], "removed": []}},
        "new_operations": [{"name": "confirmOtp", "kind": "stage", "why_new": "nothing confirms an OTP"}],
        "test_shape": {"guideline_lines": 30, "methods": {
            "ShopWebTest#pay": {"body_lines": 41},
            "ShopWebTest#refund": {"body_lines": 12}}},
        "rebuilt_pages": [{"file": "ShopHelper.java", "method": "ShopHelper.pay(ShopData)",
                           "page": "CartPage"}],
    }
    regression = {"changed": {"src/main/java/automation/modules/shop/ShopHelper.java": ["choose"]},
                  "tests": ["ShopWebTest#refund"], "not_run_reason": "",
                  "results": {"ShopWebTest#refund": {"status": "failed", "first_error": "[ERROR] boom"}}}
    notes = ship._review_notes(gen, regression)
    assert "- `WaitHelper.waitForUrl(Config config, String url)`" in notes
    assert "- changed `ShopHelper.choose(PaymentMethod method)` — add the wallet case" in notes
    assert "❌ re-ran `ShopWebTest#refund`: failed — `[ERROR] boom`" in notes
    assert "why new: nothing confirms an OTP" in notes
    assert "`ShopWebTest#pay`: 41 lines" in notes and "ShopWebTest#refund`: 12" not in notes
    assert "- ⚠️ `ShopHelper.pay(ShopData)` constructs `CartPage`" in notes
    assert ship._review_notes({}, {}) == ""


@pytest.mark.parametrize("regression,verdict", [
    ({}, "APPROVED"),
    ({"results": {"ShopWebTest#refund": {"status": "passed", "first_error": ""}}}, "APPROVED"),
    ({"results": {"ShopWebTest#refund": {"status": "failed", "first_error": "x"}}}, "NEEDS-REVIEW"),
    ({"tests": ["ShopWebTest#refund"], "not_run_reason": "11 existing tests reach it"}, "NEEDS-REVIEW"),
])
def test_an_existing_test_broken_or_not_rerun_needs_review(tmp_path, monkeypatch, regression, verdict):
    (tmp_path / "03-generate.json").write_text(json.dumps(
        {"files_written": ["src/test/java/CartTest.java"]}))
    (tmp_path / "04-run-and-fix.json").write_text(json.dumps({"passed": True}))
    if regression:
        (tmp_path / "04-regression.json").write_text(json.dumps(regression))
    (tmp_path / ".fix-passed").write_text("true")
    monkeypatch.setattr(ship, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ship, "AUTO_PUSH", False)
    monkeypatch.setattr(ship, "create_branch_and_commit", lambda *a, **k: (None, None))

    ship.main()

    assert (tmp_path / ".verdict").read_text() == verdict


CHECK = "Verify the toast 'Item saved to your list.' appears"


@pytest.mark.parametrize("fix,gate,verdict", [
    # Step 02 missed it, the passing test asserts it: observed there.
    ({"passed": True, "confirmed_checks": [CHECK]}, "true", "APPROVED"),
    # Passed, but no assertion could be matched to it: nothing observed it.
    ({"passed": True}, "true", "NEEDS-REVIEW"),
    # A confirmation only counts for the run that passed.
    ({"passed": False, "confirmed_checks": [CHECK]}, "false", "NEEDS-REVIEW"),
])
def test_a_check_the_passing_test_asserts_is_not_a_reason_for_review(
        tmp_path, monkeypatch, fix, gate, verdict):
    (tmp_path / "03-generate.json").write_text(json.dumps(
        {"files_written": ["src/test/java/CartTest.java"],
         "kept_unverified_checks": [CHECK]}))
    (tmp_path / "04-run-and-fix.json").write_text(json.dumps(fix))
    (tmp_path / ".fix-passed").write_text(gate)
    monkeypatch.setattr(ship, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ship, "AUTO_PUSH", False)
    monkeypatch.setattr(ship, "create_branch_and_commit", lambda *a, **k: (None, None))

    ship.main()

    assert (tmp_path / ".verdict").read_text() == verdict


def _pr_body(tmp_path, monkeypatch, fix, gate=""):
    sent = {}
    if gate:
        (tmp_path / ".fix-passed").write_text(gate)
    monkeypatch.setattr(ship, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(ship, "AUTO_PUSH", True)
    monkeypatch.setattr(ship, "GITHUB_ORG", "org")
    monkeypatch.setattr(ship, "git", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(ship, "create_pr",
                        lambda **kw: (sent.update(kw) or "https://example.test/pr/1", ""))
    ship.push_and_create_pr("authoring/x", {"kept_unverified_checks": [CHECK]}, fix)
    return sent["body"]


def test_the_pr_does_not_warn_about_a_check_the_passing_test_asserts(tmp_path, monkeypatch):
    body = _pr_body(tmp_path, monkeypatch, {"passed": True, "confirmed_checks": [CHECK]})
    assert CHECK not in body
    assert "fails on purpose" not in body


def test_a_passing_test_is_never_described_as_failing_on_purpose(tmp_path, monkeypatch):
    body = _pr_body(tmp_path, monkeypatch, {"passed": True})
    assert CHECK in body
    assert "fails on purpose" not in body
    assert "not found in the test" in body


def test_a_test_that_failed_on_the_check_keeps_the_finding(tmp_path, monkeypatch):
    body = _pr_body(tmp_path, monkeypatch, {"passed": False}, gate="defect")
    assert CHECK in body
    assert "fails on purpose" in body


@pytest.mark.parametrize("gate", ["false", "stuck"])
def test_a_test_that_failed_elsewhere_says_nothing_about_the_check(tmp_path, monkeypatch, gate):
    """A run stopped on a later step, and its PR said the test fails on purpose
    under a check whose assertion had passed in every attempt."""
    body = _pr_body(tmp_path, monkeypatch, {"passed": False}, gate=gate)
    assert CHECK in body and "not seen by step 02" in body
    assert "fails on purpose" not in body


def test_every_property_the_committed_code_reads_reaches_the_pr():
    """A generated test read its login from two properties the PR left out, so it
    ran only in the checkout that wrote them."""
    committed = "shop.url=https://shop.example.test\n"
    working = (committed + "shop.username=buyer1\nshop.password=Secret1\n"
               "other.password=NotThisRuns\n")
    sources = {
        "src/test/java/automation/shop/ShopWebTest.java":
            'String u = config.getRunTimeProperty("shop.username");\n'
            'String p = config.getRunTimeProperty("shop.password");\n'
            'String url = config.getRunTimeProperty("shop.url");\n',
        "src/test/resources/shop/csvFiles/users.csv": "user_key,username\nbuyer,buyer1\n",
    }
    assert ship.run_properties(sources, committed, working) == {
        "shop.username": "buyer1", "shop.password": "Secret1"}
    # A login kept in the sheet reads no property, so none is committed.
    assert ship.run_properties({"A.java": 'shop.getUser("buyer");'}, committed, working) == {}


def test_the_runs_properties_are_committed_after_the_branch_reset(tmp_path, monkeypatch):
    """The branch is cut with a forced checkout, which puts the repository's copy of
    the properties file back: what the run wrote has to be read before it."""
    import subprocess

    def run(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    repo = tmp_path / "fw"
    props = repo / "parameters" / "staging-sg.properties"
    props.parent.mkdir(parents=True)
    props.write_text("shop.url=https://shop.example.test\n")
    run("init", "-q", "-b", "main")
    run("config", "user.email", "qa@example.test")
    run("config", "user.name", "QA")
    run("add", "-A")
    run("commit", "-q", "-m", "base")
    head = run("rev-parse", "HEAD")
    # What steps 03 and 04 left in the working copy, beside a key nothing reads.
    props.write_text(props.read_text() + "# Shop\nshop.username=buyer1\n"
                     "shop.password=Secret1\nother.password=NotThisRuns\n")

    monkeypatch.setenv("AUTHORING_ENVIRONMENT", "staging")
    monkeypatch.setenv("AUTHORING_COUNTRY", "SG")
    monkeypatch.setattr(ship, "AUTOMATION_FRAMEWORK_DIR", repo)
    monkeypatch.setattr(ship, "AUTO_PUSH", True)
    monkeypatch.setattr(ship, "GITHUB_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(ship.workspace_helper, "prepare_base",
                        lambda *a, **k: {"ok": True, "branch": "main", "sha": head})
    monkeypatch.setattr(ship.workspace_helper, "checkout_base",
                        lambda *a, **k: run("checkout", "-q", "-f", "main") or {"ok": True})
    test = "src/test/java/automation/shop/ShopWebTest.java"
    gen = {"feature": "shop", "files_written": [test], "files_content": {
        test: 'String u = config.getRunTimeProperty("shop.username");\n'
              'String p = config.getRunTimeProperty("shop.password");\n'}}

    branch, _ = ship.create_branch_and_commit(gen, [])

    assert branch
    committed = run("show", "HEAD:parameters/staging-sg.properties")
    assert "shop.username=buyer1" in committed and "shop.password=Secret1" in committed
    assert "other.password" not in committed, "only what this run's code reads"
