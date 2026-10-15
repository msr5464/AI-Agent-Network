"""Replies to the agents' Slack messages, read back as feedback (shared/feedback.py).

Every message a run posts is remembered in its audit dir (shared/slack.py). A reply
in that message's thread can then teach or judge the run:

    lesson: read the order total from the cart page, not the receipt
    verdict: reject wrong-flow — it never reached the cart
    verdict: edited no-reuse

Plain words rather than `/lesson`: Slack takes a leading slash as one of its own
commands. Other replies are kept as comments.

Reading a thread needs the bot to have `channels:history` (and `groups:history`
for a private channel). Without it Slack answers `missing_scope`, and this says so
once and stops, rather than failing the run that called it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shared import feedback
from shared.slack import MESSAGES_FILE

LOOKBACK_DAYS = 60
_LESSON = re.compile(r"^\s*lesson\s*:\s*(.+)$", re.I | re.S)
_VERDICT = re.compile(r"^\s*verdict\s*:\s*(accept|edited|reject)\b(.*)$", re.I | re.S)
# The note follows a dash with space on both sides; a reason's own hyphen has none.
_NOTE_DASH = re.compile(r"\s+(?:—|–|--|-)\s+")


def parse(text: str) -> Dict:
    """What a reply says: {"kind": "lesson"|"verdict"|"comment", ...}."""
    text = (text or "").strip()
    match = _LESSON.match(text)
    if match:
        return {"kind": "lesson", "text": match.group(1).strip()}
    match = _VERDICT.match(text)
    if match:
        rest = match.group(2) or ""
        parts = _NOTE_DASH.split(rest, maxsplit=1)
        words = [w for w in re.split(r"[\s,]+", parts[0]) if w]
        reasons = [w.lower() for w in words if w.lower() in feedback.REASONS]
        return {"kind": "verdict", "verdict": match.group(1).lower(), "reasons": reasons,
                "text": parts[1].strip() if len(parts) > 1 else ""}
    return {"kind": "comment", "text": text}


def _replies(token: str, channel: str, ts: str) -> Dict:
    query = urlencode({"channel": channel, "ts": ts, "limit": 200})
    request = Request(f"https://slack.com/api/conversations.replies?{query}",
                      headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _sessions(agents_dir: Path, since: float):
    for messages in Path(agents_dir).glob(f"*/audit/*/{MESSAGES_FILE}"):
        try:
            if messages.stat().st_mtime < since:
                continue
            sent = json.loads(messages.read_text())
        except (OSError, ValueError):
            continue
        session = messages.parent
        yield session.parent.parent.name, session.name, sent


def events_from(agent: str, session: str, channel: str, parent_ts: str,
                replies: List[Dict]) -> List[Dict]:
    """Feedback events for the replies in one message's thread (not the message itself)."""
    found = []
    for reply in replies:
        if reply.get("ts") == parent_ts or reply.get("bot_id"):
            continue
        said = parse(reply.get("text") or "")
        if not said.get("text") and said["kind"] != "verdict":
            continue
        found.append(feedback.event(
            said["kind"], source="slack", explicit=True,
            key=(channel, parent_ts, reply.get("ts")),
            agent=agent, session=session,
            artifact={"channel": channel, "thread_ts": parent_ts, "reply_ts": reply.get("ts")},
            verdict=said.get("verdict"), reasons=said.get("reasons") or [],
            text=said.get("text", ""), who=reply.get("user", ""),
            at=reply.get("ts", "")))
    return found


def collect(agents_dir: Path, log: Callable[[str], None] = lambda m: None,
            fetch: Optional[Callable[[str, str, str], Dict]] = None) -> Dict:
    """Read the threads of the last LOOKBACK_DAYS of messages into the store."""
    token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if not token and fetch is None:
        return {"skipped": "SLACK_BOT_TOKEN not set"}
    fetch = fetch or (lambda channel, ts, _token=token: _replies(_token, channel, ts))
    since = time.time() - LOOKBACK_DAYS * 86400
    found: List[Dict] = []
    threads = 0
    for agent, session, sent in _sessions(agents_dir, since):
        for message in sent:
            try:
                answer = fetch(message.get("channel", ""), message.get("ts", ""))
            except Exception as exc:
                return {"skipped": f"Slack not read: {exc}", "threads": threads,
                        "new": _store(found, agents_dir)}
            if not answer.get("ok"):
                error = answer.get("error", "unknown error")
                if error in ("missing_scope", "not_allowed_token_type", "invalid_auth"):
                    log(f"Slack: replies not read ({error}); the bot needs channels:history "
                        f"(groups:history for private channels)")
                    return {"skipped": f"Slack: {error}", "threads": threads,
                            "new": _store(found, agents_dir)}
                continue
            threads += 1
            found += events_from(agent, session, message.get("channel", ""),
                                 message.get("ts", ""), answer.get("messages") or [])
    return {"threads": threads, "new": _store(found, agents_dir)}


def _store(found: List[Dict], agents_dir: Path) -> List[Dict]:
    """Write the new events; a new verdict also reaches where its agent looks."""
    new = feedback.append(found) if found else []
    for e in new:
        if e["kind"] == "verdict" and e.get("verdict"):
            feedback.verdict_effects(e["agent"], e["session"], e["verdict"], e["reasons"],
                                     source="slack", agents_dir=Path(agents_dir))
    return new
