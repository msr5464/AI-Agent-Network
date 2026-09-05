"""Who asked for a check, and whether a step states a proof or a mechanism.

Not to be confused with shared/step_provenance.py, which reconstructs what a
test DID from its execution log. This module is about what a test was ASKED to
prove, and by whom — the plan side, before any code exists.

Two questions the authoring pipeline never asked, and had to start asking after a
generated test shipped green with its assertion deleted:

  1. **Is this step a proof or a mechanism?** "Verify the success toast appears"
     names the proof and is fixed. "Save the profile" names an outcome — on Naukri
     the summary autosaves a second after the last keystroke, so there may be no
     Save button to press at all. `shared/intent.py` already draws this line for
     repairs ("the mechanism becomes mutable and the proof does not"); this module
     draws it one stage earlier, on the plain-English step.

  2. **Did the user actually ask for it?** In the run that prompted this module the
     user wrote "save / wait 2 seconds / go again and validate the profile is
     updated". Step 01 turned that into a `successToast` locator, an
     `isSuccessToastVisible()` accessor and an `assertTrue` on it. Nobody asked for
     a toast. It failed, and step 04 deleted the assertion to go green.

**The asymmetry that sets the default.** These two errors are not equally bad:

  - Calling a user's real check "inferred" **drops an assertion they asked for**,
    and the test then proves less than it claims — the exact harm this whole
    change exists to prevent.
  - Calling an invented check "user" keeps it, the test ships red, and a human
    looks at it.

So `inferred` requires positive evidence, and `derive()` answers "user" whenever
it is unsure. `reconcile()` then drops a check only when the model's own claim and
the text scan **both** say it was invented. A conservative wrong answer costs a
red test; an aggressive wrong answer costs a silent one.
"""

from __future__ import annotations

import re
from typing import Iterable, Set

VERIFICATION = "verification"
ACTION = "action"

USER = "user"
INFERRED = "inferred"

# A step is a proof when a verifying verb appears anywhere in it, not just at the
# front: the user's own last step reads "go again to the profile page and validate
# that the profile is updated", where the proof rides on the tail of an action.
# Over-reading an action as a proof is the safe direction — it reports the step as
# unverified instead of silently guessing a mechanism for it.
_VERIFY_VERB = re.compile(
    r"\b(verify|verifies|verified|assert|asserts|asserted|validate|validates|"
    r"validated|check|checks|checked|confirm|confirms|confirmed|expect|expects|"
    r"ensure|ensures|ensured|should)\b",
    re.IGNORECASE)

# `assertEquals`, `assertTrue`, `shouldBeVisible` — the plan's assertion steps are
# written as framework calls as often as prose, and a call carries no spaced-out
# English verb for _VERIFY_VERB to find.
_ASSERT_CALL = re.compile(r"\bassert[A-Z_]\w*|\bshouldBe[A-Z]\w*")

# Words, with camelCase split apart: the plan writes assertion steps as
# `assertEquals refreshedSummary to modifiedSummary`, and left whole those
# identifiers trace back to nothing the user wrote.
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+")
_MIN_WORD = 3

# Framework plumbing the plan names in passing: `assertTrue isSuccessToastVisible
# on the returned NaukriProfilePage` is about the toast, but NaukriProfilePage
# drags in "naukri" and "profile" — both of which trace back to the input and
# would make an invented check look like the author's.
_PLUMBING = re.compile(
    r"\b\w*(?:Page|Helper|Data|Builder|Api|Test|Config|Locator)\b")

# Words that carry no provenance: they appear in almost any test step, so finding
# one in the user's input proves nothing about whether they asked for this check.
# Split in two only for readability — they are used as one set.
_STOPWORDS = {
    "the", "and", "that", "for", "with", "from", "into", "then", "now", "this",
    "there", "their", "its", "was", "were", "are", "has", "have", "had", "not",
    "but", "any", "all", "already", "again", "after", "before", "when", "where",
    "which", "while", "who", "you", "your", "our", "out", "off", "over", "under",
    "same", "each", "every", "some", "will", "would", "should", "can", "may",
}
_GENERIC_UI = {
    "page", "text", "step", "steps", "value", "values", "element", "elements",
    "field", "fields", "screen", "message", "messages", "section", "button",
    "click", "clicks", "open", "opens", "load", "loads", "loaded", "display",
    "displays", "displayed", "show", "shows", "shown", "appear", "appears",
    "visible", "present", "correct", "correctly", "expected", "actual",
    "verify", "assert", "validate", "check", "confirm", "expect", "ensure",
    "test", "tests", "case", "flow", "user", "users", "data",
}

# Enough of the check's own vocabulary must trace back to the user's text before
# we will call it theirs. Set where "Verify the success toast on the profile page
# appears" (profile alone traces) still reads as invented, while "Assert the
# displayed Profile Summary matches the modified summary saved earlier" (profile,
# summary, modified, saved all trace) reads as the user's.
_TRACE_RATIO = 0.5


def shape(step_text: str) -> str:
    """VERIFICATION when the step states a proof, ACTION when it states an outcome.

    This is the switch the rest of the pipeline hangs off: a proof that cannot be
    observed is a finding to report, while a mechanism that cannot be observed is
    a mechanism to go and discover.
    """
    text = step_text or ""
    if _VERIFY_VERB.search(text) or _ASSERT_CALL.search(text):
        return VERIFICATION
    return ACTION


def _akin(a: str, b: str) -> bool:
    """Same word up to the tense and plural changes a rewrite introduces.

    Prefix comparison rather than stemming, because a stemmer has to be
    symmetric to be safe here and the crude ones are not: trimming `-ed` turns
    `modified` into `modifi` while `modify` is left alone, and the two then fail
    to match. A missed match drops an assertion the user asked for, so the
    comparison is built to fail towards matching.
    """
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short):
        return True                       # save / saved, change / changes
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= 5                    # modify / modified, profile / profiles


def _words(text: str) -> Set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if len(w) >= _MIN_WORD}


def _is_generic(word: str) -> bool:
    """Stopword or UI filler, compared loosely so `confirms` matches `confirm`."""
    return any(_akin(word, g) for g in _STOPWORDS | _GENERIC_UI)


def _distinctive(text: str) -> Set[str]:
    """Content words that could actually identify what a step is about."""
    return {w for w in _words(_PLUMBING.sub(" ", text or "")) if not _is_generic(w)}


def _traced(distinctive: Set[str], vocabulary: Set[str]) -> Set[str]:
    """The distinctive words that appear in what the user wrote."""
    return {w for w in distinctive if any(_akin(w, v) for v in vocabulary)}


def derive(step_text: str, raw_input_text: str) -> str:
    """USER unless there is positive evidence the step was invented.

    "Invented" means the check's own distinctive vocabulary is absent from what
    the user wrote — a toast step against an input that never says toast,
    success, or confirmation. Anything less certain returns USER, because
    dropping a real assertion is the failure this module exists to prevent.
    """
    distinctive = _distinctive(step_text)
    if not distinctive:
        # Nothing specific enough to trace either way. Keep it.
        return USER
    traced = _traced(distinctive, _words(raw_input_text))
    return USER if len(traced) / len(distinctive) >= _TRACE_RATIO else INFERRED


def reconcile(claimed: str, derived: str) -> str:
    """INFERRED only when the model's own claim and the text scan agree.

    The model knows what it added and is the better judge when it is honest; the
    text scan is the check on it when it is not. Requiring both to say "invented"
    means a disagreement keeps the assertion — visible as a red test — rather
    than silently deleting it.
    """
    claimed = (claimed or "").strip().lower()
    if claimed == INFERRED and derived == INFERRED:
        return INFERRED
    return USER


def classify(step_text: str, raw_input_text: str, claimed: str = "") -> str:
    """Provenance for one step, folding in the model's claim when it made one."""
    return reconcile(claimed, derive(step_text, raw_input_text))


def droppable(step_text: str, raw_input_text: str) -> bool:
    """True when this check may be dropped rather than generated.

    Both conditions must hold: it is a proof (an action is never dropped — its
    mechanism is discovered instead, see the module docstring), and nothing in it
    traces back to the author at all.

    Deliberately stricter than `derive()`, and deliberately not a function of
    what the model claimed about itself: dropping is the irreversible direction,
    so it answers to measurement alone.
    """
    return (shape(step_text) == VERIFICATION
            and clearly_invented(step_text, raw_input_text))


def clearly_invented(step_text: str, raw_input_text: str) -> bool:
    """NOTHING in this check traces back to the author. The bar for dropping one.

    `derive()` reports a majority judgement, which is the right sensitivity for
    telling a human "this check may not be yours". Deleting a check is not
    reversible by the reader, so it needs a harder test: not one distinctive word
    in common with anything the author wrote.

    "Verify a success confirmation toast appears" against an input that never
    says toast, success or confirmation clears this bar. "Verify the profile
    summary persisted after reload" does not — the author said profile and
    summary, and their check survives even if worded very differently.
    """
    distinctive = _distinctive(step_text)
    if not distinctive:
        return False
    return not _traced(distinctive, _words(raw_input_text))


def subject_words(text: str) -> Set[str]:
    """The distinctive words naming what a step (or a locator) is about.

    Used to ask whether a verification step and a confirmed selector are talking
    about the same element: "Verify a success confirmation toast appears" and
    `successToast` share `toast`, while that step and `saveButton` share nothing.
    camelCase splits, so a locator name compares on the same footing as prose.
    """
    return _distinctive(text)


def describe_all(steps: Iterable[str], raw_input_text: str) -> dict:
    """Provenance for a list of steps, for logging and the audit trail."""
    out = {}
    for step in steps or []:
        out[step] = {"shape": shape(step), "source": derive(step, raw_input_text)}
    return out
