"""Failure classification — the gate.

The documented failure mode of every self-healing tool is *silent false repair*:
treat a redirect-to-login as a broken selector, rematch onto whatever button is
nearby, go green, ship the regression. So healing is opt-in on evidence, not the
default response to a red step.

Only LOCATOR_DRIFT proceeds to candidate search. Everything else stops with a
reason the human can act on.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from shared.baseline import is_different_page
from shared.locator_score import Volatility, jaccard, score as score_pair

DRIFT = "LOCATOR_DRIFT"


@dataclass
class Verdict:
    kind: str
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def healable(self) -> bool:
        return self.kind == DRIFT

    def __str__(self) -> str:
        return f"{self.kind}: {self.reason}"


ERROR_TEXT = re.compile(
    r"(something went wrong|internal server error|５００|\b50[0-9]\b|"
    r"unexpected error|application error)", re.I)


def _landmark_overlap(base_marks: list[str], now_marks: list[str]) -> float:
    return jaccard(set(base_marks or []), set(now_marks or []))


def _neighbour_survival(base_neighbours: list[str], snap: dict, depth: int = 3) -> float:
    """How much of the element's immediate textual context is still on the page.

    If the label, the heading and the sibling copy all vanished together, the
    feature was deleted — and healing would bind the test to something unrelated.
    """
    closest = [n for n in (base_neighbours or []) if n][:depth]
    if not closest:
        return 1.0                       # no evidence either way; don't block
    page_text = " ".join(e.get("text") or "" for e in snap["elements"]).lower()
    hits = sum(1 for n in closest if n.lower() in page_text)
    return hits / len(closest)


_SIGN_IN_URL = re.compile(r"/(login|signin|sign-in|sso|auth)\b", re.I)


def _looks_like_sign_in(url: str, snap: dict) -> bool:
    """Whether the replay bounced to a sign-in page rather than the target."""
    if _SIGN_IN_URL.search(url or ""):
        return True
    fields = {(e.get("type") or "").lower() for e in snap.get("elements") or []}
    return "password" in fields


def classify(snap: dict, baseline: dict, match_count: int, matched: dict | None,
             cfg: dict, vol: Volatility | None = None,
             http_status: int | None = None,
             page_comparison: dict | None = None) -> Verdict:
    base_el = baseline["element"]
    ct = cfg["classify"]

    # 1. Did the application itself fall over? Never heal over a real error.
    if http_status and http_status >= 500:
        return Verdict("APP_BUG", f"HTTP {http_status} on the document")
    for e in snap["elements"]:
        if not e["is_visible"]:
            continue
        if e["attrs"].get("role") == "alert" and ERROR_TEXT.search(e.get("text") or ""):
            return Verdict("APP_BUG", f"error alert on page: {e['text'][:60]!r}")

    # 2. Are we even on the right screen? (the redirect-to-login trap)
    #
    # Two different pages are in play and both have to agree. `page_comparison`
    # describes the page captured AT FAILURE TIME; the snapshot in `snap` is the
    # page in front of us NOW, replayed. They can disagree — most often because
    # the replay has no session and landed on a sign-in page — and a locator
    # verdict reached on the wrong screen would be meaningless.
    if page_comparison is not None and is_different_page(page_comparison):
        return Verdict("WRONG_STATE",
                       "page identity differs from the last good run",
                       {"mismatches": page_comparison.get("mismatches", [])})

    recorded_marks = baseline["context"].get("landmarks")
    if recorded_marks:
        overlap = _landmark_overlap(recorded_marks, snap["landmarks"])
        if overlap < ct["landmark_overlap_min"]:
            replayed = page_comparison is not None
            # Naming the page we actually reached is the difference between an
            # actionable refusal and a shrug. Pointing at HEALING_LOCATE_STORAGE_STATE was
            # worse than nothing for a test that signs in with credentials and
            # names no session at all — there is no value to put there.
            landed = (snap.get("url") or "").strip()
            where = f" — the replay landed on {landed}" if replayed and landed else ""
            if replayed and _looks_like_sign_in(landed, snap):
                where += (". That is a sign-in page, so the session was not "
                          "accepted; some sites refuse a restored session in a "
                          "fresh browser and can only be replayed by signing in")
            return Verdict(
                "WRONG_STATE",
                (f"the page being examined does not match the recorded good run "
                 f"(landmark overlap {overlap:.2f} < {ct['landmark_overlap_min']})"
                 + where),
                {"expected": recorded_marks[:6], "actual": snap["landmarks"][:6],
                 "landed_url": landed})

    # 3. The locator still resolves — so this is not a locator problem.
    if match_count > 1:
        return Verdict("AMBIGUOUS",
                       f"locator matches {match_count} elements; needs narrowing, not re-finding",
                       {"match_count": match_count})
    if match_count == 1 and matched is not None:
        if not matched["is_visible"]:
            return Verdict("NOT_LOCATOR", "element is in the DOM but not visible")
        if not matched["is_enabled"]:
            return Verdict("NOT_LOCATOR", "element is in the DOM but disabled")
        # The locator resolves — but to the element we recorded, or to a
        # different one that happens to sit where the old one did? A reordered
        # list rebinds a positional locator with no error at all: the test keeps
        # passing while quietly exercising the wrong thing.
        if vol is not None:
            sim, _ = score_pair(base_el, matched, cfg, vol)
            if sim < ct["misbound_max"]:
                return Verdict("MISBOUND",
                               f"locator resolves, but to a different element than "
                               f"recorded (similarity {sim:.2f} < {ct['misbound_max']})",
                               {"similarity": round(sim, 3),
                                "now": (matched.get("accessible_name")
                                        or matched.get("text", ""))[:60],
                                "was": (base_el.get("accessible_name")
                                        or base_el.get("text", ""))[:60]})
        return Verdict("NOT_LOCATOR", "element resolves and is actionable — likely a timing flake")

    # 4. Did the element's whole context disappear with it?
    survival = _neighbour_survival(base_el.get("neighbor_texts"), snap)
    if survival < ct["neighbour_survival_min"]:
        return Verdict("FEATURE_REMOVED",
                       f"element and its context are both gone "
                       f"({survival:.0%} of neighbouring text survives)",
                       {"neighbours": base_el.get("neighbor_texts", [])[:3]})

    # 5. Element gone, page intact, context intact → genuine locator drift.
    return Verdict(DRIFT, "element absent but page and context intact")
