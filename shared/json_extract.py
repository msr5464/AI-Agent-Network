"""Pull the JSON object out of a model reply that may wrap it in prose or a fence."""
import json
import re


def extract_json(text: str):
    # The closing fence is optional: models sometimes open ```json and never close it.
    m = re.search(r"```json\s*([\s\S]*?)\s*(?:```|$)", text or "")
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Prose before the object can carry braces of its own ("/cart/checkout/{uuid}"),
    # so decode from every "{" and keep the largest object, not the first match.
    text = text or ""
    decoder, best, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and (best is None or end - i > best[0]):
            best = (end - i, obj)
        i = text.find("{", end)
    return best[1] if best else None
