#!/usr/bin/env python3
"""
Step 01 — Locate

Work out which element a broken locator meant, and prove it by performing the
step. No model call: the answer comes from comparing the failing page against a
fingerprint recorded while the locator still worked, and is then verified by
executing the action and checking a post-condition.

Two halves, as designed. The failure-time fingerprint array written beside the
DOM snapshot is ranked offline, which needs no browser and costs milliseconds.
Only the top candidates are then verified live, which is the part that separates
a plausible match from a working one — and the part every published
relocalization tool skips.

Writes a resolution per locator. Applying it is 01_fix's job; this step never
edits a file, so a wrong answer here cannot reach the repo on its own.

Reads:   HANDOFF_FILE, AUDIT_DIR, WORKSPACE_DIR, GITHUB_REPO_AUTOMATION,
         HEALING_LOCATE_MODE (shadow|enforce), HEALING_LOCATE_STORAGE_STATE
Outputs: audit/<session>/01-locate.json + 01-locate.md

Exits 0 in every non-crash case. "No baseline for this locator" and "this is not
locator drift" are both legitimate outcomes, not errors.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → shared.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
def log(msg): _log("locate", msg)

import yaml

from shared import baseline as baseline_store
from shared import browser_mode
from shared import entry_path, mint_session, session_state
from shared import dom_snapshot, failure_context, locator_assertions, page_identity
from shared import locator_capture
from shared import workspace as workspace_helper
from shared import locator_resolve as engine
from shared import locator_verify

AUDIT_DIR    = Path(os.environ["AUDIT_DIR"])
REPO_ROOT    = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
HANDOFF_FILE = Path(os.environ["HANDOFF_FILE"])
CONFIG_FILE  = Path(os.environ.get("HEALING_LOCATE_CONFIG", REPO_ROOT / "config" / "locator.yaml"))

# shadow: locate, verify, record what we would have done, and change nothing.
# enforce: 01_fix applies a verified resolution instead of calling the model.
#
# Shadow is the default for the same reason DIAGNOSIS_MODE is: this step can
# refuse work the agent used to attempt, and that risk deserves a measurement
# rather than a leap.
HEALING_LOCATE_MODE = os.environ.get("HEALING_LOCATE_MODE", "shadow").strip().lower()

STORAGE_STATE = os.environ.get("HEALING_LOCATE_STORAGE_STATE", "")
# Minting runs the test's own login through maven, so it costs a minute the
# first time. Worth it: the alternative is refusing every locator on a page
# the replay never actually reached.
MINT_SESSION = os.environ.get("LOCATE_MINT", "true").strip().lower() != "false"
SESSION_MIN_S = int(os.environ.get("LOCATE_SESSION_MIN_S", "60"))


def _workspace() -> Path | None:
    """The automation checkout: FRAMEWORK_DIR, else WORKSPACE_DIR/repo."""
    candidate = workspace_helper.expected(
        os.environ.get("WORKSPACE_DIR", ""),
        os.environ.get("GITHUB_REPO_AUTOMATION", ""))
    return candidate if candidate and candidate.is_dir() else None


def _page_object_sources(workspace: Path) -> dict:
    """Every page object in the automation repo, by simple class name."""
    sources = {}
    modules = workspace / "src" / "main" / "java"
    if not modules.is_dir():
        return sources
    for path in modules.rglob("*.java"):
        try:
            sources[path.stem] = path.read_text(errors="ignore")
        except OSError:
            continue
    return sources


# BasePage helpers take the locator first and a human element name last:
#   click(editProfileSummaryButton, "Edit Profile Summary button")
#   fillText(summaryTextArea, text, "Profile Summary Text Area")
# The runtime error quotes that name, which makes it a second way to identify the
# field when the selector string itself no longer appears in the source.
_ELEMENT_NAME = re.compile(r"element '([^']{2,80})'")


def _field_by_element_name(sources: dict, error_text: str, prefer: str = ""):
    """Recover the field from the element name the failure quotes.

    Selector matching is exact and therefore brittle in one specific way: if the
    page object has been edited since the failing run — a partially applied fix, a
    stale handoff, a workspace on a different commit — the selector that failed is
    no longer in the file and nothing matches. The element name survives those
    edits, because it names the thing rather than how to find it.
    """
    match = _ELEMENT_NAME.search(error_text or "")
    if not match:
        return None, None, None
    wanted = re.escape(match.group(1))
    call = re.compile(r"\(\s*(\w+)\s*,[^;()]{0,120}?\"" + wanted + r"\"")
    # Same collision hazard as `_declaring_field`: "Login button" is a name several
    # pages give their own button. Look in the page object the failure named first.
    ordered = sorted(sources.items(), key=lambda kv: kv[0] != prefer)
    for class_name, source in ordered:
        found = call.search(source)
        if not found:
            continue
        field = found.group(1)
        for declared in page_identity.extract_locators(source):
            if declared.get("name") == field:
                return class_name, field, declared["raw"]
    return None, None, None


# "at automation.modules.naukari.web.NaukriLoginPage.doLogin(NaukriLoginPage.java:36)"
_TRACE_CLASS = re.compile(r"\b([A-Z]\w+)\.java\b")


def _owner_hint(issue: dict) -> str:
    """The page object the failure itself names. Empty when nothing does.

    Two independent records point at it and this step read neither: the failure
    context the framework wrote while the element was failing, which names the
    page object it belongs to, and the stack frame the assertion was raised from.
    """
    context = issue.get("failure_context") or ""
    if isinstance(context, dict):
        named = context.get("page_object") or ""
    elif context and Path(context).exists():
        named = failure_context.load(context).get("page_object") or ""
    else:
        named = ""
    if named:
        return named
    match = _TRACE_CLASS.search(issue.get("stack_trace") or "")
    return match.group(1) if match else ""


def _declaring_field(sources: dict, failed_selector: str, prefer: str = ""):
    """Which page object and field declares this selector.

    Matched on the normalised selector so a runtime-reported locator that differs
    only in quoting or whitespace still finds its declaration.

    `prefer` is the page object the failure named, and it is what makes the answer
    a fact rather than a coincidence. A selector like "button[type=\'submit\']" is
    declared by several unrelated pages in any real repo; taking the first class
    that matched let `rglob` order decide, which sent this step off to find a
    baseline for a page the test never opened. When the failure names none of the
    candidates, refuse: a confident wrong owner is worse than no owner.
    """
    wanted = page_identity.normalize_selector(failed_selector) or failed_selector
    found = []
    for class_name, source in sources.items():
        for declared in page_identity.extract_locators(source):
            if not declared.get("name"):
                continue
            raw = declared["raw"]
            if raw == failed_selector or (declared.get("selector") or raw) == wanted:
                found.append((class_name, declared["name"], raw))
    if not found:
        return None, None, None
    if len(found) == 1:
        return found[0]

    listed = ", ".join(f"{c}#{f}" for c, f, _ in found)
    chosen = next((entry for entry in found if entry[0] == prefer), None)
    if not chosen:
        log(f"  {failed_selector!r} is declared by {len(found)} page objects "
            f"({listed}) and the failure names "
            f"{prefer or 'none of them'} — refusing to guess which one broke")
        return None, None, None
    log(f"  {failed_selector!r} is declared by {len(found)} page objects ({listed}) "
        f"— using {chosen[0]}#{chosen[1]}, the one the failure names")
    return chosen


def _failure_fingerprints(issue: dict) -> list:
    """The element array captured beside the DOM snapshot at failure time.

    Re-deriving these from the saved HTML would lose bounding boxes and computed
    ARIA roles, which is most of what separates two similar candidates.
    """
    path = issue.get("dom_snapshot") or issue.get("dom_snapshot_path") or ""
    if not path or not Path(path).exists():
        return []
    try:
        header = dom_snapshot.parse_header(Path(path).read_text(errors="ignore")[:2000])
    except OSError:
        return []
    sidecar = header.get("fingerprints") or ""
    if not sidecar or not Path(sidecar).exists():
        return []
    try:
        return (json.loads(Path(sidecar).read_text()) or {}).get("elements") or []
    except (OSError, ValueError):
        return []


def _headless(workspace) -> bool:
    """Launch the browser the way the run was asked to.

    Not cosmetic. Some sites serve a bot-block page to a headless browser, and a
    verdict reached on "Access Denied" is worse than no verdict — every locator
    looks removed and the page looks like the wrong one.

    HEADLESS_BROWSER governs, as it does for every other browser in the
    network; failing that, follow the framework's own answer in
    parameters/config.properties rather than guessing.

    The bot-block case does not get its own switch. A manual variable is no
    defence against something nobody knows to set it for — the suite-level
    circuit breaker in locator_resolve is, because it trips on the signature
    itself: twenty locators that all look removed at once.
    """
    decided = browser_mode.configured()
    if decided is not None:
        return decided
    from_framework = baseline_store.framework_property(workspace, "headless")
    return (from_framework or "true").strip().lower() != "false"


def _headless_source() -> str:
    """Which setting decided it. Worth logging: a browser in the wrong mode
    explains a whole run's worth of odd verdicts, and nothing else says so."""
    if browser_mode.configured() is not None:
        return f"from {browser_mode.ENV_VAR}"
    return "from parameters/config.properties"


# A page object whose name says it is where you sign in, and a URL that says the
# same. Either alone is weak; the pair is what the check below asks for.
_SIGN_IN_CLASS = re.compile(r"(login|signin|sign_in|auth)", re.I)
_SIGN_IN_URL = re.compile(r"/(login|signin|sign-in|auth)(/|\?|$)", re.I)


def _is_sign_in_page(class_name: str, url: str) -> bool:
    """Whether the page holding this locator is the one you sign in on.

    Named by the page object OR by the URL: a repo may call it AuthPage while the
    route is /nlogin/login, and either naming is enough to make authenticating
    before the examination the wrong move.
    """
    return bool(_SIGN_IN_CLASS.search(class_name or "")
                or _SIGN_IN_URL.search(url or ""))


def _entry(workspace: Path | None, issue: dict) -> dict:
    """How this test signs in, read from its own setup. {} when unknown.

    entry_path expects Class#method; the handoff carries Class.method. A method
    name starts lower-case and a class name does not, which is what separates
    "…WebTest.toggleDot" from a bare "…WebTest".
    """
    if not workspace:
        return {}
    test_id = issue.get("test_name") or ""
    tail = test_id.rsplit(".", 1)
    if "#" not in test_id and len(tail) == 2 and tail[1][:1].islower():
        test_id = f"{tail[0]}#{tail[1]}"
    try:
        return entry_path.extract(workspace, test_id) or {}
    except Exception as exc:                         # noqa: BLE001 - advisory only
        log(f"  could not read the test's entry path ({type(exc).__name__})")
        return {}


_USER_FIELD = re.compile(r"user|email|login|mobile|phone", re.I)
_PASS_FIELD = re.compile(r"pass|pwd|secret", re.I)
_SUBMIT_FIELD = re.compile(r"login|signin|sign_in|submit|continue", re.I)


def _login_fields(workspace: Path, module: str) -> dict:
    """The sign-in page object's username / password / submit selectors.

    Found by shape rather than by name: a repo names these LoginPage, SignInPage
    or AuthPage, and the fields inside them are usernameField / emailInput /
    mobileNo. Guessing wrong here is cheap — the replay simply fails and we fall
    back to refusing, which is what happens today anyway.
    """
    roots = list((workspace / "src" / "main" / "java").rglob("*Login*.java"))
    roots += list((workspace / "src" / "main" / "java").rglob("*SignIn*.java"))
    preferred = [r for r in roots if module and module in str(r).lower()]
    for source_file in (preferred or roots):
        try:
            declared = page_identity.extract_locators(source_file.read_text(errors="ignore"))
        except OSError:
            continue
        found = {}
        for entry in declared:
            name, selector = entry.get("name") or "", entry.get("selector") or ""
            if not selector:
                continue
            if "user" not in found and _USER_FIELD.search(name) and not _PASS_FIELD.search(name) \
                    and not _SUBMIT_FIELD.search(name):
                found["user"] = selector
            elif "password" not in found and _PASS_FIELD.search(name):
                found["password"] = selector
            elif "submit" not in found and _SUBMIT_FIELD.search(name):
                found["submit"] = selector
        if {"user", "password", "submit"} <= set(found):
            return found
    return {}


def _login_replay(workspace: Path | None, issue: dict, entry: dict, target_url: str):
    """Sign in the way the test does, in the browser we are about to search with.

    Restoring a saved session is the cheaper route and the one Locate tried first,
    but it is not universal: this site binds a session to a short-lived bot-manager
    cookie and refuses a restored one in a fresh browser, so the replay landed on
    the sign-in page with a session that was, by every local check, perfectly
    valid. Performing the login is the only replay that is true by construction —
    it is what the test itself does.

    Returns None when anything is missing, because a half-configured login is a
    worse answer than an honest refusal.
    """
    if not workspace or entry.get("mode") != "credential" or not target_url:
        return None
    parts = [part for part in (issue.get("test_name") or "").split(".") if part]
    module = parts[1].lower() if len(parts) > 2 else ""

    props = mint_session.read_properties(mint_session.properties_path(workspace))
    keys = [k for k in (entry.get("arg_keys") or []) if k]
    values = [props.get(k) for k in keys]
    if len(values) < 2 or not all(values):
        return None
    username, password = values[0], values[1]

    login_url = ""
    for key in (f"{module}.login.url", f"{module}LoginUrl", f"{module}.url"):
        if props.get(key):
            login_url = props[key]
            break
    fields = _login_fields(workspace, module)
    if not login_url or not fields:
        return None

    def replay(page):
        page.goto(login_url)
        page.fill(fields["user"], username)
        page.fill(fields["password"], password)
        page.click(fields["submit"])
        try:
            # The click starts a navigation that has not committed yet; going
            # straight to the target races the redirect and one of them aborts.
            page.wait_for_url(lambda url: "login" not in url.lower(), timeout=30_000)
        except Exception:                            # noqa: BLE001 - best effort
            pass
        page.goto(target_url)

    log(f"  replaying the sign-in the test performs ({', '.join(keys)})")
    return replay


def _storage_state(workspace: Path | None, issue: dict) -> str | None:
    """The signed-in session to replay with, established the way the test does.

    Globbing loginStorage/ for the newest file was wrong twice over. It answered
    "is there a session for this module?" when the question is "how does THIS test
    sign in" — and this repo has tests that never touch a stored session, so it
    handed the replay a file the test does not use. And it validated that file by
    mtime, which says nothing: an expired session still parses, the browser still
    accepts it, and the flow simply lands on a login page. Every locator then looks
    missing and the honest-looking verdict is WRONG_STATE, blaming a page that was
    never examined.

    `entry_path` already reads the test's own setup and `session_state` already
    audits cookie expiry — both written for the adaptation agent, both skipped here.
    """
    if STORAGE_STATE:
        return STORAGE_STATE if Path(STORAGE_STATE).exists() else None
    if not workspace:
        return None

    test_id = issue.get("test_name") or ""
    parts = [part for part in test_id.split(".") if part]
    module = parts[1].lower() if len(parts) > 2 else ""
    if not module:
        return None

    entry = _entry(workspace, issue)

    mode = entry.get("mode")
    # The default headroom is sized for adaptation's long exploration. A locate
    # replay is seconds, and sites like this one rotate short-lived bot-manager
    # cookies every couple of minutes — demanding five minutes of life from those
    # would re-mint a perfectly good session on every run.
    state = session_state.usable(workspace, module, min_remaining_s=SESSION_MIN_S)
    if state.get("ok"):
        log(f"  session: {Path(state['path']).name} — {entry_path.describe(entry)}")
        return str(state["path"])

    if mode == "credential":
        # The test signs in with credentials, so there is nothing stale to
        # apologise for — mint the session by making the very call it makes.
        if not MINT_SESSION:
            log(f"  no usable session and LOCATE_MINT=false — replaying "
                f"unauthenticated ({entry_path.describe(entry)})")
            return None
        log(f"  no usable session — minting one: {entry_path.describe(entry)}")
        result = mint_session.mint(workspace, module, entry,
                                   headless=_headless(workspace), log=log)
        if result.get("ok") and result.get("path"):
            log(f"  session minted: {Path(result['path']).name}")
            return str(result["path"])
        log(f"  could not mint a session ({result.get('reason', 'unknown')}) — "
            f"replaying unauthenticated")
        return None

    # A stored-session test with no usable session: say which cookies died,
    # rather than pointing at a config key that would not help.
    log(f"  {state.get('reason', 'no usable session')}")
    return None


def locate_one(issue: dict, sources: dict, assertion_used: set, cfg: dict,
               workspace: Path, browser) -> dict:
    """One locator: identify it, rank offline, then prove it live."""
    failed = issue.get("failed_selector") or ""
    record = {
        "test_name": issue.get("test_name", ""),
        "failed_selector": failed,
        "verdict": "SKIPPED",
        "reason": "",
        "locator_id": "",
    }
    if not failed:
        record["reason"] = "no failing selector recorded — not a locator failure"
        return record

    owner = _owner_hint(issue)
    class_name, field, raw = _declaring_field(sources, failed, prefer=owner)
    if not field:
        # The selector is not in the source. Usually that means the page object
        # was edited after the run that failed, so fall back to the element name,
        # which survives a locator change.
        class_name, field, raw = _field_by_element_name(
            sources, (issue.get("error_message") or "") + (issue.get("root_cause") or ""),
            prefer=owner)
        if field:
            log(f"  {failed!r} is no longer in the source — matched by element name "
                f"to {class_name}#{field} ({raw!r})")
    if not field:
        record["reason"] = (f"no page object declares {failed!r}, and no element "
                            f"name in the failure matched a locator field either")
        record["verdict"] = "NO_DECLARATION"
        return record
    record["locator_id"] = f"{class_name}#{field}"

    captured_at = (issue.get("failure_context") or {}).get("captured_at", "") \
        if isinstance(issue.get("failure_context"), dict) else ""
    stored = baseline_store.load(class_name, workspace, not_after=captured_at)
    if not stored.get("available"):
        record["verdict"] = "NO_BASELINE"
        record["reason"] = stored.get("rejected") or (
            f"no recorded good run for {class_name} — nothing to compare against")
        return record

    baseline = engine.baseline_for(class_name, field, raw, stored,
                                   action=_action_for(issue))
    if baseline is None:
        record["verdict"] = "NO_BASELINE"
        record["reason"] = (f"{class_name} has a baseline but no fingerprint for "
                            f"{field} — it did not resolve on the last good run")
        return record

    # Prefer the repo's own page comparison over the engine's landmark fallback:
    # baseline.diff weighs url shape, title, body class and locator coverage
    # together and demands corroboration before calling a page "different".
    comparison = None
    snapshot_path = issue.get("dom_snapshot") or issue.get("dom_snapshot_path") or ""
    if snapshot_path and Path(snapshot_path).exists():
        try:
            facts = page_identity.page_facts(
                Path(snapshot_path).read_text(errors="ignore"))
            comparison = baseline_store.diff(stored, facts)
        except Exception:                          # noqa: BLE001 - advisory only
            comparison = None

    url = issue.get("failure_url") or stored.get("url_shape") or ""
    if not url or browser is None:
        record["verdict"] = "UNVERIFIED"
        record["reason"] = ("no reachable page to verify against; "
                            "offline ranking only")
        return record

    # Two ways to reach an authenticated page, best first. Signing in is true by
    # construction — it is the test's own entry path — so it is tried ahead of a
    # restored session, which some sites decline in a fresh browser however valid
    # the cookies look locally.
    #
    # Unless the failing page IS the sign-in page. Authenticating first is then
    # self-defeating: the site redirects a signed-in visitor away from /login, so
    # the replay examines the post-login home page, finds none of the login
    # page's locators, and reports WRONG_STATE about a page it never opened. That
    # is precisely what happened to this login button.
    if _is_sign_in_page(class_name, url):
        log(f"  {class_name} is the sign-in page — examining it signed out, "
            f"because signing in first would redirect away from it")
        entry, replay, storage_state = {}, None, None
    else:
        entry = _entry(workspace, issue)
        replay = _login_replay(workspace, issue, entry, url)
        storage_state = None if replay else _storage_state(workspace, issue)
        if replay is None and storage_state is None:
            log("  no way to authenticate this replay — the page cannot be verified; "
                "mint a session with scripts/mint_session.py --module <module> --headed")
    page = browser.new_page(viewport=locator_capture.VIEWPORT,
                            storage_state=storage_state)
    try:
        result = engine.heal(page, baseline, cfg, url, browser=browser,
                             storage_state=storage_state, replay=replay,
                             assertion_fields=assertion_used,
                             page_comparison=comparison)
    finally:
        page.close()

    record.update({
        "verdict": result.verdict,
        "classification": result.classification,
        "reason": result.reason,
        "score": round(result.score, 3),
        "margin": round(result.margin, 3),
        "tier": result.tier,
        "verification": result.verification,
        "elapsed_ms": result.elapsed_ms,
        "page_object": class_name,
        "field": field,
        "rejected": result.top_rejected,
        "attempts": result.attempts,
    })
    if result.emitted:
        record["new_locator"] = result.emitted.get("sel")
        record["new_expression"] = (result.emitted.get("java")
                                    or f'page.locator("{result.emitted["sel"]}")')
        record["strategy"] = result.emitted.get("strategy")
        record["fragile"] = result.emitted.get("fragile")
    return record


def _action_for(issue: dict) -> str:
    """What the failing step was doing, from the runtime error."""
    text = (issue.get("error_message") or "") + (issue.get("root_cause") or "")
    lowered = text.lower()
    if "enter data" in lowered or "fill" in lowered or "type" in lowered:
        return "fill"
    if "select" in lowered:
        return "select"
    return "click"


def main() -> int:
    started = time.time()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(CONFIG_FILE.read_text())

    handoff = json.loads(HANDOFF_FILE.read_text())
    issues = handoff.get("automation_issues") or []
    log(f"{len(issues)} issue(s) from the handoff; mode={HEALING_LOCATE_MODE}")

    workspace = _workspace()
    if workspace is None:
        log("WORKSPACE_DIR/GITHUB_REPO_AUTOMATION not set — cannot read page objects")
        _write({"mode": HEALING_LOCATE_MODE, "resolutions": [], "reason": "no workspace"},
               started)
        return 0

    sources = _page_object_sources(workspace)
    fields = {declared["name"]
              for source in sources.values()
              for declared in page_identity.extract_locators(source)
              if declared.get("name")}
    assertion_used = locator_assertions.assertion_fields(sources, fields)
    if assertion_used:
        log(f"{len(assertion_used)} locator(s) are read by assertions and will not be healed")

    browser = None
    playwright = None
    try:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        headless = _headless(workspace)
        browser = playwright.chromium.launch(headless=headless)
        log(f"browser: {browser_mode.label(headless)} ({_headless_source()})")
    except Exception as exc:                       # noqa: BLE001 - optional capability
        log(f"no browser available ({type(exc).__name__}) — offline ranking only")

    resolutions = []
    try:
        for issue in issues:
            resolution = locate_one(issue, sources, assertion_used, cfg,
                                    workspace, browser)
            resolutions.append(resolution)
            _log_resolution(resolution)
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    located = [r for r in resolutions if r["verdict"] == "HEALED"]
    log(f"located {len(located)} of {len(resolutions)} deterministically "
        f"({'applied by fix' if HEALING_LOCATE_MODE == 'enforce' else 'shadow — fix unchanged'})")

    _write({
        "mode": HEALING_LOCATE_MODE,
        "attempted": len(resolutions),
        "located": len(located),
        "refused": len([r for r in resolutions
                        if r["verdict"] not in ("HEALED", "SKIPPED")]),
        "verdicts": _counts(resolutions),
        "resolutions": resolutions,
    }, started)
    return 0


def _log_resolution(r: dict) -> None:
    """One console line per decision. The Live Run panel reads stdout, and a
    reviewer looking for *why* looks there rather than at a badge."""
    name = r.get("locator_id") or r.get("failed_selector", "")[:40]
    if r["verdict"] == "HEALED":
        log(f"{name}  {r.get('classification')}  score {r.get('score')} "
            f"margin {r.get('margin'):+} tier {r.get('tier')} {r.get('verification')}")
        log(f"  -> {r.get('new_expression', '')[:110]}")
        for rejected in (r.get("rejected") or [])[:2]:
            log(f"  rejected: <{rejected['tag']}> {str(rejected['name'])[:28]!r} "
                f"score {rejected['score']}")
        if r.get("fragile"):
            log(f"  fragile: {r['fragile'][:100]}")
    else:
        # Refusals carry the reason a human acts on — "set HEALING_LOCATE_STORAGE_STATE",
        # "the feature was removed". Truncating at 90 characters cut off exactly
        # the actionable half. The console scrolls; the advice should survive.
        log(f"{name}  {r.get('classification') or r['verdict']} — {r.get('reason','')[:220]}")


def _counts(resolutions: list) -> dict:
    counts: dict = {}
    for r in resolutions:
        key = r.get("classification") or r["verdict"]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _write(payload: dict, started: float) -> None:
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload["duration_s"] = round(time.time() - started, 1)
    (AUDIT_DIR / "01-locate.json").write_text(json.dumps(payload, indent=2))
    (AUDIT_DIR / "01-locate.md").write_text(_markdown(payload))
    # No record_stage() here: run_step in shared/session.sh already records one
    # per step, with the right index. Recording our own would double-count the
    # stage and inflate the time/cost table the UI renders.


def _markdown(payload: dict) -> str:
    lines = [f"# Locate ({payload.get('mode')})", "",
             f"- attempted: {payload.get('attempted', 0)}",
             f"- located deterministically: {payload.get('located', 0)}",
             f"- refused: {payload.get('refused', 0)}", ""]
    for r in payload.get("resolutions", []):
        lines.append(f"## {r.get('locator_id') or r.get('failed_selector', '?')}")
        lines.append(f"- verdict: **{r.get('classification') or r['verdict']}** — {r.get('reason','')}")
        if r.get("new_expression"):
            lines += [f"- was: `{r.get('failed_selector')}`",
                      f"- now: `{r['new_expression']}`",
                      f"- score {r.get('score')} (margin {r.get('margin'):+}), "
                      f"{r.get('tier')}, verification {r.get('verification')}"]
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    # This step is an optimisation: it saves a model call when it works and costs
    # nothing when it does not. run.sh runs under `set -e` with an ERR trap, so a
    # traceback escaping here would abort the whole run before Fix ever ran —
    # turning a missing baseline into a failed heal. Same principle as
    # Baseline.java: never allowed to fail the thing it is helping.
    try:
        sys.exit(main())
    except Exception as exc:                       # noqa: BLE001 - deliberate catch-all
        import traceback
        log(f"locate failed ({type(exc).__name__}: {exc}) — continuing to Fix")
        traceback.print_exc()
        try:
            _write({"mode": HEALING_LOCATE_MODE, "resolutions": [],
                    "error": f"{type(exc).__name__}: {exc}"}, time.time())
        except Exception:                          # noqa: BLE001
            pass
        sys.exit(0)
