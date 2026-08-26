"""Tests for scripts/diagnosis_soak.py.

The soak decides whether the diagnosis is trusted enough to gate work, so the
script has to see both mistakes. It originally saw only the runs that stopped,
which measures false stops and is structurally blind to the costlier error — a
fix attempted on a page the test never reached.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def soak():
    spec = importlib.util.spec_from_file_location(
        "diagnosis_soak", ROOT / "scripts" / "diagnosis_soak.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session(tmp_path, name="20260826-120000-fix-x", reproduce=None, fix=None):
    d = tmp_path / name
    d.mkdir()
    if reproduce is not None:
        (d / "00-reproduce.json").write_text(json.dumps(reproduce))
    if fix is not None:
        (d / "01-fix.json").write_text(json.dumps(fix))
    return d


class TestReadSession:
    def test_a_stop_verdict_with_no_fix(self, soak, tmp_path):
        row = soak.read_session(_session(tmp_path, reproduce={
            "status": "WRONG_PAGE", "diagnosis": {"verdict": "WRONG_PAGE",
                                                  "confidence": "HIGH"}}))
        assert (row["verdict"], row["outcome"]) == ("WRONG_PAGE", "no_fix")

    def test_a_verdict_that_was_acted_on_and_worked(self, soak, tmp_path):
        # The blind spot: this row has to exist for false LOCATOR_STALE to be
        # measurable at all.
        row = soak.read_session(_session(tmp_path, reproduce={
            "status": "queued", "diagnosis": {"verdict": "LOCATOR_STALE",
                                              "confidence": "HIGH"}},
            fix={"fixes": [{"target_file": "P.java"}], "failed_fixes": []}))
        assert (row["verdict"], row["outcome"]) == ("LOCATOR_STALE", "fix_verified")

    def test_a_fix_that_could_not_survive_verification(self, soak, tmp_path):
        row = soak.read_session(_session(tmp_path, reproduce={
            "status": "queued", "diagnosis": {"verdict": "LOCATOR_STALE"}},
            fix={"fixes": [], "failed_fixes": [{"status": "test_failed"}]}))
        assert row["outcome"] == "fix_reverted"

    def test_a_forced_override_is_recorded(self, soak, tmp_path):
        row = soak.read_session(_session(tmp_path, reproduce={
            "status": "queued", "forced": True,
            "diagnosis": {"verdict": "WRONG_PAGE"}},
            fix={"fixes": [{"target_file": "P.java"}]}))
        assert row["forced"] is True and row["outcome"] == "fix_verified"

    def test_a_session_with_no_verdict_is_skipped(self, soak, tmp_path):
        assert soak.read_session(_session(tmp_path, reproduce={"status": "passing"})) is None

    def test_an_empty_session_is_skipped(self, soak, tmp_path):
        assert soak.read_session(_session(tmp_path)) is None
