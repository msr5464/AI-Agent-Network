"""Every URL a generated test uses belongs in the framework's environment+country
properties file, never in Java source.

Jarvis/CLAUDE.md has said so from the start ("Hardcoded URL in test/page → put it
in the properties file"), but nothing enforced it, and a prompt rule alone loses:
a generated Naukri module shipped with

    private static final String BASE_URL  = "https://www.naukri.com";
    private static final String LOGIN_URL = "https://www.naukri.com/nlogin/login";

while parameters/staging-sg.properties carried no naukari entry at all. Pointing
the module at a different environment then means editing Java.

Three parts, in the order test-authoring-agent uses them:

  collect_urls()        harvest every URL the plan and the browser validation run
                        mention, and name a property key for each
  write_url_properties() write those keys into the properties file BEFORE codegen,
                        so the prompt can hand Claude keys that already resolve
  hardcoded_urls()      the guard: a literal URL still left in generated Java

Unlike credentials (see credential_properties.py), URLs are not secrets and MUST
be committed — code referencing {feature}.login.url is broken for everyone else
if the key never reaches the repo. 05_ship.py commits the URL keys only.
"""
import re
from pathlib import Path
from urllib.parse import urlsplit

from shared import properties_file

# A URL as it appears in free text (a plan step, a validation log line). The
# trailing class excludes the punctuation that normally ends a sentence or closes
# a quote around the URL rather than belonging to it.
URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)\]}]+")

# A URL literal in source: what the guard looks for.
_QUOTED_URL = re.compile(r'"(https?://[^"]*)"')

# Hosts that are not application URLs and never belong in a properties file:
# the local CDP endpoint the framework itself dials, and XML/XHTML schema URIs
# that are identifiers rather than addresses.
_EXEMPT_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]",
                 "www.w3.org", "java.sun.com", "xmlns.jcp.org", "maven.apache.org")

# A path segment that identifies one record rather than one page — an id, a hash,
# a date. Naming a property after it would produce a key nobody can reuse.
_OPAQUE_SEGMENT = re.compile(r"^(?:\d+|[0-9a-f]{8,}|.*\d{4,}.*)$")


# ── Naming ────────────────────────────────────────────────────────────────────

def normalize(url: str) -> str:
    """Trim the punctuation a URL picks up from surrounding prose, and drop a
    trailing slash so "https://x.com/" and "https://x.com" are one entry."""
    url = (url or "").strip().rstrip("'\".,;:!?)]}")
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return url.rstrip("/") or url


def _slug(segment: str) -> str:
    segment = re.sub(r"\.(html?|jsp|php|aspx?|do)$", "", segment.strip().lower())
    return re.sub(r"[^a-z0-9]+", "", segment)


def derive_key(feature_lower: str, url: str, taken=()) -> str:
    """Name the property key for one URL.

    The host on its own is the module's base URL ({feature}.url, matching the
    saucedemo.url / githubUrl entries already in the file). Anything with a path
    is named after its last meaningful segment — /nlogin/login → {feature}.login.url
    — so the key says which page it opens rather than repeating the URL.
    """
    segments = [_slug(s) for s in urlsplit(url).path.split("/") if s.strip()]
    segments = [s for s in segments if s and not _OPAQUE_SEGMENT.match(s)]

    candidates = [f"{feature_lower}.url"] if not segments else [
        f"{feature_lower}.{segments[-1]}.url",
        f"{feature_lower}.{'.'.join(segments[-2:])}.url",
    ]
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    stem = candidates[-1][:-len(".url")]
    return next(f"{stem}{n}.url" for n in range(2, 100) if f"{stem}{n}.url" not in taken)


# ── Harvesting ────────────────────────────────────────────────────────────────

def _plan_text(plan: dict) -> list:
    """The plan fields that can name a URL the test will navigate to.

    Deliberately not the whole plan: a URL mentioned in passing (a doc link in a
    description) would become a property nobody reads.
    """
    chunks = list(plan.get("web_steps_for_validation") or [])
    for step in (plan.get("interleaved_steps") or []):
        chunks.append(str(step))
    for method in (plan.get("web_test_methods") or []):
        chunks.extend(str(s) for s in (method.get("steps") or []))
    return chunks


def collect_urls(plan: dict, web_validation: dict = None) -> dict:
    """{property_key: url} for every URL this module needs, base URLs first.

    Sources, in priority order: the plan's declared base URLs, the steps the
    browser validation actually walked (the most trustworthy — they loaded), then
    the steps the plan asked for.
    """
    feature = (plan.get("feature_name") or "app").lower()
    web_validation = web_validation or {}
    urls: dict = {}
    seen: set = set()

    def add(raw_url, preferred_key=None):
        url = normalize(raw_url)
        if not url or url in seen:
            return
        if any(h in urlsplit(url).netloc.lower() for h in _EXEMPT_HOSTS):
            return
        key = preferred_key if (preferred_key and preferred_key not in urls) \
            else derive_key(feature, url, urls)
        urls[key] = url
        seen.add(url)

    add(plan.get("web_base_url"), f"{feature}.url")
    add(plan.get("api_base_url"), f"{feature}.api.url")
    for text in (list(web_validation.get("steps_passed") or [])
                 + list(web_validation.get("steps_failed") or [])
                 + _plan_text(plan)):
        for match in URL_IN_TEXT.finditer(str(text)):
            add(match.group(0))
    return urls


def write_url_properties(automation_framework_dir: Path, urls: dict,
                         feature_lower: str = "", log=lambda msg: None) -> str:
    """Write {key: url} into parameters/{environment}-{country}.properties.
    Idempotent, and never overwrites a value already set there.

    Returns "written" / "already present" / "nothing to write".
    """
    label = f"{feature_lower.capitalize()} URLs" if feature_lower else "URLs"
    return properties_file.upsert(
        properties_file.properties_path(automation_framework_dir),
        dict(urls), f"{label} (auto-added by test-authoring-agent)", log)


# ── The guard ─────────────────────────────────────────────────────────────────

def strip_comments(java_source: str) -> str:
    """Java source minus its comments, string literals intact.

    Needed because a URL in JavaDoc ("navigates to https://x/login") is fine and a
    URL in code is not — and because the naive "cut at //" would slice a literal
    "https://..." in half and hide exactly the violations this looks for.
    """
    out, i, n = [], 0, len(java_source)
    in_string = in_char = in_line_comment = in_block_comment = False
    while i < n:
        c = java_source[i]
        nxt = java_source[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                out.append(c)
        elif in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment, i = False, i + 1
            elif c == "\n":
                out.append(c)
        elif in_string or in_char:
            out.append(c)
            if c == "\\":
                out.append(nxt)
                i += 1
            elif (c == '"' and in_string) or (c == "'" and in_char):
                in_string = in_char = False
        elif c == "/" and nxt == "/":
            in_line_comment = True
        elif c == "/" and nxt == "*":
            in_block_comment, i = True, i + 1
        else:
            in_string, in_char = c == '"', c == "'"
            out.append(c)
        i += 1
    return "".join(out)


def hardcoded_urls(java_source: str) -> list:
    """Literal http(s) URLs left in Java code. Empty is the passing case."""
    found = []
    for match in _QUOTED_URL.finditer(strip_comments(java_source)):
        url = match.group(1)
        host = urlsplit(url).netloc.lower()
        if host and not any(h in host for h in _EXEMPT_HOSTS):
            found.append(url)
    return sorted(set(found))


def no_hardcoded_url(before: str, after: str) -> tuple:
    """Guard for an edit: reject one that introduces a literal URL. (ok, reason).

    Judged on ADDED lines only — a fix must not be blocked by a hardcoded URL that
    was already sitting in the file it happens to touch.
    """
    import difflib
    added = "\n".join(line[1:] for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++"))
    found = hardcoded_urls(added)
    if not found:
        return True, ""
    return False, (
        f"adds a literal URL {found[0][:60]!r} — URLs belong in "
        f"parameters/{{environment}}-{{country}}.properties and are read with "
        f"config.getRunTimeProperty(\"<feature>.<page>.url\") "
        f"(Jarvis/CLAUDE.md: \"Hardcoded URL in test/page → put in properties file\")")


def referenced_keys(java_source: str) -> list:
    """URL property keys the generated code reads. Used to catch a key the model
    invented that nothing ever wrote to the properties file."""
    return sorted({m.group(1) for m in re.finditer(
        r'getRunTimeProperty\s*\(\s*"([^"]+)"', java_source)
        if m.group(1).lower().endswith("url") or ".url" in m.group(1).lower()})
