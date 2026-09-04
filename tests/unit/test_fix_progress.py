"""Whether a fix is kept when the test still fails.

The bug this pins: the healing agent repaired `button[type='submit']`, the login
succeeded, the flow reached a page it had never reached before, and the test then
failed on a *different* element in a *different* page object. The gate was
whole-test pass/fail, so that scored as a failure, the file was reverted, and the
next attempt started over on the locator that was already fixed.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load():
    os.environ.setdefault("AUDIT_DIR", str(ROOT / "tests" / "fixtures"))
    os.environ.setdefault("HANDOFF_FILE", str(ROOT / "tests" / "fixtures" / "none.json"))
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_fix.py"
    spec = importlib.util.spec_from_file_location("healing_fix_progress", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_fix_progress"] = mod
    spec.loader.exec_module(mod)
    return mod


fix = pytest.importorskip("bs4") and _load()

LOGIN_FAILURE = (
    "Failed to click on element 'Login button' with locator: "
    "Locator@button[type='submit']: Error {")
PROFILE_FAILURE = (
    "Failed to click on element 'Edit Profile Summary button' with locator: "
    "Locator@#profile-section-profile-summary img[alt='mukesh']: Error {\n"
    "\tat automation.modules.naukari.web.NaukriProfilePage.edit(NaukriProfilePage.java:20)")


def _member(output, error_message=LOGIN_FAILURE,
            stack="NaukriLoginPage.java:36", name="pkg.SomeTest.aCase"):
    return (name, output,
            {"error_message": error_message, "stack_trace": stack}, 1000.0)


class TestSplitByProgress:
    def test_a_later_element_counts_as_progress(self):
        advanced, unchanged = fix.split_by_progress([_member(PROFILE_FAILURE)])
        assert len(advanced) == 1 and not unchanged
        _name, _out, _member_ctx, _started, before, after = advanced[0]
        assert before["element"] == "Login button"
        assert after["element"] == "Edit Profile Summary button"

    def test_the_same_element_is_not_progress(self):
        advanced, unchanged = fix.split_by_progress([_member(LOGIN_FAILURE)])
        assert not advanced and len(unchanged) == 1

    def test_an_unreadable_failure_is_not_progress(self):
        # Conservative: unknown means revert, which is the pre-existing behaviour.
        advanced, unchanged = fix.split_by_progress([_member("BUILD FAILURE")])
        assert not advanced and len(unchanged) == 1

    def test_members_are_judged_independently(self):
        advanced, unchanged = fix.split_by_progress([
            _member(PROFILE_FAILURE, name="pkg.SomeTest.moved"),
            _member(LOGIN_FAILURE, name="pkg.SomeTest.stuck"),
        ])
        assert [a[0] for a in advanced] == ["pkg.SomeTest.moved"]
        assert [u[0] for u in unchanged] == ["pkg.SomeTest.stuck"]

    def test_nothing_failing_splits_to_nothing(self):
        assert fix.split_by_progress([]) == ([], [])
