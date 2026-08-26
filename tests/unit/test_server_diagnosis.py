"""Tests for how the server reports a diagnosed run.

A run that worked out why a test fails and correctly declined to edit anything
used to surface as "0 fixed / could not fix" — the same unhelpful outcome as
before any of this existed, with the answer buried in an audit file. Showing it
red also trains people to skip exactly the runs worth reading.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa_agents_server import audit_reader
from shared import diagnosis


def _session(tmp_path, gate="skipped", reason="diagnosed", reproduce=None):
    session = tmp_path / "20260826-120000-fix-local-SomeTest"
    session.mkdir()
    (session / ".fix-passed").write_text(gate)
    if reason:
        (session / ".skip-reason").write_text(reason)
    if reproduce is not None:
        (session / "00-reproduce.json").write_text(json.dumps(reproduce))
    return session


class TestStatus:
    def test_a_diagnosed_run_is_not_a_failure(self, tmp_path):
        session = _session(tmp_path, reproduce={"status": "WRONG_PAGE",
                                                "headline": "never reached the page"})
        assert audit_reader._derive_status(session, None) == "diagnosed"

    def test_an_ordinary_skip_is_unchanged(self, tmp_path):
        session = _session(tmp_path, reason="no-work")
        assert audit_reader._derive_status(session, None) != "diagnosed"

    def test_a_real_failure_is_unchanged(self, tmp_path):
        session = _session(tmp_path, gate="false", reason=None)
        assert audit_reader._derive_status(session, None) != "diagnosed"

    @pytest.mark.parametrize("verdict", sorted(diagnosis.STOP))
    def test_every_stop_verdict_reads_as_diagnosed(self, verdict):
        assert audit_reader._skipped_status(verdict) == "diagnosed"

    def test_infrastructure_still_reads_as_blocked(self):
        # The test never ran, so there is nothing diagnosed about it.
        assert audit_reader._skipped_status("INFRA_DB") == "blocked"

    def test_an_unrecognised_shape_is_not_claimed_as_diagnosed(self):
        assert audit_reader._skipped_status("UNKNOWN") == "not_a_locator"


class TestOutcomePayload:
    def test_carries_the_verdict_and_remediation(self, tmp_path):
        session = _session(tmp_path, reproduce={
            "status": "DATA_PRECONDITION", "headline": "regenerate the session"})
        outcome = audit_reader._diagnosis_outcome(session, "skipped")
        assert outcome["verdict"] == "DATA_PRECONDITION"
        assert "regenerate" in outcome["remediation"]

    def test_absent_for_a_run_that_was_not_diagnosed(self, tmp_path):
        assert audit_reader._diagnosis_outcome(
            _session(tmp_path, reason="no-work"), "skipped") is None

    def test_absent_for_a_failed_run(self, tmp_path):
        assert audit_reader._diagnosis_outcome(
            _session(tmp_path, gate="false", reason=None), "false") is None


class TestNoDrift:
    def test_the_server_shares_the_engines_verdict_list(self):
        # This file already carries three "keep in sync" comments against copies
        # elsewhere and nothing enforces any of them; this one is imported.
        assert audit_reader._STOP_VERDICTS is diagnosis.STOP
