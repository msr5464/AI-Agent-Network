"""Mechanical guards on an automated code edit, and the machinery to apply one.

Extracted from test-healing-agent's fix step so test-adaptation-agent can reuse
them. Nothing here knows which agent called it, what the failure was, or how the
edit was generated — that is the point. These are the checks a re-run cannot do
for us.

The reasoning they encode, which is worth keeping in one place: **the
verification loop cannot catch a fix built on a wrong diagnosis, because the
easiest way to make a page assertion pass is to weaken it.** A run that had
already been told the avatar was missing "fixed" it by moving the page-load
anchor onto a link that exists on the logged-out page too — that would have gone
green while the login was still broken.

`agents/test-healing-agent/actions/01_fix.py` re-exports every public name here,
so its own callers and tests keep working unchanged.
"""

import difflib
import re

from shared.code_analyzer import split_class_members
from shared.dom_snapshot import selector_visibility
from shared.page_identity import normalize_selector as _normalize_selector

# How large a diff may be before it stops looking like a targeted edit. The
# caller passes its own budget; healing reads AUTOFIX_MAX_DIFF_LINES.
DEFAULT_MAX_DIFF_LINES = 40

def _line_of(text: str, needle: str) -> int:
    """1-based line where needle starts, or 0."""
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 0


def _condense(value: str, width: int = 100) -> str:
    """One readable line: collapse whitespace, elide the middle if long."""
    flat = " ".join((value or "").split())
    if len(flat) <= width:
        return flat
    return flat[: width - 20] + " … " + flat[-17:]


def log_edits(target_file, original: str, edits: list, log_fn) -> None:
    """Print what actually changed, file and line, before -> after.

    The prose fix_description says WHY; without this nobody could see WHAT.
    Reading a run meant scrolling maven output hunting for the new selector in
    the next failure message, and a reverted attempt left no record at all.
    """
    total = len(edits or [])
    for n, edit in enumerate(edits or [], 1):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        line = _line_of(original, old)
        where = f"{target_file.name}:{line}" if line else target_file.name
        log_fn(f"    edit {n}/{total} — {where}")
        log_fn(f"      - {_condense(old)}")
        log_fn(f"      + {_condense(new)}")


def apply_edits(original: str, edits: list) -> tuple:
    """Apply search/replace edits. Returns (updated_text, error).

    Every old_string must appear exactly once — an ambiguous match means the
    model did not give enough context, and guessing which occurrence it meant is
    how an autofix corrupts a file.
    """
    if not edits:
        return None, "no edits supplied"

    updated = original
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return None, f"edit {i} is not an object"
        old = edit.get("old_string")
        new = edit.get("new_string")
        if old is None or new is None:
            return None, f"edit {i} missing old_string/new_string"
        if old == new:
            return None, f"edit {i} is a no-op"
        count = updated.count(old)
        if count == 0:
            return None, f"edit {i}: old_string not found in file"
        if count > 1:
            return None, f"edit {i}: old_string matches {count} times — not unique"
        updated = updated.replace(old, new, 1)

    return updated, ""


def validate_fix(original: str, updated: str, filename: str,
                 max_diff_lines: int = DEFAULT_MAX_DIFF_LINES) -> tuple:
    """Reject a 'locator fix' that is actually a rewrite. Returns (ok, reason).

    The model only ever sees part of a large file, so a change far bigger than a
    locator is the signature of it regenerating content it never read.
    """
    if not updated.strip():
        return False, "fix produced an empty file"

    changed = [line for line in difflib.unified_diff(
        original.splitlines(), updated.splitlines(), lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    if not changed:
        return False, "fix changed nothing"
    if len(changed) > max_diff_lines:
        return False, (f"fix touches {len(changed)} lines (limit {max_diff_lines}) — "
                       f"too large to be a targeted edit; this is the signature of "
                       f"the model regenerating a file it only saw part of")

    # Losing a method is the classic whole-file-rewrite failure: the model
    # regenerates a file it only saw an excerpt of and silently drops the rest.
    if filename.endswith((".java", ".kt")):
        try:
            before = {m["name"] for m in split_class_members(original)
                      if m["kind"] in ("method", "constructor") and m["name"]}
            after = {m["name"] for m in split_class_members(updated)
                     if m["kind"] in ("method", "constructor") and m["name"]}
            lost = before - after
            if lost:
                return False, f"fix removed method(s): {', '.join(sorted(lost))}"
        except Exception:
            pass  # never let the guard itself break a valid fix

    return True, ""




# ── Fix-integrity guards ──────────────────────────────────────────────────────
#
# The verification loop cannot catch a fix built on a wrong diagnosis, because
# the easiest way to make a page assertion pass is to weaken it. A run that had
# already been told the avatar was missing "fixed" it by moving the page-load
# anchor onto a link that exists on the logged-out page too — and that would have
# gone green while the login was still broken. These guards are what the re-run
# cannot do for us.

# Selectors that assert *which page* we are on. Broadening one of these turns a
# real failure into a silent pass.
_IDENTITY_CALL = re.compile(r"assertPageLoaded\s*\(")

# A quoted selector, so a replacement can be compared against what it replaced.
_QUOTED = re.compile(r"""(["'])((?:\\.|(?!\1).)+)\1""")


# Only the argument of a locator call is a selector. Framework code is full of
# quoted human labels passed alongside one — click(loginButton, "Login button"),
# isElementDisplayed(toast, "Success Toast") — and treating those as selectors made
# the broadening check compare a label against a real selector: "Success Toast" vs
# "[class*='toast'], [role='alert']" trips the comma rule and rejects a valid fix.
#
# Scoping to the call rather than to how the string LOOKS is what keeps a bare tag
# (page.locator("input"), a real collapse-to-tag broadening) in scope while leaving
# labels out — a shape-based filter cannot separate those two.
_LOCATOR_CALL = re.compile(
    r"""(?:locator|cssSelector|querySelectorAll|querySelector|waitForSelector)\s*"""
    r"""\(\s*(["'])((?:\\.|(?!\1).)*)\1""",
    re.I,
)


def _selectors_in(text: str) -> list:
    return [m.group(2) for m in _LOCATOR_CALL.finditer(text or "")]


def _is_broader(before: str, after: str) -> bool:
    """Whether `after` is a strictly weaker version of `before`.

    Only the unambiguous cases: adding comma-alternatives, dropping attribute or
    class constraints, or collapsing to a bare tag. A different-but-equally-tight
    selector is a normal fix and must pass.
    """
    if not before or not after or before == after:
        return False
    # Every added comma is another alternative the assertion will accept, so a
    # selector that already had one gets looser the same way one that had none
    # does. Counting rather than testing presence is what catches
    # "[a], [b], [c]" -> "[a], [b], [c], [d], [role='alert']", which is a guess
    # widening its net until something matches.
    if after.count(",") > before.count(","):
        return True
    def tightness(selector):
        return (selector.count("[") + selector.count("#") + selector.count(".")
                + selector.count(":"))
    if tightness(after) == 0 and tightness(before) > 0:
        return True
    return False


def no_selector_broadening(original: str, updated: str) -> tuple:
    """Reject an edit that replaces a selector with a looser one. (ok, reason).

    Verdict-independent, which is why it lives on its own: broadening is how a
    wrong-page failure gets papered over into a pass, and that is true whatever
    the diagnosis said — or whether one was made at all. validate_diagnosis_fit()
    calls this as its rule 2, so healing's behaviour is unchanged.
    """
    changed = [line for line in difflib.unified_diff(
        original.splitlines(), updated.splitlines(), lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    removed = [line[1:] for line in changed if line.startswith("-")]
    added = [line[1:] for line in changed if line.startswith("+")]
    for before, after in zip(_selectors_in("\n".join(removed)),
                             _selectors_in("\n".join(added))):
        if _is_broader(before, after):
            return False, (f"fix broadens the selector {before!r} to {after!r}, "
                           f"which would make the assertion weaker rather than correct")
    return True, ""


def validate_diagnosis_fit(original: str, updated: str, verdict: str,
                           snapshot_soup=None, fingerprints=None) -> tuple:
    """Reject an edit that does not match what the diagnosis actually found.

    Returns (ok, reason). Runs before the test does, so a fix that could only
    pass by weakening the test never reaches a runner at all.

    `fingerprints` is the sidecar captured beside the DOM snapshot. It is optional
    because callers without one must keep working unchanged, but supplying it is
    what makes rule 3 able to see visibility — the saved markup cannot.
    """
    changed = [line for line in difflib.unified_diff(
        original.splitlines(), updated.splitlines(), lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    removed = [line[1:] for line in changed if line.startswith("-")]
    added = [line[1:] for line in changed if line.startswith("+")]

    # 1. Never weaken a page-identity assertion unless the locator really is the
    #    thing that broke.
    if verdict != "LOCATOR_STALE":
        touched_identity = any(_IDENTITY_CALL.search(line) for line in removed + added)
        if touched_identity:
            return False, (f"fix changes a page-load assertion, but the diagnosis is "
                           f"{verdict or 'unknown'} rather than a stale locator — "
                           f"weakening a page check would make the test pass on the "
                           f"wrong page. Re-run with FORCE=true to override.")

    # 2. Never broaden a selector. That is how a wrong-page failure gets papered
    #    over into a pass.
    ok, reason = no_selector_broadening(original, updated)
    if not ok:
        return False, reason

    # 3. A genuinely stale locator has a replacement that exists on the page we
    #    were actually on, and that a user could actually have interacted with.
    #    One matching nothing is a guess; one matching only hidden elements is a
    #    fix that cannot work. The failure-time DOM says so before maven spends a
    #    minute discovering it.
    if verdict == "LOCATOR_STALE" and snapshot_soup is not None:
        prints = fingerprints or {}
        known_visibility = bool(prints.get("elements"))
        checked = matched = visible = 0
        for candidate in _selectors_in("\n".join(added)):
            result = selector_visibility(candidate, snapshot_soup, prints)
            if result is None:
                continue                 # cannot evaluate: not evidence either way
            hits, seen = result
            checked += 1
            matched += 1 if hits else 0
            visible += seen
        if checked and not matched:
            return False, ("the replacement selector matches nothing in the DOM "
                           "captured at failure, so it is a guess rather than a fix")
        # Only assert invisibility when the capture actually recorded some, so a
        # missing sidecar degrades to the match-count rule rather than to "reject".
        if known_visibility and matched and not visible:
            return False, ("the replacement selector matches only elements that "
                           "were not visible when the test failed, so the click "
                           "would time out exactly as the original did")

    # 4. A fix for an ambiguous locator has to be unambiguous. Playwright refuses
    #    to act on a selector that resolves to more than one element, so a
    #    "narrower" selector that still matches two fails identically — as
    #    `#loginForm button[type='submit']` did, scoping to a form that contained
    #    both buttons. The captured DOM answers this in a millisecond; discovering
    #    it by running the test costs half a minute and a revert.
    if verdict == "AMBIGUOUS_LOCATOR" and snapshot_soup is not None:
        for candidate in _selectors_in("\n".join(added)):
            result = selector_visibility(candidate, snapshot_soup, fingerprints or {})
            if result is None:
                continue                 # cannot evaluate: not evidence either way
            hits, _seen = result
            if hits > 1:
                return False, (f"the replacement selector still matches {hits} "
                               f"elements in the DOM captured at failure, and the "
                               f"diagnosis is that matching more than one IS the "
                               f"failure — it would throw the same ambiguous locator "
                               f"error")
            if hits == 0:
                return False, ("the replacement selector matches nothing in the "
                               "DOM captured at failure, so it is a guess rather "
                               "than a fix")

    return True, ""


def compute_diff(original: str, fixed: str, filename: str) -> str:
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=3,
    ))
    return "".join(diff_lines[:100])



# ── Guards for edits larger than a locator ────────────────────────────────────
#
# The guards above bound a *locator* edit: keep the diff small, do not lose a
# method, do not broaden a selector. Once an edit may add and remove steps those
# stop applying, because a legitimate flow change is large by definition. What
# still applies is that the test must go on proving what it proved before, and
# that the model must be transcribing something it observed rather than inventing
# something plausible. These check exactly that.

def _added_lines(before: str, after: str) -> list:
    return [line[1:] for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++")]


# An edit that makes a failure survivable rather than fixing it. Each of these
# turns a red test green without changing what the product does.
_SWALLOW_PATTERNS = (
    (re.compile(r"\bThread\.sleep\s*\("), "Thread.sleep — CONVENTIONS.md §2 bans it; "
     "use the framework's waits"),
    (re.compile(r"@Ignore\b"), "@Ignore — disables the test rather than fixing it"),
    (re.compile(r"\benabled\s*=\s*false"), "enabled=false — disables the test"),
    (re.compile(r"\bassumeTrue\s*\("), "assumeTrue — skips the test at runtime"),
    (re.compile(r"\bthrow\s+new\s+SkipException"), "SkipException — skips the test"),
)

# A catch whose body neither rethrows nor records a failure: the exception
# happened, and nobody will ever know.
_CATCH = re.compile(r"\bcatch\s*\([^)]*\)\s*\{")
_CATCH_OK = re.compile(r"\b(throw|AssertHelper|logFail|logFailToEndExecution|fail)\b")

# Raw driver calls. CONVENTIONS.md §1 and config/skills/automation-repo.md both
# forbid these, and until now nothing checked — the rule lived only in a prompt.
_RAW_DRIVER = (
    (re.compile(r"\bdriver\s*\.\s*findElement"), "driver.findElement"),
    (re.compile(r"\.\s*sendKeys\s*\("), ".sendKeys()"),
    (re.compile(r"\bnew\s+WebDriverWait\b"), "new WebDriverWait"),
    (re.compile(r"\bdriver\s*\.\s*get\s*\("), "driver.get()"),
)

_INTERACTION = re.compile(
    r"\bElement\s*\.\s*(click\w*|enterData|clearData|selectBy\w*|hover\w*)\s*\(")
_LOGSTEP = re.compile(r"\blogStep\s*\(")


def no_new_swallowing(before: str, after: str) -> tuple:
    """Reject an edit that makes the test survive a failure instead of fixing it."""
    added = _added_lines(before, after)
    blob = "\n".join(added)
    reasons = []

    for pattern, why in _SWALLOW_PATTERNS:
        if pattern.search(blob):
            reasons.append(why)

    for match in _CATCH.finditer(blob):
        tail = blob[match.end():match.end() + 400]
        body = tail.split("}")[0]
        if not _CATCH_OK.search(body):
            reasons.append("a catch block that neither rethrows nor records a "
                           "failure — the exception happened and nobody will know")
            break

    return (not reasons), "; ".join(reasons)


def wrapper_compliance(before: str, after: str) -> tuple:
    """Reject raw Selenium/WebDriver calls in added code."""
    blob = "\n".join(_added_lines(before, after))
    found = [name for pattern, name in _RAW_DRIVER if pattern.search(blob)]
    if found:
        return False, (f"added raw driver calls ({', '.join(found)}) — the framework "
                       f"wrappers exist so waits, retries and logging happen; "
                       f"CONVENTIONS.md §1")
    return True, ""


def logstep_present(before: str, after: str, is_test_class: bool) -> tuple:
    """An added interaction in a test class must say what it is doing.

    CONVENTIONS.md §10 requires it, and it is not bookkeeping: the derived intent
    contract is built from these strings, so a step added without one quietly
    degrades the contract that protects the *next* adaptation.
    """
    if not is_test_class:
        return True, ""
    added = "\n".join(_added_lines(before, after))
    if _INTERACTION.search(added) and not _LOGSTEP.search(added):
        return False, ("added an interaction to a test class with no logStep — "
                       "CONVENTIONS.md §10, and the intent contract is derived "
                       "from those strings")
    return True, ""


def matches_negative(selectors: list, negatives: list) -> tuple:
    """Reject an anchor that also matches a page the test must NOT be on.

    A selector that matches the logged-out page is not proof of a successful
    login. The negatives are DOM snapshots of pages that represent failure —
    logged-out, error, empty-state — which the agents already produce in volume
    and have never been compared against.
    """
    from shared.page_identity import parse as _parse

    for candidate in selectors or []:
        normalized = _normalize_selector(candidate)
        if not normalized:
            continue
        for negative in negatives or []:
            try:
                soup = negative if hasattr(negative, "select") else _parse(negative)
                if soup is not None and soup.select(normalized, limit=1):
                    return False, (f"the new anchor {candidate!r} also matches a "
                                   f"page the test must not be on — it would pass "
                                   f"there too, so it proves nothing")
            except Exception:
                continue
    return True, ""


def steps_justified(before: str, after: str, flow_steps: list) -> tuple:
    """Every interaction the edit ADDS must correspond to one that was observed.

    This is the inversion the whole design rests on: the model transcribes what
    exploration saw, it does not invent what would be convenient. A step whose
    selector could not be verified unique justifies nothing — an unverifiable
    observation is not an observation.
    """
    added = _added_lines(before, after)
    interactions = [line.strip() for line in added if _INTERACTION.search(line)]
    if not interactions:
        return True, ""

    usable = [s for s in (flow_steps or [])
              if (s.get("maps_to_test") or {}).get("kind") in ("new", "replaces")
              and (s.get("selector_check") or {}).get("unique") is True]
    if not usable:
        return False, (f"{len(interactions)} interaction(s) added but exploration "
                       f"observed no new step with a uniquely verified selector — "
                       f"this is invention, not transcription")
    if len(interactions) > len(usable):
        return False, (f"{len(interactions)} interaction(s) added against "
                       f"{len(usable)} observed new step(s) — more steps than were "
                       f"seen in the product")

    observed = set()
    for step in usable:
        target = (step.get("action") or {}).get("target") or {}
        for key in ("name", "selector", "accessible_name"):
            if target.get(key):
                observed.add(re.sub(r"[^a-z0-9]+", "", str(target[key]).lower()))

    unjustified = []
    for line in interactions:
        flat = re.sub(r"[^a-z0-9]+", "", line.lower())
        if not any(token and token in flat for token in observed):
            unjustified.append(line[:80])
    if unjustified:
        return False, ("added interaction(s) matching nothing exploration observed: "
                       + "; ".join(unjustified[:3]))
    return True, ""
