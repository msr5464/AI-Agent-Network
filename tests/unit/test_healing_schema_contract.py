"""The dashboards read 01-fix.json. Changing its shape breaks them silently.

`qa_agents_server.analytics` and `qa_agents_server.audit_reader` both consume the
healing agent's output, and neither validates it — a renamed or dropped key
surfaces as a zero on a chart, not as an error. Additions are safe; these tests
pin the keys that are not.

This is a contract test, so it deliberately restates the field names rather than
importing them. A test that derived the names from the writer would agree with
any rename, which is exactly the failure it exists to catch.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa_agents_server import analytics

# Every key the two readers pull out of 01-fix.json.
REQUIRED = ("succeeded", "unverified", "failed", "distinct_fixes", "fixes",
            "attempts", "attempted", "candidates", "unverified_fixes",
            "failed_fixes", "pr_branch")

# The only status values `_adaptation_outcomes` scores. Adding a sixth would be
# counted as neither adapted nor escalated.
KNOWN_STATUSES = {"applied", "partial", "escalated", "declined"}


def _fix_json(**overrides):
    payload = {
        "timestamp": "2026-09-03T04:07:15Z", "build_tag": "local-X",
        "fix_attempt": 1, "eligible_count": 1, "attempted": 1,
        "succeeded": 1, "unverified": 0, "failed": 0,
        "distinct_fixes": 1, "distinct_unverified": 0,
        "pr_branch": "chore/qa-autofix/local-x",
        "candidates": [], "fixes": [{"test_name": "T#m", "status": "applied"}],
        "unverified_fixes": [], "failed_fixes": [],
        "attempts": [{"attempt": 1, "entries": []}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def session(tmp_path):
    def _write(**overrides):
        d = tmp_path / "20260903-093332-fix-X"
        d.mkdir(parents=True, exist_ok=True)
        (d / "01-fix.json").write_text(json.dumps(_fix_json(**overrides)))
        (d / ".fix-passed").write_text("true")
        return d
    return _write


class TestFixJsonContract:
    @pytest.mark.parametrize("field", REQUIRED)
    def test_field_is_present(self, field):
        assert field in _fix_json()

    def test_analytics_reads_the_outcome_counts(self, session):
        out = analytics._healing_outcomes(session())
        assert out == {"tests_fixed": 1, "tests_unverified": 0,
                       "tests_still_failing": 0, "distinct_fixes": 1}

    def test_distinct_fixes_falls_back_to_the_fixes_list(self, session):
        """The reader tolerates a missing count by counting entries."""
        out = analytics._healing_outcomes(session(distinct_fixes=None))
        assert out["distinct_fixes"] == 1

    def test_a_missing_file_is_zeroes_not_an_error(self, tmp_path):
        assert analytics._healing_outcomes(tmp_path)["tests_fixed"] == 0

    @pytest.mark.parametrize("gate,expected", [
        ("true", "completed"), ("false", "failed"), ("skipped", "diagnosed"),
    ])
    def test_the_fix_gate_still_drives_status(self, session, gate, expected):
        d = session()
        (d / ".fix-passed").write_text(gate)
        assert analytics._healing_status(d) == expected

    def test_status_vocabulary_has_not_grown(self, session):
        """A new status value would be silently uncounted by the dashboards."""
        used = {f.get("status") for f in _fix_json()["fixes"] if f.get("status")}
        assert used <= KNOWN_STATUSES


class TestAdditionsAreSafe:
    def test_new_keys_do_not_disturb_the_readers(self, session):
        """anchor_state and the locate route are additions; they must not count."""
        d = session(locate_route="took_ownership", anchor_state="hidden")
        assert analytics._healing_outcomes(d)["tests_fixed"] == 1
        assert analytics._healing_status(d) == "completed"
