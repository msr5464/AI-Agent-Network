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
