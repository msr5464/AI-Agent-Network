"""Centralised filesystem paths for qa_agents_server.

Extracted to a standalone module so every other module can import it without
creating an import cycle through app.py / routes.py.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

AGENTS_DIR: Path = REPO_ROOT / "agents"
AUTHORING_AGENT_DIR: Path = AGENTS_DIR / "test-authoring-agent"
AUTHORING_QUEUE_DIR: Path = AUTHORING_AGENT_DIR / "queue"
AUTHORING_AUDIT_DIR: Path = AUTHORING_AGENT_DIR / "audit"
AUTHORING_RUN_SH: Path = AUTHORING_AGENT_DIR / "run.sh"

SERVER_STORAGE_DIR: Path = REPO_ROOT / "qa_agents_server" / "storage"
RUNS_REGISTRY_FILE: Path = SERVER_STORAGE_DIR / "agent_runs.json"

CONFIG_DIR: Path = REPO_ROOT / "config"
CONFIG_ENV_FILE: Path = CONFIG_DIR / ".env"
