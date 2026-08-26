"""Tests for shared/verdict_feedback.py.

Recording only. Nothing reads these back automatically, and that is deliberate:
a threshold moved by a machine, on evidence the same machine gathered, is how a
system talks itself into a corner.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import verdict_feedback


class TestRecord:
    def test_records_a_false_stop(self, tmp_path):
        path = tmp_path / "known-issues.json"
        assert verdict_feedback.record(path, "false_stop", "a.b.C.d", "WRONG_PAGE",
                                       detail="forced fix verified") is True
        entries = json.loads(path.read_text())
        assert entries[0]["kind"] == "false_stop"
        assert entries[0]["verdict"] == "WRONG_PAGE"
        assert entries[0]["recorded_at"]

    def test_appends_rather_than_replaces(self, tmp_path):
        path = tmp_path / "known-issues.json"
        verdict_feedback.record(path, "false_stop", "t1", "WRONG_PAGE")
        verdict_feedback.record(path, "false_locator_fix", "t2", "LOCATOR_STALE")
        assert len(json.loads(path.read_text())) == 2

    def test_preserves_an_existing_file(self, tmp_path):
        path = tmp_path / "known-issues.json"
        path.write_text(json.dumps([{"kind": "confirmed", "test_name": "old"}]))
        verdict_feedback.record(path, "false_stop", "new", "WRONG_PAGE")
        entries = json.loads(path.read_text())
        assert entries[0]["test_name"] == "old" and len(entries) == 2

    def test_an_unknown_kind_is_refused(self, tmp_path):
        assert verdict_feedback.record(tmp_path / "f.json", "nonsense", "t", "v") is False

    def test_an_unwritable_path_never_raises(self, tmp_path):
        # Losing a data point must never cost a fix run.
        blocker = tmp_path / "file"
        blocker.write_text("x")
        assert verdict_feedback.record(blocker / "nested.json", "false_stop",
                                       "t", "v") is False

    def test_a_corrupt_file_does_not_stop_recording(self, tmp_path):
        path = tmp_path / "known-issues.json"
        path.write_text("{not json")
        assert verdict_feedback.record(path, "false_stop", "t", "v") is True


class TestSummarize:
    def test_counts_per_kind(self, tmp_path):
        path = tmp_path / "f.json"
        verdict_feedback.record(path, "false_stop", "t1", "WRONG_PAGE")
        verdict_feedback.record(path, "false_stop", "t2", "ERROR_STATE")
        verdict_feedback.record(path, "false_locator_fix", "t3", "LOCATOR_STALE")
        assert verdict_feedback.summarize(path) == {"false_stop": 2,
                                                    "false_locator_fix": 1}

    def test_empty_file_is_empty_counts(self, tmp_path):
        assert verdict_feedback.summarize(tmp_path / "nope.json") == {}
