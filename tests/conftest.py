"""Shared pytest fixtures.

There was no conftest.py at all, which made the suite quietly dependent on the
developer's own environment: most of shared/ now resolves its behaviour through
the active framework plugin, so a shell with AUTOMATION_FRAMEWORK=selenium
exported turned green tests red in test_page_identity, test_edit_guards,
test_dom_snapshot and test_diagnosis — none of which are about frameworks at all.

Pinning it here makes the default explicit rather than ambient. Tests that are
genuinely about framework selection override it themselves.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Agent steps read these into module constants at import. A test module that sets
# them with os.environ at import leaks them into every later test — and a later
# metrics or analytics write lands wherever they point (it used to be
# tests/fixtures). Set them only around the import: mock.patch.dict(os.environ, ...).
_IMPORT_TIME_ENV = ("AUDIT_DIR", "HANDOFF_FILE")
_ENV_AT_START = {k: os.environ.get(k) for k in _IMPORT_TIME_ENV}


def pytest_collection_finish(session):
    leaked = [k for k in _IMPORT_TIME_ENV if os.environ.get(k) != _ENV_AT_START[k]]
    if leaked:
        raise pytest.UsageError(
            f"A test module changed {', '.join(leaked)} at import and left it set; "
            "wrap the import in mock.patch.dict(os.environ, ...) instead.")


@pytest.fixture(autouse=True)
def pinned_automation_framework(monkeypatch):
    """Default every test to Playwright unless it says otherwise."""
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", "playwright")
    # FRAMEWORK_DIR would otherwise point plugin resolution at a developer's own
    # checkout, whose build files decide the answer.
    monkeypatch.delenv("FRAMEWORK_DIR", raising=False)


@pytest.fixture(autouse=True)
def isolated_run_analytics(monkeypatch, tmp_path):
    """Keep analytics rows out of the real store.

    Any test that runs an agent's run.sh fires shared/session.sh's EXIT trap,
    which appends a row to qa_agents_server/storage/run_analytics.jsonl — the
    file the Studio's Analytics page reads. Without this, every `make test`
    added rows there (agent `pytest-NNN`, or a blank agent for AUDIT_DIR=/tmp).
    The variable is inherited by the subprocesses those tests start.
    """
    monkeypatch.setenv("RUN_ANALYTICS_FILE", str(tmp_path / "run_analytics.jsonl"))
