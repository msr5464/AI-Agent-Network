"""Shared timestamped logging for all QA-Agent-Network steps."""

from datetime import datetime


def log(step: str, message: str) -> None:
    """Print a timestamped log line: [HH:MM:SS] [step] message"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{step}] {message}", flush=True)
