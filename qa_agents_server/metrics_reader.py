"""Read per-session time/cost metrics written by shared/metrics.py.

The agent owns the writing; this module only reads. It deliberately tolerates
sessions that predate metrics entirely (returns None) and sessions still in
flight or killed mid-run (rebuilds a partial rollup from the JSONL streams), so
the UI can show spend for a run that never reached its own rollup.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from shared import metrics as _metrics

# Fields the UI reads off a step. Kept in one place so the live path
# (runner.py) and the historical path (audit_reader.py) cannot drift — a
# divergence this codebase has regressed on before.
STEP_METRIC_FIELDS = ("duration_s", "cost_usd", "input_tokens", "output_tokens",
                      "llm_calls", "num_turns", "tool_duration_s", "attempts")


def read_session_metrics(audit_dir: Path) -> Optional[Dict[str, Any]]:
    """The rollup for one session, or None when it has no metrics at all."""
    try:
        if not Path(audit_dir).is_dir():
            return None
        return _metrics.read_rollup(Path(audit_dir))
    except Exception:
        return None


def stage_map(session_metrics: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Rollup stages keyed by step key, for joining onto the server's step model."""
    if not session_metrics:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for stage in session_metrics.get("stages") or []:
        key = stage.get("key")
        if key:
            out[key] = stage
    return out


def step_metrics(session_metrics: Optional[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """The metric fields for one step, as a flat dict. Empty when unknown."""
    stage = stage_map(session_metrics).get(key)
    if not stage:
        return {}
    return {field: stage.get(field) for field in STEP_METRIC_FIELDS
            if stage.get(field) is not None}


def totals(session_metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Run-level totals plus the per-model split, in the shape the UI consumes."""
    if not session_metrics:
        return {}
    data = dict(session_metrics.get("totals") or {})
    if session_metrics.get("by_model"):
        data["by_model"] = session_metrics["by_model"]
    if session_metrics.get("duration_s") is not None:
        data["duration_s"] = session_metrics["duration_s"]
    return data


def summary_fields(session_metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The flat subset that list/history rows show, so a table needs no N+1 fetch."""
    data = totals(session_metrics)
    if not data:
        return {}
    return {
        "cost_usd": data.get("cost_usd"),
        "llm_calls": data.get("llm_calls"),
        "num_turns": data.get("num_turns"),
        "input_tokens": data.get("input_tokens"),
        "output_tokens": data.get("output_tokens"),
        "metrics_duration_s": data.get("duration_s"),
    }


def stage_list(session_metrics: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordered stage breakdown for the session-detail modal."""
    if not session_metrics:
        return []
    return list(session_metrics.get("stages") or [])
