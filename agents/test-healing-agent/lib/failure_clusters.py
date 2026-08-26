"""Group failing tests by the locator that actually broke.

A single broken locator usually fails several tests: every journey that passes
through that page dies at the same element. Fixing them one test at a time means
N Claude calls and N edits for one underlying defect — and worse, only the first
one works. The rest arrive to find the file already corrected, fail to apply
their edit, and get reported as "needs manual fix" while their fix is sitting in
the same pull request.

Clustering turns "30 failing tests" into "6 broken locators", which is both the
honest unit of work and the one worth spending a model call on.
"""

import re
from typing import Dict, List, Optional

# Words that carry no signal when comparing two element names.
_NOISE = re.compile(r'[^a-z0-9]+')


def _normalize(text: str) -> str:
    """Reduce a name or selector to a comparable form."""
    return _NOISE.sub("", (text or "").lower())


def _element_key(context: dict) -> str:
    """The most specific identifier available for the element that broke."""
    # The selector the runtime actually used is the strongest signal — it comes
    # from the trace, not from a human-written description.
    if context.get("failed_selector"):
        return f"selector:{_normalize(context['failed_selector'])}"

    names = context.get("element_names") or []
    if names:
        # element_names is ranked; the first entry is the most specific form,
        # usually "SomePage:Element Name".
        return f"element:{_normalize(names[0].split(':')[-1])}"

    signature = context.get("failure_signature") or ""
    if signature:
        return f"signature:{_normalize(signature)}"

    # Last resort before giving up: the grouping triage already worked out from
    # the raw error text.
    triage_key = context.get("cause_group_key") or ""
    if triage_key:
        return f"triage:{_normalize(triage_key)}"
    return ""


def cluster_key(context: dict) -> str:
    """The key two failures must share to be one unit of work.

    Pairs the file that has to change with the element inside it. Two tests
    failing on the same element of the same page object are one fix; the same
    element name in two different page objects is not.

    A diagnosis changes what "unit of work" means. Grouping on (file, element) is
    right for locator breaks, and wrong for everything else: thirty tests failing
    because the environment is down are one cause but thirty different elements,
    so they would each consume a slot and then be truncated into "deferred" by
    AUTO_FIX_MAX_FIXES_PER_RUN. Non-locator verdicts therefore group on the cause
    itself, and are reported once.
    """
    verdict = (context.get("diagnosis") or {}).get("verdict") or ""
    if verdict and verdict not in ("LOCATOR_STALE", "INSUFFICIENT_EVIDENCE"):
        return f"cause:{verdict}"

    page_objects = context.get("page_objects") or []
    target = page_objects[0]["path"] if page_objects else ""
    element = _element_key(context)

    if target and element:
        return f"{target}::{element}"
    if element:
        return element
    # Nothing to group on — keep it as its own cluster rather than risk merging
    # unrelated failures into one edit.
    return f"test:{context.get('test_name', '')}"


def evidence_rank(context: dict) -> tuple:
    """How well-evidenced a failure is, for picking a cluster's representative.

    The representative is the member whose context gets sent to the model, so it
    should be the one that can see the most: a live browser or captured DOM beats
    a trace, which beats matched page objects, which beats nothing.
    """
    return (
        # A member that already carries a diagnosis can see why the element was
        # missing, not just that it was. That outranks every raw artefact.
        1 if (context.get("diagnosis") or {}).get("verdict") else 0,
        # dom_snapshot_path is known during Phase A; dom_snapshot only after
        # grounding — check both so ranking works before a cluster is fixed.
        1 if (context.get("dom_snapshot") or context.get("dom_snapshot_path")) else 0,
        1 if context.get("trace_timeline") else 0,
        1 if context.get("failed_selector") else 0,
        len(context.get("page_objects") or []),
        len(context.get("element_names") or []),
        1 if context.get("test_method_code") else 0,
    )


class Cluster:
    """One broken locator and every test that trips over it."""

    def __init__(self, key: str, contexts: List[dict], issues: List[dict]):
        self.key = key
        self.contexts = contexts
        self.issues = issues
        # Sending the best-evidenced member means the model sees a DOM snapshot
        # if ANY of these tests captured one.
        self.representative = max(contexts, key=evidence_rank)

    @property
    def test_names(self) -> List[str]:
        return [c.get("test_name", "") for c in self.contexts]

    @property
    def size(self) -> int:
        return len(self.contexts)

    def merged_element_names(self) -> List[str]:
        """Every element name any member reported, best-ranked first, deduped."""
        merged: List[str] = []
        for context in sorted(self.contexts, key=evidence_rank, reverse=True):
            for name in context.get("element_names") or []:
                if name not in merged:
                    merged.append(name)
        return merged[:10]

    def describe(self) -> str:
        target = self.representative.get("page_objects") or []
        where = target[0]["path"] if target else "unknown file"
        element = (self.merged_element_names() or ["unknown element"])[0]
        return f"{element} in {where}"


def build_clusters(contexts: List[dict], issues: List[dict]) -> List[Cluster]:
    """Group parallel context/issue lists into clusters, largest first.

    Ordering by size means that when a run is capped, the fixes that unblock the
    most tests are the ones that get made.
    """
    grouped: Dict[str, List[int]] = {}
    for index, context in enumerate(contexts):
        grouped.setdefault(cluster_key(context), []).append(index)

    clusters = [
        Cluster(key, [contexts[i] for i in indexes], [issues[i] for i in indexes])
        for key, indexes in grouped.items()
    ]
    # Biggest blast radius first, then by evidence quality so a well-evidenced
    # single-test fix outranks a poorly-evidenced one of the same size.
    clusters.sort(key=lambda c: (c.size, evidence_rank(c.representative)), reverse=True)
    return clusters
