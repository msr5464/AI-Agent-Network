"""Durable, append-only analytics store — one row per terminal agent run.

Why a separate store rather than deriving from the audit trail at query time:
audit directories are gitignored and local-only (measured on this install: 47
registry runs but only 7 audit dirs still on disk), a resumed run deletes and
rewrites its predecessor's step files, and the run registry is capped at 500
entries. None of those survive a reporting window. This file does.

Written from two places, because neither alone is sufficient:
  * end of each run.sh — covers plain `make run` CLI invocations, which never
    touch the server's registry at all;
  * _wait_and_reap — covers a run whose run.sh was SIGKILLed before it got there.
Duplicate session ids are resolved newest-wins by `query`.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from shared import metrics as _metrics

SCHEMA_VERSION = 1
_STORE = Path(__file__).resolve().parent / "storage" / "run_analytics.jsonl"

_OUTCOME_KEYS = ("tests_created", "tests_fixed", "tests_unverified",
                 "tests_still_failing", "items_adapted", "escalations",
                 "distinct_fixes", "files_changed", "test_cases_generated")


def _store_path() -> Path:
    override = (os.getenv("RUN_ANALYTICS_FILE") or "").strip()
    if override:
        return Path(override)
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    return _STORE


def _load_json(path: Path) -> Dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


# ── Outcome extraction ────────────────────────────────────────────────────────
#
# A run that never shipped records ZEROS, not nulls — it produced nothing, and a
# null would silently drop out of a sum. Each agent has a trap; see the comments.

def _authoring_outcomes(d: Path) -> Dict[str, int]:
    gen = _load_json(d / "03-generate.json")
    ship = _load_json(d / "05-ship.json")
    # 05-ship.json's files_count is a deduplicated FILE count (generated + fixed),
    # not a test count. Counting test sources written is the closest honest proxy.
    written = gen.get("files_written") or []
    tests = sum(1 for f in written
                if isinstance(f, str) and "src/test/java" in f.replace("\\", "/"))
    return {"tests_created": tests, "files_changed": int(ship.get("files_count") or 0)}


def _healing_outcomes(d: Path) -> Dict[str, int]:
    fix = _load_json(d / "01-fix.json")
    # Deliberately 01-fix.json, not 02-ship.json: ship counts distinct EDITS,
    # fix counts TESTS. One cluster fix can green several tests, and using ship
    # would under-report the agent's actual output.
    return {
        "tests_fixed": int(fix.get("succeeded") or 0),
        "tests_unverified": int(fix.get("unverified") or 0),
        "tests_still_failing": int(fix.get("failed") or 0),
        "distinct_fixes": int(fix.get("distinct_fixes")
                              or len(fix.get("fixes") or [])),
    }


def _adaptation_outcomes(d: Path) -> Dict[str, int]:
    adapt = _load_json(d / "04-adapt.json")
    ship = _load_json(d / "05-ship.json")
    items = adapt.get("items") or []
    escalations = (adapt.get("escalations") or []) + (ship.get("escalations") or [])
    return {
        # 05-ship.json does not persist the applied set at all.
        "items_adapted": sum(1 for i in items
                             if isinstance(i, dict)
                             and i.get("status") in ("applied", "partial")),
        "escalations": sum(1 for i in items
                           if isinstance(i, dict)
                           and i.get("status") in ("escalated", "declined"))
                       + len(escalations),
    }


_OUTCOME_EXTRACTORS = {
    "test-authoring-agent": _authoring_outcomes,
    "test-healing-agent": _healing_outcomes,
    "test-adaptation-agent": _adaptation_outcomes,
}


def _pr_url(d: Path) -> Optional[str]:
    for name in ("05-ship.json", "02-ship.json"):
        url = _load_json(d / name).get("pr_url")
        if url:
            return url
    return None


def _adaptation_status(d: Path) -> str:
    """Adaptation's own status ladder.

    05_ship.py asserts verdict == "NEEDS-REVIEW" unconditionally, and both
    _derive_status and the server's final-status derivation map that to
    "failed" — so scoring adaptation by verdict makes EVERY adaptation run a
    failure. A dashboard showing 0% adaptation success is that bug.
    """
    if (d / ".crashed").exists():
        return "failed"
    if (d / ".cancelled").exists():
        return "cancelled"
    if (d / ".interrupted").exists():
        return "interrupted"
    ship = _load_json(d / "05-ship.json")
    if ship:
        return "failed" if ship.get("ship_status") in ("push_failed", "pr_failed") \
            else "completed"
    return "unknown"


# ── Writing ───────────────────────────────────────────────────────────────────

def build_record(audit_dir: Path, agent: str = "", status: str = "",
                 exit_code: Optional[int] = None, module: str = "",
                 started_at: Optional[float] = None,
                 ended_at: Optional[float] = None,
                 auto_push: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """Assemble one analytics row from a finished session's audit dir."""
    d = Path(audit_dir)
    if not d.is_dir():
        return None

    agent = agent or (d.parent.parent.name if d.parent.parent else "")
    session = _metrics.read_rollup(d) or {}
    totals = session.get("totals") or {}

    # Adaptation's status must come from its OWN ladder, and must override
    # whatever the caller passed. `_wait_and_reap` passes the server's derived
    # status, which reads `.verdict` — and 05_ship.py writes NEEDS-REVIEW
    # unconditionally, which the server maps to "failed". Honouring the caller
    # here would score every adaptation run as a failure.
    if agent == "test-adaptation-agent":
        status = _adaptation_status(d) or status

    outcomes = {key: 0 for key in _OUTCOME_KEYS}
    extractor = _OUTCOME_EXTRACTORS.get(agent)
    if extractor:
        try:
            outcomes.update(extractor(d))
        except Exception:
            pass

    record = {
        "schema": SCHEMA_VERSION,
        "session_id": d.name,
        "agent": agent,
        "module": module or "",
        "started_at": started_at or session.get("started_at"),
        "ended_at": ended_at or session.get("ended_at"),
        "duration_s": session.get("duration_s"),
        "status": status or "unknown",
        "exit_code": exit_code,
        "auto_push": auto_push,
        "cost_usd": float(totals.get("cost_usd") or 0.0),
        "input_tokens": int(totals.get("input_tokens") or 0),
        "output_tokens": int(totals.get("output_tokens") or 0),
        "cache_read_input_tokens": int(totals.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(totals.get("cache_creation_input_tokens") or 0),
        "llm_calls": int(totals.get("llm_calls") or 0),
        "num_turns": int(totals.get("num_turns") or 0),
        "llm_duration_s": float(totals.get("llm_duration_s") or 0.0),
        "tool_duration_s": float(totals.get("tool_duration_s") or 0.0),
        "by_model": session.get("by_model") or {},
        "outcomes": outcomes,
        "pr_url": _pr_url(d),
        "stages": [{"key": s.get("key"), "duration_s": s.get("duration_s"),
                    "cost_usd": s.get("cost_usd"), "llm_calls": s.get("llm_calls"),
                    "attempts": s.get("attempts")}
                   for s in (session.get("stages") or [])],
        "written_at": time.time(),
    }
    if record["started_at"] and record["ended_at"] and not record["duration_s"]:
        record["duration_s"] = round(record["ended_at"] - record["started_at"], 3)
    return record


def append_from_session(audit_dir, **kwargs) -> bool:
    """Append one row. Best-effort — never raises into a caller's exit path."""
    try:
        record = build_record(Path(audit_dir), **kwargs)
        if record is None:
            return False
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(str(_store_path()),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


# ── Reading ───────────────────────────────────────────────────────────────────

WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}


def _read_all() -> List[Dict[str, Any]]:
    path = _store_path()
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue      # a SIGKILL mid-write leaves a truncated line
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest row wins per session id.

    Both the agent and the server may write for the same session, and a resumed
    run legitimately produces two rows for one id.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        sid = row.get("session_id")
        if not sid:
            continue
        prior = best.get(sid)
        if prior is None or (row.get("written_at") or 0) >= (prior.get("written_at") or 0):
            best[sid] = row
    return list(best.values())


def _blank_rollup() -> Dict[str, Any]:
    data = {"runs": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "other": 0,
            "cost_usd": 0.0, "duration_s": 0.0, "llm_calls": 0, "num_turns": 0,
            "input_tokens": 0, "output_tokens": 0}
    data.update({key: 0 for key in _OUTCOME_KEYS})
    return data


def _accumulate(target: Dict[str, Any], row: Dict[str, Any]) -> None:
    target["runs"] += 1
    status = row.get("status")
    if status == "completed":
        target["succeeded"] += 1
    elif status == "failed":
        target["failed"] += 1
    elif status == "cancelled":
        target["cancelled"] += 1
    else:
        target["other"] += 1
    # Spend is real whether or not the run produced anything, so cost and time
    # accumulate for every status — not just successes.
    target["cost_usd"] = round(target["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
    target["duration_s"] = round(target["duration_s"] + float(row.get("duration_s") or 0.0), 3)
    for field in ("llm_calls", "num_turns", "input_tokens", "output_tokens"):
        target[field] += int(row.get(field) or 0)
    for key, value in (row.get("outcomes") or {}).items():
        if key in target:
            target[key] += int(value or 0)


def query(window: str = "7d", agent: Optional[str] = None,
          since: Optional[float] = None, until: Optional[float] = None) -> Dict[str, Any]:
    """Rollups over a time window.

    Returns raw counts, cost and duration only. Time-saved is deliberately NOT
    computed here — the baselines live in the Studio's settings, so the Studio
    applies them uniformly across every flow it reports on.
    """
    rows = _dedupe(_read_all())
    now = time.time()
    if since is None and window in WINDOWS and WINDOWS[window] is not None:
        since = now - WINDOWS[window]
    until = until or now

    data_since = min((float(r.get("started_at") or 0) for r in rows
                      if r.get("started_at")), default=None)

    selected = []
    for row in rows:
        started = float(row.get("started_at") or 0)
        if since is not None and started < since:
            continue
        if started > until:
            continue
        if agent and row.get("agent") != agent:
            continue
        selected.append(row)

    overall = _blank_rollup()
    by_agent: Dict[str, Dict[str, Any]] = {}
    series: Dict[str, Dict[str, Any]] = {}
    for row in selected:
        _accumulate(overall, row)
        slot = by_agent.setdefault(row.get("agent") or "unknown", _blank_rollup())
        _accumulate(slot, row)
        bucket = time.strftime("%Y-%m-%d",
                               time.localtime(float(row.get("started_at") or 0)))
        _accumulate(series.setdefault(bucket, _blank_rollup()), row)

    produced = (overall["tests_created"] + overall["tests_fixed"]
                + overall["items_adapted"])
    overall["cost_per_outcome_usd"] = (round(overall["cost_usd"] / produced, 4)
                                       if produced else None)

    return {
        "window": {"from": since, "to": until, "label": _window_label(window)},
        "data_since": data_since,
        "overall": overall,
        "by_agent": by_agent,
        "series": [dict(bucket=b, **v) for b, v in sorted(series.items())],
    }


def _window_label(window: str) -> str:
    return {"24h": "Last 24 hours", "7d": "Last 7 days",
            "30d": "Last 30 days", "all": "All time"}.get(window, window)


if __name__ == "__main__":
    # `python3 -m qa_agents_server.analytics <audit_dir>` — called from run.sh.
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AUDIT_DIR", "")
    if target:
        append_from_session(Path(target),
                            status=os.environ.get("RUN_STATUS", ""),
                            module=os.environ.get("MODULE") or os.environ.get("TEST_NAME")
                                   or os.environ.get("BUILD_TAG", ""))
