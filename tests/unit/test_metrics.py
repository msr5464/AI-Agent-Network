"""Tests for shared/metrics.py and the usage capture in shared/claude.py."""

import json
import os
from pathlib import Path

import pytest

from shared import metrics
from shared.claude import _StreamJsonDecoder, _absorb_usage, _unwrap_json_envelope

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_LOG = (REPO_ROOT / "agents/test-authoring-agent/audit"
              / "20260827-151447-create-naukari_profile_update"
              / "claude-20260827-151449-991501.log")


@pytest.fixture
def audit(tmp_path, monkeypatch):
    session = tmp_path / "agents" / "test-healing-agent" / "audit" / "20260827-1-fix-X"
    session.mkdir(parents=True)
    monkeypatch.setenv("AUDIT_DIR", str(session))
    for var in ("STEP_KEY", "STEP_LABEL", "STEP_ATTEMPT"):
        monkeypatch.delenv(var, raising=False)
    return session


class _Result:
    """Stand-in for ClaudeResult carrying only what record_llm_call reads."""
    def __init__(self, **kw):
        self.status = kw.get("status", "ok")
        self.cost_usd = kw.get("cost_usd", 0.0)
        self.usage = kw.get("usage", {})
        self.by_model = kw.get("by_model", {})
        self.num_turns = kw.get("num_turns", 0)
        self.duration_s = kw.get("duration_s", 0.0)
        self.duration_api_s = kw.get("duration_api_s", 0.0)
        self.model_resolved = kw.get("model_resolved", "")


# ── round-trip ────────────────────────────────────────────────────────────────

def test_record_and_rollup_round_trip(audit, monkeypatch):
    monkeypatch.setenv("STEP_KEY", "fix")
    monkeypatch.setenv("STEP_LABEL", "[01/02] Fix")
    metrics.record_llm_call(_Result(
        cost_usd=1.5, num_turns=10, duration_s=30.0,
        usage={"input_tokens": 100, "output_tokens": 200},
        by_model={"claude-sonnet-4-6": {"cost_usd": 1.5, "output_tokens": 200}},
        model_resolved="claude-sonnet-4-6"), model_requested="claude-opus-4-6")
    metrics.record_tool("build", "mvn test", 12.5, "passed")
    metrics.record_stage("fix", "[01/02] Fix", 1, 100.0, 140.0)

    data = metrics.rollup()
    assert data["totals"]["cost_usd"] == 1.5
    assert data["totals"]["llm_calls"] == 1
    assert data["totals"]["num_turns"] == 10
    assert data["totals"]["output_tokens"] == 200
    assert data["totals"]["tool_duration_s"] == 12.5
    assert data["agent"] == "test-healing-agent"
    assert (audit / "metrics.json").is_file()

    stage = data["stages"][0]
    assert stage["key"] == "fix"
    assert stage["duration_s"] == 40.0
    assert stage["cost_usd"] == 1.5
    assert stage["llm_calls"] == 1
    assert stage["tool_duration_s"] == 12.5


def test_stage_totals_reconcile_with_run_totals(audit, monkeypatch):
    for key in ("parse", "generate"):
        monkeypatch.setenv("STEP_KEY", key)
        metrics.record_llm_call(_Result(cost_usd=0.25, num_turns=3,
                                        usage={"output_tokens": 50}))
    data = metrics.rollup()
    assert sum(s["llm_calls"] for s in data["stages"]) == data["totals"]["llm_calls"]
    assert round(sum(s["cost_usd"] for s in data["stages"]), 6) == data["totals"]["cost_usd"]
    assert sum(s["num_turns"] for s in data["stages"]) == data["totals"]["num_turns"]


def test_multi_attempt_stage_sums_into_one_entry(audit, monkeypatch):
    monkeypatch.setenv("STEP_KEY", "fix")
    for attempt in (1, 2):
        metrics.record_stage("fix", "[01/02] Fix", 1, 0.0, 10.0 * attempt,
                             attempt=attempt)
        monkeypatch.setenv("STEP_ATTEMPT", str(attempt))
        metrics.record_llm_call(_Result(cost_usd=0.5))
    data = metrics.rollup()
    stages = [s for s in data["stages"] if s["key"] == "fix"]
    assert len(stages) == 1, "a retried stage must fold into one rollup entry"
    assert stages[0]["attempts"] == 2
    assert stages[0]["duration_s"] == 30.0
    assert stages[0]["cost_usd"] == 1.0


def test_retry_sums_every_attempt_not_just_the_last(audit, monkeypatch):
    """A stage that ran twice cost twice. Reporting only the successful attempt
    would hide the cost of the failure that made the retry necessary."""
    monkeypatch.setenv("STEP_KEY", "fix")
    for attempt, (dur, cost, turns) in enumerate([(30.0, 0.50, 7), (45.0, 0.75, 9)], 1):
        monkeypatch.setenv("STEP_ATTEMPT", str(attempt))
        metrics.record_stage("fix", f"[01/02] Fix (attempt {attempt}/2)", 1,
                             0.0, dur, attempt=attempt)
        metrics.record_llm_call(_Result(cost_usd=cost, num_turns=turns,
                                        usage={"output_tokens": 100}))
        metrics.record_tool("build", "mvn test", 20.0,
                            "failed" if attempt == 1 else "passed")

    data = metrics.rollup()
    stage = next(s for s in data["stages"] if s["key"] == "fix")
    assert stage["attempts"] == 2
    assert stage["duration_s"] == 75.0          # 30 + 45, not 45
    assert stage["cost_usd"] == 1.25            # 0.50 + 0.75, not 0.75
    assert stage["llm_calls"] == 2
    assert stage["num_turns"] == 16             # 7 + 9
    assert stage["tool_duration_s"] == 40.0     # both builds, incl. the failed one

    totals = data["totals"]
    assert totals["cost_usd"] == 1.25
    assert totals["tool_duration_s"] == 40.0
    assert totals["output_tokens"] == 200


def test_a_failed_attempt_still_counts(audit, monkeypatch):
    """Spend on an attempt that produced nothing is still spend."""
    monkeypatch.setenv("STEP_KEY", "fix")
    metrics.record_llm_call(_Result(cost_usd=0.9, status="error"))
    metrics.record_llm_call(_Result(cost_usd=0.1, status="ok"))
    assert metrics.rollup()["totals"]["cost_usd"] == 1.0


def test_stage_totals_reconcile_after_retries(audit, monkeypatch):
    for key, attempts in (("fix", 3), ("ship", 1)):
        monkeypatch.setenv("STEP_KEY", key)
        for a in range(1, attempts + 1):
            monkeypatch.setenv("STEP_ATTEMPT", str(a))
            metrics.record_stage(key, key, 1, 0.0, 10.0, attempt=a)
            metrics.record_llm_call(_Result(cost_usd=0.25))
    data = metrics.rollup()
    assert sum(s["cost_usd"] for s in data["stages"]) == data["totals"]["cost_usd"]
    assert sum(s["llm_calls"] for s in data["stages"]) == data["totals"]["llm_calls"]
    assert {s["key"]: s["attempts"] for s in data["stages"]} == {"fix": 3, "ship": 1}


def test_a_stage_that_ran_then_was_reused_is_not_marked_skipped(audit):
    """A resumed session appends a skip row for a stage that already ran.
    Last-row-wins would label a stage carrying real time as skipped."""
    metrics.record_stage("parse", "[01/05] Parse", 1, 0.0, 12.0)          # really ran
    metrics.record_stage("parse", "[01/05] Parse", 1, 0.0, 0.0, skipped=True)  # reused
    stage = next(s for s in metrics.rollup()["stages"] if s["key"] == "parse")
    assert stage["skipped"] is False
    assert stage["duration_s"] == 12.0
    assert stage["attempts"] == 2


def test_a_stage_only_ever_skipped_stays_skipped(audit):
    metrics.record_stage("scope", "[02/05] Scope", 2, 0.0, 0.0, skipped=True)
    metrics.record_stage("scope", "[02/05] Scope", 2, 0.0, 0.0, skipped=True)
    stage = next(s for s in metrics.rollup()["stages"] if s["key"] == "scope")
    assert stage["skipped"] is True


def test_rollup_exposes_no_private_keys(audit):
    metrics.record_stage("fix", "Fix", 1, 0.0, 5.0)
    metrics.record_llm_call(_Result(cost_usd=0.1))
    data = metrics.rollup()
    for stage in data["stages"]:
        assert not [k for k in stage if k.startswith("_")], stage


def test_malformed_jsonl_line_is_skipped_not_fatal(audit):
    metrics.record_llm_call(_Result(cost_usd=1.0))
    path = audit / "metrics" / "llm-calls.jsonl"
    # A SIGKILL mid-write leaves exactly this: a truncated final line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"cost_usd": 0.5, "trunca')
    data = metrics.rollup()
    assert data["totals"]["llm_calls"] == 1
    assert data["totals"]["cost_usd"] == 1.0


def test_no_audit_dir_is_a_no_op(monkeypatch):
    monkeypatch.delenv("AUDIT_DIR", raising=False)
    metrics.record_llm_call(_Result(cost_usd=1.0))   # must not raise
    assert metrics.rollup() is None


def test_read_rollup_rebuilds_when_metrics_json_missing(audit):
    metrics.record_llm_call(_Result(cost_usd=2.0))
    assert not (audit / "metrics.json").exists()
    data = metrics.read_rollup(audit)
    assert data["totals"]["cost_usd"] == 2.0


# ── CLI envelope parsing ──────────────────────────────────────────────────────

def test_absorb_usage_maps_camelcase_model_usage():
    usage = {}
    _absorb_usage(usage, {
        "total_cost_usd": 0.5, "num_turns": 7, "duration_api_ms": 1500,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {"claude-sonnet-4-6": {
            "inputTokens": 10, "outputTokens": 20,
            "cacheReadInputTokens": 30, "cacheCreationInputTokens": 40,
            "costUSD": 0.5}},
    })
    assert usage["cost_usd"] == 0.5
    assert usage["num_turns"] == 7
    assert usage["duration_api_s"] == 1.5
    assert usage["model_resolved"] == "claude-sonnet-4-6"
    # modelUsage is camelCase where the top-level usage block is snake_case.
    assert usage["by_model"]["claude-sonnet-4-6"]["output_tokens"] == 20
    assert usage["by_model"]["claude-sonnet-4-6"]["cache_read_input_tokens"] == 30


def test_absorb_usage_tolerates_a_shape_change():
    usage = {}
    _absorb_usage(usage, {"total_cost_usd": "not-a-number", "usage": "wrong"})
    assert usage.get("cost_usd") is None


def test_unwrap_json_envelope_extracts_result_text():
    payload = json.dumps({"result": "STEP_PASSED: yes", "total_cost_usd": 0.25,
                          "num_turns": 2})
    text, usage = _unwrap_json_envelope(payload)
    assert text == "STEP_PASSED: yes"
    assert usage["cost_usd"] == 0.25


@pytest.mark.parametrize("raw", [
    "STEP_PASSED: plain text output",       # older CLI, or no envelope
    '{"no_result_key": true}',              # envelope shape changed
    '{"result": "truncated', # killed mid-write
    "",
])
def test_unwrap_json_envelope_falls_back_to_raw_stdout(raw):
    """The fallback is what makes --output-format json safe for the call sites
    that json.loads() the result."""
    text, usage = _unwrap_json_envelope(raw)
    assert text == raw
    assert usage == {}


# ── golden file ───────────────────────────────────────────────────────────────

@pytest.mark.skipif(not GOLDEN_LOG.is_file(), reason="captured log not present")
def test_golden_log_reports_real_cli_numbers():
    decoder = _StreamJsonDecoder()
    with GOLDEN_LOG.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("[stdout] "):
                decoder.feed(line[len("[stdout] "):])
    usage = decoder.usage
    assert usage["cost_usd"] == 0.8191181999999996
    assert usage["num_turns"] == 44
    assert usage["output_tokens"] == 10805
    assert usage["input_tokens"] == 65
    assert usage["cache_read_input_tokens"] == 1555869
    # The CLI resolved sonnet from a run that requested opus — cost must be
    # attributed to what actually ran, never to the --model flag.
    assert usage["model_resolved"] == "claude-sonnet-4-6"
    assert usage["by_model"]["claude-sonnet-4-6"]["output_tokens"] == 10805
