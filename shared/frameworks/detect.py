"""Work out which automation framework a target repository actually uses.

Framework is a property of the repository, not a preference. A repo either is or
is not a Playwright repo; a person choosing it from a dropdown can only ever be
redundant or, as happened here, wrong — config/.env said `selenium` while the
target repo was Playwright-Java, which silently emptied locator extraction,
rejected every trace, and disabled ambiguous-locator diagnosis without one error
message. Reading the answer off the repo removes that entire class of bug, and
with it the need for a UI dropdown, per-run plumbing and a process-wide cache.

Order of precedence, most authoritative first:

  1. AUTOMATION_FRAMEWORK — an explicit override, for debugging and for repos
     whose build files cannot be read.
  2. The repo's own build files — pom.xml, build.gradle, package.json.
  3. config/repo-map.json's `framework` string.
  4. Playwright, as the historical default.

Detection and declaration disagreeing is worth saying out loud rather than
quietly picking one: that disagreement is the bug, not a preference to resolve.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple

PLAYWRIGHT = "playwright"
SELENIUM = "selenium"
SUPPORTED = (PLAYWRIGHT, SELENIUM)

# Ordered: a repo can depend on both (Appium pulls in selenium-remote-driver
# while the tests are written in Playwright), so the winner is whichever the
# TESTS are written against, and a direct Playwright dependency settles it.
_BUILD_MARKERS = (
    (PLAYWRIGHT, re.compile(r"com\.microsoft\.playwright|@playwright/test|playwright-java")),
    (SELENIUM, re.compile(r"selenium-java|org\.openqa\.selenium|selenium-webdriver")),
)

_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json")


def detect_from_repo(workspace) -> Optional[str]:
    """The framework a checkout's build files declare, or None when unreadable."""
    if not workspace:
        return None
    root = Path(workspace)
    if not root.is_dir():
        return None
    for filename in _BUILD_FILES:
        path = root / filename
        if not path.is_file():
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue
        for framework, pattern in _BUILD_MARKERS:
            if pattern.search(content):
                return framework
    return None


def declared_in_repo_map(repo_name: str = "") -> Optional[str]:
    """What config/repo-map.json claims, normalised. None when it says nothing."""
    try:
        from shared.repo_config import load_repo_config
        declared = (load_repo_config(repo_name or None).get("framework") or "").lower()
    except Exception:
        return None
    # The field is prose ("TestNG + Selenium"), not an enum.
    for framework in SUPPORTED:
        if framework in declared:
            return framework
    return None


def resolve(workspace=None, repo_name: str = "") -> Tuple[str, str]:
    """Return (framework, why). Never raises — a bad value falls back loudly."""
    override = (os.environ.get("AUTOMATION_FRAMEWORK") or "").strip().lower()
    detected = detect_from_repo(workspace)
    declared = declared_in_repo_map(repo_name)

    if override:
        if override not in SUPPORTED:
            print(f"[frameworks] AUTOMATION_FRAMEWORK={override!r} is not one of "
                  f"{SUPPORTED}; falling back to detection")
        else:
            if detected and detected != override:
                # Loud, because this is exactly the misconfiguration that made
                # every locator and trace silently unreadable.
                print(f"[frameworks] WARNING: AUTOMATION_FRAMEWORK={override} but "
                      f"{workspace} looks like a {detected} repo. Using {override}; "
                      f"unset AUTOMATION_FRAMEWORK to use what the repo says.")
            return override, "AUTOMATION_FRAMEWORK"

    if detected:
        if declared and declared != detected:
            print(f"[frameworks] note: repo-map.json says {declared!r} but "
                  f"{workspace} builds against {detected}; trusting the build files")
        return detected, "build files"

    if declared:
        return declared, "repo-map.json"

    return PLAYWRIGHT, "default"
