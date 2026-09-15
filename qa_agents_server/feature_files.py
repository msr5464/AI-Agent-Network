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

from qa_agents_server import seed_examples
from qa_agents_server.agents import DEFAULT_AGENT, get_agent
from qa_agents_server.paths import REPO_ROOT

# Kept for callers that still import them directly.
QUEUE_DIR: Path = REPO_ROOT / "agents" / "test-authoring-agent" / "queue"
PROCESSED_DIR: Path = QUEUE_DIR / "processed"


# Same treatment the feature NAME already gets below, for the same reason. This
# value arrives from the X-User-ID header and is joined straight onto a path, so
# without it "/tmp/pwn" replaced the queue directory outright (pathlib lets an
# absolute segment win) and "../.." walked out of it — arbitrary directory
# creation and .txt read/write as the server user. routes.current_user_id()
# validates at the edge; this is the second lock on the same door.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _safe_user_id(user_id: str) -> str:
    candidate = (user_id or "").strip()
    return candidate if _USER_ID_RE.match(candidate) else "default"


# Each agent's run.sh applies exactly this carve-out where it locates its input
# file, and the server has to agree with it — a spec written where run.sh does
# not look can be listed and edited but never run. The anonymous and CLI
# identities work out of the queue root; everyone else gets a private
# subdirectory. Appending "default" unconditionally, as this used to, put the
# server in agents/<a>/queue/default while run.sh read agents/<a>/queue.
_SHARED_QUEUE_IDS = frozenset({"default", "cli"})


def _queue_dir(agent: str = DEFAULT_AGENT, user_id: str = "default") -> Path:
    spec = get_agent(agent)
    if spec.queue_kind != "txt":
        raise FeatureFileError(
            f"{spec.name}'s queue is not human-authored text", status=405)
    safe = _safe_user_id(user_id)
    return spec.queue_dir if safe in _SHARED_QUEUE_IDS else spec.queue_dir / safe


def _processed_dir(agent: str = DEFAULT_AGENT, user_id: str = "default") -> Path:
    return _queue_dir(agent, user_id) / "processed"

MAX_SIZE_BYTES = 64 * 1024  # 64 KB — a human-written spec won't be larger
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class FeatureFileError(Exception):
    """Raised for validation / IO errors the API should surface as 4xx."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _ensure_dirs(agent: str = DEFAULT_AGENT, user_id: str = "default"):
    _queue_dir(agent, user_id).mkdir(parents=True, exist_ok=True)
    _processed_dir(agent, user_id).mkdir(parents=True, exist_ok=True)
    _seed_once(agent, user_id)


def _seed_once(agent: str, user_id: str) -> None:
    """Put the shipped examples in this user's queue the first time it is used.

    Boot-time seeding fills the queue ROOT, which under per-user queues only the
    anonymous and CLI identities read. Every logged-in user's picker was
    therefore empty — and examples nobody can see are the whole point of
    seeding. seed_agent's marker file keeps this to once per user, so an example
    someone deletes stays deleted.
    """
    if not seed_examples.enabled():
        return
    try:
        seed_examples.seed_agent(get_agent(agent), _queue_dir(agent, user_id))
    except OSError:
        pass  # an empty picker is cosmetic; it must not fail the request


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


def list_features(agent: str = DEFAULT_AGENT, user_id: str = "default") -> List[Dict]:
    """List feature files in the queue (does not include processed/)."""
    _ensure_dirs(agent, user_id)
    items: List[Dict] = []
    for path in sorted(_queue_dir(agent, user_id).glob("*.txt")):
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


def read_feature(name: str, agent: str = DEFAULT_AGENT, user_id: str = "default") -> Dict:
    _ensure_dirs(agent, user_id)
    bare = _validate_name(name)
    path = _queue_dir(agent, user_id) / f"{bare}.txt"
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


def write_feature(name: str, content: str, agent: str = DEFAULT_AGENT, user_id: str = "default") -> Dict:
    _ensure_dirs(agent, user_id)
    bare = _validate_name(name)
    if not isinstance(content, str):
        raise FeatureFileError("content must be a string")
    payload = content.encode("utf-8")
    if len(payload) > MAX_SIZE_BYTES:
        raise FeatureFileError(
            f"content exceeds max size ({MAX_SIZE_BYTES} bytes)", status=413
        )

    target = _queue_dir(agent, user_id) / f"{bare}.txt"
    # Atomic write: create a sibling temp file and rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{bare}.", suffix=".tmp", dir=str(_queue_dir(agent, user_id))
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


def feature_exists(name: str, agent: str = DEFAULT_AGENT, user_id: str = "default") -> Optional[Path]:
    """Return the queue path if a feature file exists, else None. No raise."""
    try:
        bare = _validate_name(name)
    except FeatureFileError:
        return None
    path = _queue_dir(agent, user_id) / f"{bare}.txt"
    return path if path.exists() else None
