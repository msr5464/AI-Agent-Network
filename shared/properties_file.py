"""Read and write the Java framework's environment+country properties file
(parameters/{environment}-{country}.properties), matching Config.java's own
file-loading convention (confirmed by reading Config.java's loadPropertiesFile
calls: filename = environment + "-" + country + ".properties", loaded from the
"parameters" directory).

Two callers write into it — credential_properties (login credentials) and
url_properties (every URL a generated test navigates to). Both need the same
idempotence rules: never overwrite a value a human may have set, and fill a key
that exists with an EMPTY value in place rather than appending a second copy.
Those rules live here once rather than in each caller.
"""
import os
from pathlib import Path


def properties_path(automation_framework_dir: Path) -> Path:
    """The properties file Config.java loads for this run's environment+country."""
    environment = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging").lower()
    country     = os.environ.get("AUTOCREATE_COUNTRY", "SG").lower()
    return automation_framework_dir / "parameters" / f"{environment}-{country}.properties"


def read_values(text: str) -> dict:
    """{key: value} for every non-comment line that carries a value.

    A key present with an EMPTY value is deliberately left out: getRunTimeProperty
    hands the test "" and the failure then surfaces far from the missing setting —
    as a locator error, or a navigation to nowhere. Every caller here wants such a
    key treated as absent so it gets filled.
    """
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if value.strip():
            values[key.strip()] = value.strip()
    return values


def apply(text: str, wanted: dict, comment: str) -> tuple:
    """Return (new_text, filled, appended) for adding `wanted` to `text`.

    Pure text in, text out — no filesystem. 05_ship.py uses this to compute what
    the *committed* properties file should contain without touching the working
    copy, which at that point holds run credentials that must never be committed.
    """
    missing = {k: v for k, v in wanted.items() if k not in read_values(text)}
    if not missing:
        return text, {}, {}

    # Java's Properties takes the last occurrence, so appending a duplicate key
    # would technically work — but a file listing the same key twice, once blank,
    # is exactly what misleads whoever opens it while debugging.
    filled = {}
    if text:
        rewritten = []
        for line in text.splitlines(keepends=True):
            bare = line.strip()
            if bare and not bare.startswith(("#", "!")) and "=" in bare:
                name, value = bare.split("=", 1)
                name = name.strip()
                if name in missing and not value.strip():
                    rewritten.append(f"{name}={missing[name]}\n")
                    filled[name] = missing[name]
                    continue
            rewritten.append(line)
        text = "".join(rewritten)

    appended = {k: v for k, v in missing.items() if k not in filled}
    if appended:
        separator = "" if (not text or text.endswith("\n")) else "\n"
        body = "\n".join(f"{k}={v}" for k, v in appended.items())
        text = f"{text}{separator}# {comment}\n{body}\n"
    return text, filled, appended


def upsert(path: Path, wanted: dict, comment: str, log=lambda msg: None) -> str:
    """Ensure every key in `wanted` holds a value in `path`. Idempotent —
    a key that already has a real value is left alone (a human may have
    deliberately changed it).

    Returns "written" / "already present" / "nothing to write".
    """
    wanted = {k: v for k, v in wanted.items() if k and v}
    if not wanted:
        return "nothing to write"

    text = path.read_text() if path.exists() else ""
    new_text, filled, appended = apply(text, wanted, comment)
    if not filled and not appended:
        log(f"  {path.name} already has {', '.join(sorted(wanted))} — leaving as-is")
        return "already present"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    for label, keys in (("Filled empty value(s) in", filled), ("Wrote to", appended)):
        if keys:
            log(f"  {label} {path.name}: {', '.join(sorted(keys))}")
    return "written"
