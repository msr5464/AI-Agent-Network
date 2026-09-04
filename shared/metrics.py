"""Per-run time and cost metrics for all QA-Agent-Network agents.

Actions are standalone `python3` processes that share nothing but `$AUDIT_DIR`, so
metrics are written to files rather than returned. Three append-only JSONL streams
plus one rolled-up `metrics.json` that the server and UI actually read:

    agents/<agent>/audit/<session-id>/
      metrics/
        llm-calls.jsonl   one line per `claude -p` invocation
        stages.jsonl      one line per completed run_step
        tools.jsonl       one line per maven/gradle/playwright/compile subprocess
      metrics.json        rolled-up totals

Every public function here is best-effort: a metrics failure must never fail a heal.
Callers are not expected to guard these calls.
"""

import calendar
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


# ── Location ───────────────────────────────────────────────────────────────────

def audit_dir() -> Optional[Path]:
    """The session's audit directory, or None when not running under an agent."""
    raw = (os.environ.get("AUDIT_DIR") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw)
    except (ValueError, OSError):
        return None


def _metrics_dir(base: Optional[Path] = None) -> Optional[Path]:
    base = base or audit_dir()
    if base is None:
        return None
    try:
        path = base / "metrics"
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return None


def _append(filename: str, record: dict, base: Optional[Path] = None) -> None:
    """Append one JSON line. O_APPEND + a single write keeps concurrent action
    processes from interleaving mid-line."""
    directory = _metrics_dir(base)
    if directory is None:
        return
    try:
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        return
    try:
        fd = os.open(str(directory / filename), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _read_jsonl(path: Path) -> List[dict]:
    """Read a JSONL file, skipping malformed lines rather than failing the rollup.

    A truncated final line is the expected case when a run is SIGKILLed mid-write.
    """
    rows: List[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, UnicodeDecodeError):
        pass
    return rows


# ── Current stage ──────────────────────────────────────────────────────────────

def current_stage() -> Dict[str, Any]:
    """The stage an action is running inside, as exported by run_step()."""
    try:
        attempt = int(os.environ.get("STEP_ATTEMPT") or 1)
    except (TypeError, ValueError):
        attempt = 1
    return {
        "stage": (os.environ.get("STEP_KEY") or "").strip() or None,
        "stage_label": (os.environ.get("STEP_LABEL") or "").strip() or None,
        "attempt": attempt,
    }


# ── Recording ──────────────────────────────────────────────────────────────────

def record_llm_call(result: Any, model_requested: str = "") -> None:
    """Record one `claude -p` invocation from its ClaudeResult.

    Called from shared/claude.py, so every call site is instrumented without
    touching any of them.
    """
    try:
        usage = dict(getattr(result, "usage", None) or {})
        by_model = dict(getattr(result, "by_model", None) or {})
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **current_stage(),
            "model_requested": model_requested or "",
            "model_resolved": getattr(result, "model_resolved", "") or "",
            "by_model": by_model,
            "cost_usd": float(getattr(result, "cost_usd", 0.0) or 0.0),
            "num_turns": int(getattr(result, "num_turns", 0) or 0),
            "duration_s": round(float(getattr(result, "duration_s", 0.0) or 0.0), 3),
            "duration_api_s": round(float(getattr(result, "duration_api_s", 0.0) or 0.0), 3),
            "status": getattr(result, "status", "") or "",
        }
        for field in _TOKEN_FIELDS:
            record[field] = int(usage.get(field) or 0)
        _append("llm-calls.jsonl", record)
    except Exception:
        pass


def record_tool(kind: str, cmd: str, duration_s: float, verdict: str = "") -> None:
    """Record one non-LLM subprocess (maven, gradle, playwright, compile)."""
    try:
        _append("tools.jsonl", {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **current_stage(),
            "kind": kind or "",
            "cmd": (cmd or "")[:500],
            "duration_s": round(float(duration_s or 0.0), 3),
            "verdict": verdict or "",
        })
    except Exception:
        pass


def record_stage(key: str, label: str, index: int, started_at: float,
                 ended_at: float, exit_code: int = 0, attempt: int = 1,
                 skipped: bool = False, base: Optional[Path] = None) -> None:
    """Record one completed run_step. Normally called from shared/session.sh."""
    try:
        _append("stages.jsonl", {
            "index": int(index or 0),
            "key": key or "",
            "label": label or "",
            "attempt": int(attempt or 1),
            "started_at": round(float(started_at or 0.0), 3),
            "ended_at": round(float(ended_at or 0.0), 3),
            "duration_s": max(0.0, round(float(ended_at or 0) - float(started_at or 0), 3)),
            "exit_code": int(exit_code or 0),
            "skipped": bool(skipped),
        }, base=base)
    except Exception:
        pass


# ── Rollup ─────────────────────────────────────────────────────────────────────

def _epoch(ts) -> float:
    """Parse a metrics timestamp back to epoch seconds. 0 when unparseable."""
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def _blank_totals() -> Dict[str, Any]:
    totals = {"cost_usd": 0.0, "llm_calls": 0, "llm_duration_s": 0.0,
              "tool_duration_s": 0.0, "num_turns": 0}
    totals.update({field: 0 for field in _TOKEN_FIELDS})
    return totals


def _merge_by_model(target: Dict[str, dict], source: Dict[str, Any]) -> None:
    for name, stats in (source or {}).items():
        if not isinstance(stats, dict):
            continue
        slot = target.setdefault(name, {"cost_usd": 0.0,
                                        **{field: 0 for field in _TOKEN_FIELDS}})
        slot["cost_usd"] = round(slot["cost_usd"] + float(stats.get("cost_usd") or 0.0), 6)
        for field in _TOKEN_FIELDS:
            slot[field] += int(stats.get(field) or 0)


def build_rollup(base: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Build the rollup in memory without writing it.

    Used by the server to report partial spend for a run that is still going or
    was killed before it could roll up.
    """
    base = base or audit_dir()
    if base is None:
        return None
    directory = base / "metrics"
    if not directory.is_dir():
        return None

    llm_calls = _read_jsonl(directory / "llm-calls.jsonl")
    stage_rows = _read_jsonl(directory / "stages.jsonl")
    tool_rows = _read_jsonl(directory / "tools.jsonl")

    totals = _blank_totals()
    by_model: Dict[str, dict] = {}
    # Keyed by stage key so a stage that ran twice folds into one entry; the
    # per-attempt detail stays in stages.jsonl.
    per_stage: Dict[str, dict] = {}

    def stage_slot(key: str, label: str = "", index: int = 0) -> dict:
        slot = per_stage.get(key)
        if slot is None:
            slot = {"key": key, "label": label, "index": index,
                    "duration_s": 0.0, "tool_duration_s": 0.0,
                    "attempts": 0, "skipped": False, **_blank_totals()}
            # A stage entry tracks its own duration separately from llm_duration_s.
            slot.pop("llm_duration_s", None)
            slot["llm_duration_s"] = 0.0
            per_stage[key] = slot
        if label and not slot.get("label"):
            slot["label"] = label
        if index and not slot.get("index"):
            slot["index"] = index
        return slot

    for row in stage_rows:
        key = row.get("key") or row.get("label") or "unknown"
        slot = stage_slot(key, row.get("label") or "", int(row.get("index") or 0))
        slot["duration_s"] = round(slot["duration_s"] + float(row.get("duration_s") or 0.0), 3)
        slot["attempts"] += 1
        # "skipped" means the stage never actually ran. A resumed session
        # appends a skip row for a stage that DID run in the earlier attempt,
        # and last-row-wins would then label a stage carrying real time as
        # skipped. Only all-skipped counts as skipped.
        if not row.get("skipped"):
            slot["skipped"] = False
        elif "skipped" not in slot or slot.get("_all_skipped", True):
            slot["skipped"] = True
        slot["_all_skipped"] = slot.get("_all_skipped", True) and bool(row.get("skipped"))

    for row in llm_calls:
        cost = float(row.get("cost_usd") or 0.0)
        turns = int(row.get("num_turns") or 0)
        duration = float(row.get("duration_s") or 0.0)
        totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)
        totals["llm_calls"] += 1
        totals["num_turns"] += turns
        totals["llm_duration_s"] = round(totals["llm_duration_s"] + duration, 3)
        for field in _TOKEN_FIELDS:
            totals[field] += int(row.get(field) or 0)
        _merge_by_model(by_model, row.get("by_model") or {})

        key = row.get("stage")
        if key:
            slot = stage_slot(key, row.get("stage_label") or "")
            slot["cost_usd"] = round(slot["cost_usd"] + cost, 6)
            slot["llm_calls"] += 1
            slot["num_turns"] += turns
            slot["llm_duration_s"] = round(slot["llm_duration_s"] + duration, 3)
            for field in _TOKEN_FIELDS:
                slot[field] += int(row.get(field) or 0)

    for row in tool_rows:
        duration = float(row.get("duration_s") or 0.0)
        totals["tool_duration_s"] = round(totals["tool_duration_s"] + duration, 3)
        key = row.get("stage")
        if key:
            slot = stage_slot(key)
            slot["tool_duration_s"] = round(slot["tool_duration_s"] + duration, 3)

    stages = sorted(per_stage.values(),
                    key=lambda s: (s.get("index") or 999, s.get("key") or ""))
    for slot in stages:
        slot["skipped"] = bool(slot.pop("_all_skipped", slot.get("skipped", False)))

    started = min((float(r.get("started_at") or 0) for r in stage_rows
                   if r.get("started_at")), default=0.0)
    ended = max((float(r.get("ended_at") or 0) for r in stage_rows
                 if r.get("ended_at")), default=0.0)

    # A run still in flight, or one whose stages never landed, has LLM calls but
    # no stage rows. Their timestamps still bound the run, so a partial rollup
    # reports a real duration instead of nothing.
    if not (started and ended):
        stamps = sorted(t for t in (_epoch(r.get("ts")) for r in llm_calls) if t)
        if stamps:
            first_duration = float(llm_calls[0].get("duration_s") or 0.0)
            started = started or max(0.0, stamps[0] - first_duration)
            ended = ended or stamps[-1]

    return {
        "schema": SCHEMA_VERSION,
        "session_id": base.name,
        "agent": _agent_name(base),
        "started_at": started or None,
        "ended_at": ended or None,
        "duration_s": round(ended - started, 3) if (started and ended) else None,
        "totals": totals,
        "by_model": by_model,
        "stages": stages,
    }


def _agent_name(base: Path) -> str:
    """`agents/<agent>/audit/<session>` — the agent is two levels up from the session."""
    try:
        return base.parent.parent.name
    except (AttributeError, IndexError):
        return ""


def rollup(base: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Build the rollup and write it to `metrics.json`. Returns it, or None."""
    try:
        data = build_rollup(base)
        if data is None:
            return None
        target = (base or audit_dir()) / "metrics.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(target))
        return data
    except Exception:
        return None


def read_rollup(base: Path) -> Optional[Dict[str, Any]]:
    """Read `metrics.json`, falling back to rebuilding it from the JSONL streams."""
    try:
        path = Path(base) / "metrics.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError, TypeError):
        pass
    try:
        return build_rollup(Path(base))
    except Exception:
        return None


def format_summary(data: Optional[Dict[str, Any]]) -> str:
    """One-line cost summary for the end-of-run terminal table."""
    if not data:
        return ""
    totals = data.get("totals") or {}
    cost = float(totals.get("cost_usd") or 0.0)
    calls = int(totals.get("llm_calls") or 0)
    turns = int(totals.get("num_turns") or 0)
    out_tokens = int(totals.get("output_tokens") or 0)
    return (f"${cost:.4f} · {calls} call{'' if calls == 1 else 's'} "
            f"({turns} turns) · {out_tokens:,} output tokens")


if __name__ == "__main__":
    # `python3 -m shared.metrics` — roll up and print the summary. Used by run.sh.
    _data = rollup()
    _line = format_summary(_data)
    if _line:
        print(_line)
