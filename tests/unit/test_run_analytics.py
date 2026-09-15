"""Tests for the durable run-analytics store."""

import json
import time
from pathlib import Path

import pytest

from qa_agents_server import analytics


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "run_analytics.jsonl"
    monkeypatch.setenv("RUN_ANALYTICS_FILE", str(path))
    return path


def _session(tmp_path, agent, name="20260828-120000-x", **files):
    d = tmp_path / "agents" / agent / "audit" / name
    (d / "metrics").mkdir(parents=True)
    for fname, payload in files.items():
        (d / fname.replace("_", "-", 2)).write_text(json.dumps(payload))
    return d


def _write_metrics(d, cost=1.0, calls=1, out_tokens=100):
    (d / "metrics" / "llm-calls.jsonl").write_text(json.dumps({
        "ts": "2026-08-28T12:00:00Z", "stage": "fix", "cost_usd": cost,
        "num_turns": 3, "duration_s": 10.0, "output_tokens": out_tokens,
    }) + "\n")
    (d / "metrics" / "stages.jsonl").write_text(json.dumps({
        "index": 1, "key": "fix", "label": "[01/02] Fix", "attempt": 1,
        "started_at": 1000.0, "ended_at": 1060.0, "duration_s": 60.0,
        "exit_code": 0, "skipped": False,
    }) + "\n")


# ── outcome extraction ────────────────────────────────────────────────────────

def test_healing_counts_tests_not_edits(tmp_path, store):
    """01-fix.json counts TESTS; 02-ship.json counts distinct EDITS. One cluster
    fix can green several tests, so using ship would under-report output."""
    d = _session(tmp_path, "test-healing-agent")
    (d / "01-fix.json").write_text(json.dumps(
        {"succeeded": 3, "unverified": 1, "failed": 0, "distinct_fixes": 1}))
    (d / "02-ship.json").write_text(json.dumps({"succeeded": 1}))
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-healing-agent", status="completed")
    assert rec["outcomes"]["tests_fixed"] == 3
    assert rec["outcomes"]["distinct_fixes"] == 1


def test_authoring_counts_test_sources_not_files_count(tmp_path, store):
    d = _session(tmp_path, "test-authoring-agent")
    (d / "03-generate.json").write_text(json.dumps({"files_written": [
        "src/test/java/LoginTest.java", "src/test/java/CartTest.java",
        "src/main/java/Helper.java"]}))
    (d / "05-ship.json").write_text(json.dumps({"files_count": 9}))
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-authoring-agent")
    assert rec["outcomes"]["tests_created"] == 2   # not 9, and not 3
    assert rec["outcomes"]["files_changed"] == 9


def test_adaptation_is_not_scored_by_its_always_needs_review_verdict(tmp_path, store):
    """05_ship.py asserts NEEDS-REVIEW unconditionally; scoring by verdict would
    make every adaptation run a failure."""
    d = _session(tmp_path, "test-adaptation-agent")
    (d / ".verdict").write_text("NEEDS-REVIEW")
    (d / "04-adapt.json").write_text(json.dumps({"items": [
        {"status": "applied"}, {"status": "partial"}, {"status": "escalated"}]}))
    (d / "05-ship.json").write_text(json.dumps({"ship_status": "pr_created"}))
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-adaptation-agent")
    assert rec["status"] == "completed"
    assert rec["outcomes"]["items_adapted"] == 2
    assert rec["outcomes"]["escalations"] == 1


def test_adaptation_ladder_overrides_a_caller_supplied_failed_status(tmp_path, store):
    """_wait_and_reap passes the server's derived status, which reads .verdict —
    and adaptation writes NEEDS-REVIEW unconditionally, which the server maps to
    "failed". Honouring the caller here would score EVERY adaptation run as a
    failure; a dashboard showing 0% adaptation success is this bug."""
    d = _session(tmp_path, "test-adaptation-agent")
    (d / ".verdict").write_text("NEEDS-REVIEW")
    (d / "05-ship.json").write_text(json.dumps({"ship_status": "pr_created"}))
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-adaptation-agent", status="failed")
    assert rec["status"] == "completed"


def test_adaptation_ship_failure_is_still_a_failure(tmp_path, store):
    d = _session(tmp_path, "test-adaptation-agent")
    (d / "05-ship.json").write_text(json.dumps({"ship_status": "push_failed"}))
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-adaptation-agent", status="completed")
    assert rec["status"] == "failed"


def test_unshipped_run_records_zeros_not_nulls(tmp_path, store):
    """A null would silently drop out of a sum; the run produced nothing."""
    d = _session(tmp_path, "test-healing-agent")
    _write_metrics(d, cost=0.42)
    rec = analytics.build_record(d, agent="test-healing-agent", status="cancelled")
    assert all(v == 0 for v in rec["outcomes"].values())
    # Spend is real even with no output.
    assert rec["cost_usd"] == 0.42


# ── store semantics ───────────────────────────────────────────────────────────

def test_spend_counts_for_every_terminal_status(tmp_path, store):
    for i, status in enumerate(("completed", "failed", "cancelled", "interrupted")):
        d = _session(tmp_path, "test-healing-agent", name=f"20260828-12000{i}-x")
        _write_metrics(d, cost=1.0)
        analytics.append_from_session(d, agent="test-healing-agent", status=status,
                                      started_at=time.time())
    overall = analytics.query("all")["overall"]
    assert overall["runs"] == 4
    assert overall["cost_usd"] == 4.0
    assert (overall["succeeded"], overall["failed"], overall["cancelled"]) == (1, 1, 1)


def test_duplicate_session_ids_resolve_newest_wins(tmp_path, store):
    """Both the agent and the server write for a server-launched run, and a
    resumed run legitimately produces two rows for one id."""
    d = _session(tmp_path, "test-healing-agent")
    _write_metrics(d, cost=1.0)
    analytics.append_from_session(d, agent="test-healing-agent", status="failed",
                                  started_at=time.time())
    time.sleep(0.01)
    _write_metrics(d, cost=2.5)
    analytics.append_from_session(d, agent="test-healing-agent", status="completed",
                                  started_at=time.time())
    overall = analytics.query("all")["overall"]
    assert overall["runs"] == 1
    assert overall["cost_usd"] == 2.5
    assert overall["succeeded"] == 1


def test_truncated_line_is_skipped(tmp_path, store):
    d = _session(tmp_path, "test-healing-agent")
    _write_metrics(d, cost=1.0)
    analytics.append_from_session(d, agent="test-healing-agent", status="completed",
                                  started_at=time.time())
    with store.open("a", encoding="utf-8") as h:
        h.write('{"session_id": "half-writ')
    assert analytics.query("all")["overall"]["runs"] == 1


def test_window_filtering_selects_the_right_subset(tmp_path, store):
    now = time.time()
    ages = {"recent": now - 3600, "week": now - 3 * 86400, "old": now - 20 * 86400}
    for i, (label, ts) in enumerate(ages.items()):
        d = _session(tmp_path, "test-healing-agent", name=f"20260828-1200{i}0-{label}")
        _write_metrics(d, cost=1.0)
        analytics.append_from_session(d, agent="test-healing-agent",
                                      status="completed", started_at=ts)
    assert analytics.query("24h")["overall"]["runs"] == 1
    assert analytics.query("7d")["overall"]["runs"] == 2
    assert analytics.query("30d")["overall"]["runs"] == 3
    assert analytics.query("all")["overall"]["runs"] == 3


def test_query_survives_a_missing_store(store):
    assert analytics.query("7d")["overall"]["runs"] == 0


def test_cost_per_outcome_is_none_when_nothing_was_produced(tmp_path, store):
    d = _session(tmp_path, "test-healing-agent")
    _write_metrics(d, cost=5.0)
    analytics.append_from_session(d, agent="test-healing-agent", status="failed",
                                  started_at=time.time())
    overall = analytics.query("all")["overall"]
    assert overall["cost_usd"] == 5.0
    assert overall["cost_per_outcome_usd"] is None    # not a divide-by-zero


# ── status ladders ────────────────────────────────────────────────────────────

def test_healing_gate_decides_status_not_the_exit_code(tmp_path, store):
    """run.sh exits 0 whether or not anything was fixed, so a shell-derived
    "completed" would report a gated no-op as a successful heal."""
    d = _session(tmp_path, "test-healing-agent")
    (d / ".fix-passed").write_text("false")
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-healing-agent", status="completed")
    assert rec["status"] == "failed"


def test_healing_gate_skipped_is_diagnosed_not_failed(tmp_path, store):
    """The gate stopping a run on purpose is the design working — painting it
    red trains people to ignore the runs worth reading."""
    d = _session(tmp_path, "test-healing-agent")
    (d / ".fix-passed").write_text("skipped")
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-healing-agent", status="completed")
    assert rec["status"] == "diagnosed"


def test_healing_crash_marker_wins(tmp_path, store):
    d = _session(tmp_path, "test-healing-agent")
    (d / ".fix-passed").write_text("true")
    (d / ".crashed").write_text("boom")
    _write_metrics(d)
    assert analytics.build_record(d, agent="test-healing-agent")["status"] == "failed"


def test_verdict_and_gate_are_recorded(tmp_path, store):
    d = _session(tmp_path, "test-authoring-agent")
    (d / ".verdict").write_text("APPROVED\n")
    (d / ".fix-passed").write_text("true\n")
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-authoring-agent", status="completed")
    assert rec["verdict"] == "APPROVED"
    assert rec["fix_gate"] == "true"


def test_missing_markers_record_none_not_empty_string(tmp_path, store):
    d = _session(tmp_path, "test-authoring-agent")
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-authoring-agent")
    assert rec["verdict"] is None and rec["fix_gate"] is None


# ── The base a run was actually built on ──────────────────────────────────────

def test_the_base_branch_comes_from_the_session_marker(tmp_path, store):
    """Read from the session's own file, never from os.environ. build_record has
    two callers — shared/session.sh inside the agent, and runner.py in the
    *server's* environment — so an env read would be right for one and would
    quietly report the org default for the other."""
    d = _session(tmp_path, "test-authoring-agent")
    (d / ".base-branch").write_text("release/2.3\ndeadbeefcafe\n")

    assert analytics.build_record(d)["base_branch"] == "release/2.3"


def test_a_run_on_the_default_records_an_empty_base_branch(tmp_path, store):
    """Absent is a normal run, not a missing field — it must not raise, and it
    must not be reported as 'main' when nobody asked for main."""
    d = _session(tmp_path, "test-authoring-agent")

    assert analytics.build_record(d)["base_branch"] == ""
