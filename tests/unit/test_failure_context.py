"""Tests for shared/failure_context.py."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import failure_context


def _write(tmp_path, name="LoginTest_120000", **overrides):
    payload = {
        "schema": 1, "test": "a.b.LoginTest.x",
        "failure": {"kind": "PAGE_NOT_LOADED", "pageObject": "DashboardPage",
                    "anchors": [{"selector": "img.avatar", "count": 0, "visible": False}],
                    "elapsedMs": 30029, "budgetMs": 30000},
        "page": {"url": "https://a.com/", "title": "T", "bodyClass": "logged-out",
                 "readyState": "complete", "ariaBusy": ""},
        "pageObjectCoverage": {"DashboardPage": {"matched": 0, "evaluable": 2,
                                                 "details": {"a": 0, "b": 0}}},
        "domVolatility": {"changedDuringWait": False},
        "navigation": [], "httpErrors": [], "jsErrors": [],
    }
    payload.update(overrides)
    path = tmp_path / f"{name}.context.json"
    path.write_text(json.dumps(payload))
    return path


class TestLocating:
    def test_found_beside_its_dom_snapshot(self, tmp_path):
        _write(tmp_path, "LoginTest_120000")
        snapshot = tmp_path / "LoginTest_120000.html"
        snapshot.write_text("<html></html>")
        assert failure_context.beside_snapshot(str(snapshot)) is not None

    def test_falls_back_to_the_newest_for_the_method(self, tmp_path):
        # The two files straddle a second boundary, which happens in practice.
        _write(tmp_path, "LoginTest_120001")
        snapshot = tmp_path / "LoginTest_120000.html"
        snapshot.write_text("<html></html>")
        assert failure_context.beside_snapshot(str(snapshot)) is not None

    def test_no_snapshot_no_context(self):
        assert failure_context.beside_snapshot("") is None


class TestLoad:
    def test_absent_file_is_unavailable_not_empty(self, tmp_path):
        assert failure_context.load(tmp_path / "nope.json")["available"] is False

    def test_reads_the_recorded_fields(self, tmp_path):
        context = failure_context.load(_write(tmp_path))
        assert context["available"] is True
        assert context["page_object"] == "DashboardPage"
        assert context["ready_state"] == "complete"
        assert context["dom_changed_during_wait"] is False


class TestDerivedSignals:
    def test_anchor_matches_sums_the_counts(self, tmp_path):
        context = failure_context.load(_write(tmp_path, failure={
            "pageObject": "P", "anchors": [{"count": 2}, {"count": 3}],
            "elapsedMs": 1, "budgetMs": 2}))
        assert failure_context.anchor_matches(context) == 5

    def test_absent_context_has_no_anchor_opinion(self):
        assert failure_context.anchor_matches({"available": False}) is None

    def test_uncounted_anchors_stay_unknown(self, tmp_path):
        # A locator that could not be evaluated must not read as zero matches.
        context = failure_context.load(_write(tmp_path, failure={
            "pageObject": "P", "anchors": [{"count": None}],
            "elapsedMs": 1, "budgetMs": 2}))
        assert failure_context.anchor_matches(context) is None

    def test_self_coverage_is_marked_as_measured_live(self, tmp_path):
        coverage = failure_context.self_coverage(failure_context.load(_write(tmp_path)))
        assert coverage["source"] == "live"
        assert (coverage["matched"], coverage["evaluable"]) == (0, 2)
