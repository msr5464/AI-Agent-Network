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
    (d / ".fix-passed").write_text("true")
    _write_metrics(d)
    rec = analytics.build_record(d, agent="test-authoring-agent")
    assert rec["outcomes"]["tests_created"] == 2   # not 9, and not 3
    assert rec["outcomes"]["files_changed"] == 9


@pytest.mark.parametrize("gate", ["false", "stuck", None])
def test_authoring_credits_no_tests_until_the_fix_gate_passes(tmp_path, store, gate):
    """A generated test that never passed is not a test produced."""
    d = _session(tmp_path, "test-authoring-agent")
    (d / "03-generate.json").write_text(json.dumps(
        {"files_written": ["src/test/java/LoginTest.java"]}))
    if gate:
        (d / ".fix-passed").write_text(gate)
    rec = analytics.build_record(d, agent="test-authoring-agent", status="failed")
    assert rec["outcomes"]["tests_created"] == 0


def test_a_dir_outside_a_real_agent_writes_no_row(tmp_path, store):
    """Its grandparent's name (once a tmp UUID) must not become an agent."""
    d = tmp_path / "26d649f1-7c35-452d-88b8-217ce6bfe27c" / "x" / "fake-audit-renamed"
    d.mkdir(parents=True)
    assert analytics.build_record(d) is None
    assert analytics.append_from_session(d) is False
    assert not store.exists()


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
    # Cancelled and interrupted are no verdict on the agent: not runs in the
    # success rate, but every dollar still counts.
    assert overall["runs"] == 2
    assert overall["cost_usd"] == 4.0
    assert (overall["succeeded"], overall["failed"], overall["excluded"]) == (1, 1, 2)


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
    # The trend's per-agent days add up to that agent's row.
    q = analytics.query("all")
    days = q["series_by_agent"]["test-healing-agent"]
    assert len(days) == 3
    for key in ("runs", "cost_usd", "llm_calls"):
        assert sum(d[key] for d in days) == pytest.approx(q["by_agent"]["test-healing-agent"][key])


def test_custom_range_clear_removes_only_rows_inside_it(tmp_path, store):
    now = time.time()
    ages = {"recent": now - 3600, "week": now - 3 * 86400, "old": now - 20 * 86400}
    for i, (label, ts) in enumerate(ages.items()):
        d = _session(tmp_path, "test-healing-agent", name=f"20260828-1200{i}0-{label}")
        _write_metrics(d, cost=1.0)
        analytics.append_from_session(d, agent="test-healing-agent",
                                      status="completed", started_at=ts)
    removed = analytics.clear_history(window="custom", since=now - 5 * 86400,
                                      until=now - 86400)
    assert removed == ["20260828-120010-week"]
    assert analytics.query("all")["overall"]["runs"] == 2


def test_every_form_of_a_users_id_is_the_same_person(tmp_path, store):
    """Legacy "default", the md5 hash and the readable `user-<name>` all name
    the admin; the Studio filters by hash, a member's own view by readable id."""
    for i, uid in enumerate(("default", "21232f297a57", "user-admin", "13d815eda90a")):
        d = _session(tmp_path, "test-healing-agent", name=f"20260828-1200{i}0-u{i}")
        _write_metrics(d, cost=1.0)
        analytics.append_from_session(d, agent="test-healing-agent", status="completed",
                                      started_at=time.time(), user_id=uid)
    assert analytics.query("all", user_id="21232f297a57")["overall"]["runs"] == 3
    assert analytics.query("all", user_id="user-admin")["overall"]["runs"] == 3
    assert analytics.query("all", user_id="13d815eda90a")["overall"]["runs"] == 1


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


def test_a_diagnosed_run_counts_as_succeeded(tmp_path, store):
    d = _session(tmp_path, "test-healing-agent")
    (d / ".fix-passed").write_text("skipped")
    _write_metrics(d)
    analytics.append_from_session(d, agent="test-healing-agent", started_at=time.time())
    overall = analytics.query("all")["overall"]
    assert (overall["runs"], overall["succeeded"], overall["excluded"]) == (1, 1, 0)


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


def test_adaptation_crash_marker_wins(tmp_path, store):
    d = _session(tmp_path, "test-adaptation-agent")
    (d / ".crashed").write_text("boom")
    _write_metrics(d)
    assert analytics.build_record(d, agent="test-adaptation-agent")["status"] == "failed"


@pytest.mark.parametrize("agent", ["test-healing-agent", "test-adaptation-agent"])
def test_cancel_outranks_the_crash_it_causes(tmp_path, store, agent):
    """Cancelling kills the step, and run.sh's ERR trap then writes .crashed as
    well. The run was cancelled, not failed — the order session.sh already uses."""
    d = _session(tmp_path, agent)
    (d / ".crashed").write_text("Crashed at run.sh:66 with exit 1")
    (d / ".cancelled").write_text("true")
    _write_metrics(d)
    assert analytics.build_record(d, agent=agent)["status"] == "cancelled"


# ── how a run counts ──────────────────────────────────────────────────────────

def _run(tmp_path, agent, caller="completed", **files):
    """A session with metrics and the given marker files; returns its record."""
    d = _session(tmp_path, agent)
    for name, body in files.items():
        (d / name).write_text(body if isinstance(body, str) else json.dumps(body))
    _write_metrics(d)
    return analytics.build_record(d, agent=agent, status=caller)


@pytest.mark.parametrize("agent", ["test-authoring-agent", "test-healing-agent",
                                   "test-adaptation-agent"])
def test_a_restart_outranks_the_crash_it_causes(tmp_path, store, agent):
    rec = _run(tmp_path, agent, caller="failed",
               **{".crashed": "exit 143", ".interrupted": "true"})
    assert rec["status"] == "interrupted"


@pytest.mark.parametrize("caller", ["completed", "failed"])
@pytest.mark.parametrize("gate,expected", [("true", "completed"), ("false", "failed"),
                                           ("stuck", "failed")])
def test_authoring_is_judged_by_its_gate_not_by_who_launched_it(tmp_path, store, caller,
                                                                gate, expected):
    """The CLI passes the exit code, the server passes .verdict — one outcome
    must not score differently depending on which one wrote last."""
    assert _run(tmp_path, "test-authoring-agent", caller,
                **{".fix-passed": gate})["status"] == expected


def test_authoring_reproducing_a_documented_defect_is_a_correct_test(tmp_path, store):
    rec = _run(tmp_path, "test-authoring-agent", "failed", **{
        ".fix-passed": "defect",
        "03-generate.json": {"files_written": ["src/test/java/CartTest.java"]}})
    assert rec["status"] == "diagnosed"
    assert rec["outcomes"]["tests_created"] == 1


def test_authoring_that_never_ran_a_test_is_blocked(tmp_path, store):
    assert _run(tmp_path, "test-authoring-agent", **{".fix-passed": "skipped"})["status"] == "blocked"


def test_authoring_work_that_passed_counts_even_when_the_push_failed(tmp_path, store):
    """Delivery is not the work: the test passed, and a Ship retry re-sends it."""
    rec = _run(tmp_path, "test-authoring-agent", **{
        ".fix-passed": "true", "05-ship.json": {"ship_status": "push_failed"},
        "03-generate.json": {"files_written": ["src/test/java/CartTest.java"]}})
    assert rec["status"] == "completed"
    assert rec["outcomes"]["tests_created"] == 1


def test_healing_that_could_not_run_the_test_is_blocked_not_diagnosed(tmp_path, store):
    rec = _run(tmp_path, "test-healing-agent",
               **{".fix-passed": "skipped", ".skip-reason": "infra"})
    assert rec["status"] == "blocked"


def test_healing_fixes_count_even_when_the_push_was_rejected(tmp_path, store):
    """The verified fixes are committed on the local branch; only the push failed."""
    rec = _run(tmp_path, "test-healing-agent", **{
        ".fix-passed": "true", "01-fix.json": {"succeeded": 5},
        "02-ship.json": {"succeeded": 5, "pr_url": None}})
    assert rec["status"] == "completed"
    assert rec["outcomes"]["tests_fixed"] == 5


@pytest.mark.parametrize("reason,expected", [
    ("no-work", "diagnosed"), ("escalate", "diagnosed"), ("stuck", "failed"),
    ("unsafe", "failed"), ("infra", "blocked"), ("no-session", "blocked"),
    ("unreachable", "blocked"), ("explore-only", "explore-only"),
])
def test_adaptation_skip_reasons(tmp_path, store, reason, expected):
    rec = _run(tmp_path, "test-adaptation-agent", **{
        ".fix-passed": "skipped", ".skip-reason": reason,
        "05-ship.json": {"ship_status": "dry_run"}})
    assert rec["status"] == expected


def test_adaptation_that_declined_every_item_handed_off_by_design(tmp_path, store):
    """04_adapt writes gate false for it, but a decline is an escalation."""
    rec = _run(tmp_path, "test-adaptation-agent", **{
        ".fix-passed": "false", "05-ship.json": {"ship_status": "dry_run"},
        "04-adapt.json": {"items": [{"status": "declined"}, {"status": "escalated"}]}})
    assert rec["status"] == "diagnosed"


def test_adaptation_whose_items_failed_is_failed_even_after_shipping(tmp_path, store):
    rec = _run(tmp_path, "test-adaptation-agent", **{
        "05-ship.json": {"ship_status": "dry_run"},
        "04-adapt.json": {"items": [{"status": "failed"}, {"status": "failed"}]}})
    assert rec["status"] == "failed"


def test_only_verdicts_are_runs_but_every_dollar_counts(tmp_path, store):
    for i, gate in enumerate(("true", "false")):
        d = _session(tmp_path, "test-healing-agent", name=f"20260828-12000{i}-judged")
        (d / ".fix-passed").write_text(gate)
        _write_metrics(d, cost=1.0)
        analytics.append_from_session(d, agent="test-healing-agent", started_at=time.time())
    d = _session(tmp_path, "test-healing-agent", name="20260828-120009-blocked")
    (d / ".fix-passed").write_text("skipped")
    (d / ".skip-reason").write_text("infra")
    _write_metrics(d, cost=1.0)
    analytics.append_from_session(d, agent="test-healing-agent", started_at=time.time())
    overall = analytics.query("all")["overall"]
    assert (overall["runs"], overall["succeeded"], overall["failed"], overall["excluded"]) == (2, 1, 1, 1)
    assert overall["cost_usd"] == 3.0


def test_a_run_that_did_nothing_is_ignored(tmp_path, store):
    """No tokens, no spend, no output: a CLI that answered with nothing."""
    d = _session(tmp_path, "test-authoring-agent")
    analytics.append_from_session(d, agent="test-authoring-agent", status="failed",
                                  started_at=time.time())
    q = analytics.query("all")
    assert (q["overall"]["runs"], q["overall"]["excluded"]) == (0, 0)
    assert q["by_agent"] == {}


def test_every_exit_trap_in_an_agent_still_finalizes_metrics():
    """A `trap ... EXIT` in a run.sh replaces shared/session.sh's finalize_metrics.

    Healing's queue mode did exactly that to release its handoff claim, so every
    queue-mode run exited without metrics.json or an analytics row.
    """
    import re
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for run_sh in sorted((root / "agents").glob("*/run.sh")):
        for line in run_sh.read_text().splitlines():
            if re.search(r"^\s*trap\s.*\bEXIT\s*$", line) and "finalize_metrics" not in line:
                offenders.append(f"{run_sh.parent.name}: {line.strip()}")
    assert offenders == [], offenders
