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
