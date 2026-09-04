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
from datetime import datetime
from pathlib import Path

from shared import properties_file
from typing import Dict, List, Optional

_DIR_ENV = "BASELINE_DIR"
_DEFAULT_DIRNAME = "baselines"

_UUID = re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}")
_NUMERIC = re.compile(r"/\d+")


def url_shape(url: str) -> str:
    """A URL with its variable parts removed, matching the Java writer's rule."""
    without_query = (url or "").split("?")[0].split("#")[0]
    return _NUMERIC.sub("/{id}", _UUID.sub("/{uuid}", without_query))


def framework_property(workspace, key: str) -> str:
    """One setting from the automation framework's own properties.

    Mirrors Config.java's load order: parameters/config.properties is the base and
    the environment-specific file overrides it. Reading the same files the Java
    side reads keeps one source of truth instead of two that drift — which is how
    the framework ended up writing baselines somewhere the agent never looked.
    """
    if not workspace:
        return ""
    root = Path(workspace)
    value = ""
    for path in (root / "parameters" / "config.properties",
                 properties_file.properties_path(root)):
        try:
            found = properties_file.read_values(
                path.read_text(encoding="utf-8", errors="ignore")).get(key, "")
        except OSError:
            continue
        if found:
            value = found          # later file wins, as Config.java does
    return value


def _framework_configured(workspace) -> Optional[Path]:
    """Where the automation framework itself says baselines live.

    Without this the two halves disagree silently: the framework writes into the
    repo while the agent reads test-output, which usually exists from older runs
    — so the answer comes back as "no baseline recorded" rather than as the
    misconfiguration it is.
    """
    value = framework_property(workspace, "baselineDir")
    if not value:
        return None
    configured = Path(value)
    return configured if configured.is_absolute() else Path(workspace) / configured


def directory(workspace=None, results_dirname: str = "test-output",
              preserved: Optional[str] = None) -> Optional[Path]:
    """Where baselines live.

    A directory preserved into the audit session wins: it was copied at the time
    of the failure and survives CI deleting the report directory afterwards. The
    live workspace is the fallback for local runs.
    """
    if preserved and Path(preserved).exists():
        return Path(preserved)
    override = os.environ.get(_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if not workspace:
        return None
    configured = _framework_configured(workspace)
    if configured is not None:
        return configured
    return Path(workspace) / results_dirname / _DEFAULT_DIRNAME


def load(page_object: str, workspace=None, preserved: Optional[str] = None,
         not_after: str = "") -> Dict:
    """The recorded fingerprint for one page object, if there is one.

    `not_after` is the moment the failure was captured. A baseline stamped at or
    after it was written by the failing run itself — or by a diagnosis probe
    re-running it minutes later — and records the broken page under the name of
    the last good one. Every rule that compares against it then confirms the
    breakage instead of contradicting it.
    """
    result: Dict = {"available": False, "page_object": page_object,
                    "url_shape": "", "title": "", "body_class": "", "coverage": {},
                    "fingerprints": {}, "landmarks": [], "rejected": ""}
    if not page_object:
        return result
    folder = directory(workspace, preserved=preserved)
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
    recorded_at = data.get("recordedAt", "")
    if not _is_older(recorded_at, not_after):
        result["rejected"] = (f"{page_object} was recorded at {recorded_at}, "
                              f"not before {not_after}")
        return result
    result.update({
        "available": True,
        "url_shape": data.get("urlShape", ""),
        "title": data.get("title", ""),
        "body_class": data.get("bodyClass", ""),
        "coverage": data.get("coverage") or {},
        # What each locator actually matched, not just how many did. Counts answer
        # "did this still resolve last time"; only the fingerprint can answer "what
        # did it resolve to", which is what a renamed or moved element needs.
        # Absent for baselines recorded before fingerprinting, hence the default.
        "fingerprints": data.get("fingerprints") or {},
        # Headings and landmark roles from the good run. Answers "are we on the
        # right screen" without depending on a URL, which a redirect leaves intact.
        "landmarks": data.get("landmarks") or [],
        "recorded_at": data.get("recordedAt", ""),
    })
    return result


_TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def _timestamp(value: str) -> Optional[datetime]:
    text = (value or "").strip().rstrip("Z")
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _is_older(recorded_at: str, not_after: str) -> bool:
    """Whether this baseline predates the failure. Unknown timestamps pass.

    An older framework writes no `recordedAt`, and a caller that does not know
    when the failure happened cannot ask the question — neither is a reason to
    discard the only baseline there is.
    """
    recorded = _timestamp(recorded_at)
    failed = _timestamp(not_after)
    if not recorded or not failed:
        return True
    return recorded < failed


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
