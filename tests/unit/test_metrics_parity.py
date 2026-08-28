"""Live-vs-replay parity, and backward compatibility with pre-metrics sessions.

The live stream (runner.py) and the replayed stream (audit_reader.replay_events)
are separate code paths. Comments in audit_reader record past regressions caused
by them drifting, so every metrics field must be asserted equal in both.
"""

import json

import pytest

from qa_agents_server import audit_reader, metrics_reader


def _point_agent_at(monkeypatch, name, audit_dir):
    """Re-register the agent with a temp audit dir. AgentSpec is a frozen
    dataclass, so the whole entry is replaced rather than a field mutated."""
    import dataclasses
    from qa_agents_server import agents as agents_mod
    spec = dataclasses.replace(agents_mod.AGENTS[name], audit_dir=audit_dir)
    monkeypatch.setitem(agents_mod.AGENTS, name, spec)
    return spec


@pytest.fixture
def healing_session(tmp_path, monkeypatch):
    """A healing session on disk, with metrics, as the agent would leave it."""
    root = tmp_path / "agents" / "test-healing-agent" / "audit"
    session = root / "20260828-120000-fix-LoginTest"
    (session / "metrics").mkdir(parents=True)

    (session / "00-reproduce.json").write_text(json.dumps(
        {"status": "locator", "test_name": "LoginTest#login", "headline": "stale"}))
    (session / "01-fix.json").write_text(json.dumps(
        {"succeeded": 2, "unverified": 0, "failed": 0, "distinct_fixes": 1}))
    (session / "02-ship.json").write_text(json.dumps(
        {"pr_url": "https://example.test/pr/1", "timestamp": "2026-08-28T12:10:00Z"}))
    (session / ".fix-passed").write_text("true")

    (session / "metrics" / "stages.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"index": 1, "key": "reproduce", "label": "[00/02] Reproduce", "attempt": 1,
         "started_at": 1000.0, "ended_at": 1030.0, "duration_s": 30.0,
         "exit_code": 0, "skipped": False},
        {"index": 2, "key": "fix", "label": "[01/02] Fix", "attempt": 1,
         "started_at": 1030.0, "ended_at": 1090.0, "duration_s": 60.0,
         "exit_code": 0, "skipped": False},
        {"index": 3, "key": "ship", "label": "[02/02] Ship", "attempt": 1,
         "started_at": 1090.0, "ended_at": 1100.0, "duration_s": 10.0,
         "exit_code": 0, "skipped": False},
    ]) + "\n")
    (session / "metrics" / "llm-calls.jsonl").write_text(json.dumps(
        {"ts": "2026-08-28T12:05:00Z", "stage": "fix", "attempt": 1,
         "cost_usd": 1.25, "num_turns": 12, "duration_s": 44.0,
         "input_tokens": 100, "output_tokens": 900,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "by_model": {"claude-sonnet-4-6": {"cost_usd": 1.25}}}) + "\n")

    _point_agent_at(monkeypatch, "test-healing-agent", root)
    return session


def test_replay_step_events_match_the_live_path(healing_session):
    """runner._mark_step_done folds in metrics_reader.step_metrics; the replayed
    stream must carry byte-identical fields for the same session."""
    rollup = metrics_reader.read_session_metrics(healing_session)
    events = audit_reader.replay_events(healing_session.name,
                                        agent="test-healing-agent") or []
    step_events = [e for e in events if e["kind"] == "step"]
    assert step_events, "expected step events"

    fields = set(metrics_reader.STEP_METRIC_FIELDS)
    for event in step_events:
        key = event["data"]["key"]
        live = metrics_reader.step_metrics(rollup, key)
        replayed = {k: v for k, v in event["data"].items() if k in fields}
        assert live == replayed, f"{key}: live={live} replayed={replayed}"


def test_replay_done_event_carries_the_same_totals(healing_session):
    rollup = metrics_reader.read_session_metrics(healing_session)
    expected = metrics_reader.totals(rollup)
    expected["stages"] = metrics_reader.stage_list(rollup)

    events = audit_reader.replay_events(healing_session.name,
                                        agent="test-healing-agent") or []
    done = [e for e in events if e["kind"] == "done"]
    assert done, "expected a done event"
    assert done[0]["data"]["metrics"] == expected


def test_stage_costs_are_attributed_to_the_right_stage(healing_session):
    rollup = metrics_reader.read_session_metrics(healing_session)
    by_key = {s["key"]: s for s in rollup["stages"]}
    assert by_key["fix"]["cost_usd"] == 1.25
    assert by_key["fix"]["llm_calls"] == 1
    # A stage that made no LLM call must report zero, not the run total.
    assert by_key["reproduce"]["cost_usd"] == 0.0
    assert by_key["ship"]["llm_calls"] == 0


def test_duration_falls_back_to_metrics_for_an_unshipped_run(tmp_path, monkeypatch):
    """_compute_duration_s returns None without a ship timestamp — which is
    exactly when knowing how long a run took still matters."""
    root = tmp_path / "agents" / "test-healing-agent" / "audit"
    session = root / "20260828-130000-fix-Gated"
    (session / "metrics").mkdir(parents=True)
    (session / "00-reproduce.json").write_text(json.dumps({"status": "passing"}))
    (session / "metrics" / "stages.jsonl").write_text(json.dumps(
        {"index": 1, "key": "reproduce", "label": "R", "attempt": 1,
         "started_at": 500.0, "ended_at": 545.0, "duration_s": 45.0,
         "exit_code": 0, "skipped": False}) + "\n")

    spec = _point_agent_at(monkeypatch, "test-healing-agent", root)

    summary = audit_reader._healing_summary(spec, session)
    assert summary["duration_s"] == 45.0


# ── backward compatibility ────────────────────────────────────────────────────

@pytest.fixture
def legacy_session(tmp_path, monkeypatch):
    """A session from before metrics existed: no metrics/ dir at all."""
    root = tmp_path / "agents" / "test-healing-agent" / "audit"
    session = root / "20260101-090000-fix-Old"
    session.mkdir(parents=True)
    (session / "00-reproduce.json").write_text(json.dumps({"status": "locator"}))
    (session / "01-fix.json").write_text(json.dumps({"succeeded": 1}))
    (session / "02-ship.json").write_text(json.dumps({"pr_url": "https://x/1"}))
    _point_agent_at(monkeypatch, "test-healing-agent", root)
    return session


def test_legacy_session_replays_without_metrics(legacy_session):
    events = audit_reader.replay_events(legacy_session.name,
                                        agent="test-healing-agent")
    assert events is not None
    for event in events:
        if event["kind"] == "step":
            # No invented zeros — the field is simply absent, so the UI renders
            # an em-dash rather than a confident "$0.00".
            assert "cost_usd" not in event["data"]
        if event["kind"] == "done":
            assert event["data"]["metrics"] == {}


def test_legacy_session_summary_has_no_cost(legacy_session):
    spec = audit_reader.get_agent("test-healing-agent")
    summary = audit_reader._healing_summary(spec, legacy_session)
    assert summary.get("cost_usd") is None


def test_metrics_reader_returns_none_for_a_legacy_session(legacy_session):
    assert metrics_reader.read_session_metrics(legacy_session) is None
    assert metrics_reader.step_metrics(None, "fix") == {}
    assert metrics_reader.totals(None) == {}
    assert metrics_reader.summary_fields(None) == {}
