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
        # beside_snapshot matches on name alone, which is why callers that care
        # which run wrote it use for_failure instead.
        _write(tmp_path, "LoginTest_120001")
        snapshot = tmp_path / "LoginTest_120000.html"
        snapshot.write_text("<html></html>")
        assert failure_context.beside_snapshot(str(snapshot)) is not None

    def test_no_snapshot_no_context(self):
        assert failure_context.beside_snapshot("") is None


class TestForFailure:
    """Which run wrote it, not what it is called.

    The fallback search matches on method name, and every previous run of a test
    leaves a file with that name. Adopting one reports another run's page,
    coverage, anchors and navigation as this failure's measurements — the whole
    diagnosis then argues from evidence belonging to a different failure.
    """

    def _snapshot(self, tmp_path, name="LoginTest_120000"):
        snapshot = tmp_path / f"{name}.html"
        snapshot.write_text("<html></html>")
        return snapshot

    def test_the_sibling_is_trusted_outright(self, tmp_path):
        _write(tmp_path, "LoginTest_120000")
        context = failure_context.for_failure(str(self._snapshot(tmp_path)))
        assert context["available"] is True

    def test_a_near_neighbour_still_counts(self, tmp_path):
        # The two files straddle a second boundary, which happens in practice.
        _write(tmp_path, "LoginTest_120001", failedAt="2026-09-02T12:00:01")
        context = failure_context.for_failure(
            str(self._snapshot(tmp_path)), test_name="x",
            captured_at="2026-09-02T12:00:00")
        assert context["available"] is True

    def test_a_context_from_another_run_is_refused(self, tmp_path):
        # The failure that started all of this: no context was written for the
        # run, and the newest file for the method was five days old.
        _write(tmp_path, "LoginTest_190032", failedAt="2026-08-28T19:00:32")
        context = failure_context.for_failure(
            str(self._snapshot(tmp_path)), test_name="x",
            captured_at="2026-09-02T12:00:00")
        assert context["available"] is False
        assert "minute(s) from this failure" in context["rejected"]

    def test_a_context_for_another_test_is_refused(self, tmp_path):
        _write(tmp_path, "LoginTest_120001", test="a.b.CheckoutTest.pay",
               failedAt="2026-09-02T12:00:01")
        context = failure_context.for_failure(
            str(self._snapshot(tmp_path)), test_name="x",
            captured_at="2026-09-02T12:00:00")
        assert context["available"] is False
        assert "written for pay" in context["rejected"]

    def test_a_context_nothing_ties_to_the_failure_is_refused(self, tmp_path):
        # No timestamps on either side: unverifiable, and read with the same
        # authority as a measured one if it is let through.
        _write(tmp_path, "LoginTest_120001", test="", failedAt="")
        context = failure_context.for_failure(str(self._snapshot(tmp_path)))
        assert context["available"] is False
        assert context["rejected"]

    def test_the_fully_qualified_name_compares_to_the_bare_method(self, tmp_path):
        # The context records a.b.LoginTest.x; the snapshot header records x.
        _write(tmp_path, "LoginTest_120001", failedAt="2026-09-02T12:00:00")
        context = failure_context.for_failure(
            str(self._snapshot(tmp_path)), test_name="x",
            captured_at="2026-09-02T12:00:00")
        assert context["available"] is True

    def test_no_snapshot_no_context(self):
        assert failure_context.for_failure("")["available"] is False


class TestRunFloor:
    def test_find_ignores_anything_older_than_the_run(self, tmp_path):
        written = _write(tmp_path, "LoginTest_120000")
        floor = written.stat().st_mtime + 60
        assert failure_context.find(tmp_path, "LoginTest") is not None
        assert failure_context.find(tmp_path, "LoginTest", not_before=floor) is None


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


class TestAnchorState:
    """Separating "matched nothing" from "matched but hidden".

    Both time out at the same budget and read identically in a stack trace, but
    only the first is a stale locator. Replacing the selector for the second
    swaps one hidden element for another and fails the same way.
    """

    @staticmethod
    def _ctx(anchors, available=True):
        return {"available": available, "anchors": anchors}

    def test_no_matches_is_absent(self):
        assert failure_context.anchor_state(
            self._ctx([{"selector": "img[alt='mukesh']", "count": 0,
                        "visible": False}])) == "absent"

    def test_matched_but_not_visible_is_hidden(self):
        assert failure_context.anchor_state(
            self._ctx([{"selector": "button:has-text('Profile summary')",
                        "count": 1, "visible": False}])) == "hidden"

    def test_matched_and_visible(self):
        assert failure_context.anchor_state(
            self._ctx([{"selector": "#ok", "count": 1, "visible": True}])) == "visible"

    @pytest.mark.parametrize("ctx", [
        {"available": False, "anchors": [{"count": 0}]},   # nothing was captured
        {"available": True, "anchors": []},                # no anchors recorded
        {"available": True, "anchors": [{"selector": "#x"}]},  # no count recorded
    ])
    def test_unknown_is_none(self, ctx):
        assert failure_context.anchor_state(ctx) is None

    def test_a_missing_visible_flag_is_not_hidden(self):
        """Absent evidence of visibility is not evidence of invisibility."""
        assert failure_context.anchor_state(
            self._ctx([{"selector": "#x", "count": 2}])) is None
