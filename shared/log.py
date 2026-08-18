"""Shared timestamped logging for all QA-Agent-Network steps."""

import os
from datetime import datetime

# Infrastructure secrets that must never reach a log line — including inside
# a raw subprocess error message we didn't author ourselves (e.g. git's own
# "could not read Password for 'https://TOKEN@github.com'" failure text, if
# a token ever ends up embedded in a URL again). Read fresh from the
# environment on every call, not cached, since each action script sets up
# its own environment before this module is used.
_SECRET_ENV_VARS = ("GITHUB_TOKEN", "SLACK_BOT_TOKEN")


def _redact(message: str) -> str:
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var, "")
        # A length floor avoids redacting every occurrence of a trivially
        # short/placeholder value (e.g. an accidentally-empty or single-char
        # env var) — real tokens are always long random strings.
        if len(value) >= 8 and value in message:
            message = message.replace(value, f"***REDACTED({var})***")
    return message


def log(step: str, message: str) -> None:
    """Print a timestamped log line: [HH:MM:SS] [step] message"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{step}] {_redact(message)}", flush=True)
