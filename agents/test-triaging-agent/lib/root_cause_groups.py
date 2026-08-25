"""Group failures that share a root cause before classifying them.

Thirty tests failing on one broken locator is one defect wearing thirty hats.
Classifying each separately is not just slower — it is inconsistent: the same
failure can come back HIGH confidence for one test and MEDIUM for another, and
only the HIGH ones reach the healing agent. Three of five siblings get fixed and
the other two stay red for no reason anyone can explain.

Grouping first means one judgement per defect, applied to every test showing it.
The grouping is deliberately conservative: when the evidence is thin, failures
stay in their own group rather than risk inheriting a verdict that does not
apply to them.
"""

import re
from typing import Dict, List

# Anything run-specific must be stripped, or two identical failures look different.
_VOLATILE = [
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I), '<uuid>'),
    (re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*'), '<timestamp>'),
    (re.compile(r'\b0x[0-9a-f]+\b', re.I), '<addr>'),
    (re.compile(r'\b\d{3,}\b'), '<n>'),          # ids, ports, long counts
    (re.compile(r'\bafter \d+ ?m?s\b', re.I), 'after <duration>'),
    (re.compile(r'\s+'), ' '),
]

# Categories where "same message" genuinely means "same defect". An assertion
# failure saying "expected 5 but got 4" can arise from unrelated causes, so those
# are never merged.
_GROUPABLE_CATEGORIES = {"ELEMENT_NOT_FOUND", "TIMEOUT"}


def normalize(text: str) -> str:
    """Strip the run-specific parts of a failure message."""
    result = (text or "").strip().lower()
    for pattern, replacement in _VOLATILE:
        result = pattern.sub(replacement, result)
    return result.strip()


def signature(failure: dict) -> str:
    """A conservative key for 'these two failures are the same defect'.

    Built from the error type plus the normalized message. The locator or element
    name is the discriminating part of an element-not-found message, so it is
    carried implicitly rather than parsed out — parsing it wrong would merge
    failures that are not the same.
    """
    error_type = (failure.get("error_type") or "").strip()
    message = normalize(failure.get("error_message") or "")

    if not message:
        # With no message there is nothing to compare; keep it separate.
        return f"unique:{failure.get('full_name', '')}"

    return f"{error_type}|{message[:400]}"


def group_failures(failures: List[dict]) -> Dict[str, List[dict]]:
    """Map signature → the failures sharing it, insertion-ordered."""
    groups: Dict[str, List[dict]] = {}
    for failure in failures:
        groups.setdefault(signature(failure), []).append(failure)
    return groups


def pick_representative(group: List[dict]) -> dict:
    """The member a classifier should look at: the one with the most evidence."""
    def rank(failure: dict) -> tuple:
        return (
            1 if failure.get("execution_log") else 0,
            1 if failure.get("stack_trace") else 0,
            len(failure.get("error_message") or ""),
        )
    return max(group, key=rank)


def is_groupable(classification: dict) -> bool:
    """Whether one verdict may be shared across a group.

    Only for categories where an identical message really does imply an identical
    cause, and only when the classifier was confident. A LOW-confidence guess
    should not be propagated to tests nobody looked at.
    """
    return (
        classification.get("root_cause_category") in _GROUPABLE_CATEGORIES
        and classification.get("confidence") in ("HIGH", "MEDIUM")
    )
