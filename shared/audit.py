"""Shared audit directory helpers for all QA-Agent-Network steps."""

import json
import sys
from pathlib import Path
from typing import Any, Optional

from shared.log import emit


def load_json(audit_dir: Path, filename: str, required: bool = True) -> Any:
    """
    Load a JSON file from audit_dir.
    Exits with status 1 if required=True and the file is missing.
    Returns {} for optional missing files.
    """
    path = audit_dir / filename
    if not path.exists():
        if required:
            emit(f"ERROR: {filename} not found in {audit_dir}")
            sys.exit(1)
        return {}
    return json.loads(path.read_text())


def write_gate(audit_dir: Path, gate_file: str, value: str) -> None:
    """Write a gate file (e.g. .fix-passed, .verdict) into audit_dir."""
    (audit_dir / gate_file).write_text(value)


def read_gate(audit_dir: Path, gate_file: str, default: str = "skipped") -> str:
    """Read a gate file from audit_dir. Returns default if file doesn't exist."""
    path = audit_dir / gate_file
    return path.read_text().strip() if path.exists() else default
