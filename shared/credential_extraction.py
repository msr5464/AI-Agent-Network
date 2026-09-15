"""Pull login credentials out of raw, human-written test-case text.

Queue input files are plain English, so credentials arrive in whatever shape the
author typed them — `Username: foo`, `username=foo`, `login using username foo`.
Every step that needs them (01_parse's demo_credentials fallback, 02_validate_web's
"is this login step runnable?" check) used to carry its own regex, and they did not
agree: a real run with

    2. Do login by using the credentials given below:
    username=ms00000raj@gmail.com
    password=SingIsKing@1234

was rejected with "no credentials found in input file" because both regexes matched
only `:` or whitespace after the label, while credential_masking.py — which accepts
`[:=]` — had already masked those same two lines in the run header. One extractor,
used everywhere, is what keeps that from happening again.

This module owns the label vocabulary (LABELS); shared/credential_masking.py masks
exactly the same one, so nothing extractable can be printed unredacted. Bare "user"
is in neither — it false-positives on "Login as Admin user" — but "username" is.
"""

import os
import re
from pathlib import Path

# Label alternatives per credential field. Ordered longest-first within each group
# so "user name" wins over a bare "user*" prefix match. Public because
# credential_masking.py masks exactly this vocabulary (plus its own secret-ish
# labels): a value this module can extract is a value that module must redact.
LABELS = {
    "username": r"user\s*name|username|user\s*id|userid|login\s*id|e-?mail(?:\s*id)?",
    "password": r"password|passwd|pwd",
    "otp":      r"otp(?:\s*code)?|2fa(?:\s*code)?|mfa(?:\s*code)?",
    "api_key":  r"api[_\- ]?key|apikey",
}

# `label = value` / `label: value`. The explicit separator makes the value
# unambiguous, so this pass runs first and its result is never second-guessed.
_SEPARATED = {
    field: re.compile(rf"\b(?:{alts})\b\s*[:=]\s*(\S+)", re.IGNORECASE)
    for field, alts in LABELS.items()
}

# `label value`, for prose like "login using username foo, password bar". Much
# weaker — the token after the label is only a value if it isn't ordinary English —
# so it is a fallback for fields the separated pass did not fill.
_ADJACENT = {
    field: re.compile(rf"\b(?:{alts})\b\s+(?:is\s+|as\s+)?([^\s,;]+)", re.IGNORECASE)
    for field, alts in LABELS.items()
}

# Words that follow a credential label in a sentence rather than a value —
# "enter the username in the username field", "the password below".
_NOT_A_VALUE = {
    "and", "or", "the", "a", "an", "is", "are", "was", "in", "into", "on", "to", "of",
    "for", "from", "with", "using", "use", "used", "below", "above", "given", "field",
    "fields", "box", "input", "value", "values", "here", "then", "that", "this", "as",
    "enter", "type", "provide", "credentials", "credential", "will", "be", "should",
}


def _clean(value: str) -> str:
    """Strip the punctuation a sentence wraps a value in, but nothing a value can
    legitimately end with — a trailing `.` stays, since it may be part of a
    password or an email-ish username."""
    return value.strip().strip("\"'`<>()[]").rstrip(";:,")


def extract_credentials(text: str) -> dict:
    """Return whichever of username / password / otp / api_key the text states.

    Fields the text does not state are simply absent — callers decide what is
    required (a login flow needs username + password; an OTP-gated one also needs
    otp).
    """
    found: dict = {}
    if not text:
        return found

    for field, pattern in _SEPARATED.items():
        match = pattern.search(text)
        if match:
            value = _clean(match.group(1))
            if value:
                found[field] = value

    for field, pattern in _ADJACENT.items():
        if found.get(field):
            continue
        for match in pattern.finditer(text):
            value = _clean(match.group(1))
            if value and value.lower() not in _NOT_A_VALUE:
                found[field] = value
                break

    return found


def has_login_credentials(text: str) -> bool:
    """True when the text supplies both halves of a login."""
    creds = extract_credentials(text)
    return bool(creds.get("username") and creds.get("password"))


def credentials_from_plan(plan: dict, input_file: str = "") -> dict:
    """A plan's demo_credentials, completed from the input file it names.

    01_parse fills demo_credentials, but a plan can reach a later step without
    them: one Claude returned without them, or — the case that bit us — a
    TESTING_MODE-cached plan written before this extractor understood
    `username=foo`. Every step that needs credentials reads them through here,
    so a run whose input file has them never writes an empty
    {feature}.username property or reports them missing.

    Anything already in the plan wins; the file only fills the gaps.
    """
    creds = {k: v for k, v in (plan.get("demo_credentials") or {}).items() if v}
    if creds.get("username") and creds.get("password"):
        return creds

    path = input_file or plan.get("_input_file") or os.environ.get("INPUT_FILE", "")
    if not path:
        return creds
    # queue/<module>.txt moves to queue/processed/<module>.txt once a run
    # completes, so a session resumed from a later step finds it there — the
    # same two candidates 05_ship.py reads the raw test case from.
    candidates = [Path(path), Path(path).parent / "processed" / Path(path).name]
    for candidate in candidates:
        try:
            text = candidate.read_text()
        except OSError:      # not there, or unreadable
            continue
        return {**extract_credentials(text), **creds}
    return creds
