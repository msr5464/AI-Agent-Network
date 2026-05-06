"""Shared Slack notification helper for all QA-Agent-Network steps."""

import json
from urllib.request import Request, urlopen
from urllib.error import URLError


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
            return result.get("ok", False)
    except (URLError, Exception):
        return False
