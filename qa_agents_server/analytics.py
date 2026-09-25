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
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from qa_agents_server.paths import AGENTS_DIR
from shared import metrics as _metrics
from shared import workspace as _workspace

# Serialises whole-file rewrites against each other. Plain appends do not need
# it — a single O_APPEND write is atomic — but a read-modify-rewrite does, and
# every run's reap thread appends while a clear may be rewriting.
_write_lock = threading.Lock()

# md5("admin")[:12], the id AI-Test-Studio derives for its bootstrap admin.
# Rows written before user attribution existed carry no owner, and showing them
# to nobody would silently lose history, so they are attributed here. This was
# duplicated as a bare literal in three places that could drift apart.
ADMIN_USER_ID = "21232f297a57"
_UNATTRIBUTED = ("", "default", "admin", None)


def _owner_of(row: Dict[str, Any]) -> str:
    """Who an analytics row belongs to, resolving legacy/unattributed rows."""
    owner = row.get("user_id")
    return ADMIN_USER_ID if owner in _UNATTRIBUTED else owner


def _atomic_write_rows(rows: List[Dict[str, Any]]) -> None:
    """Replace the store in one step, never truncating it in place."""
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".run_analytics.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

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
    # A generated test only counts once it passes — or fails exactly as the input
    # documents the product misbehaving ("defect"). A run whose fix gate failed or
    # got stuck produced a file nobody can use.
    passed = (_read_text(d / ".fix-passed") or "").lower() in ("true", "defect")
    tests = sum(1 for f in written
                if isinstance(f, str) and "src/test/java" in f.replace("\\", "/")) if passed else 0
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


def _read_text(path: Path) -> Optional[str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except (OSError, UnicodeDecodeError):
        return None


# ── How a run counts ──────────────────────────────────────────────────────────
#
# completed  the agent's work passed: the test passes, the fix is verified
# diagnosed  a correct stop: nothing to do, or handed to a human by design
# failed     tried and did not get there
# blocked    infrastructure stopped it before it had a fair chance
# cancelled / interrupted / explore-only
#
# Only the first three are a verdict on the agent, so only they are runs in the
# success rate. Everything still counts toward spend and time.
#
# A push or PR that fails after the work passed does not make the run failed:
# the work survives (a local branch, or the audit trail a Ship retry re-reads),
# and the run's own history and Slack alert already flag the delivery.

SUCCEEDED = ("completed", "diagnosed")
JUDGED = SUCCEEDED + ("failed",)

_HANDED_OFF = ("declined", "escalated", "covered")
_ADAPTATION_SKIPS = {"no-work": "diagnosed", "escalate": "diagnosed",
                     "stuck": "failed", "unsafe": "failed", "infra": "blocked",
                     "no-session": "blocked", "unreachable": "blocked"}


def _status(agent: str, d: Path, caller: str) -> str:
    """How this run counts, read from the session's own files.

    Never from the launcher. The caller's status is the exit code (session.sh)
    or the exit code plus .verdict (the server), so one authoring outcome scored
    differently from the CLI and the Studio — and adaptation, whose verdict is
    always NEEDS-REVIEW, scored every run as failed. Only a run with no gate file
    to go on (triaging, or a crash before the gate) keeps the caller's answer.
    """
    # Cancelled and interrupted before crashed: killing a run trips run.sh's ERR
    # trap into writing .crashed as well. .interrupted is only written for a run
    # still alive when the server stopped, so it is the real cause.
    if (d / ".cancelled").exists():
        return "cancelled"
    if (d / ".interrupted").exists():
        return "interrupted"
    if (d / ".crashed").exists():
        return "failed"
    gate = (_read_text(d / ".fix-passed") or "").lower()
    why = (_read_text(d / ".skip-reason") or "").lower()
    if agent == "test-adaptation-agent" and why == "explore-only":
        return "explore-only"
    if gate == "true":
        return "completed"

    if agent == "test-authoring-agent":
        # defect: the test fails exactly where the input says the product
        # misbehaves today, which is a correct test. skipped: no test could run.
        return {"defect": "diagnosed", "skipped": "blocked",
                "false": "failed", "stuck": "failed"}.get(gate, caller)

    if agent == "test-healing-agent":
        if gate == "skipped":
            # The test already passes, or the failure is not a locator's: a
            # correct stop. "infra" means it never got to look.
            return "blocked" if why == "infra" else "diagnosed"
        return "failed" if gate == "false" else caller

    if agent == "test-adaptation-agent":
        if gate == "skipped":
            return _ADAPTATION_SKIPS.get(why, "failed")
        items = [i.get("status") for i in (_load_json(d / "04-adapt.json").get("items") or [])
                 if isinstance(i, dict)]
        # Every item declined or escalated is a hand-off, which this agent treats
        # as the design working — even though 04_adapt writes gate false for it.
        handed_off = bool(items) and all(s in _HANDED_OFF for s in items)
        if gate == "false":
            return "diagnosed" if handed_off else "failed"
        # No gate: a resume that re-runs only Ship clears it. Judge by the items.
        if items:
            if any(s in ("applied", "partial", "proposed") for s in items):
                return "completed"
            return "diagnosed" if handed_off else "failed"
        # Nothing else to go on but the ship step itself.
        ship = _load_json(d / "05-ship.json")
        if ship:
            return "failed" if ship.get("ship_status") in ("push_failed", "pr_failed") \
                else "completed"
        return caller

    return caller


# ── Writing ───────────────────────────────────────────────────────────────────

def build_record(audit_dir: Path, agent: str = "", status: str = "",
                 exit_code: Optional[int] = None, module: str = "",
                 started_at: Optional[float] = None,
                 ended_at: Optional[float] = None,
                 auto_push: Optional[bool] = None,
                 user_id: str = "default") -> Optional[Dict[str, Any]]:
    """Assemble one analytics row from a finished session's audit dir."""
    d = Path(audit_dir)
    if not d.is_dir():
        return None

    agent = agent or (d.parent.parent.name if d.parent.parent else "")
    # A stray audit dir (a temp dir, an ad-hoc check) would otherwise report its
    # grandparent's name — once a tmp UUID — as an agent on the Analytics page.
    if not (AGENTS_DIR / agent / "run.sh").is_file():
        return None
    session = _metrics.read_rollup(d) or {}
    totals = session.get("totals") or {}

    status = _status(agent, d, status)

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
        "user_id": user_id,
        "agent": agent,
        "module": module or "",
        "started_at": started_at or session.get("started_at"),
        "ended_at": ended_at or session.get("ended_at"),
        "duration_s": session.get("duration_s"),
        "status": status or "unknown",
        "verdict": _read_text(d / ".verdict"),
        "fix_gate": _read_text(d / ".fix-passed"),
        "exit_code": exit_code,
        "auto_push": auto_push,
        # Read from the session's own marker, never from os.environ. This
        # function has two callers — shared/session.sh, running inside the
        # agent, and runner.py, running in the *server's* environment — and an
        # env read would be right for the first and silently report the org
        # default for the second.
        "base_branch": _workspace.read_base_marker(d)[0],
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
    data = {"runs": 0, "succeeded": 0, "failed": 0, "excluded": 0,
            "cost_usd": 0.0, "duration_s": 0.0, "llm_calls": 0, "num_turns": 0,
            "input_tokens": 0, "output_tokens": 0}
    data.update({key: 0 for key in _OUTCOME_KEYS})
    return data


def _did_nothing(row: Dict[str, Any]) -> bool:
    """No tokens, no spend, no output and no success: not an attempt at all.

    A Claude CLI that answered with nothing, a crash before the first step, a
    cancel before anything started. Counted, they made the success rate read as
    a measure of setup trouble, and were deleted by hand to fix it.
    """
    if row.get("status") in SUCCEEDED:
        return False
    outcomes = row.get("outcomes") or {}
    return (int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0) == 0
            and not float(row.get("cost_usd") or 0.0)
            and not any(int(outcomes.get(k) or 0)
                        for k in ("tests_created", "tests_fixed", "items_adapted")))


def _accumulate(target: Dict[str, Any], row: Dict[str, Any]) -> None:
    status = row.get("status")
    # Only a verdict on the agent is a run in the success rate. Blocked,
    # cancelled, interrupted and explore-only runs are "excluded" from it.
    if status in JUDGED:
        target["runs"] += 1
        target["succeeded" if status in SUCCEEDED else "failed"] += 1
    else:
        target["excluded"] += 1
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
          since: Optional[float] = None, until: Optional[float] = None,
          user_id: Optional[str] = None) -> Dict[str, Any]:
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
        if user_id and _owner_of(row) != user_id:
            continue
        if _did_nothing(row):
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


def clear_history(user_id: Optional[str] = None, window: str = "all") -> List[str]:
    """Drop analytics rows. Returns the session ids removed.

    Serialised and atomic, which it was not. Appends come from every run's reap
    thread, so a clear running concurrently with a finishing run silently lost
    whatever was written between _read_all() and the rewrite — and a crash
    mid-rewrite left a truncated store, because the file was opened "w" and
    written in place. storage.py already had the mkstemp + os.replace pattern;
    this now uses the same one.
    """
    now = time.time()
    since = None
    if window in WINDOWS and WINDOWS[window] is not None:
        since = now - WINDOWS[window]

    path = _store_path()
    removed_sessions: List[str] = []
    with _write_lock:
        if not path.exists():
            return removed_sessions
        rows = _read_all()
        if (not user_id or user_id == "all") and since is None:
            removed_sessions = [r.get("session_id") for r in rows if r.get("session_id")]
            _atomic_write_rows([])
            return removed_sessions

        kept = []
        for row in rows:
            row_user = _owner_of(row)
            if not user_id or user_id == "all" or row_user == user_id:
                ts = float(row.get("started_at") or 0)
                if since is not None and ts < since:
                    kept.append(row)
                elif row.get("session_id"):
                    removed_sessions.append(row.get("session_id"))
            else:
                kept.append(row)
        _atomic_write_rows(kept)
    return removed_sessions


if __name__ == "__main__":
    # `python3 -m qa_agents_server.analytics <audit_dir>` — called from run.sh.
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AUDIT_DIR", "")
    if target:
        append_from_session(Path(target),
                            status=os.environ.get("RUN_STATUS", ""),
                            module=os.environ.get("MODULE") or os.environ.get("TEST_NAME")
                                   or os.environ.get("BUILD_TAG", ""),
                            # The runner exports USER_ID into every agent's
                            # environment; this path never read it, so rows
                            # written by the agent itself (which is the path a
                            # plain `make run` takes) lost their owner.
                            user_id=os.environ.get("USER_ID") or "default")
