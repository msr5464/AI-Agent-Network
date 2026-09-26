"""The framework's own account of a run: readable, and small enough to carry.

Two problems, one source. The framework narrates itself with `STEP:` / `ACTION:`
lines and decorates some of them with HTML for the Studio UI, so:

  * a terminal or CI log shows raw markup — `<a href='...'>&#128247; View
    Screenshot</a>` instead of a link, which no plain-text consumer renders; and
  * the handoff's `execution_log` was the last 4000 characters of the run, which
    on a failing maven build is entirely the stack-trace block. Every narration
    line fell off the front, so the diagnosis reported "0 step(s), 0 action(s)
    completed" for a run that completed several of both.

`plain` fixes the first, `for_handoff` the second: it keeps the narration AND the
failure, because a diagnosis needs to know both how far the flow got and how it
stopped.
"""

import html
import re
from typing import List

# <a href='/path/x.png' target='_blank' style='...'>&#128247; View Screenshot</a>
# The href is the only part a reader cannot recover from anywhere else, so it is
# kept alongside the text rather than dropped with the tag.
_ANCHOR = re.compile(r"<a\s[^>]*href='([^']*)'[^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")

# The framework's timestamped narration, which `step_provenance` reads.
_NARRATION = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\]\s*(STEP:|ACTION:|API:|WARNING:|Description:|Navigating|"
    r"Browser initialized|Repair mode|Failed to |Locator for |Wait for )", re.I)


def plain(text: str) -> str:
    """One log line with its markup rendered for a plain-text reader."""
    if not text or "<" not in text:
        return html.unescape(text) if "&" in (text or "") else text
    rendered = _ANCHOR.sub(lambda m: f"{m.group(2).strip()} ({m.group(1)})", text)
    rendered = _TAG.sub("", rendered)
    return html.unescape(rendered)


def narration_lines(output: str) -> List[str]:
    """Just the framework's own account of what the test did, in order."""
    return [line for line in (output or "").splitlines()
            if _NARRATION.match(line.strip())]


def for_handoff(output: str, limit: int = 6000, tail: int = 3000) -> str:
    """The slice of a run worth carrying into a handoff.

    The tail alone loses the flow; the narration alone loses the failure. This
    keeps the most recent narration and the end of the output, in that order, so
    both `step_provenance` and the error parsers find what they read.
    """
    output = plain(output or "")
    if len(output) <= limit:
        return output
    end = output[-tail:]
    told = narration_lines(output)
    room = limit - len(end) - 40
    kept: List[str] = []
    for line in reversed(told):
        if room - len(line) - 1 < 0:
            break
        kept.insert(0, line)
        room -= len(line) + 1
    if not kept:
        return output[-limit:]
    return "\n".join(kept) + "\n…\n" + end
