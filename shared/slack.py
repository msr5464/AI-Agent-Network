"""Shared Slack notification helper for all QA-Agent-Network steps."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# Every message a run posts, kept in its audit dir so that replies in the
# message's thread can be read back as feedback (shared/slack_feedback.py).
MESSAGES_FILE = "slack-messages.json"


def _remember(channel: str, ts: str) -> None:
    audit = (os.environ.get("AUDIT_DIR") or "").strip()
    if not audit or not ts:
        return
    path = Path(audit) / MESSAGES_FILE
    try:
        sent = json.loads(path.read_text()) if path.exists() else []
        sent.append({"channel": channel, "ts": ts,
                     "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        path.write_text(json.dumps(sent, indent=2))
    except (OSError, ValueError):
        pass


def send_slack(token: str, channel: str, text: str) -> bool:
    """
    Post a message to a Slack channel via the chat.postMessage API.
    Returns True if Slack confirmed ok=true, False otherwise.
    Does nothing (returns False) if token or channel is empty.
    """
    if not token or not channel:
        return False
    payload = {"channel": channel, "text": text}
    try:
        req = Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                _remember(result.get("channel") or channel, result.get("ts") or "")
            return result.get("ok", False)
    except (URLError, Exception):
        return False
