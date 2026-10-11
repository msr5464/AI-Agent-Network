"""A cancelled run reads back as cancelled, not failed.

Cancelling SIGTERMs the run; the killed step trips run.sh's ERR trap, which
writes .crashed next to the runner's .cancelled. The session detail used to
check .crashed first, so every cancel was reported as a failure.
"""

import dataclasses

import pytest

from qa_agents_server import analytics, audit_reader, runner
from qa_agents_server import agents as agents_mod


def _session(tmp_path, monkeypatch, name, *markers):
    audit_dir = tmp_path / name / "audit"
    spec = dataclasses.replace(agents_mod.AGENTS[name], audit_dir=audit_dir)
    monkeypatch.setitem(agents_mod.AGENTS, name, spec)
    session = audit_dir / f"20260101-120000-{spec.session_prefix}-demo"
    session.mkdir(parents=True)
    (session / "00-session-init.md").write_text("# init\n")
    for marker in markers:
        (session / marker).write_text("x\n")
    return session


@pytest.mark.parametrize("name", ["test-adaptation-agent", "test-healing-agent"])
def test_cancel_outranks_the_crash_it_causes(tmp_path, monkeypatch, name):
    session = _session(tmp_path, monkeypatch, name, ".crashed", ".cancelled")
    assert audit_reader.get_session(session.name, agent=name)["status"] == "cancelled"


@pytest.mark.parametrize("name,expected", [("test-adaptation-agent", "failed"),
                                           ("test-healing-agent", "crashed")])
def test_a_real_crash_still_reads_as_one(tmp_path, monkeypatch, name, expected):
    session = _session(tmp_path, monkeypatch, name, ".crashed")
    assert audit_reader.get_session(session.name, agent=name)["status"] == expected


def test_an_authoring_run_stopped_by_a_failing_step_reads_as_failed(tmp_path, monkeypatch):
    """Observed: step 02 exited 1, run.sh stopped, and the session replayed as
    "running" with no Cancel button: no verdict, and step 02's file said "skipped".
    shared/session.sh now writes .crashed for every agent's non-zero exit."""
    name = "test-authoring-agent"
    session = _session(tmp_path, monkeypatch, name)
    assert audit_reader.get_session(session.name, agent=name)["status"] == "running"
    (session / ".crashed").write_text("Exited with 1\n")
    assert audit_reader.get_session(session.name, agent=name)["status"] == "failed"
    (session / ".cancelled").write_text("true\n")
    assert audit_reader.get_session(session.name, agent=name)["status"] == "cancelled"


def test_a_failing_fix_attempt_with_another_to_come_is_still_running(tmp_path, monkeypatch):
    """Observed: History showed "failed" while fix attempt 2 ran, read off attempt
    1's result. Step 04 now says, as step 02 does, whether another attempt follows."""
    import json
    name = "test-authoring-agent"
    session = _session(tmp_path, monkeypatch, name)
    result = session / "04-run-and-fix.json"
    result.write_text(json.dumps({"attempt": 1, "passed": False, "final_attempt": False}))
    assert audit_reader.get_session(session.name, agent=name)["status"] == "running"
    result.write_text(json.dumps({"attempt": 2, "passed": False, "final_attempt": True}))
    assert audit_reader.get_session(session.name, agent=name)["status"] == "failed"


def test_an_adaptation_stopped_by_infra_is_blocked_not_completed(tmp_path, monkeypatch):
    # Observed: a usage cap stopped item 2, ship still ran, and the run read "completed".
    name = "test-adaptation-agent"
    session = _session(tmp_path, monkeypatch, name)
    (session / ".skip-reason").write_text("infra\n")
    (session / "05-ship.json").write_text('{"verdict": "NEEDS-REVIEW", "ship_status": "dry_run"}')
    assert audit_reader.get_session(session.name, agent=name)["status"] == "blocked"


ADAPTATION = "test-adaptation-agent"


def _ended(session, gate="skipped", reason=""):
    (session / ".fix-passed").write_text(gate + "\n")
    if reason:
        (session / ".skip-reason").write_text(reason + "\n")
    (session / ".verdict").write_text("NEEDS-REVIEW\n")
    (session / "05-ship.json").write_text('{"verdict": "NEEDS-REVIEW", "ship_status": "dry_run"}')


@pytest.mark.parametrize("reason,expected", [
    ("no-work", "diagnosed"), ("escalate", "diagnosed"), ("stuck", "failed"),
    ("unsafe", "failed"), ("infra", "blocked"), ("no-session", "blocked"),
    ("unreachable", "blocked"), ("explore-only", "completed"),
])
def test_an_adaptation_row_reads_as_analytics_counts_it(tmp_path, monkeypatch, reason, expected):
    # One run was failed in the run record, completed in History and diagnosed
    # in Analytics, each by a rule of its own.
    session = _session(tmp_path, monkeypatch, ADAPTATION)
    _ended(session, reason=reason)
    row = audit_reader.get_session(session.name, agent=ADAPTATION)["status"]
    counted = analytics.run_status(ADAPTATION, session)
    assert row == expected
    # Analytics leaves an explore-only run out of its rates; as a row it finished.
    assert row == ("completed" if counted == "explore-only" else counted)


def test_a_gate_written_between_adapt_attempts_does_not_end_the_row(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, ADAPTATION)
    (session / ".fix-passed").write_text("false\n")
    assert audit_reader.get_session(session.name, agent=ADAPTATION)["status"] == "running"


@pytest.mark.parametrize("gate,reason,expected", [
    ("true", "", "completed"), ("skipped", "no-work", "completed"),
    ("skipped", "infra", "completed"), ("skipped", "unreachable", "completed"),
    ("skipped", "unsafe", "failed"), ("skipped", "stuck", "failed"),
])
def test_an_adaptation_run_record_is_not_failed_for_needing_review(
        tmp_path, monkeypatch, gate, reason, expected):
    # Its verdict is NEEDS-REVIEW by design, so "not APPROVED" recorded every run
    # as failed.
    session = _session(tmp_path, monkeypatch, ADAPTATION)
    _ended(session, gate, reason)
    assert runner._final_status(ADAPTATION, session, 0) == expected


@pytest.mark.parametrize("name", [ADAPTATION, "test-healing-agent"])
def test_a_run_record_only_ever_holds_a_state_the_stream_ends_on(tmp_path, monkeypatch, name):
    # Anything else leaves the live view open on a run that has finished.
    session = _session(tmp_path, monkeypatch, name)
    for gate, reason in (("skipped", "infra"), ("skipped", "escalate"), ("true", ""),
                         ("false", "")):
        _ended(session, gate, reason)
        for exit_code in (0, 1):
            assert runner._final_status(name, session, exit_code) in runner.TERMINAL_STATUSES
    assert runner._final_status(name, session, 1) == "failed"


def test_other_agents_run_records_still_follow_the_verdict(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, "test-healing-agent")
    (session / ".verdict").write_text("NEEDS-REVIEW\n")
    assert runner._final_status("test-healing-agent", session, 0) == "failed"
    (session / ".verdict").write_text("APPROVED\n")
    assert runner._final_status("test-healing-agent", session, 0) == "completed"
