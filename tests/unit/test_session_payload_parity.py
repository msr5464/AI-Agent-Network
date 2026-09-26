"""Every agent's session detail payload has the same shape.

The three parsers in audit_reader used to hand-write their own `steps` and
`reports` literals, and they drifted exactly as you would expect: authoring
never grew a `reports` dict at all even though its agent writes the markdown;
healing's `steps` omitted `locate` while its `reports` included it; and the two
keyed reports differently (`<name>_md` vs the step key), so the UI needed a
different renderer per agent for one endpoint.

These tests pin the contract that replaced them: both dicts are derived from
spec.detail_artifacts(), so they always agree, for every agent — including one
registered after these tests were written.
"""

import dataclasses

import pytest

from qa_agents_server import audit_reader
from qa_agents_server import agents as agents_mod


def _point_agent_at(monkeypatch, name, audit_dir):
    """Re-register the agent with a temp audit dir. AgentSpec is a frozen
    dataclass, so the whole entry is replaced rather than a field mutated."""
    spec = dataclasses.replace(agents_mod.AGENTS[name], audit_dir=audit_dir)
    monkeypatch.setitem(agents_mod.AGENTS, name, spec)
    return spec


def _build_session(spec, audit_dir, *, write_markdown=True):
    """A session on disk with every artefact the spec names."""
    session = audit_dir / f"20260101-120000-{spec.session_prefix}-demo"
    session.mkdir(parents=True)
    (session / "00-session-init.md").write_text("# init\n")
    for _key, filename, _label in spec.detail_artifacts():
        (session / filename).write_text("{}")
        if write_markdown:
            (session / filename.replace(".json", ".md")).write_text("# report\n")
    return session


@pytest.mark.parametrize("name", sorted(agents_mod.AGENTS))
def test_steps_and_reports_share_a_key_set(tmp_path, monkeypatch, name):
    """A renderer must be able to walk one dict and index the other."""
    audit_dir = tmp_path / name / "audit"
    spec = _point_agent_at(monkeypatch, name, audit_dir)
    session = _build_session(spec, audit_dir)

    payload = audit_reader.get_session(session.name, agent=name)

    expected = {key for key, _f, _l in spec.detail_artifacts()}
    assert set(payload["steps"]) == expected
    assert set(payload["reports"]) == expected


@pytest.mark.parametrize("name", sorted(agents_mod.AGENTS))
def test_every_step_is_represented(tmp_path, monkeypatch, name):
    """Every progress-bar step appears in the detail payload.

    Healing's hand-written literal dropped `locate`, so the one step whose whole
    point is explaining how a locator was chosen had nothing behind it.
    """
    audit_dir = tmp_path / name / "audit"
    spec = _point_agent_at(monkeypatch, name, audit_dir)
    session = _build_session(spec, audit_dir)

    payload = audit_reader.get_session(session.name, agent=name)

    for key in spec.step_keys():
        assert key in payload["steps"], f"{name} step {key!r} missing from payload"
        assert key in payload["reports"], f"{name} step {key!r} has no report slot"


@pytest.mark.parametrize("name", sorted(agents_mod.AGENTS))
def test_missing_markdown_is_none_not_an_error(tmp_path, monkeypatch, name):
    """A run that died mid-pipeline still has to open.

    Authoring's API-only path writes 02-validate-web.json and no matching .md at
    all, so a missing report is a normal state, not a corrupt session.
    """
    audit_dir = tmp_path / name / "audit"
    spec = _point_agent_at(monkeypatch, name, audit_dir)
    session = _build_session(spec, audit_dir, write_markdown=False)

    payload = audit_reader.get_session(session.name, agent=name)

    assert set(payload["reports"]) == {key for key, _f, _l in spec.detail_artifacts()}
    assert all(v is None for v in payload["reports"].values())


@pytest.mark.parametrize("name", sorted(agents_mod.AGENTS))
def test_detail_carries_the_fields_history_rows_carry(tmp_path, monkeypatch, name):
    """Detail builds on the row summary, rather than being a second literal.

    Authoring's detail getter used to be written separately from its list
    function, so a detail payload had no pr_url, duration_s or cost fields even
    though the row rendered beside it did. The Result card needs them.
    """
    audit_dir = tmp_path / name / "audit"
    spec = _point_agent_at(monkeypatch, name, audit_dir)
    session = _build_session(spec, audit_dir)

    detail = audit_reader.get_session(session.name, agent=name)
    rows = audit_reader.list_sessions(agent=name, limit=10, offset=0)
    row = next(r for r in rows if r["session_id"] == session.name)

    missing = sorted(set(row) - set(detail))
    assert not missing, f"{name} detail payload is missing row fields: {missing}"


def test_extra_artifacts_are_not_progress_steps():
    """Extras ride in the payload without becoming steps the UI polls.

    Authoring's validate_api shares step 02's slot and adaptation's explore
    halves both feed the combined 03-explore.json the chip is keyed on. Promoting
    either to a step would leave a progress chip stuck on a file that a given run
    may never write.
    """
    for name, spec in agents_mod.AGENTS.items():
        extra_keys = {key for key, _f, _l in spec.extra_artifacts}
        assert not (extra_keys & set(spec.step_keys())), (
            f"{name} lists the same key as both a step and an extra artifact")
