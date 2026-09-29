#!/usr/bin/env python3
"""
Step 02 — Validate Web
Uses Claude + Playwright MCP to directly control a browser and validate web flows.
Claude navigates, clicks, and fills forms using browser tools; outputs structured
STEP_PASSED / STEP_FAILED / SELECTOR_FOUND markers that are parsed into a selector map.

Skipped automatically by run.sh when test_type=api.

Reads:  $AUDIT_DIR/01-parse.json
Writes: $AUDIT_DIR/02-validate-web.json   (selector map + step results)
        $AUDIT_DIR/02-validate-web.md     (human-readable summary)
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

sys.path.insert(0, str(REPO_ROOT))   # repo root → shared.*
from shared import browser_mode      # noqa: E402  (after sys.path update)
from shared.credential_extraction import (credentials_from_plan, input_text,  # noqa: E402
                                          mentions_login)
from shared.test_case import given_values          # noqa: E402

CLAUDE_CLI  = os.environ.get("CLAUDE_CLI_PATH", "claude")
# Set in config/.env, no default here: run.sh stops the run when it is missing.
MODEL       = os.environ.get("AUTHORING_MODEL", "")
# This step drives a browser, so it takes the effort every agent's browser step
# shares; empty, it falls back to AUTHORING_EFFORT.
EFFORT      = os.environ.get("BROWSER_EFFORT") or os.environ.get("AUTHORING_EFFORT") or None
# Per-action wait budget handed to Claude for individual browser interactions.
PW_TIMEOUT  = int(os.environ.get("AUTHORING_BROWSER_TIMEOUT_MS", "30000"))
PW_HEADLESS = browser_mode.headless()
# Wall-clock budget for the whole validation run. A login-gated flow of 10+ steps
# on a heavy site routinely needs 15-25 minutes, so the default is generous;
# lower it for simple flows or CI.
VALIDATE_TIMEOUT = int(os.environ.get("VALIDATE_WEB_TIMEOUT_S", "1800"))
# Bounded self-heal: if a pass leaves failed steps worth retrying, re-run the
# WHOLE flow this many additional times (each call gets a fresh isolated
# browser — there is no mid-flow resume), feeding back what failed and why.
# 0 disables retries.
VALIDATE_RETRY_ATTEMPTS = int(os.environ.get("VALIDATE_WEB_RETRY_ATTEMPTS", "1"))

# ── Shared helpers ─────────────────────────────────────────────────────────────
from shared.claude import call_claude_ex            # noqa: E402  (after sys.path update)
from shared.mcp_config import write_mcp_config, allowed_tools as mcp_allowed_tools, CAPTURE_RULES  # noqa: E402
from shared.log import log as _log      # noqa: E402  (shared, redacts known secrets)
from shared.page_identity import (is_alternatives, is_dom_selector,  # noqa: E402
                                  qualified_locator_names)
from shared import check_provenance, flow_map, proven_locators, value_match  # noqa: E402


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    _log("02-validate-web", msg)


def _fmt_budget(seconds: int) -> str:
    """Human-readable wall-clock budget, e.g. '30 minutes' / '45 seconds'."""
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"


# ── Output parsers ─────────────────────────────────────────────────────────────

# The match count Claude is required to report, taken from the END of the value so
# that a selector legitimately containing "|" (e.g. [data-x="a|b"]) is unaffected.
# `visible=` follows `count=` when present, so both suffixes are stripped from the
# end in turn and a selector containing a literal "|" is still safe.
_COUNT_SUFFIX = re.compile(r"\|\s*count\s*=\s*(\d+)\s*$", re.I)
_VISIBLE_SUFFIX = re.compile(r"\|\s*visible\s*=\s*(\d+)\s*$", re.I)


def parse_selector_output(output: str) -> tuple:
    """Parse SELECTOR_FOUND: lines. Returns (selectors, counts, visibles, rejected).

    Enforces three properties a "confirmed" selector must have, because the prompt
    asks for all three and a model that skips one leaves no trace:

    1. It must be a real DOM selector. Claude drives the page through Playwright
       MCP, whose snapshots label every node with an ephemeral ref (`e71`,
       `generic[ref=f2e585]`); reporting the handle it just clicked instead of a
       selector is an easy mistake, and the resulting page object polls a locator
       that can never match until a 30-second timeout in step 04.

    2. It must match EXACTLY ONE element. A selector matching several compiles
       fine and then dies at runtime with Playwright's "strict mode violation:
       resolved to N elements" — the failure that cost a whole fix budget.

    3. It must match a VISIBLE element. `document.querySelectorAll` counts nodes
       the user cannot see, so a uniqueness check alone happily confirms a
       display:none template, a collapsed panel or a toast container that is
       always in the DOM and empty. Generating an assertion against one of those
       produces a test that fails for a reason nobody can see on the page.

    All three are hard drops. An unreported count used to be kept-but-flagged, on
    the theory that a probably-fine selector beats none. It does not: step 03 cannot
    tell a verified selector from an unverified one at codegen time, so the only
    thing the flag bought was a line in the log explaining, after the fact, why
    the generated test died of a strict mode violation. "Confirmed" now means
    measured, and a selector this step could not measure is not confirmed.

    A MISSING `visible=` is the one exception, and is kept rather than dropped: a
    cached run recorded before the visibility protocol existed would otherwise
    empty the selector map and abort codegen entirely. It is recorded as an
    unmeasured visibility instead, so step 03 can tell "measured and visible"
    from "never checked".

    counts maps name -> the reported match count, which is 1 for every entry that
    survives. It is retained so downstream code and the audit record can still see
    that the measurement happened rather than having to assume it.

    visibles maps name -> the visible match count, or None when the run never
    reported one. Parallel to counts, and the pair keeps "measured and visible"
    distinguishable from "never checked".

    rejected maps name -> why it was dropped, and holds only dropped names. Kept
    rather than only logged so the audit trail answers "why is there no locator
    for the toast?" — a question the console line scrolls away from long before
    anyone reads the PR.
    """
    selectors, counts, visibles, rejected = {}, {}, {}, {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("SELECTOR_FOUND:"):
            continue
        rest = line[len("SELECTOR_FOUND:"):].strip()
        if "=" not in rest:
            continue
        name, selector = rest.split("=", 1)
        name, selector = name.strip(), selector.strip()

        # `visible=` is emitted last, so it comes off first.
        visible = None
        match = _VISIBLE_SUFFIX.search(selector)
        if match:
            visible = int(match.group(1))
            selector = selector[:match.start()].strip()

        count = None
        match = _COUNT_SUFFIX.search(selector)
        if match:
            count = int(match.group(1))
            selector = selector[:match.start()].strip()

        def drop(reason: str) -> None:
            log(f"WARNING: dropped {name} — {reason}")
            rejected[name] = reason

        if not is_dom_selector(selector):
            drop(f"{selector!r} is not a usable DOM selector "
                 f"(Playwright-MCP ref or pseudo-attribute)")
            continue
        if is_alternatives(selector):
            # Counts 1 while only one alternative matches, and names no element: a run
            # reported `button:has-text("Buy Now"), a:has-text("Buy Now")` for a link.
            # A control the browser saw clicked is recovered from that click instead.
            drop(f"{selector!r} is a list of alternatives, which is a guess: count each "
                 f"alternative and report the one that matched.")
            continue
        if count is None:
            drop(f"{selector!r} was reported without a |count=, so its uniqueness "
                 f"was never measured. Re-report it with the count from the batch "
                 f"check (rule 2c).")
            continue
        if count != 1:
            drop(f"{selector!r} matched {count} element(s), not 1. Generating from "
                 f"it would fail at runtime with a strict mode violation; narrow "
                 f"the selector and re-report it.")
            continue
        if visible is not None and visible != 1:
            drop(f"{selector!r} matched {count} element(s) but {visible} of them "
                 f"were visible. A locator for an element nobody can see produces "
                 f"a test that fails for an invisible reason — if the element is "
                 f"genuinely absent, report the step, do not report a selector.")
            continue
        if visible is None:
            log(f"NOTE: {name} was reported without |visible=, so it was never "
                f"checked for visibility — keeping it, but step 03 cannot treat "
                f"it as confirmed-visible.")

        if selectors.get(name, selector) != selector:
            # One name is one element. Keeping the last one silently gave three
            # pages the payment-success amount, because the popup, the bank page
            # and the success screen had all been reported as `amountDisplay`.
            log(f"WARNING: {name} was reported again as {selector!r} — keeping the "
                f"first, {selectors[name]!r}. A different element needs its own name.")
            continue
        selectors[name] = selector
        counts[name] = count
        visibles[name] = visible
    return selectors, counts, visibles, rejected


def parse_step_results(output: str) -> tuple:
    """Parse STEP_PASSED / STEP_FAILED / STEP_UNVERIFIED lines. Returns three lists.

    The third state is the point. With only pass and fail, "I performed the action
    but could not observe the outcome it claims" has nowhere to go, and it lands on
    pass — which is how a run reported `STEP_PASSED: Verify a success confirmation
    toast appears` for a toast that never rendered, on the strength of the save API
    returning 200. Unverified is neither: the flow is not broken, but nothing was
    proved, and only step 03 can decide what to do about that.
    """
    passed, failed, unverified = [], [], []
    for line in output.splitlines():
        line = line.strip()
        for marker, bucket in (("STEP_PASSED:", passed), ("STEP_FAILED:", failed),
                               ("STEP_UNVERIFIED:", unverified)):
            if not line.startswith(marker):
                continue
            step = line[len(marker):].strip()
            # Interleaved flows label each step. An [API] step's proof is a response,
            # never an element, so it must not reach the element-evidence check or
            # step 03's drop decision — only its failure is kept, because a UI step
            # that depended on it is otherwise unexplained. The label itself never
            # survives: its word would skew the check-provenance vocabulary.
            api = step.startswith("[API]")
            step = re.sub(r"^\[(?:API|WEB)\]\s*", "", step)
            if not api or bucket is failed:
                bucket.append(step)
            break
    return passed, failed, unverified


# `MECHANISM_FOUND: <action>|<kind>|<how to trigger>|<how to know it finished>`
MECHANISM_KINDS = ("click", "autosave", "enter_key", "form_submit", "blur")


def parse_mechanisms(output: str) -> dict:
    """How each action actually takes effect, when it is not a plain click.

    "Save the profile" names an outcome, not a control. Naukri's profile summary
    persists about a second after the last keystroke, so a run that cannot find a
    visible Save button has not hit a dead end — it has found an autosave, and the
    generated page object needs to blur and wait rather than click something that
    is not there. Without this the only honest options were a guessed locator or a
    failed step, and the pipeline took the guess.
    """
    mechanisms = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("MECHANISM_FOUND:"):
            continue
        parts = [p.strip() for p in line[len("MECHANISM_FOUND:"):].split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        name, kind = parts[0], parts[1].lower()
        if kind not in MECHANISM_KINDS:
            log(f"WARNING: ignored MECHANISM_FOUND for {name} — unknown kind "
                f"{kind!r}, expected one of {', '.join(MECHANISM_KINDS)}")
            continue
        mechanisms[name] = {
            "kind": kind,
            "trigger": parts[2] if len(parts) > 2 else "",
            "settles_when": parts[3] if len(parts) > 3 else "",
        }
    return mechanisms


def _readings(rows: list) -> dict:
    """{selector: [every count the browser helpers took of it]}, from the evidence."""
    seen: dict = {}
    for row in rows or []:
        for sel, c in (row.get("checks") or {}).items():
            if isinstance(c, dict) and "total" in c:
                seen.setdefault(sel, []).append(c)
        for e in row.get("known") or []:
            if e.get("selector") and "total" in e:
                seen.setdefault(e["selector"], []).append(e)
    return seen


def proven_for_test_case(plan: dict) -> dict:
    """{name: selector} the last passing run of this test case proved, or {}.

    Read from run.sh's copy in the step cache, which it keeps for every module
    whatever CACHE_STEPS says. Only this test case's: another flow on the same
    site can use the same name, `submitButton`, for a different element.
    """
    module = os.environ.get("MODULE", "")
    path = AGENT_DIR / "cache" / os.environ.get("USER_ID", "cli") / module / proven_locators.FILE
    try:
        data = json.loads(path.read_text()) if module else {}
    except (OSError, ValueError):
        return {}
    if (urlparse(data.get("web_base_url") or "").netloc
            != urlparse(plan.get("web_base_url") or "").netloc
            or data.get("input_file") != Path(plan.get("_input_file") or "").name):
        return {}
    return proven_locators.selectors(path)


def prefer_proven(found: dict, counts: dict, visibles: dict, rows: list,
                  proven: dict, wanted: list) -> list:
    """Keep, for each name, the selector the last passing run of this test case
    used, when this run counted it at one visible element. Returns what changed.

    The prompt asks for proven locators first, and it was not enough: a run was
    seeded with a form's five proven field locators, counted every one at 1/1,
    and reported other selectors for the same fields. Both kinds work on the page,
    but only the proven one has been through a passing test, and a map that
    changes between runs hands step 03 and step 04 new code to get right each
    time. A name the model did not report at all is filled the same way.
    """
    live = {sel for sel, taken in _readings(rows).items()
            if any(c.get("total") == 1 and c.get("visible") == 1 for c in taken)}
    changed = []
    for name, selector in proven.items():
        if name not in wanted or found.get(name) == selector or selector not in live:
            continue
        changed.append({"name": name, "reported": found.get(name), "proven": selector})
        found[name], counts[name], visibles[name] = selector, 1, 1
        log(f"  PROVEN {name} = {selector} — a passing test used it and it counted 1/1 "
            + (f"here; kept over the reported {changed[-1]['reported']}"
               if changed[-1]["reported"] else "here, but it was never reported"))
    return changed


def verify_with_evidence(found: dict, counts: dict, visibles: dict,
                         rejected: dict, rows: list) -> tuple:
    """Hold every SELECTOR_FOUND to what the browser helpers measured.

    The count and visible numbers on a SELECTOR_FOUND line are the model's report.
    The helpers write what they actually counted to an evidence file, so where
    they counted the same selector, that decides: exactly 1/1 in any state it was
    seen in confirms it; present but never uniquely visible drops it. A selector
    the helpers never counted keeps the model's numbers, and is counted in the
    returned stats so how much is still claimed stays visible.

    Returns (found, counts, visibles, rejected, {"live": n, "claimed": n, "dropped": n}).
    """
    seen = _readings(rows)
    stats = {"live": 0, "claimed": 0, "dropped": 0}
    for name, selector in list(found.items()):
        measured = seen.get(selector) or []
        if any(c.get("total") == 1 and c.get("visible") == 1 for c in measured):
            counts[name], visibles[name] = 1, 1
            stats["live"] += 1
            continue
        present = [c for c in measured if (c.get("total") or 0) >= 1]
        if present:
            c = present[-1]
            rejected[name] = (f"{selector} — the browser helpers measured "
                              f"{c.get('total')} match(es), {c.get('visible')} visible, "
                              f"where the marker reported 1/1")
            for d in (found, counts, visibles):
                d.pop(name, None)
            stats["dropped"] += 1
            continue
        stats["claimed"] += 1
    return found, counts, visibles, rejected, stats


# Words that name a control's kind, not which control it is.
_CONTROL_WORDS = {"button", "btn", "field", "input", "icon", "link", "text", "label",
                  "option", "tab", "the", "and"}


def _naming_words(text: str) -> set:
    """camelCase or prose split into lowercase words. Plainer than
    check_provenance.subject_words, which drops "checkout" as a form of "check"."""
    return {w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+", text or "")
            if len(w) >= 3} - _CONTROL_WORDS


def recover_clicked_locators(found: dict, counts: dict, visibles: dict, rows: list,
                             wanted: list) -> list:
    """Confirm a plan locator the model clicked but never reported, from the click
    the browser recorded. Returns the names recovered.

    A run clicked a page's main button with a selector copied from its prompt's
    example, reported nothing for it, and step 03 guessed `button:has-text('Buy
    Now')` for a link: the test failed on its first locator. The helpers record
    every click with the clicked control's selector, counted where it lives. A
    name is recovered only from one click, measured 1/1, whose text shares a word
    with the name, and never onto an element another name already has.
    """
    clicks = [r["clicked"] for r in rows or [] if isinstance(r.get("clicked"), dict)]
    clicks = [c for c in clicks if c.get("sel") and c.get("total") == 1 and c.get("visible") == 1]
    recovered = []
    for name in wanted:
        if name in found:
            continue
        # The click sharing the most words wins: `buyNowButton` shares one with
        # "Pay now" and two with "BUY NOW". A tie names nothing.
        words, shared = _naming_words(name.rsplit(".", 1)[-1]), {}
        for c in clicks:
            n = len(words & _naming_words(c.get("text") or ""))
            if n:
                shared[c["sel"]] = max(shared.get(c["sel"], 0), n)
        top = [sel for sel, n in shared.items() if shared and n == max(shared.values())]
        if len(top) != 1 or top[0] in found.values():
            continue
        found[name], counts[name], visibles[name] = top[0], 1, 1
        recovered.append(name)
        log(f"  RECOVERED {name} = {found[name]} — clicked in the browser, counted 1/1, "
            f"but never reported")
    return recovered


_TAKES_TYPING = ("input", "textarea", "select")


def _was_typed(value: str, selector, typed: list) -> bool:
    """Whether `value` is among what the browser recorded typed. A field may
    format what it is given (`4111111111111111` shown as `4111 1111 1111 1111`),
    so any relation or the same letters and digits count. A password's value is
    never recorded: it counts when it went into this field's own selector."""
    letters = lambda s: re.sub(r"\W", "", str(s)).casefold()
    for t in typed:
        if t.get("password"):
            if selector and t.get("sel") == selector:
                return True
            continue
        shown = t.get("value")
        if shown is not None and (value_match.relation(value, shown)
                                  or letters(value) == letters(shown) != ""):
            return True
    return False


def enforce_typed_fields(inputs: dict, found: dict, counts: dict, visibles: dict,
                         rejected: dict, rows: list) -> dict:
    """Drop an INPUT_USED, and its name's selector, when the browser helpers
    measured that element as one nothing can be typed into.

    Unique and visible says an element exists, not that it is the one the plan
    means. A run matched the plan's `amountField` to an earlier run's
    `amountText = td.amount`, the cart's read-only total: it counted 1/1, was
    kept, and the model reported `INPUT_USED: amountField|20,000` for a value it
    had only read. Step 03 generated fillText() on a <td>, and two fix attempts
    went into a field step 02 had never typed into.

    The helpers also record every value typed into any field, as it is typed. When
    they recorded some, an INPUT_USED value none of them matches was never typed,
    and is dropped alone: the field may be right, the claim is not.

    What the helpers never measured keeps the model's word, as selectors do.
    Returns the inputs kept.
    """
    seen: dict = {}
    typed = []
    for row in rows or []:
        for sel, c in (row.get("checks") or {}).items():
            if isinstance(c, dict) and ("editable" in c or c.get("tag")):
                seen.setdefault(sel, []).append(c)
        for e in row.get("known") or []:
            if e.get("selector") and "editable" in e:
                seen.setdefault(e["selector"], []).append(e)
        if isinstance(row.get("typed"), dict):
            typed.append(row["typed"])
    kept = {}
    for field, value in inputs.items():
        selector = found.get(field)
        readings = seen.get(selector) or []
        editable = [c["editable"] for c in readings if "editable" in c]
        if (not readings or any(editable)
                or (not editable and any(c.get("tag") in _TAKES_TYPING for c in readings))):
            if typed and not _was_typed(value, selector, typed):
                log(f"WARNING: dropped INPUT_USED {field}|{value!r} — the browser recorded "
                    f"every value typed in this run, and this was not one of them")
                continue
            kept[field] = value
            continue
        tag = next((c["tag"] for c in readings if c.get("tag")), "")
        rejected[field] = (f"{selector} — reported as typed into ({value!r}), but the browser "
                           f"helpers measured {'a <' + tag + '>' if tag else 'an element'} "
                           f"that takes no typing. It is not this field.")
        log(f"WARNING: dropped {field} and its INPUT_USED — {rejected[field]}")
        for d in (found, counts, visibles):
            d.pop(field, None)
    return kept


# How many known selectors seed step 02: in total, and per locator name. The prompt
# lists every one, re-read on each turn, and the browser helpers count every one on
# each new page state.
KNOWN_LIMIT = 60
KNOWN_PER_NAME = 2


# run.sh's log line for a restored step 02. The second is how sessions logged it
# before the setting was CACHE_STEPS; their copies are still in audit/.
_RESTORED_MARKERS = ("Step cache: restored 02-validate-web.json",
                     "TESTING_MODE: restored 02-validate-web.json")


def _restored_from_cache(session_dir: Path) -> bool:
    """Whether a session's step 02 is a step-cache copy. run.sh restores it with a
    plain `cp`, so old results carry a fresh file time and would rank as the newest
    run. The run they came from is read on its own."""
    try:
        text = (session_dir / "stdout.log").read_text(errors="ignore")
    except OSError:
        return False
    return any(marker in text for marker in _RESTORED_MARKERS)


def known_selectors(plan: dict, roots=None) -> tuple:
    """(sessions, [{name, selector, from}]) — what earlier step-02 runs on the same
    site confirmed, merged — or ([], []).

    Every run used to start from nothing, and then from one earlier run's map:
    whatever an older run had confirmed and the chosen one had not reported was lost,
    and step 03 guessed that locator. A site's selectors are facts about the site, so
    every earlier run on the host counts: this test case's runs first, then this
    module's, then the rest, newest first within each. One entry per selector, at most
    KNOWN_PER_NAME per name (the first in that order, then the most recently confirmed
    other one, in case the site changed since), KNOWN_LIMIT in all. They are
    candidates only; the prompt has each counted live before it is reported.
    """
    host = urlparse(plan.get("web_base_url") or "").netloc
    if not host:
        return [], []
    test_case = Path(plan.get("_input_file") or "").name
    module = plan.get("feature_name") or ""

    def tier(input_file: str, feature_name: str) -> int:
        return (0 if test_case and Path(input_file or "").name == test_case
                else 1 if module and feature_name == module else 2)

    def usable(selectors: dict) -> dict:
        # A list of alternatives names no element; it is never a seed.
        return {n: s for n, s in (selectors or {}).items() if s and not is_alternatives(s)}

    runs = []   # (tier, mtime, session, {name: selector}, proven)
    for root in roots or (AGENT_DIR / "audit", AGENT_DIR / "cache"):
        for path in Path(root).rglob("02-validate-web.json"):
            if path.parent == AUDIT_DIR or _restored_from_cache(path.parent):
                continue
            try:
                data = json.loads(path.read_text())
                earlier = json.loads((path.parent / "01-parse.json").read_text())
            except (OSError, ValueError):
                continue
            if urlparse(earlier.get("web_base_url") or "").netloc != host:
                continue
            selectors = usable(data.get("selectors"))
            # A selector step 04 replaced on the way to a passing test is not seeded:
            # the proven file beside it, when at least as new, has the working one.
            proven_path = path.parent / proven_locators.FILE
            if proven_path.is_file() and proven_path.stat().st_mtime >= path.stat().st_mtime:
                replaced = proven_locators.selectors(proven_path)
                selectors = {n: s for n, s in selectors.items() if replaced.get(n, s) == s}
            if selectors:
                # Each selector was measured 1/1 on its own, so an unfinished run counts.
                runs.append((tier(earlier.get("_input_file"), earlier.get("feature_name")),
                             path.stat().st_mtime, path.parent.name, selectors, False))
        # Carries its own site, test case and module: the cache copy has no plan beside it.
        for path in Path(root).rglob(proven_locators.FILE):
            if path.parent == AUDIT_DIR:
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            selectors = usable(proven_locators.selectors(path))
            if urlparse(data.get("web_base_url") or "").netloc != host or not selectors:
                continue
            runs.append((tier(data.get("input_file"), data.get("feature_name")),
                         path.stat().st_mtime, path.parent.name, selectors, True))
    runs.sort(key=lambda r: (r[0], -r[1]))

    # Per name: the first proven selector in that order (else the first of any), and
    # the most recently confirmed other one. By recency alone, so older runs of this
    # test case cannot push out the selector a newer run found after the site changed.
    first, first_proven, by_recency = {}, {}, {}
    for _tier, _mtime, _session, selectors, proven in runs:
        for name, sel in selectors.items():
            first.setdefault(name, sel)
            if proven:
                first_proven.setdefault(name, sel)
    first.update(first_proven)
    for _tier, _mtime, _session, selectors, _proven in sorted(runs, key=lambda r: -r[1]):
        for name, sel in selectors.items():
            if sel not in by_recency.setdefault(name, []):
                by_recency[name].append(sel)
    allowed = {name: {first[name]} | set([s for s in newest if s != first[name]]
                                         [:KNOWN_PER_NAME - 1])
               for name, newest in by_recency.items()}
    proven_selectors = {sel for *_rest, selectors, proven in runs if proven
                        for sel in selectors.values()}

    entries, seen, sessions = [], set(), []
    for _tier, _mtime, session, selectors, _proven in runs:
        for name, sel in selectors.items():
            if len(entries) >= KNOWN_LIMIT or sel in seen or sel not in allowed[name]:
                continue
            seen.add(sel)
            entries.append({"name": name, "selector": sel, "from": session,
                            "proven": sel in proven_selectors})
            if session not in sessions:
                sessions.append(session)
    return sessions, entries


def progress_notes(outcome: str, parsed: dict, web_steps: list) -> list:
    """Retry notes for an attempt that ran out before finishing, but not before
    confirming most of the flow.

    A fresh browser has to walk the flow from the start; it does not have to
    rediscover it. The retry used to be told the attempt "produced no usable
    output" — after one had measured 19 selectors and passed 21 of 25 steps — so
    it spent its whole budget finding the same elements again and would have run
    out in the same place. Every marker is still re-emitted, so the retry stands
    on its own when the better of the two attempts is picked.
    """
    passed = parsed.get("steps_passed") or []
    selectors = parsed.get("selectors") or {}
    if not selectors and not passed:
        return ["\nPRIOR ATTEMPT NOTES — a previous run of this exact flow did not "
                f"complete ({outcome}) and produced no usable output. Execute efficiently "
                "and emit markers as you go (rule 3b) so partial progress is captured even "
                "if this attempt also runs out of budget."]
    remaining = [s for s in web_steps if not any(p == s or p.startswith(s) for p in passed)]
    notes = [f"\nPRIOR ATTEMPT NOTES — a previous run of this exact flow ran out ({outcome}) "
             "after confirming the selectors and steps below. This is a fresh browser, so "
             "walk the flow from the start, but do NOT search for these elements again: use "
             "each selector as it is, confirm a page's ones with a single rule-2c batch "
             "count, and re-emit their SELECTOR_FOUND / INPUT_USED / VALUE_CHECK / "
             "STEP_PASSED markers as you pass them. Spend the budget on the steps not yet "
             "confirmed."]
    notes += [f"  {name}={selector}" for name, selector in selectors.items()]
    if remaining:
        notes += ["Not yet confirmed:"] + [f"  - {s}" for s in remaining]
    return notes


def parse_inputs_used(output: str) -> dict:
    """`INPUT_USED: <field>|<value typed>` — what this run actually typed, by field.

    Step 03 writes the test's data, and data of a different shape is a different
    test: step 02 filled `Test User`, step 03 generated a one-word name, and the
    demo checkout appended a default last name the name check then failed on —
    a value step 02 never tried. The first report of a field wins, as it does for
    selectors.
    """
    inputs = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("INPUT_USED:"):
            continue
        field, sep, value = line[len("INPUT_USED:"):].partition("|")
        field = field.strip()
        if sep and field and field not in inputs:
            inputs[field] = value.strip()
    return inputs


def parse_value_checks(output: str) -> list:
    """`VALUE_CHECK:` lines — both sides of each comparison step 02 made, with the
    relation between them computed in Python, never taken from the model."""
    checks = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("VALUE_CHECK:"):
            check = value_match.parse_value_check(line[len("VALUE_CHECK:"):])
            if check:
                checks.append(check)
    return checks


def drop_untraced_sources(value_checks: list, web_steps: list, inputs: dict) -> list:
    """Drop a VALUE_CHECK whose expected side is not what its source says it is.

    Step 03 asserts every VALUE_CHECK as a contract, with the expected side taken
    from its source. `literal` is a value the test case quotes: "Record the amount
    displayed on the order form" came back as `literal|20,000` and was generated
    as `assertEquals(amount, "20,000")`, a check nobody asked for. `input:<field>`
    is a value this run typed: one with no INPUT_USED (or whose INPUT_USED was
    dropped) points the contract at test data step 02 never entered.
    """
    steps_text = "\n".join(web_steps)
    kept = []
    for c in value_checks:
        kind, _, name = c["source"].partition(":")
        if kind == "literal" and not (value_match.appears_in(c["expected"], c["check"])
                                      or value_match.appears_in(c["expected"], steps_text)):
            log(f"  DROPPED VALUE_CHECK for {c['check']!r} — its literal {c['expected']!r} is "
                f"not in the test case, so it was read off the page, not expected by anyone")
            continue
        if kind == "input" and name not in inputs:
            log(f"  DROPPED VALUE_CHECK for {c['check']!r} — nothing was typed into {name}")
            continue
        kept.append(c)
    return kept


# Only a claim that two values are the SAME is refuted by their differing. "The
# amount decreased" holds precisely because its two sides differ, and "the order id
# is not null" has no second value at all — both were downgraded when every
# VALUE_CHECK without a relation was.
_EQUALITY = re.compile(r"\b(match(es|ed|ing)?|same|equals?|identical)\b", re.I)
_NOT_EQUALITY = re.compile(r"decreas|increas|\bless\b|greater|more than|fewer|lower|higher"
                           r"|not (null|empty|blank)|differ", re.I)


def enforce_value_checks(passed: list, unverified: list, value_checks: list) -> tuple:
    """Downgrade a comparison reported as passed whose two recorded sides match
    under no relation at all.

    The same rule as enforce_verification_evidence: the step says the values
    matched, the values it wrote down say they did not, and the values win. The
    downgraded step then takes the path every unverified check already takes.
    """
    unmatched = [c["check"] for c in value_checks if not c["relation"]
                 and _EQUALITY.search(c["check"]) and not _NOT_EQUALITY.search(c["check"])]
    if not unmatched:
        return passed, unverified
    kept, downgraded = [], list(unverified)
    for step in passed:
        if any(step == c or step.startswith(c) for c in unmatched):
            log(f"  DOWNGRADED to unverified: {step!r} — the two values recorded for "
                f"it do not match under any relation")
            downgraded.append(step)
        else:
            kept.append(step)
    return kept, downgraded


def _traced(check: dict, selectors: dict, inputs: dict) -> bool:
    """Whether a VALUE_CHECK's expected side is what its source says it is.

    Promotion rests on the expected text, so it must not be one the model wrote
    down freely: a typed value must be the one INPUT_USED recorded, an earlier
    element must have been measured, and a literal must be in the step itself.
    """
    kind, _, name = check["source"].partition(":")
    if kind == "input":
        return value_match.relation(inputs.get(name, ""), check["expected"]) in ("equal", "formatting")
    if kind == "element":
        return name in selectors
    return value_match.appears_in(check["expected"], check["check"])


def promote_matched_values(passed: list, unverified: list, value_checks: list,
                           selectors: dict, inputs: dict) -> tuple:
    """Promote a comparison reported as unverified whose two recorded sides match.

    The other half of enforce_value_checks: the values win in both directions. A
    run reported `08123456789` against a shown `+628123456789` as unverified
    ("the digit sequence differs"), and an unverified requested check makes step
    03 keep a strict assertion that fails on purpose. The relation is measured
    here, never taken from the model. The element must have a confirmed selector
    and the expected side must trace to its source (`_traced`), so what is
    promoted is a measured element showing a value that matches a known one.
    """
    matched = {c["check"]: c for c in value_checks
               if c["relation"] and c.get("element") in selectors
               and _EQUALITY.search(c["check"]) and not _NOT_EQUALITY.search(c["check"])
               and _traced(c, selectors, inputs)}
    if not matched:
        return passed, unverified
    promoted, still = list(passed), []
    for entry in unverified:
        step = entry.split("|", 1)[0].strip()
        check = next((c for text, c in matched.items()
                      if step == text or step.startswith(text)), None)
        if check and step not in promoted:
            log(f"  PROMOTED to passed: {step!r} — {check['element']} shows "
                f"{check['rendered']!r}, which {value_match.MEANING[check['relation']]} "
                f"{check['expected']!r} ({check['relation']})")
            promoted.append(step)
        elif not check:
            still.append(entry)
    return promoted, still


def drop_unverified_actions(unverified: list) -> list:
    """Keep only the STEP_UNVERIFIED entries that are checks.

    Unverified means a claim was never observed, and an action claims nothing: it
    ran, or it failed. A run clicked a payment-method tab, counted the tab after
    the click, found it gone, and wrote `STEP_UNVERIFIED: Select Credit Card as the
    payment method (locator report)`. Step 03 kept that as a check the product
    failed, and step 04 stopped on the `defect` gate when the tab's guessed
    locator did not load. A comparison is a claim even without a verifying verb.
    """
    kept = []
    for entry in unverified:
        step = entry.split("|", 1)[0].strip()
        if (check_provenance.shape(step) == check_provenance.VERIFICATION
                or _EQUALITY.search(step) or _NOT_EQUALITY.search(step)):
            kept.append(entry)
        else:
            log(f"  IGNORED STEP_UNVERIFIED for {step!r} — an action is not a check, "
                f"so it cannot be one the product failed")
    return kept


def enforce_verification_evidence(passed: list, unverified: list,
                                  selectors: dict) -> tuple:
    """Downgrade a verification step that passed without confirming an element.

    The rest of this module already holds selectors to "measured, not claimed" —
    a SELECTOR_FOUND without a count is dropped precisely because nothing
    downstream can tell a measured selector from an eyeballed one. Step outcomes
    had no such check, and were pure self-report.

    They need one for the same reason. A run reported `STEP_PASSED: Verify a
    success confirmation toast or message appears` having never seen a toast,
    reasoning from the save API's 200 response; the guessed locator that step 03
    then generated failed in step 04, and the fix deleted the assertion. The step
    that claims an element appeared is the step that must have produced that
    element's selector, so the two are checked against each other here.

    Only verification steps are subject to this. An action step ("Click Save")
    legitimately passes without confirming anything new, and an action whose
    control does not exist is a mechanism to discover, not a proof to downgrade.
    """
    if not passed:
        return passed, unverified

    # Not lower-cased: subject_words splits camelCase, and `profileSummaryText`
    # flattened to one word matches nothing.
    confirmed_subjects = [check_provenance.subject_words(n) for n in selectors]
    kept, downgraded = [], list(unverified)
    for step in passed:
        if check_provenance.shape(step) != check_provenance.VERIFICATION:
            kept.append(step)
            continue
        # The element the step is about, in the vocabulary a selector name uses:
        # "Verify a success confirmation toast appears" -> {success, confirmation,
        # toast}, which should meet `successToast` if one was confirmed.
        subject = check_provenance.subject_words(step)
        if not subject or any(subject & names for names in confirmed_subjects):
            kept.append(step)
            continue
        log(f"WARNING: downgrading to unverified — {step!r} was reported as passed, "
            f"but no selector was measured for the element it checks. Seeing it in "
            f"a screenshot, or inferring it from a network response, leaves step 03 "
            f"nothing to assert against.")
        downgraded.append(f"{step}|a selector for what it checks|none — "
                          f"{check_provenance.UNMEASURED}")
    return kept, downgraded


# Categories Claude tags onto every STEP_FAILED (see the FAILURE PROTOCOL rule
# in the prompt). This replaces guessing the cause from Claude's free-text error
# after the fact — the model states the category itself, at the moment it has
# the most context to know which one applies.
CATEGORY_FIX_HINTS = {
    "selector_not_found": "Element not found — check login state, target URL, or "
        "whether the site's DOM structure changed. Check the PAGE_DUMP/screenshot "
        "recorded for this failure.",
    "login_failed": "Login failed — verify Username/Password in the queue input "
        "file, or check the screenshot for a CAPTCHA/2FA prompt.",
    "timeout": "Action timed out — the element may exist but load slowly, be "
        "hidden, or be off-screen. Consider raising AUTHORING_BROWSER_TIMEOUT_MS.",
    "overlay_blocking": "A cookie-consent banner, modal, or popup blocked "
        "interaction and could not be dismissed automatically — check the "
        "screenshot for what's covering the target element.",
    "network_error": "An API/network request failed — see the console/network "
        "summary in the failure detail. Likely a backend or environment issue, "
        "not a selector problem.",
    "unexpected_content": "Page content differed from what the step expected "
        "(different layout, A/B test, maintenance page, …) — check the "
        "screenshot.",
    "skipped": "Fix the login failure above; these steps will then run.",
    "other": "See the raw error and screenshot for details.",
}

# Fallback for STEP_FAILED lines that don't carry a category= tag — the model
# didn't follow the newer protocol. Same heuristics used before categories
# existed, kept only as a safety net so an out-of-format failure still gets
# some guidance instead of none.
_LEGACY_FIX_HEURISTICS = [
    (("TEST_USERNAME", "TEST_PASSWORD", "EXPECTED_USERNAME"),
     "Set credentials in your queue input file under Username/Password fields"),
    (("could not find", "not found"),
     "Element not found — check login state or target URL"),
    (("login did not succeed",),
     "Login failed — verify Username/Password in the queue input file"),
    (("skipped",),
     "Fix the login failure above; these steps will then run"),
]


def parse_failure_category(error_detail: str) -> str | None:
    """Extract `category=<name>` from a STEP_FAILED detail string, if present."""
    m = re.search(r"category=(\w+)", error_detail, re.IGNORECASE)
    if not m:
        return None
    candidate = m.group(1).lower()
    return candidate if candidate in CATEGORY_FIX_HINTS else None


def fix_hint_for(error_detail: str) -> str | None:
    """FIX suggestion for a STEP_FAILED detail string — category-tagged first,
    falling back to the legacy text-guess heuristics if untagged."""
    category = parse_failure_category(error_detail)
    if category:
        return CATEGORY_FIX_HINTS[category]
    lowered = error_detail.lower()
    for needles, hint in _LEGACY_FIX_HEURISTICS:
        if any(n.lower() in lowered for n in needles):
            return hint
    return None


def parse_page_dumps(output: str) -> dict:
    """Parse PAGE_DUMP: label|json_array lines from Claude output.

    Logs (rather than silently swallowing) a malformed dump — the model
    pretty-printing the JSON despite the single-line instruction is a real,
    observed failure mode, and a silent drop gives no operator-visible trace
    that evidence was captured but lost.
    """
    dumps = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("PAGE_DUMP:"):
            continue
        rest = line[len("PAGE_DUMP:"):].strip()
        if "|" not in rest:
            log(f"WARNING: malformed PAGE_DUMP (no '|' separator) — dropped: {rest[:100]}")
            continue
        label, json_part = rest.split("|", 1)
        try:
            dumps[label.strip()] = json.loads(json_part.strip())
        except json.JSONDecodeError as e:
            log(f"WARNING: PAGE_DUMP for '{label.strip()}' is not valid single-line "
                f"JSON (likely emitted pretty-printed across multiple lines) — dropped: {e}")
    return dumps


def parse_interaction_hints(output: str) -> list:
    """Parse INTERACTION_HINT: <json object> lines from Claude output.

    JSON rather than a pipe-delimited format: a selector or visible-text field
    legitimately containing a literal '|' (e.g. text='Buy 1 | Get 1 Free') used
    to silently corrupt a fixed-position split() instead of failing loudly.
    """
    hints = []
    required = {"type", "name", "selector", "text"}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("INTERACTION_HINT:"):
            continue
        rest = line[len("INTERACTION_HINT:"):].strip()
        try:
            obj = json.loads(rest)
        except json.JSONDecodeError as e:
            log(f"WARNING: malformed INTERACTION_HINT (not valid single-line JSON) — dropped: {e}")
            continue
        if not (isinstance(obj, dict) and required <= obj.keys()):
            log(f"WARNING: INTERACTION_HINT JSON missing required keys {required} — dropped: {rest[:150]}")
            continue
        hint = {k: str(obj[k]).strip() for k in ("type", "name", "selector", "text")}
        # Optional here, but required by reconcile_hints() for any hint that is not
        # backed by a confirmed SELECTOR_FOUND. Parsed leniently so a malformed
        # count degrades to "unmeasured" rather than throwing the hint away here.
        raw_count = obj.get("count")
        hint["count"] = raw_count if isinstance(raw_count, int) else None
        # Same hygiene as SELECTOR_FOUND above — step 03 treats hints as equally
        # authoritative, so an MCP ref reaching the codegen prompt through this
        # path is just as unusable.
        if not is_dom_selector(hint["selector"]):
            log(f"WARNING: dropped hint {hint['name']} — {hint['selector']!r} is not a "
                f"usable DOM selector (Playwright-MCP ref or pseudo-attribute)")
            continue
        hints.append(hint)
    return hints


def reconcile_hints(hints: list, selectors: dict) -> list:
    """Hold INTERACTION_HINTs to the same uniqueness bar as SELECTOR_FOUNDs.

    Step 03 generates locators from hints and from selectors alike, so a hint is
    not a lesser artefact that can be trusted less — but only SELECTOR_FOUND ever
    had to prove itself. Two things went wrong in practice:

    1. A hint recorded an element the model INTERACTED with, including elements an
       interaction then failed on. An observed run hinted the profile-summary edit
       icon as `img[alt='PencilSimple']`, found that clicking it did nothing, moved
       up to the parent `span` and confirmed THAT — leaving a hint pointing at the
       element that does not work next to a selector pointing at the one that does.
       Where a name has a confirmed selector, that selector is authoritative and
       the hint's copy is replaced. The hint's real value is its type/label
       metadata, which is unaffected.

    2. A hint for a name with no confirmed selector was never counted at all, so
       "button" could reach codegen and resolve to twenty elements at runtime.
       Those now need their own measured count=1, exactly like a SELECTOR_FOUND.
    """
    kept = []
    for hint in hints:
        name, confirmed = hint.get("name"), selectors.get(hint.get("name"))
        if confirmed:
            if hint["selector"] != confirmed:
                log(f"NOTE: hint {name} pointed at {hint['selector']!r} but the "
                    f"confirmed selector is {confirmed!r} — using the confirmed one "
                    f"(the hint likely recorded an interaction that did not work).")
                hint = {**hint, "selector": confirmed}
        elif hint.get("count") != 1:
            log(f"WARNING: dropped hint {name} — {hint['selector']!r} has no "
                f"confirmed selector and no measured count=1, so its uniqueness is "
                f"unknown. Report it via SELECTOR_FOUND, or add \"count\": 1.")
            continue
        elif is_alternatives(hint["selector"]):
            log(f"WARNING: dropped hint {name} — {hint['selector']!r} is a list of "
                f"alternatives, which names no element.")
            continue
        kept.append({k: v for k, v in hint.items() if k != "count"})
    return kept


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    plan = json.loads((AUDIT_DIR / "01-parse.json").read_text())

    base_url   = plan.get("web_base_url", "")
    web_steps  = plan.get("web_steps_for_validation", [])
    web_pages  = plan.get("web_pages", [])
    if plan.get("flow_style") == "interleaved" and plan.get("interleaved_steps"):
        # In order and labelled: a UI step can depend on state an API step created,
        # so the browser performs both — the API ones through fetch() (fetch_guard).
        web_steps = [f"[{str(s.get('interface', 'web')).upper()}] {s.get('description', '')}"
                     for s in plan["interleaved_steps"]
                     if isinstance(s, dict) and s.get("description")]

    if not web_steps:
        log("No web steps found in plan — writing empty selector map")
        _write_empty(reason="no web steps in plan")
        return

    if not base_url:
        log("No web_base_url in plan — writing empty selector map")
        _write_empty(reason="no web_base_url in plan")
        return

    if not base_url.startswith(("http://", "https://")):
        log(f"ERROR: web_base_url is not a valid URL: '{base_url}' — check input file (add 'URL: https://...')")
        _write_empty(reason=f"invalid web_base_url: '{base_url}' — must start with http:// or https://")
        return

    # Credential check — a login step needs credentials, and they can reach us two
    # ways: structured in the plan (step 01), or written inline in the input file.
    # Both are read with the SAME extractor the masking layer's vocabulary matches,
    # so a shape that gets masked in the run header (`username=foo`) can never be
    # reported here as "no credentials found".
    plan_creds = {k: v for k, v in (plan.get("demo_credentials") or {}).items() if v}
    demo_creds = credentials_from_plan(plan)
    steps_need_login = mentions_login("\n".join(web_steps))
    if steps_need_login and demo_creds != plan_creds:
        # Recovered from the input file, and put in the prompt's CREDENTIALS block
        # rather than left for Claude to notice in the step text.
        log("Credentials were not in the plan but are present in the input file — "
            "using them for this validation run.")
    if steps_need_login and not (demo_creds.get("username") and demo_creds.get("password")):
        # One message, not four log() calls: the remedy is part of the error,
        # and severity colouring in shared/log.py paints the whole block.
        log("ERROR: Login step detected but no credentials found in input file.\n"
            "       Add credentials as top-level fields or inline in the step "
            "(':' and '=' both work):\n"
            "         Username: your_username\n"
            "         Password: your_password")
        _write_empty(reason="login step detected but no credentials in input file — add Username/Password fields")
        sys.exit(1)

    # Credentials only for a flow that logs in. Without one, an `Email:` is a form
    # field and an `OTP:` a bank page's: shown as CREDENTIALS, a checkout run was
    # told to expect a login and to "use exactly these".
    login_creds = steps_need_login or bool(demo_creds.get("password"))
    given = given_values(input_text(plan))
    if demo_creds and not login_creds:
        stated = {value for _label, value in given}
        given += [(label, demo_creds[field]) for field, label in
                  (("username", "Email / username"), ("otp", "OTP"))
                  if demo_creds.get(field) and demo_creds[field] not in stated]
    data_section = ("\nTEST DATA — the values the test case gives. Type each exactly as "
                    "written into the field it names, even where a step says \"dummy "
                    "data\"; make up values only for fields not listed:\n"
                    + "".join(f"  {label}: {value}\n" for label, value in given)
                    if given else "")

    creds_section = ""
    if demo_creds and login_creds:
        creds_section = f"""
CREDENTIALS (use exactly these — do NOT use any other values):
  username / email : {demo_creds.get('username', '')}
  password         : {demo_creds.get('password', '')}"""
        if demo_creds.get("otp"):
            creds_section += f"""
  OTP / 2FA code   : {demo_creds.get('otp')}
  IMPORTANT: After entering the password and clicking login, an OTP/2FA prompt may appear.
  If it does, enter the OTP code above and submit before continuing."""

    all_locators = qualified_locator_names(web_pages)

    # .mcp.json goes in the audit dir, not the repo root: the root is shared
    # mutable state and the server can be running another agent against it at
    # the same time, so two concurrent runs with different headless settings
    # clobbered each other's config and the loser's browser launched with the
    # winner's settings. The other two agents already did it this way — this was
    # the last one writing to the shared root.
    mode_label = browser_mode.label(PW_HEADLESS)
    log(f"Browser mode: {mode_label}")
    # What the browser helpers measure goes here, for verify_with_evidence; the
    # selectors known from earlier runs are counted on every page state.
    known_from, known = known_selectors(plan)
    proven_here = proven_for_test_case(plan)
    known_path = AUDIT_DIR / "02-known-selectors.json"
    known_path.write_text(json.dumps([{"owner": "known", "path": "", **entry}
                                      for entry in known], indent=2))
    evidence_path = AUDIT_DIR / "02-web-evidence.jsonl"
    evidence_path.unlink(missing_ok=True)
    mcp_path = write_mcp_config(AUDIT_DIR, headless=PW_HEADLESS,
                                evidence_file=evidence_path, known_locators_file=known_path)
    log(f"Playwright MCP config written: {mcp_path}")

    steps_numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(web_steps))
    locators_hint  = (
        f"\nLocators to discover and report (use these names in SELECTOR_FOUND): "
        f"{json.dumps(all_locators)}"
        + ("\nA name written Page.name is that page's own element: report it under that "
           "full name, and never report one element under another page's name."
           if any("." in n for n in all_locators) else "")
        if all_locators else ""
    )

    known_hint = ""
    if known:
        log(f"Reusing {len(known)} selector(s) confirmed on this site by {len(known_from)} "
            f"earlier run(s), this test case's own first ({', '.join(known_from[:3])}"
            f"{', …' if len(known_from) > 3 else ''}) as candidates — each is counted again "
            f"before it is reported")
        known_hint = (
            "\nKNOWN SELECTORS — earlier runs on this site confirmed these, this test case's "
            "own runs first, then the newest. A name may appear twice: the second is a "
            "newer alternative for the same element. One marked \"(a passing test used "
            "it)\" matched exactly one visible element while a generated test passed: "
            "try it first. "
            "The site may have changed since, so they are candidates, not results: on each "
            "page, count the ones you need in ONE page.qa.check — or as `check` in the "
            "page.qa.step call that lands on their page, with harvest: false — before "
            "harvesting anything. Each that measures total 1 and visible 1 is confirmed "
            "as that element, not as any role this plan has: report it only under the "
            "plan's name for the SAME element (these are the earlier runs' names). A "
            "text an earlier run read is never a field this plan types into — a field "
            "is an input, textarea or select, and `check` reports `editable`. Harvest "
            "only for elements still missing, and never report one you did not just "
            "count.\n"
            # Without the earlier runs' page prefix. Shown `OldPage.amountDisplay`, a run
            # reported `OldPage.amountText`, on a page this plan does not have, and the
            # plan's own `NewPage.amountText` went unconfirmed.
            + "".join(f"  {entry['name'].rsplit('.', 1)[-1]} = {entry['selector']}"
                      + ("   (a passing test used it)" if entry.get("proven") else "") + "\n"
                      for entry in known))

    fetch_guard = (
        "\n[API] STEPS: never browser_navigate to an API endpoint — loading it replaces the "
        "application page every later step needs. Perform the request with browser_evaluate "
        "and fetch() from the current page, so it shares the session, then carry on. Report an "
        "[API] step as STEP_PASSED or STEP_FAILED from the response status; it needs no "
        "SELECTOR_FOUND."
        if any(s.startswith("[API] ") for s in web_steps) else "")

    def build_prompt(attempt_notes: str = "") -> str:
        return f"""You are a QA automation agent. Use the Playwright browser MCP tools to validate a web user flow.

TARGET URL: {base_url}

STEPS TO EXECUTE:
{steps_numbered}
{data_section}
{creds_section}
{locators_hint}
{known_hint}
{fetch_guard}
{attempt_notes}

══════════════════════════════════════════════════════════════
OUTPUT PROTOCOL — emit these markers on their own lines:
══════════════════════════════════════════════════════════════
• After each step succeeds:
    STEP_PASSED: <step description>

  ⚠ WHAT "SUCCEEDS" MEANS — this is the difference between a real test and a
  test-shaped log file. It depends on what the step CLAIMS:

  · A step claiming a UI ELEMENT APPEARED ("verify the success toast appears")
    succeeds only when you have SEEN THAT ELEMENT IN THE DOM and can report a
    SELECTOR_FOUND for it. A 200 response, a closed form, a redirect, a spinner
    stopping — none of these are proof that an element rendered. They are proof
    that something happened on the server. If you cannot point at the element,
    the step did NOT pass; emit STEP_UNVERIFIED below.
    This is checked mechanically after the run: a verification step reported as
    passed with no matching SELECTOR_FOUND is downgraded to unverified anyway,
    so claiming the pass gains nothing and loses the detail of what you saw.
    Seen inside an iframe, or on a screen that closes by itself? Rule 2f says
    how to measure it there.

  · A step claiming a STATE CHANGE or that DATA PERSISTED ("the profile summary
    is updated") succeeds when you RE-READ THE STATE and see the new value —
    reload the page, read it back. A network call may tell you HOW the change
    happened; it is never the proof THAT it did.

  · A step performing an ACTION ("click Save", "log in") succeeds when the action
    took effect. If the named control does not exist, see rule 2e — that is a
    mechanism to discover, not a failure.

• After a step whose action completed but whose claimed outcome you could NOT
  observe:
    STEP_UNVERIFIED: <step description>|<what you looked for>|<what you saw instead> [url=<current page URL>]
  (e.g. STEP_UNVERIFIED: Verify a success confirmation toast appears|searched for
   any [class*='toast'], [role='alert'] or [role='status'] element for 5s after
   save|no such element ever entered the DOM; the edit form closed and POST
   /update/fullprofiles returned 200 [url=https://www.naukri.com/mnjuser/profile])

  Only a step that CLAIMS something (verify, validate, matches, decreases) can be
  unverified, under that step's own text — never a variant of it. An action that
  worked is STEP_PASSED even when you could not measure its control; that control
  is simply not reported. An unverified action is ignored.

  This is NOT a failure and NOT a pass, and it is the right answer far more often
  than either. The flow is fine; the thing the step asserts was never there to
  see. Reporting it honestly is what lets the next step decide whether that check
  belongs in the generated test at all. Guessing a pass gets an assertion
  generated against a locator that does not exist, which fails later and much
  more expensively.

• After each step fails (see FAILURE PROTOCOL, rule 6, for what to do FIRST):
    STEP_FAILED: <step description>|category=<CATEGORY>|<error details> [url=<current page URL>] [screenshot=<path>] [console=<summary>] [network=<summary>]
  CATEGORY must be exactly one of:
    selector_not_found | login_failed | timeout | overlay_blocking |
    network_error | unexpected_content | skipped | other
  (use skipped for LOGIN GATE cascades per rule 4d/6 below — never other)
  (e.g. STEP_FAILED: Click submit button|category=selector_not_found|no element matched [name='submit'] after 3 retries [url=https://example.com/checkout])

• Whenever you find a working selector/locator:
    SELECTOR_FOUND: <camelCaseName>=<actualSelector>|count=<matchCount>|visible=<visibleCount>
  (e.g. SELECTOR_FOUND: loginButton=[name='commit']|count=1|visible=1)

  ⚠ count AND visible ARE BOTH MANDATORY, and are the number of elements the
  selector matched — and how many of those were actually visible — when you
  evaluated it in the browser (see rule 2c). They go at the very
  END of the line, count then visible, so a selector containing a literal | is
  still safe.
  A marker reporting visible != 1 is DROPPED. The DOM is full of things nobody
  can see: display:none templates, collapsed panels, and toast containers that
  are always present and always empty. A locator for one of those produces a
  generated test that fails for a reason invisible on the page — and if the
  element you were looking for is genuinely not there, that is a fact worth
  reporting as STEP_UNVERIFIED, not papering over with a selector that matches
  a hidden node.
  A marker reporting count != 1 is DROPPED — a selector matching several elements
  kills the generated test at runtime (or makes it act on the wrong element).
  Narrow it and re-report it with count=1 instead.
  A marker with NO count is ALSO DROPPED. Nothing downstream can tell a selector
  you measured from one you eyeballed, so an unmeasured selector is not a
  confirmed one. page.qa measures both numbers for every selector it returns —
  report the numbers it returned.

  ⚠ REPORT THE SELECTOR AS CSS, NOT AS A JAVASCRIPT STRING LITERAL.
  When you paste a selector into browser_evaluate you escape it for JS, so the
  class `gap-4.5` becomes 'div.gap-4\\\\.5' inside your snippet. The selector
  itself is `div.gap-4\\.5` — report that. Copying the doubled backslash out of
  your own JS source produces a selector that matches nothing in the generated
  test, no matter what count you measured.

  ⚠ REPORT A REAL DOM SELECTOR, NEVER A SNAPSHOT REF.
  The `ref=` handles in your browser_snapshot output (e71, f2e585, aria-ref=f2e750)
  are ephemeral labels for YOUR session. They are not selectors: generated Java runs
  in a different browser where they match nothing, forever. The same applies to the
  role prefixes snapshots print — `generic[ref=…]`, `img[ref=…]`, `textbox[ref=…]`.
  Read the element's real attributes and report those instead, in this priority
  order: [data-cy] > [data-testid] > [id] > [name] > a stable class or
  attribute selector > :has-text("visible label").
  There is no `text` attribute in CSS — write button:has-text('Save'), never
  button[text='Save'].
  Any marker carrying a ref is DROPPED, and the locator is then guessed at codegen.

• Whenever you interact with an element worth recording for later code generation:
    INTERACTION_HINT: <json object>
  Valid JSON on a SINGLE LINE with these keys: "type" (one of input | button |
  link | dropdown | checkbox | other), "name" (camelCase), "selector", "text"
  (visible label), and "count" (see below). Using JSON (not a delimiter) means
  the selector or text may safely contain any character, including a literal | —
  do not use a | to separate fields.
  (e.g. INTERACTION_HINT: {{"type":"input","name":"resumeHeadline","selector":"[id='resumeHeadlineTxt']","text":"Resume Headline","count":1}})

  ⚠ HINTS ARE HELD TO THE SAME UNIQUENESS BAR AS SELECTOR_FOUND, because step 03
  generates locators from both. So:
  • If you have already emitted SELECTOR_FOUND for this name, repeat that EXACT
    selector here. Do not hint a different element for the same name — if you
    tried one element, it did not work, and a different one did, the one that
    worked is the only one worth recording.
  • If this name has no SELECTOR_FOUND, the hint must carry its own measured
    "count": 1 measured by page.qa (rule 2c). A hint with no confirmed selector
    and no count=1 is DROPPED.

• On every STEP_FAILED, also emit a snapshot of the page at the moment of
  failure (see FAILURE PROTOCOL, rule 6):
    PAGE_DUMP: <step description>|<json array>
  The JSON array must be valid JSON on a SINGLE LINE (no pretty-printing, no
  embedded literal newlines) — up to 15 of the most relevant visible
  interactive elements. Each element is a JSON object with a "tag" key plus
  whichever of these identifying attributes the element actually has —
  OMIT keys it doesn't have, do not emit empty strings: "data-cy",
  "data-testid", "id", "name" (the element's literal name attribute, not a
  description), "aria-label", "placeholder", "type", "text" (visible text,
  truncated to ~40 chars).
  (e.g. PAGE_DUMP: Click submit button|[{{"tag":"button","data-cy":"submit-btn","text":"Submit"}}])

══════════════════════════════════════════════════════════════
EXECUTION RULES — follow exactly:
══════════════════════════════════════════════════════════════
1. Execute every step in order. Do NOT skip or reorder steps.

1b. REPEATED SUB-FLOWS — a flow often repeats itself on purpose: log out and log
   back in to prove a change persisted, or revisit a page already seen. EXECUTE
   those steps for real — the repetition IS what the test is checking, and
   skipping it would validate nothing. But do NOT re-discover on the way through:
   reuse the selectors you already confirmed for those elements and pass
   harvest: false — they have already been counted on that page and cannot return
   a different answer the second time. Emit STEP_PASSED as
   normal; do not re-emit SELECTOR_FOUND for a name you have already reported.

2. SELECTOR STRATEGY — one call per step, and never write measuring code yourself.

   ⚠ BUDGET. Every browser tool call is a full round-trip, and everything it
   returns is re-read on every turn after it. Every page this browser opens has
   helpers preloaded for all the measuring below — use them instead of writing a
   harvest, a count, a frame loop or a recorder yourself (a run used to spend a
   third of its time retyping exactly those):

     page.qa.step(action, opts) → {{ ok, error, settledMs, url, check, frames }}
         Runs `action` (an async function; leave it out to just look), waits until
         every frame has stopped changing — never add sleeps — and reports what is
         on the page now:
           frames: [{{ frame, prefix, result: [{{ tag, text, type, sel, total,
                                                visible, near, within }}] }}]
         Each `sel` is that element's best stable selector, frame chain included,
         ALREADY COUNTED: `total` and `visible` were measured just now.
         opts: scope: '<selector>'  harvest only that section, CSS or Playwright
                               syntax — use it as soon as you know which one (a
                               whole page is mostly site chrome).
                               Inside an iframe, name the frame the same way a
                               selector does: '#pay >> internal:control=enter-frame >> form',
                               or '#pay >> internal:control=enter-frame' for all of it
               texts: true     also return plain text elements, to read values
               check: {{ name: '<selector>', … }}  count these AFTER the action — CSS,
                               Playwright syntax (:has-text(), role=) or a chain
               before: {{ name: '<selector>', … }}  count these BEFORE the action:
                               the control the step clicks. A tab or menu item
                               that navigates is gone by the time `check` runs
               harvest: false  skip the harvest when `check` tells you enough
     page.qa.check({{ name: '<selector>', … }}) → {{ name: {{ total, visible, text, editable, checked }} }}
         `checked` is there for a radio or checkbox, or the label that controls one:
         count the option you selected in the same call that selects it.
     page.qa.record(action, ms) — rule 2f, screens that close by themselves.

   ONE browser_run_code_unsafe PER STEP — the step's action and the next page's
   elements come back in the same call:
       async (page) => page.qa.step(() => page.locator('#add-to-cart').click(),
                                    {{ before: {{ addToCartButton: '#add-to-cart' }}, scope: '.cart' }})
   The selector in that example is made up: find each one on the page.
   A step with several actions puts them all in one function. Use browser_click,
   browser_type, browser_evaluate or browser_snapshot only when a page.qa call has
   itself failed. If page.qa is undefined, print QA_HELPERS_MISSING once and fall
   back to browser_evaluate with your own code.

   2a) FROM THE RESULT — for every element the plan needs on this page, a `sel`
       with total 1 and visible 1 is confirmed: emit SELECTOR_FOUND for it now,
       with those two numbers, under the name the plan uses. A field with no
       unique selector of its own already comes back anchored on the text beside
       it (`tr:has-text('Name') input`), counted; a clickable element with no
       role (a `div` with a pointer cursor) is in the result too.

   2b) WHEN IT IS NOT UNIQUE — `sel` null, or total above 1 — build a narrower
       candidate, in this priority order:
         a) [data-cy='...'] or [data-testid='...'] or [data-test='...']
         b) [id='...'] — always this form for an id starting with a digit:
            #690 is not valid CSS
         c) [name='...']
         d) [aria-label='...']
         e) a stable class or attribute combination, scoped under `within` (the
            nearest ancestor the result says is unique), e.g. .order-group > div
         f) role-based  (e.g. role=button[name='Sign in'])
         g) text anchored on `near` — what the user reads next to it, e.g.
            tr:has-text('Name') input
       and count every candidate for the page in ONE page.qa.check call, or as
       `check` in the next step's call.

   2c) UNIQUENESS — never emit a selector whose measured total or visible is not
       exactly 1.
       • visible 0 → the element is in the DOM but nobody can see it. Do NOT emit
         it and do NOT go looking for a looser selector that happens to match
         something visible. If this was the element a verification step needed,
         that step is STEP_UNVERIFIED; if it was a control an action step needed,
         go to rule 2e.
       • total above 1, or an error → narrow ONLY those and count them again. Two
         check rounds settle a page — do not degrade into one call per selector.
       • never a comma list of alternatives (`button:has-text('X'), a:has-text('X')`):
         it counts 1 while only one of them matches, and names no element. Count
         each alternative on its own and report the one that matched; a list is
         dropped.
       Report both numbers as |count=<total>|visible=<visible> on the
       SELECTOR_FOUND line. This is parsed and enforced: count != 1 is dropped,
       and so is visible != 1.

2d. OBSTRUCTIONS — before concluding an element is not found or not clickable,
   and before spending any of rule 3's retry budget on it:
   Check whether a cookie-consent banner, promotional/interstitial modal,
   "enable notifications" prompt, or newsletter popup is covering the page.
   If one is present, look for a dismiss control (commonly labeled Accept,
   Close, ✕, "No thanks", "Got it", or similar), click it ONCE, then proceed
   to the original action (which still gets its own separate rule-3 retry
   budget — these are two independent mechanisms, not stacked). Dismissing an
   obstruction is NOT itself a failure — only emit STEP_FAILED if the original
   action still fails afterward, using category=overlay_blocking and noting
   what you dismissed in the error detail.

2e. NO VISIBLE CONTROL FOR AN ACTION — the step names an outcome, not a button.
   When an ACTION step ("Save the profile", "Submit the form", "Log in") names a
   control you cannot find as a VISIBLE element, you have NOT hit a dead end and
   you must NOT fail the step yet. Modern pages often have no such control: the
   user's words describe what should happen, not the widget that makes it happen.
   Work out how the outcome actually occurs, in this order:

   a) CHECK WHETHER IT ALREADY HAPPENED. Blur the field (click a neutral part of
      the page or press Tab), wait ~2s, then re-read the value — reload the page
      and read it again. Many editors autosave a second after the last keystroke.
      Naukri's profile summary does exactly this.
   b) If not, try the ordinary implicit triggers, in order, re-checking after
      each: press Enter in the field; submit the enclosing <form>; look for a
      submit control belonging to that form specifically.
   c) Use the network as a SIGNAL OF MECHANISM, never as proof of outcome. A
      POST firing on blur tells you it is an autosave; the proof is still (a)'s
      reload-and-read-back. Do not report a step passed because a request
      returned 200.
   d) When you find how it works, emit:
        MECHANISM_FOUND: <actionName>|<kind>|<how to trigger it>|<how to know it finished>
      kind is exactly one of: click | autosave | enter_key | form_submit | blur
      (e.g. MECHANISM_FOUND: saveProfileSummary|autosave|blur the textarea, then
       wait|value is still there after a page reload; POST /update/fullprofiles
       observed on blur)
      A plain visible button is `click` and needs no marker — just the
      SELECTOR_FOUND. This marker is for everything else, and it is what lets the
      generated page object do the right thing instead of clicking a locator that
      does not exist.
   e) ONLY when every route above fails is the step a genuine
      STEP_FAILED|category=selector_not_found — meaning no control AND no
      implicit mechanism.

2f. FRAMES AND BRIEF SCREENS — two things browser_evaluate cannot see. Both are
   still held to rules 2a-2c: harvest, count, and report count=1 and visible=1.
   An element inside an iframe is reported as its whole chain, which is what the
   generated page object enters the frame with:
     SELECTOR_FOUND: bankAmount=#checkout >> internal:control=enter-frame >> #amount|count=1|visible=1
{CAPTURE_RULES}

2g. VALUES — write down what you typed and what you compared, verbatim. The test is
   generated from these lines; a value you only judged in your head is lost.
   a) For every field you fill:
        INPUT_USED: <fieldName>|<the exact value you typed>
      using the same fieldName as that field's SELECTOR_FOUND. A step that fills
      fields fills every field the plan names for it, in the element that takes
      typing (the harvest's `tag` is input, textarea or select). Only a value you
      typed in this run is an INPUT_USED; a value you read is not. One whose
      element takes no typing is dropped, with that element's selector.
   b) For every step that compares two values ("matches the one we filled", "same as
      earlier", "shows the amount"), right after its outcome marker — STEP_PASSED
      or STEP_UNVERIFIED, whatever you concluded:
        VALUE_CHECK: <that step's STEP_PASSED text>|<elementName>|<text the element shows>|<source>|<the other side's text>
      where source says where the other side came from:
        input:<fieldName>   a value you typed (its INPUT_USED)
        element:<name>      a value you read earlier in the flow — give that element
                            its own SELECTOR_FOUND (e.g. the cart total a later page
                            must repeat)
        literal             text quoted in the test case — never a value you only read
                            on the page. When the test case compares with something
                            "earlier" ("same as we passed earlier"), read that earlier
                            value (e.g. the cart total) and name it as element:<name>
      Copy both texts exactly as the page shows them — never normalise, round or
      reformat: `Rp20.000` stays `Rp20.000`.
      Do not decide yourself whether two differently formatted values match. When
      they differ only in presentation (a country code `+62…` for `0…`, a currency
      symbol, separators, decimals, spacing, case), report the step STEP_PASSED
      with its VALUE_CHECK. Python measures the relation and downgrades the step
      if the two do not match.
      e.g. VALUE_CHECK: Validate the name shown matches the name entered|customerNameLabel|Test User|input:nameField|Test User
   A comparison of order ("the amount decreased") or of presence ("is not null")
   needs no VALUE_CHECK. Nor does a step that only reads a value for later ("Record
   the amount shown"): it compares nothing. Give its element a SELECTOR_FOUND, and
   the later comparison names it as element:<name>. A literal VALUE_CHECK whose
   text is not in the test case is dropped.

3. RETRIES — if an element is not immediately found or visible:
   Wait 1 second and retry up to 3 times before declaring failure.
   Allow at most {PW_TIMEOUT}ms for any single browser action to complete;
   past that, treat the action as failed and move on rather than waiting longer.

3b. EMIT AS YOU GO — print each SELECTOR_FOUND / MECHANISM_FOUND / INPUT_USED /
   VALUE_CHECK / STEP_PASSED / STEP_FAILED / STEP_UNVERIFIED marker the moment you
   have it, never batched at
   the end. This run has a hard
   wall-clock budget of {_fmt_budget(VALIDATE_TIMEOUT)}; if it is hit, only
   markers already printed can be salvaged.

4. LOGIN GATE — after clicking the sign-in / submit button:
   a) Wait for navigation to complete.
   b) Check whether the current URL still contains '/login', '/signin', or '/session'.
   c) If it does → mark the login step FAILED with:
      STEP_FAILED: <step>|category=login_failed|Login did not succeed — still on login page [url=<url>]
      Then mark an internal flag loginSucceeded=false.
   d) For every subsequent step that requires an authenticated session:
      If loginSucceeded is false, immediately output:
        STEP_FAILED: <step desc>|category=skipped|Skipped — login did not succeed, cannot proceed
      and move on (do NOT attempt any browser interactions for that step).

5. EVERY STEP in its own try/catch. Never abort the whole run on a single failure.

6. FAILURE PROTOCOL — the moment a step fails (after retries and obstruction
   handling above are exhausted), before moving to the next step:
   a) Take a screenshot with the screenshot tool and note its path.
   b) Check the browser console for errors; if any are relevant, summarize in
      one line (e.g. "console: TypeError at checkout.js:42").
   c) Check recent network requests for failed (4xx/5xx) responses relevant to
      this action; if any, summarize in one line (e.g. "network: POST /api/login → 401").
   d) Emit PAGE_DUMP for this step (see OUTPUT PROTOCOL above).
   e) Emit STEP_FAILED with category=, folding the screenshot path / console
      summary / network summary into the bracketed fields as shown in the
      OUTPUT PROTOCOL example.
   This applies to every failure, including cascaded "Skipped" failures from
   the LOGIN GATE — categorize those as category=skipped (screenshot/console/
   network capture is not needed for pure cascades).

7. Include the current page URL in every STEP_FAILED message.

8. SNAPSHOTS AND SCREENSHOTS — neither is needed to know where you are: every
   page.qa.step call returns the URL and what is on the page. Do NOT snapshot or
   screenshot after a click, a submit or a navigation: one snapshot is ~14k
   characters that stays in your context for the rest of the run and slows every
   turn after it, and the step's harvest tells you more in a tenth of the size.
   When a modal, dropdown or panel opens, `scope` the next harvest to it.
   Two things override this. The FAILURE PROTOCOL (rule 6): on a failure, capture
   the screenshot and PAGE_DUMP it asks for regardless. And any step that asserts
   an element appeared: LOOK for it properly before answering — count it directly
   with page.qa.check, and give an element that is slow to appear a few seconds
   (page.qa.step with no action waits for the page to settle) before re-checking.
   An element that may disappear again is caught by rule 2f, never by a wait.
   Saving a call is not a reason to report something you did not actually see;
   the whole point of this step is to find out what is really on the page.

9. Complete ALL steps — do not stop early unless the browser itself crashes.
   Completing a step means reaching an honest answer about it, which is one of
   three: passed, failed, or unverified. It does not mean producing a pass. An
   accurate STEP_UNVERIFIED is a complete step and a genuinely useful result; a
   pass you could not actually observe is neither.

Begin executing the steps now using the browser tools.
"""

    max_attempts = 1 + max(VALIDATE_RETRY_ATTEMPTS, 0)
    log(
        f"Calling Claude with Playwright MCP for {len(web_steps)} steps against "
        f"{base_url} (budget {_fmt_budget(VALIDATE_TIMEOUT)}/attempt, up to "
        f"{max_attempts} attempt(s))..."
    )

    # Same allowlist shape as 03_generate.py: the decoder turns every line of the
    # model's prose into a progress line, and a browser-driving run closes with a
    # markdown run-summary table that repeats markers already streamed above. The
    # raw transcript in claude-*.log keeps all of it for post-mortem; the console
    # only needs the markers and the tool heartbeat.
    # Markers with their colon: without it, the model's prose "VALUE_CHECK for step
    # 4 (record amount) — …" printed as if it were a malformed marker.
    _PROGRESS_PREFIXES = ("STEP_PASSED:", "STEP_FAILED:", "STEP_UNVERIFIED:",
                          "SELECTOR_FOUND:", "INTERACTION_HINT:", "MECHANISM_FOUND:",
                          "INPUT_USED:", "VALUE_CHECK:", "PAGE_DUMP:",
                          "API retry", "MCP server", "→ ")

    def _on_output(label: str, line: str) -> None:
        # Matched on the stripped line for the same reason the marker parsers do:
        # a marker the model happened to indent is still a marker.
        if label != "stdout" or not line.strip().startswith(_PROGRESS_PREFIXES):
            return
        # The mcp__playwright__browser_ prefix is on every tool line and carries no
        # information — a 25-line burst of browser_evaluate reads as noise with it.
        log(f"  {line.replace('mcp__playwright__browser_', '')[:200]}")

    def _run_attempt(attempt_notes: str):
        return call_claude_ex(
            prompt=build_prompt(attempt_notes),
            model=MODEL,
            effort=EFFORT,
            cwd=str(REPO_ROOT),
            timeout=VALIDATE_TIMEOUT,
            on_output=_on_output,
            log_dir=str(AUDIT_DIR),
            allowed_tools=mcp_allowed_tools(),
            # Load NO built-in tools. allowed_tools above only gates permission —
            # every built-in stays *defined*, costing ~10k tokens of system prompt
            # on every one of the ~50 turns this step takes, and arriving deferred
            # so the model burns whole round-trips on ToolSearch before it can
            # navigate, evaluate or press a key. call_claude_ex keeps ToolSearch
            # itself, because MCP tools also arrive deferred and it is the only
            # thing that can load their schemas — see shared/claude.py.
            tools="",
            # Skills and slash commands are equally unreachable from a headless
            # run and equally present in the prompt until asked to leave.
            disable_slash_commands=True,
            # Load exactly the Playwright server written above and nothing else.
            # By default the subprocess also inherits the user's global MCP
            # servers, so it spends startup connecting to unrelated ones
            # (Google Drive, …) and searching a tool registry it will never use.
            mcp_config=str(mcp_path),
            strict_mcp_config=True,
            # Stream events as they happen — otherwise `claude -p` buffers
            # everything until exit and a long run looks frozen with no way to
            # tell it apart from a hang.
            stream_json=True,
        )

    def _parsed(output: str) -> dict:
        passed, failed, unverified = parse_step_results(output)
        unverified = drop_unverified_actions(unverified)
        found, counts, visibles, rejected = parse_selector_output(output)
        evidence = flow_map.read_evidence(evidence_path)
        found, counts, visibles, rejected, measured = verify_with_evidence(
            found, counts, visibles, rejected, evidence)
        inputs = enforce_typed_fields(parse_inputs_used(output), found, counts, visibles,
                                      rejected, evidence)
        recover_clicked_locators(found, counts, visibles, evidence, all_locators)
        preferred = prefer_proven(found, counts, visibles, evidence, proven_here, all_locators)
        if found or measured["dropped"]:
            log(f"Selectors measured live by the browser helpers: {measured['live']} of "
                f"{measured['live'] + measured['claimed']} kept"
                + (f", {measured['dropped']} dropped as not unique or not visible"
                   if measured["dropped"] else "")
                + (f" — {measured['claimed']} rest on the marker's own count"
                   if measured["claimed"] else ""))
        passed, unverified = enforce_verification_evidence(passed, unverified, found)
        value_checks = drop_untraced_sources(parse_value_checks(output), web_steps, inputs)
        passed, unverified = enforce_value_checks(passed, unverified, value_checks)
        passed, unverified = promote_matched_values(passed, unverified, value_checks, found,
                                                    inputs)
        return {
            "output":            output,
            "selectors":         found,
            "selector_counts":   counts,
            "selector_visibles": visibles,
            "rejected_selectors": rejected,
            "steps_passed":      passed,
            "steps_failed":      failed,
            "steps_unverified":  unverified,
            "mechanisms":        parse_mechanisms(output),
            "inputs_used":       inputs,
            "value_checks":      value_checks,
            "page_elements":     parse_page_dumps(output),
            # Reconciled against `found`, so the hints written to disk carry the
            # same uniqueness guarantee the selector map does.
            "interaction_hints": reconcile_hints(parse_interaction_hints(output), found),
            "proven_preferred":  preferred,
        }

    def _score(result, p: dict) -> tuple:
        # A cleanly completed attempt always beats one that crashed/timed out/
        # came back empty, however few failures the crashed one happened to
        # record — dying after 2 steps isn't "better" than running all 10 and
        # failing 2. Then: attempted more of the flow beats attempted less
        # (a thorough attempt with 1 failure beats a barely-started one with
        # 0, since raw failure count alone rewards giving up early). Then:
        # among equally-thorough attempts, fewer failures wins; final tiebreak
        # is more confirmed selectors. Bigger tuple sorts as "better" for max().
        # Unverified counts towards thoroughness — the step did run — but is not
        # scored as a failure. Penalising it would make an attempt that honestly
        # reported "I could not see the toast" lose to one that claimed a pass.
        total_seen = (len(p["steps_passed"]) + len(p["steps_failed"])
                      + len(p.get("steps_unverified") or []))
        return (
            1 if result.status == "ok" else 0,
            total_seen,
            -len(p["steps_failed"]),
            len(p["selectors"]),
        )

    def _failure_detail(step_failed_line: str) -> str:
        return step_failed_line.split("|", 1)[1] if "|" in step_failed_line else step_failed_line

    def _every_locator_confirmed(p: dict) -> bool:
        """True when every locator the plan asked for came back count=1.

        Only meaningful when the plan actually named locators; a plan that named
        none can never satisfy this and must fall through to the other checks.
        """
        if not all_locators:
            return False
        counts = p.get("selector_counts") or {}
        confirmed = {n for n in p.get("selectors", {}) if counts.get(n) == 1}
        return set(all_locators) <= confirmed

    def _worth_retrying(result_status: str, p: dict) -> bool:
        """Skip the retry when it can't plausibly help.

        A pure login/cascade failure won't be fixed by running the identical
        flow again with the identical credentials — that needs a human to fix
        the input file, not another attempt.

        Nor will it help once every locator the plan asked for is already
        uniqueness-verified: a retry re-runs the whole flow from a fresh browser
        for another 5-12 minutes, and the selector map — which is this step's
        actual deliverable for step 03 — is already complete. What it would
        produce is a second, differently-flaky set of step results at full price.

        No STEP_FAILED markers at all means one of two very different things:
        every step genuinely passed (status == "ok" — nothing to retry), or
        the attempt crashed/timed out/came back empty before producing any
        markers (status != "ok") — exactly the scenario retries exist to
        recover from, so that case is always worth another try regardless of
        the (empty) failure list.

        A third case exists now that an unmeasured selector is dropped rather than
        kept: an attempt can walk the whole flow, report every step as passed, and
        still confirm nothing, because it never ran the rule-2c count check. That
        looks like total success by every other signal here, and it leaves step 03
        with an empty map to abort on. It is worth exactly one more attempt, whose
        notes say what was missing.
        """
        steps_failed = p["steps_failed"]
        if all_locators and not p["selectors"]:
            log("Retrying: the flow reported no confirmed selectors at all — likely "
                "SELECTOR_FOUND markers emitted without the mandatory |count=.")
            return True
        if not steps_failed:
            return result_status != "ok"
        if _every_locator_confirmed(p):
            log(f"Not retrying: all {len(set(all_locators))} planned locator(s) are "
                f"uniqueness-verified, so a second pass cannot add a selector.")
            return False
        categories = [parse_failure_category(_failure_detail(s)) for s in steps_failed]
        if all(c in ("login_failed", "skipped") for c in categories):
            return False
        return True

    attempts: list = []  # list of (result, parsed_dict)

    def _persist_snapshot(final: bool) -> None:
        """Persist the best result seen so far. Called after every attempt, and
        the last of those calls IS the final write — a cancel arriving during
        attempt 2 would otherwise discard attempt 1's fully completed, perfectly
        usable results too, not just attempt 2's.

        `final` is False when a retry follows. The server reads this file the
        moment it lands; without the flag it judged attempt 1's snapshot as the
        finished step, painted it green and started Generate while attempt 2
        was still running — and never re-read it when attempt 2 failed.

        Do not add a second call after the loop: every path through the body
        reaches this one, `attempts` cannot change afterwards, so a trailing call
        only rewrites the same file and logs the selector tally twice."""
        r, p = max(attempts, key=lambda ra: _score(ra[0], ra[1]))
        _write_result(
            selectors=p["selectors"],
            selector_counts=p.get("selector_counts"),
            steps_passed=p["steps_passed"],
            steps_failed=p["steps_failed"],
            steps_unverified=p.get("steps_unverified"),
            selector_visibles=p.get("selector_visibles"),
            rejected_selectors=p.get("rejected_selectors"),
            mechanisms=p.get("mechanisms"),
            inputs_used=p.get("inputs_used"),
            value_checks=p.get("value_checks"),
            page_elements=p["page_elements"],
            interaction_hints=p["interaction_hints"],
            skipped=False,
            reason=None if r.ok else r.describe(),
            status=r.status,
            raw_output=p["output"][-3000:] if p["output"] else "",
            attempts=len(attempts),
            urls_visited=list(getattr(r, "navigated_urls", []) or []),
            final_attempt=final,
            proven_preferred=p.get("proven_preferred"),
        )

    attempt_notes = ""
    for attempt_num in range(1, max_attempts + 1):
        if attempt_num > 1:
            log(f"Retry attempt {attempt_num}/{max_attempts} — re-running the full "
                f"flow (fresh isolated browser; no mid-flow resume is possible)")
        result = _run_attempt(attempt_notes)
        if result.status == "usage_limit":
            # Checked before tool_uses: a capped call never runs a turn, so it
            # also drove the browser zero times, and was reported as missing
            # MCP tools. Retrying hits the same cap, so stop and name it.
            log(f"ERROR: Claude {result.describe()}\n"
                "       → FIX: re-run this session once the limit resets.")
            _write_result({}, [], [], status="error", attempts=attempt_num,
                          reason=f"Claude {result.describe()}")
            sys.exit(1)
        if result.tool_uses == 0:
            # No browser tool was ever called, so nothing on the page was ever
            # seen. A model handed a browser-driving prompt and no usable tools
            # does not stop — it narrates the whole session, inventing
            # <function_calls> blocks, their results, and selectors that look
            # exactly like real ones to the parsers below. Parsing that output
            # would ship a page object built from fiction, so refuse it here.
            log("ERROR: Claude drove the browser zero times — every step in its "
                "output is narrated, not observed, and has been discarded.\n"
                "       → FIX: the Playwright MCP tools never reached the model. "
                "Check .mcp.json in this audit dir and that --tools still admits "
                "ToolSearch, which is what loads deferred MCP tool schemas.")
            _write_empty(reason="Playwright MCP tools unavailable — Claude fabricated the run instead of driving a browser")
            sys.exit(1)
        parsed = _parsed(result.stdout)
        attempts.append((result, parsed))

        # Report the actual cause rather than guessing. Each of these produces
        # an empty-or-short result for a completely different reason and needs
        # a different fix.
        if result.status == "timeout":
            log(f"WARNING: validation {result.describe()}")
            log(f"  → FIX: raise VALIDATE_WEB_TIMEOUT_S (currently {VALIDATE_TIMEOUT}s) "
                f"or split the flow into fewer steps")
            if result.stdout.strip():
                log("  Partial results from this attempt were recovered.")
        elif result.status == "error":
            log(f"WARNING: Claude {result.describe()}")
            log("  → FIX: check the claude-*.log in this audit dir for the CLI error")
        elif result.status == "empty":
            log(f"WARNING: Claude {result.describe()}")
            log("  → FIX: check model availability and that the Playwright MCP server "
                "connected (look for \"MCP server 'playwright'\" above)")

        retrying = attempt_num < max_attempts and _worth_retrying(result.status, parsed)
        _persist_snapshot(final=not retrying)
        if retrying:
            if parsed["steps_failed"]:
                notes = ["\nPRIOR ATTEMPT NOTES — a previous run of this exact flow failed "
                         "on the steps below. Apply the noted fix where relevant, but still "
                         "execute every step from the start (this is a fresh browser with no "
                         "session state carried over):"]
                notes.extend(f"  - {s}" for s in parsed["steps_failed"])
            elif not parsed["selectors"]:
                notes = ["\nPRIOR ATTEMPT NOTES — a previous run of this exact flow walked "
                         "the steps but confirmed ZERO selectors, because SELECTOR_FOUND "
                         "markers were emitted without the mandatory |count=<n> and were "
                         "therefore all dropped. Take the total and visible page.qa measures "
                         "and put both on every SELECTOR_FOUND line."]
            else:
                notes = progress_notes(result.describe(), parsed, web_steps)
            attempt_notes = "\n".join(notes)
            continue
        break

    result, parsed = max(attempts, key=lambda ra: _score(ra[0], ra[1]))
    selectors          = parsed["selectors"]
    steps_passed       = parsed["steps_passed"]
    steps_failed       = parsed["steps_failed"]
    steps_unverified   = parsed.get("steps_unverified") or []
    mechanisms         = parsed.get("mechanisms") or {}
    rejected_selectors = parsed.get("rejected_selectors") or {}
    page_elements      = parsed["page_elements"]
    interaction_hints  = parsed["interaction_hints"]

    if len(attempts) > 1:
        log(f"Ran {len(attempts)} attempt(s); selected the best one "
            f"(status={result.status}, {len(steps_failed)} failed, "
            f"{len(steps_passed)} passed, {len(selectors)} selectors).")

    log(
        f"Selectors found: {len(selectors)} | "
        f"Steps passed: {len(steps_passed)} | "
        f"Steps failed: {len(steps_failed)} | "
        f"Steps unverified: {len(steps_unverified)} | "
        f"Page dumps: {len(page_elements)} | "
        f"Interaction hints: {len(interaction_hints)}"
    )

    urls_visited = list(getattr(result, "navigated_urls", []) or [])
    if urls_visited:
        log(f"URLs visited: {len(urls_visited)} — step 03 mints a property for each")
        for url in urls_visited:
            log(f"  {url}")

    if selectors:
        for name, sel in selectors.items():
            log(f"  {name} = {sel}")
    if interaction_hints:
        for h in interaction_hints:
            log(f"  HINT [{h['type']}] {h['name']} → {h['selector']} ({h['text']})")
    if steps_failed:
        log("Failed steps:")
        for s in steps_failed:
            if "|" in s:
                step_desc, error_msg = s.split("|", 1)
                log(f"  FAIL [{step_desc.strip()}]: {error_msg.strip()}")
                hint = fix_hint_for(error_msg)
                if hint:
                    log(f"  → FIX: {hint}")
            else:
                log(f"  FAIL: {s}")
    if mechanisms:
        log("Discovered mechanisms:")
        for name, m in mechanisms.items():
            log(f"  {name} → {m['kind']}: {m.get('trigger', '')}")
    if rejected_selectors:
        log("Rejected selectors:")
        for name, why in rejected_selectors.items():
            log(f"  {name} — {why}")
    if steps_unverified:
        # Loud on purpose. The run this was built for reported a clean 15/15 and
        # the one thing it could not see became a deleted assertion three steps
        # later; a quiet line here would have been read the same way.
        log("UNVERIFIED — these steps ran, but what they claim was never seen "
            "on the page:")
        for u in steps_unverified:
            parts = [x.strip() for x in u.split("|")]
            log(f"  UNVERIFIED [{parts[0]}]"
                + (f": looked for {parts[1]}" if len(parts) > 1 else "")
                + (f"; saw {parts[2]}" if len(parts) > 2 else ""))
        log("  → If you asked for one of these, the product did not do it. Step 03 "
            "keeps the assertion and the test will fail on purpose; a check the "
            "pipeline invented is dropped instead.")


def _write_empty(reason: str) -> None:
    """Write a deliberately-empty result — the flow had nothing to validate.

    Distinct from a failed run: status stays "skipped" so step 03 can tell an
    intentional no-op apart from a validation that died before producing anything.
    """
    _write_result({}, [], [], page_elements={}, interaction_hints=[],
                  skipped=True, reason=reason, status="skipped", attempts=0)


def _write_result(selectors, steps_passed, steps_failed,
                  page_elements=None, interaction_hints=None,
                  skipped=False, reason=None, status="ok", raw_output="",
                  attempts=1, selector_counts=None, steps_unverified=None,
                  selector_visibles=None, rejected_selectors=None,
                  mechanisms=None, urls_visited=None, final_attempt=True,
                  inputs_used=None, value_checks=None, proven_preferred=None) -> None:
    # Every selector that survives parse_selector_output() was measured at exactly
    # one element, and every hint that survives reconcile_hints() is either backed
    # by one of those or measured itself. Assert it rather than trusting it: this
    # file is the contract step 03 generates from, and a regression that quietly
    # re-admitted unmeasured locators would only show up as a strict mode violation
    # several minutes later in step 04.
    unverified = [n for n, c in (selector_counts or {}).items() if c != 1]
    if unverified:
        log(f"BUG: {len(unverified)} selector(s) reached the result without a "
            f"measured count of 1 ({', '.join(sorted(unverified))}) — dropping them.")
        selectors = {n: sel for n, sel in (selectors or {}).items() if n not in unverified}
        selector_counts = {n: c for n, c in (selector_counts or {}).items() if n not in unverified}
    if selectors:
        log(f"All {len(selectors)} selector(s) are uniqueness-verified (count=1).")

    data = {
        "skipped":           skipped,
        "reason":            reason,
        "status":            status,
        "attempts":          attempts,
        "selectors":         selectors,
        # name -> element count the browser reported, or null when the model did
        # not report one. Keeps "checked, and it was unique" distinguishable from
        # "never checked", which the selector map alone cannot express.
        "selector_match_counts": selector_counts or {},
        # name -> how many of those matches were actually visible, or null when
        # the run never measured it. A locator for an element nobody can see is
        # how an assertion ends up failing for an invisible reason.
        "selector_visible_counts": selector_visibles or {},
        # name -> why a reported selector was not kept. The console line scrolls
        # away; this is what answers "why is there no locator for the toast?"
        "rejected_selectors": rejected_selectors or {},
        "steps_passed":      steps_passed,
        "steps_failed":      steps_failed,
        # Every URL the browser was actually told to open, in order. steps_passed
        # is prose the model chose to write and often omits the URL ("Navigate to
        # the profile page"); this is the argument it passed. Step 03 mints a URL
        # property per entry, so a page whose summary named no URL still gets a key
        # instead of a getRunTimeProperty that returns null at runtime.
        "urls_visited":      urls_visited or [],
        # Steps whose action completed but whose claimed outcome was never
        # observed. Neither a pass nor a failure — step 03 decides, on whether
        # the user asked for the check or the pipeline invented it.
        "steps_unverified":  steps_unverified or [],
        # action -> how it actually takes effect, when it is not a plain click.
        "mechanisms":        mechanisms or {},
        # field -> the value this run typed into it. Step 03 keeps test data in
        # the same shape, so the test exercises what was validated.
        "inputs_used":       inputs_used or {},
        # Both sides of every comparison, with the relation Python measured
        # between them — what step 03 asserts each check with.
        "value_checks":      value_checks or [],
        "page_elements":     page_elements or {},
        "interaction_hints": interaction_hints or [],
        # name -> the selector the model reported and the proven one kept instead
        # (prefer_proven). Empty when the model chose the proven one itself.
        "proven_preferred":  proven_preferred or [],
        # False while a retry follows — the server keeps the chip running
        # instead of judging this snapshot as the step's outcome.
        "final_attempt":     final_attempt,
    }
    if raw_output:
        data["raw_output_tail"] = raw_output
    (AUDIT_DIR / "02-validate-web.json").write_text(json.dumps(data, indent=2))

    lines = ["# Validate Web Results", ""]
    if skipped:
        lines.append(f"Skipped: {reason}")
    else:
        lines.append(f"Outcome:         {status}")
        if reason:
            lines.append(f"Detail:          {reason}")
        lines.append(f"Attempts:        {attempts}")
        lines.append(f"Steps passed:    {len(steps_passed)}")
        lines.append(f"Steps failed:    {len(steps_failed)}")
        lines.append(f"Steps unverified:{len(steps_unverified or [])}")
        lines.append(f"Selectors found: {len(selectors)}")
        # Ahead of the selector table on purpose: this is the part a human most
        # needs to see, and the run that prompted it read as a clean 15/15 pass.
        if steps_unverified:
            lines.append("")
            lines.append("## ⚠ Unverified — could not be observed in the UI")
            lines.append("")
            lines.append("These steps ran, but what they claim was never seen on the "
                         "page. If you asked for one of these, the product did not do "
                         "it and the generated test will fail on purpose.")
            lines.append("")
            for u in steps_unverified:
                lines.append(f"- {u}")
        if inputs_used or value_checks:
            lines.append("")
            lines.append("## Values observed")
            for field, value in (inputs_used or {}).items():
                lines.append(f"- typed `{field}` = `{value}`")
            for c in value_checks or []:
                lines.append(f"- {c['check']}: `{c['rendered']}` vs `{c['expected']}` "
                             f"({c['source']}) → **{c['relation'] or 'no match'}**")
        if mechanisms:
            lines.append("")
            lines.append("## Discovered Mechanisms")
            for name, m in mechanisms.items():
                lines.append(f"- `{name}` → **{m['kind']}** — {m.get('trigger', '')}"
                             + (f" (settles when: {m['settles_when']})"
                                if m.get("settles_when") else ""))
        if rejected_selectors:
            lines.append("")
            lines.append("## Rejected Selectors")
            for name, why in rejected_selectors.items():
                lines.append(f"- `{name}` — {why}")
        if selectors:
            lines.append("")
            lines.append("## Confirmed Selectors")
            for name, sel in selectors.items():
                lines.append(f"- `{name}` → `{sel}`")
        if steps_failed:
            lines.append("")
            lines.append("## Failed Steps (step 03 will use inferred selectors)")
            for s in steps_failed:
                lines.append(f"- {s}")
    (AUDIT_DIR / "02-validate-web.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
