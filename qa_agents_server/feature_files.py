"""CRUD over an agent's queue/*.txt files.

The run.sh orchestrator reads queue files from this directory; the UI lets users
create / edit them without shelling in. Writes are atomic (write to a temp file
then rename) so a half-written file can never be picked up by a run.

Originally hardcoded to test-authoring-agent. The queue directory now comes from
the AgentSpec, so any agent declaring `queue_kind="txt"` gets the same editor —
a change note and a feature spec are the same shape of artifact, and the name
rules and size cap apply equally to both.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Optional

from qa_agents_server.agents import DEFAULT_AGENT, get_agent
from qa_agents_server.paths import REPO_ROOT

# Kept for callers that still import them directly.
QUEUE_DIR: Path = REPO_ROOT / "agents" / "test-authoring-agent" / "queue"
PROCESSED_DIR: Path = QUEUE_DIR / "processed"


def _queue_dir(agent: str = DEFAULT_AGENT) -> Path:
    spec = get_agent(agent)
    if spec.queue_kind != "txt":
        raise FeatureFileError(
            f"{spec.name}'s queue is not human-authored text", status=405)
    return spec.queue_dir


def _processed_dir(agent: str = DEFAULT_AGENT) -> Path:
    return _queue_dir(agent) / "processed"

MAX_SIZE_BYTES = 64 * 1024  # 64 KB — a human-written spec won't be larger
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class FeatureFileError(Exception):
    """Raised for validation / IO errors the API should surface as 4xx."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _ensure_dirs(agent: str = DEFAULT_AGENT):
    _queue_dir(agent).mkdir(parents=True, exist_ok=True)
    _processed_dir(agent).mkdir(parents=True, exist_ok=True)


def _validate_name(name: str) -> str:
    """Validate and normalise a feature name. Returns the bare name (no .txt)."""
    if not isinstance(name, str):
        raise FeatureFileError("name must be a string")
    # Strip the extension if the caller included it
    if name.endswith(".txt"):
        name = name[:-4]
    if not _NAME_RE.match(name):
        raise FeatureFileError(
            "name must be 1-64 chars: letters, digits, underscore, hyphen "
            "(must start with a letter or digit)"
        )
    return name


def _preview(content: str, max_chars: int = 200) -> str:
    snippet = content.strip().splitlines()
    out: List[str] = []
    total = 0
    for line in snippet:
        line = line.strip()
        if not line:
            continue
        out.append(line)
        total += len(line) + 1
        if total >= max_chars:
            break
    s = " · ".join(out)
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


def list_features(agent: str = DEFAULT_AGENT) -> List[Dict]:
    """List feature files in the queue (does not include processed/)."""
    _ensure_dirs(agent)
    items: List[Dict] = []
    for path in sorted(_queue_dir(agent).glob("*.txt")):
        if path.is_dir():
            continue
        try:
            stat = path.stat()
            content = path.read_text(errors="replace")
        except OSError:
            continue
        items.append({
            "name": path.stem,
            "filename": path.name,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "preview": _preview(content),
        })
    # Newest first by mtime
    items.sort(key=lambda d: d["modified"], reverse=True)
    return items


def read_feature(name: str, agent: str = DEFAULT_AGENT) -> Dict:
    _ensure_dirs(agent)
    bare = _validate_name(name)
    path = _queue_dir(agent) / f"{bare}.txt"
    if not path.exists():
        raise FeatureFileError(f"feature file not found: {bare}.txt", status=404)
    content = path.read_text(errors="replace")
    stat = path.stat()
    return {
        "name": bare,
        "filename": path.name,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "content": content,
    }


def write_feature(name: str, content: str, agent: str = DEFAULT_AGENT) -> Dict:
    _ensure_dirs(agent)
    bare = _validate_name(name)
    if not isinstance(content, str):
        raise FeatureFileError("content must be a string")
    payload = content.encode("utf-8")
    if len(payload) > MAX_SIZE_BYTES:
        raise FeatureFileError(
            f"content exceeds max size ({MAX_SIZE_BYTES} bytes)", status=413
        )

    target = _queue_dir(agent) / f"{bare}.txt"
    # Atomic write: create a sibling temp file and rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{bare}.", suffix=".tmp", dir=str(_queue_dir(agent))
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        # Cleanup the temp file if rename failed.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    stat = target.stat()
    return {
        "name": bare,
        "filename": target.name,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "created_at": time.time(),
    }


def feature_exists(name: str, agent: str = DEFAULT_AGENT) -> Optional[Path]:
    """Return the queue path if a feature file exists, else None. No raise."""
    try:
        bare = _validate_name(name)
    except FeatureFileError:
        return None
    path = _queue_dir(agent) / f"{bare}.txt"
    return path if path.exists() else None
