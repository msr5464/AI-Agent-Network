"""Unit tests for shared/trace_network.py.

Built around one asymmetry: a single failed analytics beacon is normal on almost
every page, while a document request that never completed is an outage. The
summary has to keep those far enough apart that the diagnosis engine can too.
"""

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import trace_network


def _trace(tmp_path, records, name="t.zip"):
    path = tmp_path / name
    lines = "\n".join(json.dumps({
        "type": "resource-snapshot",
        "snapshot": {"request": {"url": url, "method": method},
                     "response": {"status": status, "statusText": ""},
                     "time": time_ms, "startedDateTime": "2026-08-26T00:00:00Z"},
    }) for url, method, status, time_ms in records)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("trace.network", lines)
        archive.writestr("trace.trace", "")
    return path


PAGE = "https://app.example.com/"


class TestReadEntries:
    def test_missing_file_is_empty_not_fatal(self, tmp_path):
        assert trace_network.read_entries(tmp_path / "nope.zip") == []

    def test_corrupt_zip_is_empty_not_fatal(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_text("not a zip")
        assert trace_network.read_entries(bad) == []

    def test_none_path_is_empty(self):
        assert trace_network.read_entries(None) == []


class TestSummarize:
    def test_absent_trace_reports_unavailable(self, tmp_path):
        # Unavailable must never be read as "the network was fine".
        assert trace_network.summarize(tmp_path / "nope.zip")["available"] is False

    def test_document_status_is_matched_to_the_failure_url(self, tmp_path):
        trace = _trace(tmp_path, [(PAGE, "GET", 200, 120),
                                  (PAGE + "assets/a.js", "GET", 200, 10)])
        assert trace_network.summarize(trace, PAGE)["document_status"] == 200

    def test_trailing_slash_still_matches_the_document(self, tmp_path):
        trace = _trace(tmp_path, [("https://app.example.com", "GET", 200, 12)])
        assert trace_network.summarize(trace, PAGE)["document_status"] == 200

    def test_server_errors_are_separated_from_client_errors(self, tmp_path):
        trace = _trace(tmp_path, [(PAGE, "GET", 200, 5),
                                  (PAGE + "api/x", "GET", 500, 30),
                                  (PAGE + "api/y", "GET", 404, 8)])
        summary = trace_network.summarize(trace, PAGE)
        assert len(summary["server_errors"]) == 1
        assert len(summary["client_errors"]) == 1

    def test_auth_rejections_are_their_own_bucket(self, tmp_path):
        trace = _trace(tmp_path, [(PAGE + "api/me", "GET", 401, 20)])
        summary = trace_network.summarize(trace, PAGE)
        assert len(summary["auth_rejections"]) == 1
        assert summary["client_errors"] == []

    def test_failed_requests_are_collected(self, tmp_path):
        trace = _trace(tmp_path, [(PAGE, "GET", 200, 10),
                                  ("https://collector.example/x", "POST", -1, -1)])
        assert len(trace_network.summarize(trace, PAGE)["failed"]) == 1

    def test_slow_requests_are_flagged(self, tmp_path):
        trace = _trace(tmp_path, [(PAGE, "GET", 200, 9000)])
        assert trace_network.summarize(trace, PAGE)["slow"]


class TestDescribe:
    def test_empty_when_the_channel_has_nothing(self):
        assert trace_network.describe({"available": False}) == ""

    def test_says_so_when_nothing_went_wrong(self, tmp_path):
        trace = _trace(tmp_path, [(PAGE, "GET", 200, 10)])
        assert "document request -> HTTP 200" in trace_network.describe(
            trace_network.summarize(trace, PAGE))


class TestDescribeOrdering:
    """The bug this pins: a diagnosis block whose network evidence was two
    failed Google ad beacons and "… 1 more".

    `summarize` already marks each request first- or third-party. `describe`
    ignored that and listed them in arrival order, so third-party noise pushed
    the requests the application actually made past `max_lines` and out of the
    report.
    """

    SUMMARY = {
        "available": True, "total": 9, "document_status": 200,
        "document_url": PAGE,
        "failed": [
            {"url": "https://www.google.co.in/pagead/1p-user-list/123",
             "method": "GET", "status": None, "first_party": False},
            {"url": "https://www.google.co.in/pagead/1p-user-list/456",
             "method": "GET", "status": None, "first_party": False},
            {"url": "https://app.example.com/api/profile",
             "method": "POST", "status": None, "first_party": True},
        ],
        "server_errors": [], "auth_rejections": [], "client_errors": [], "slow": [],
    }

    def test_the_application_request_is_reported_first(self):
        lines = trace_network.describe(self.SUMMARY).splitlines()
        assert "api/profile" in lines[1]

    def test_third_party_requests_are_labelled(self):
        rendered = trace_network.describe(self.SUMMARY)
        assert "pagead/1p-user-list/123 (None) [third-party]" in rendered

    def test_a_first_party_failure_survives_a_tight_budget(self):
        # The failure mode itself: with max_lines low, the one request that
        # matters must not be the one that gets truncated away.
        rendered = trace_network.describe(self.SUMMARY, max_lines=2)
        assert "api/profile" in rendered
