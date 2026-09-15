"""Tests for shared/baseline.py.

The distinction being drawn is between a page that was *edited* and a page the
test never *reached*. A changed title alone is a copy edit; identity signals
disagreeing while every locator has vanished is a different page. Getting that
wrong in either direction is expensive — one blocks real fixes, the other lets
the agent rewrite selectors for a page it was never on.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import baseline


GOOD = {"available": True, "url_shape": "https://app.example.com/inventory",
        "title": "Dashboard", "body_class": "logged-in env-prod",
        "coverage": {"pageTitle": 1, "cartLink": 1, "menuButton": 1}}


class TestUrlShape:
    @pytest.mark.parametrize("url,expected", [
        ("https://a.com/users/42/details", "https://a.com/users/{id}/details"),
        ("https://a.com/x?q=1#top", "https://a.com/x"),
        ("https://a.com/i/8f14e45f-ceea-467a-9575-9f2a1c2b3d4e/edit",
         "https://a.com/i/{uuid}/edit"),
        ("", ""),
    ])
    def test_variable_parts_are_removed(self, url, expected):
        assert baseline.url_shape(url) == expected


class TestLoad:
    def test_reads_a_recorded_fingerprint(self, tmp_path, monkeypatch):
        folder = tmp_path / "baselines"
        folder.mkdir()
        (folder / "ProductsPage.json").write_text(json.dumps({
            "pageObject": "ProductsPage", "urlShape": "https://a.com/x",
            "title": "T", "bodyClass": "c", "coverage": {"a": 1}}))
        monkeypatch.setenv("HEALING_BASELINE_DIR", str(folder))
        loaded = baseline.load("ProductsPage")
        assert loaded["available"] is True
        assert loaded["coverage"] == {"a": 1}

    def test_reads_a_module_scoped_fingerprint(self, tmp_path, monkeypatch):
        # Two modules each own a CartPage; the failing test's module picks its own.
        for module, count in (("checkout", 1), ("saucedemo", 2)):
            (tmp_path / module).mkdir()
            (tmp_path / module / "CartPage.json").write_text(json.dumps({
                "module": module, "pageObject": "CartPage",
                "coverage": {"checkoutButton": count}}))
        monkeypatch.setenv("HEALING_BASELINE_DIR", str(tmp_path))
        module = baseline.module_of("automation.saucedemo.SauceDemoWebTest.checkout")
        assert module == "saucedemo"
        assert baseline.load("CartPage", module=module)["coverage"] == {"checkoutButton": 2}
        assert baseline.load("CartPage")["available"] is True, (
            "without a module the lookup still finds a module-scoped baseline")

    def test_pending_is_never_read_as_a_baseline(self, tmp_path, monkeypatch):
        (tmp_path / "pending").mkdir()
        (tmp_path / "pending" / "CartPage.json").write_text('{"coverage": {"a": 1}}')
        monkeypatch.setenv("HEALING_BASELINE_DIR", str(tmp_path))
        assert baseline.load("CartPage")["available"] is False

    def test_absent_baseline_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALING_BASELINE_DIR", str(tmp_path))
        assert baseline.load("NeverSeen")["available"] is False

    def test_corrupt_baseline_is_ignored(self, tmp_path, monkeypatch):
        (tmp_path / "P.json").write_text("{not json")
        monkeypatch.setenv("HEALING_BASELINE_DIR", str(tmp_path))
        assert baseline.load("P")["available"] is False


class TestDiff:
    def test_one_missing_locator_names_it(self):
        live = {"details": {"pageTitle": 0, "cartLink": 1, "menuButton": 1}}
        result = baseline.diff(GOOD, {"url": "https://app.example.com/inventory",
                                      "title": "Dashboard",
                                      "body_class": "logged-in env-prod"}, live)
        assert any("pageTitle" in m for m in result["mismatches"])
        assert baseline.is_different_page(result) is False

    def test_everything_vanished_is_a_different_page(self):
        live = {"details": {"pageTitle": 0, "cartLink": 0, "menuButton": 0}}
        result = baseline.diff(GOOD, {"url": "https://app.example.com/",
                                      "title": "Marketing",
                                      "body_class": "logged-out"}, live)
        assert baseline.is_different_page(result) is True

    def test_body_class_change_is_reported_as_gained_and_lost(self):
        result = baseline.diff(GOOD, {"url": "https://app.example.com/inventory",
                                      "title": "Dashboard",
                                      "body_class": "logged-out env-prod"}, None)
        assert any("gained logged-out" in m and "lost logged-in" in m
                   for m in result["mismatches"])

    def test_a_title_change_alone_is_not_a_different_page(self):
        # Copy edits happen. One mismatch must never be enough on its own.
        result = baseline.diff(GOOD, {"url": "https://app.example.com/inventory",
                                      "title": "Dashboard (beta)",
                                      "body_class": "logged-in env-prod"}, None)
        assert baseline.is_different_page(result) is False

    def test_surviving_locators_outrank_identity_mismatches(self):
        # If the page object's elements are still there, we are on the page,
        # whatever the title and URL now say.
        live = {"details": {"pageTitle": 1, "cartLink": 1, "menuButton": 0}}
        result = baseline.diff(GOOD, {"url": "https://app.example.com/v2/inventory",
                                      "title": "Renamed",
                                      "body_class": "logged-in env-prod"}, live)
        assert baseline.is_different_page(result) is False

    def test_no_baseline_yields_no_opinion(self):
        result = baseline.diff({"available": False}, {"url": "x"}, None)
        assert result["available"] is False
        assert baseline.is_different_page(result) is False
