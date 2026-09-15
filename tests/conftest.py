"""Shared pytest fixtures.

There was no conftest.py at all, which made the suite quietly dependent on the
developer's own environment: most of shared/ now resolves its behaviour through
the active framework plugin, so a shell with AUTOMATION_FRAMEWORK=selenium
exported turned green tests red in test_page_identity, test_edit_guards,
test_dom_snapshot and test_diagnosis — none of which are about frameworks at all.

Pinning it here makes the default explicit rather than ambient. Tests that are
genuinely about framework selection override it themselves.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def pinned_automation_framework(monkeypatch):
    """Default every test to Playwright unless it says otherwise."""
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", "playwright")
    # FRAMEWORK_DIR would otherwise point plugin resolution at a developer's own
    # checkout, whose build files decide the answer.
    monkeypatch.delenv("FRAMEWORK_DIR", raising=False)
