"""Pull login credentials out of raw, human-written test-case text.

Queue input files are plain English, so credentials arrive in whatever shape the
author typed them — `Username: foo`, `username=foo`, `login using username foo`.
Every step that needs them (01_parse's demo_credentials fallback, 02_validate_web's
"is this login step runnable?" check) used to carry its own regex, and they did not
agree: a real run with

    2. Do login by using the credentials given below:
    username=qa.user@example.com
    password=Sample@Pass123

was rejected with "no credentials found in input file" because both regexes matched
only `:` or whitespace after the label. One extractor, used everywhere, is what keeps
that from happening again.

This module owns the label vocabulary (LABELS). Bare "user" is not in it — it
false-positives on "Login as Admin user" — but "username" is.
"""

import csv
import io
import os
import re
from pathlib import Path

# Label alternatives per credential field. Ordered longest-first within each group
# so "user name" wins over a bare "user*" prefix match.
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
            if not value or value.lower() in _NOT_A_VALUE:
                continue
            # "every todo has userId 1." names a data field, not a login. A
            # username stated in prose has letters; a bare number there is a
            # field value. `User ID: 12345` still extracts via the separated pass.
            if field == "username" and not any(c.isalpha() for c in value):
                continue
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
    plan restored from the step cache, written before this extractor understood
    `username=foo`. Every step that needs credentials reads them through here,
    so a run whose input file has them never writes an empty
    {feature}.username property or reports them missing.

    Anything already in the plan wins; the file only fills the gaps.
    """
    creds = {k: v for k, v in (plan.get("demo_credentials") or {}).items() if v}
    if creds.get("username") and creds.get("password"):
        return creds
    text = input_text(plan, input_file)
    return {**extract_credentials(text), **creds} if text else creds


def input_text(plan: dict, input_file: str = "") -> str:
    """The raw test case a plan was parsed from, or "" when it cannot be found."""
    path = input_file or plan.get("_input_file") or os.environ.get("INPUT_FILE", "")
    if not path:
        return ""
    # queue/<module>.txt moves to queue/processed/<module>.txt once a run
    # completes, so a session resumed from a later step finds it there — the
    # same two candidates 05_ship.py reads the raw test case from.
    candidates = [Path(path), Path(path).parent / "processed" / Path(path).name]
    for candidate in candidates:
        try:
            return candidate.read_text()
        except OSError:      # not there, or unreadable
            continue
    return ""


LOGIN_WORDS = ("login", "log in", "sign in", "signin", "authenticate")


def mentions_login(text: str) -> bool:
    """Whether a flow logs in. An `Email:` and an `OTP:` are credentials only
    then: a checkout form asks for an email, and a bank page for an OTP."""
    lowered = (text or "").lower()
    return any(word in lowered for word in LOGIN_WORDS)


# A column holding a login secret, matched on word boundaries so a `footprint`
# column is not an `otp` one.
_SECRET_COLUMN = re.compile(r"(?<![a-z])(?:" + "|".join(
    (LABELS["password"], LABELS["api_key"], "token", "secret")) + r")(?![a-z])")
_OTP_COLUMN = re.compile(r"(?<![a-z])(?:" + LABELS["otp"] + r")(?![a-z])")


def secret_columns(header: str, test_case: str) -> list:
    """The columns of a CSV header row that hold a login secret, as written.

    A CSV is committed with the pull request; the properties file's secrets never
    are. A password, API key, token or secret column always holds one. An OTP
    column does only when the flow logs in: a bank page's 3-D Secure OTP is test
    data, and a payment run whose sheet held the sandbox OTP had the whole sheet
    refused. With no test case to tell, an OTP counts.
    """
    try:
        columns = next(csv.reader([header or ""]))
    except (csv.Error, StopIteration):
        return []
    otp_is_secret = mentions_login(test_case) or not (test_case or "").strip()
    found = []
    for column in columns:
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column).strip().lower()
        if _SECRET_COLUMN.search(name) or (otp_is_secret and _OTP_COLUMN.search(name)):
            found.append(column.strip())
    return found


def secret_property_key(feature: str, column: str) -> str:
    """The property a login-secret CSV column moves to: `{feature}.<column, snake_case>`."""
    return (f"{feature.lower()}."
            f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', column.strip()).lower()}")


# A cell the framework fills at read time ({randomString:8}): generated, not a secret.
_PLACEHOLDER = re.compile(r"^\{[A-Za-z]+(?::[^}]*)?\}$")


def take_secret_columns(content: str, columns: list) -> tuple:
    """Take the named columns out of CSV text.

    Returns (the CSV without them, {column: value}, {column: why it stayed}). A
    column whose cells are placeholders is generated data and stays. So does one
    holding several different values: one property cannot carry them.
    """
    rows = list(csv.reader(io.StringIO(content or "")))
    if not rows:
        return content, {}, {}
    header = [c.strip() for c in rows[0]]
    values, kept, strip = {}, {}, []
    for column in columns:
        if column not in header:
            continue
        i = header.index(column)
        cells = {r[i].strip() for r in rows[1:] if len(r) > i and r[i].strip()}
        if any(_PLACEHOLDER.match(v) for v in cells):
            continue
        if len(cells) > 1:
            kept[column] = f"holds {len(cells)} different values; one property cannot carry them"
            continue
        strip.append(i)
        values[column] = next(iter(cells), "")
    if not strip:
        return content, values, kept
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(
        [c for j, c in enumerate(r) if j not in strip] for r in rows)
    return out.getvalue(), values, kept
