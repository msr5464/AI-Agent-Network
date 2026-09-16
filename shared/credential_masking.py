"""Shared credential-masking helpers.

Used anywhere raw, user-authored test-case text needs to be shown (PR
descriptions, execution logs) without leaking real credentials that text can
legitimately contain — queue input files in this repo have, in practice,
contained real personal credentials for the system under test (see e.g.
agents/test-authoring-agent/queue/processed/naukari_profile_update.txt).
"""

import re

from shared.credential_extraction import LABELS

# Every label credential_extraction can pull a value from, so anything the
# pipeline treats as a credential is also redacted here — the two vocabularies
# drifting apart is how `username=foo` came to be masked in one place and
# reported as "no credentials found" in another. Plus the secret-ish labels that
# are never extracted but must never be printed either.
#
# Bare "user" is in neither list: it would false-positive on "Admin user".
_EXTRA_SECRET_LABELS = r"token|secret|authorization"
# Every `label: value` pair, not one per line: a one-line curl carries several
# (`-H "Authorization: Bearer …" -H "x-api-key: …" -d '{"password": "…"}'`), and
# the line-anchored form masked only the last. The optional quote handles JSON
# keys, and a Bearer/Basic scheme word is skipped so the token itself is masked.
_CREDENTIAL_LINE_RE = re.compile(
    r"(?i)(\b(?:"
    + "|".join(list(LABELS.values()) + [_EXTRA_SECRET_LABELS])
    + r")\b[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic)\s+)?)(\S+)"
)


def mask_credential_lines(text: str) -> str:
    """Pattern-based redaction — catches every `label: value` credential shape
    without needing to already know the actual credential values.

    This is the ONLY layer available before 01_parse.py has run (before
    demo_credentials exists — e.g. run.sh's own session-init log, printed
    before step 01 even starts). See mask_credential_values for a stronger,
    value-based pass once demo_credentials is available.
    """
    return _CREDENTIAL_LINE_RE.sub(_mask_match, text)


# A bare number under a user-id label is a record id in a request body
# (`"userId": 1,`), not a login. Only that label gets the exemption: a numeric OTP,
# PIN-style password or key is still a secret.
_USERNAME_LABEL = re.compile(rf"(?i)\b(?:{LABELS['username']})\b")
_BARE_NUMBER = re.compile(r"\d+[,;}\])\"']*")


def _mask_match(match: re.Match) -> str:
    label, value = match.group(1), match.group(2)
    if _BARE_NUMBER.fullmatch(value) and _USERNAME_LABEL.match(label):
        return match.group(0)
    return label + "***MASKED***"


def mask_credential_values(text: str, demo_creds: dict) -> str:
    """Value-based redaction — replaces every occurrence of an ALREADY-KNOWN
    credential value (e.g. from 01_parse.py's demo_credentials) with a
    labeled placeholder.

    Catches a value that appears without a recognizable label nearby, which
    mask_credential_lines alone would miss.
    """
    masked = text
    for field, value in (demo_creds or {}).items():
        value = str(value or "")
        if len(value) >= 3:  # avoid mass-redacting on a trivially short value
            masked = masked.replace(value, f"***{field.upper()}***")
    return masked


def mask_credentials(text: str, demo_creds: dict) -> str:
    """Both layers together — the full redaction used once demo_credentials
    is available (e.g. 05_ship.py's PR body, built after step 01 has run)."""
    return mask_credential_lines(mask_credential_values(text, demo_creds))
