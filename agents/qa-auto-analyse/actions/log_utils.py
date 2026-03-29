"""Shared logging with timestamps for all qa-agent steps."""

from datetime import datetime


def log(step, message):
    """Print a timestamped log line: [HH:MM:SS] [step] message"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{step}] {message}", flush=True)
