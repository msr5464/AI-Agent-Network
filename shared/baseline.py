"""Compare a failing page against what it looked like when the test last passed.

Coverage ratios answer relatively — this page object matches less of itself than
that one does — and a page object with one generic selector can score well on
almost any page. A baseline makes the question absolute: the title, the URL shape,
the body classes and the per-locator counts recorded on a successful run, against
the same measurements taken at failure.

Written by `automation.core.Baseline` on every successful page load. Absent
baselines are the normal case early on and in CI layouts that discard
`test-output/` between builds, so nothing here may require one — a missing
baseline lowers confidence, it never blocks a diagnosis.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

_DIR_ENV = "BASELINE_DIR"
_DEFAULT_DIRNAME = "baselines"

_UUID = re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}")
_NUMERIC = re.compile(r"/\d+")


def url_shape(url: str) -> str:
    """A URL with its variable parts removed, matching the Java writer's rule."""
    without_query = (url or "").split("?")[0].split("#")[0]
    return _NUMERIC.sub("/{id}", _UUID.sub("/{uuid}", without_query))


def directory(workspace=None, results_dirname: str = "test-output") -> Optional[Path]:
    """Where baselines live: the env override, else beside the run's results."""
    override = os.environ.get(_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if not workspace:
        return None
    return Path(workspace) / results_dirname / _DEFAULT_DIRNAME


def load(page_object: str, workspace=None) -> Dict:
    """The recorded fingerprint for one page object, if there is one."""
    result: Dict = {"available": False, "page_object": page_object,
                    "url_shape": "", "title": "", "body_class": "", "coverage": {}}
    if not page_object:
        return result
    folder = directory(workspace)
    if not folder:
        return result
    path = folder / f"{page_object}.json"
    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return result
    if not isinstance(data, dict):
        return result
    result.update({
        "available": True,
        "url_shape": data.get("urlShape", ""),
        "title": data.get("title", ""),
        "body_class": data.get("bodyClass", ""),
        "coverage": data.get("coverage") or {},
        "recorded_at": data.get("recordedAt", ""),
    })
    return result


def _class_tokens(value) -> set:
    if isinstance(value, (list, tuple)):
        return {str(token) for token in value}
    return {token for token in str(value or "").split() if token}


def diff(base: Dict, facts: Dict, live_coverage: Optional[Dict] = None) -> Dict:
    """What changed between the recorded good page and the observed failing one.

    `mismatches` holds only differences that indicate a *different page*, not a
    different state of the same one. A changed title with identical locator
    coverage is a copy edit; a body class that gained `logged-out` while every
    locator vanished is a different page.
    """
    result: Dict = {"available": False, "mismatches": [], "matches": []}
    if not base.get("available"):
        return result
    result["available"] = True

    observed_shape = url_shape(facts.get("url", ""))
    if base.get("url_shape") and observed_shape:
        if base["url_shape"] == observed_shape:
            result["matches"].append("url shape")
        else:
            result["mismatches"].append(
                f"url shape {base['url_shape']} -> {observed_shape}")

    base_classes = _class_tokens(base.get("body_class"))
    seen_classes = _class_tokens(facts.get("body_class"))
    if base_classes or seen_classes:
        gained = sorted(seen_classes - base_classes)
        lost = sorted(base_classes - seen_classes)
        if gained or lost:
            parts = []
            if lost:
                parts.append("lost " + ", ".join(lost[:3]))
            if gained:
                parts.append("gained " + ", ".join(gained[:3]))
            result["mismatches"].append("body class " + "; ".join(parts))
        else:
            result["matches"].append("body class")

    if base.get("title") and facts.get("title"):
        if base["title"] == facts["title"]:
            result["matches"].append("title")
        else:
            result["mismatches"].append(
                f"title {base['title'][:40]!r} -> {facts['title'][:40]!r}")

    # The strongest comparison available: elements that were present on a good run
    # and are gone now, named individually.
    if live_coverage and base.get("coverage"):
        details = live_coverage.get("details") or {}
        vanished = [name for name, count in (base["coverage"] or {}).items()
                    if isinstance(count, int) and count > 0
                    and isinstance(details.get(name), int) and details[name] == 0]
        present = [name for name, count in details.items()
                   if isinstance(count, int) and count > 0]
        if vanished:
            result["mismatches"].append(
                f"{len(vanished)} locator(s) present on the last good run are now "
                f"absent: {', '.join(vanished[:4])}")
        if present:
            result["matches"].append(f"{len(present)} locator(s) still present")
        result["vanished"] = vanished
        result["still_present"] = present

    return result


def is_different_page(comparison: Dict) -> bool:
    """Whether the diff describes a different page rather than a changed one.

    Requires corroboration: a single mismatch is a page that was edited, while
    identity signals disagreeing *and* every locator having vanished is a page the
    test never reached.
    """
    if not comparison.get("available"):
        return False
    vanished = comparison.get("vanished")
    still_present = comparison.get("still_present")
    if vanished is not None and still_present is not None:
        if vanished and not still_present:
            return True
        if still_present:
            return False
    return len(comparison.get("mismatches") or []) >= 2
