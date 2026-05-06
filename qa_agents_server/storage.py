"""Persistent session registry for qa_agents_server.

Writes a compact JSON snapshot of every known run to
`qa_agents_server/storage/agent_runs.json`. On server boot we reload this file
and treat any runs still marked `running` as `interrupted` — their subprocess
died when the server did, so we cannot resume them, but the UI can still show
them in history with a warning badge.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from qa_agents_server.paths import RUNS_REGISTRY_FILE, SERVER_STORAGE_DIR

_lock = threading.Lock()
_MAX_PERSISTED_RUNS = 500  # keep the file from growing forever


def init() -> None:
    """Ensure the storage directory exists."""
    SERVER_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def load_all() -> List[Dict]:
    """Return the persisted list of runs (oldest first)."""
    with _lock:
        return _read_locked()


def _atomic_write(data: List[Dict]) -> None:
    SERVER_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".agent_runs.", suffix=".tmp", dir=str(SERVER_STORAGE_DIR)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, RUNS_REGISTRY_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert(run: Dict) -> None:
    """Insert or update a run snapshot. Keyed by session_id."""
    session_id = run.get("session_id")
    if not session_id:
        return
    with _lock:
        data = _read_locked()
        updated = False
        for i, entry in enumerate(data):
            if entry.get("session_id") == session_id:
                data[i] = run
                updated = True
                break
        if not updated:
            data.append(run)
        # Cap size, keep newest
        if len(data) > _MAX_PERSISTED_RUNS:
            data = data[-_MAX_PERSISTED_RUNS:]
        _atomic_write(data)


def _read_locked() -> List[Dict]:
    """Read the registry file. Caller must hold _lock."""
    if not RUNS_REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(RUNS_REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def mark_all_running_as_interrupted() -> List[str]:
    """On boot, flip any run still tagged `running` to `interrupted`.

    Returns the list of affected session_ids so the runner can surface them
    in its in-memory registry (so the history UI shows them right away).
    """
    affected: List[str] = []
    with _lock:
        data = _read_locked()
        for entry in data:
            if entry.get("status") == "running":
                entry["status"] = "interrupted"
                entry["ended_at"] = entry.get("ended_at") or time.time()
                sid = entry.get("session_id")
                if sid:
                    affected.append(sid)
        if affected:
            _atomic_write(data)
    return affected


def get(session_id: str) -> Optional[Dict]:
    for entry in load_all():
        if entry.get("session_id") == session_id:
            return entry
    return None
