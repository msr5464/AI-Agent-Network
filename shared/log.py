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


# ── Severity markers ──────────────────────────────────────────────────────────
#
# Agent stdout reaches the Studio console as one undifferentiated stream, so a
# line that says "no PR will be raised" renders exactly like a line that says
# which base class was loaded. These prefixes are the contract that lets the
# console colour the difference — matched there against the bare line, after the
# "[HH:MM:SS] [step] " prefix is stripped.
#
# `BLOCKED:` is reserved for the narrow case of the run failing to produce the
# thing it exists to produce — a PR that will not be raised. `Warning:` stays
# what it has always been: something went wrong and the run carried on anyway.
# Keeping that line means BLOCKED stays rare enough to be worth noticing.
BLOCKED_PREFIX = "BLOCKED:"


def blocked(reason: str, consequence: str, remedy: str = "") -> str:
    """A blocked-outcome line: what broke, what it costs, and what to do next.

    Plain text by design. The console builds text nodes from subprocess output
    rather than markup, so styling has to come from classifying the line — and
    a CI log or a terminal shows the same words with nothing stripped out.
    """
    line = f"{BLOCKED_PREFIX} {reason} — {consequence}"
    return f"{line} · fix: {remedy}" if remedy else line
