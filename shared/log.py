"""Shared timestamped logging for all QA-Agent-Network steps."""

import os
import sys
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


# ── Severity markers ──────────────────────────────────────────────────────────
#
# Agent stdout reaches the Studio console as one undifferentiated stream, so a
# line that says "no PR will be raised" renders exactly like a line that says
# which base class was loaded. These prefixes are the contract that lets the
# console colour the difference — matched there against the bare line, after the
# "[HH:MM:SS] [step] " prefix is stripped. severity() below applies the same
# vocabulary in the terminal, and shared/session.sh mirrors it for the bash side
# of every agent, so one line is classified the same way wherever it is read.
#
# `BLOCKED:` is reserved for the narrow case of the run failing to produce the
# thing it exists to produce — a PR that will not be raised. `Warning:` stays
# what it has always been: something went wrong and the run carried on anyway.
# Keeping that line means BLOCKED stays rare enough to be worth noticing.
BLOCKED_PREFIX = "BLOCKED:"

ERROR_PREFIXES   = ("ERROR", "FATAL", "FAILED", BLOCKED_PREFIX)
WARNING_PREFIXES = ("WARNING", "WARN", "CAUTION")

_RESET   = "\033[0m"
_COLOURS = {
    "error":   "\033[1;31m",   # bold red
    "warning": "\033[0;33m",   # yellow
}


def severity(message: str) -> str:
    """'error' / 'warning' / 'info', from the first line's leading word.

    Only a leading marker counts. "ERROR: login failed" is an error; "the fix
    guard rejected a WARNING annotation" is an ordinary line that happens to
    contain the word, and colouring those would make the real ones cheaper.
    """
    head = (message or "").lstrip().split("\n", 1)[0].upper()
    if head.startswith(ERROR_PREFIXES):
        return "error"
    if head.startswith(WARNING_PREFIXES):
        return "warning"
    return "info"


def color_enabled() -> bool:
    """Colour goes to terminals only.

    Agent stdout is a pipe under qa_agents_server — the Studio console builds
    text nodes from it, so an escape sequence would be literal junk both there
    and in the persisted stdout.log; the console colours by prefix instead.
    QA_LOG_COLOR forces the decision either way (always / never), and NO_COLOR
    is honoured because it is the cross-tool convention.
    """
    mode = os.environ.get("QA_LOG_COLOR", "auto").strip().lower()
    if mode in ("always", "force", "1", "true", "yes"):
        return True
    if mode in ("never", "off", "0", "false", "no"):
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):   # a closed or exotic stdout
        return False


def paint(text: str, level: str) -> str:
    """Wrap text in the colour for a severity, one physical line at a time.

    Per line, so a multi-line message (an error plus the steps to fix it) stays
    highlighted all the way down and no escape sequence is left open across a
    line another process may interleave into.
    """
    colour = _COLOURS.get(level, "") if color_enabled() else ""
    if not colour:
        return text
    return "\n".join(f"{colour}{line}{_RESET}" for line in text.split("\n"))


def blocked(reason: str, consequence: str, remedy: str = "") -> str:
    """A blocked-outcome line: what broke, what it costs, and what to do next.

    Plain text by design. The console builds text nodes from subprocess output
    rather than markup, so styling has to come from classifying the line — and
    a CI log or a terminal shows the same words with nothing stripped out.
    """
    line = f"{BLOCKED_PREFIX} {reason} — {consequence}"
    return f"{line} · fix: {remedy}" if remedy else line


def emit(message: str) -> None:
    """Print a line that carries no timestamp/step prefix, coloured the same way.

    A few places print bare lines into the same stream an agent's log goes to —
    shared/workspace.py's prerequisite output, shared/audit.py's missing-file
    exit. They are read as part of the run log, so an error among them is
    highlighted like every other error rather than by where it came from.
    """
    print(paint(message, severity(message)), flush=True)


def log(step: str, message: str) -> None:
    """Print a timestamped log line: [HH:MM:SS] [step] message

    An ERROR / WARNING / BLOCKED line is printed in colour when the destination
    is a terminal — see color_enabled() for why that gate exists.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    message = _redact(message)
    print(paint(f"[{ts}] [{step}] {message}", severity(message)), flush=True)
