"""A cancelled run reads back as cancelled, not failed.

Cancelling SIGTERMs the run; the killed step trips run.sh's ERR trap, which
writes .crashed next to the runner's .cancelled. The session detail used to
check .crashed first, so every cancel was reported as a failure.
"""

import dataclasses

import pytest

from qa_agents_server import audit_reader
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


def test_an_adaptation_stopped_by_infra_is_blocked_not_completed(tmp_path, monkeypatch):
    # Observed: a usage cap stopped item 2, ship still ran, and the run read "completed".
    name = "test-adaptation-agent"
    session = _session(tmp_path, monkeypatch, name)
    (session / ".skip-reason").write_text("infra\n")
    (session / "05-ship.json").write_text('{"verdict": "NEEDS-REVIEW", "ship_status": "dry_run"}')
    assert audit_reader.get_session(session.name, agent=name)["status"] == "blocked"
