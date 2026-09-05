"""Whether the next fix attempt can bring anything new.

The bug this pins: step 04 burned four attempts on one failing assertion. Attempt 1 was
rejected by `no_selector_broadening`; attempt 2's prompt never mentioned it, because
`fix_rejections` was recorded and never read back. And `04-run-and-fix.json` is overwritten
each attempt, so attempt 3 could not see attempt 1 at all and was free to re-propose it.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import fix_history as fh


def _rejected(attempt, guard, file="P.java", proposed=None):
    return fh.record(attempt, root_cause=f"rc{attempt}", proposed=proposed or [],
                     rejections=[{"file": file, "reason": f"{guard}: it broadens things"}],
                     outcome=fh.ALL_REJECTED)


def _applied(attempt, proposed=None, location="T.java:45"):
    return fh.record(attempt, root_cause=f"rc{attempt}", proposed=proposed or [],
                     applied=["P.java"], outcome=fh.FAILED, failure_location=location)


class TestExhausted:

    def test_no_history_is_not_exhausted(self):
        assert fh.exhausted([], None) == (False, "")

    def test_no_edits_stops_immediately(self):
        """An explicit 'I cannot fix this' is an answer, not a failed attempt."""
        current = fh.record(1, root_cause="framework bug", outcome=fh.NO_EDITS)
        stop, why = fh.exhausted([], current)
        assert stop and "no edits" in why

    def test_first_rejection_still_earns_a_retry(self):
        """The model had not yet been told why it was blocked."""
        stop, _ = fh.exhausted([], _rejected(1, "no_selector_broadening"))
        assert not stop

    def test_same_guard_twice_running_stops(self):
        history = [_rejected(1, "no_selector_broadening")]
        stop, why = fh.exhausted(history, _rejected(2, "no_selector_broadening"))
        assert stop and "no_selector_broadening" in why

    def test_a_different_guard_is_still_progress(self):
        history = [_rejected(1, "no_selector_broadening")]
        stop, _ = fh.exhausted(history, _rejected(2, "no_hardcoded_url"))
        assert not stop

    def test_a_rejection_after_an_applied_attempt_is_not_two_running(self):
        history = [_rejected(1, "no_selector_broadening"), _applied(2)]
        stop, _ = fh.exhausted(history, _rejected(3, "no_selector_broadening"))
        assert not stop

    def test_repeating_an_earlier_proposal_stops(self):
        edits = fh.fingerprint(None, {"P.java": [{"old_string": "a", "new_string": "b"}]})
        history = [_applied(1, proposed=edits)]
        stop, why = fh.exhausted(history, _applied(2, proposed=edits, location="T.java:99"))
        assert stop and "already made" in why

    def test_one_new_edit_is_enough_to_continue(self):
        first = fh.fingerprint(None, {"P.java": [{"old_string": "a", "new_string": "b"}]})
        second = first + fh.fingerprint(None, {"Q.java": [{"old_string": "c", "new_string": "d"}]})
        stop, _ = fh.exhausted([_applied(1, proposed=first)], _applied(2, proposed=second))
        assert not stop

    def test_a_passing_attempt_never_stops_the_loop_as_exhausted(self):
        edits = fh.fingerprint(None, {"P.java": [{"old_string": "a", "new_string": "b"}]})
        current = fh.record(2, proposed=edits, applied=["P.java"], outcome=fh.PASSED)
        assert fh.exhausted([_applied(1, proposed=edits)], current) == (False, "")


class TestFingerprint:

    def test_whole_file_and_edit_for_one_path_are_not_double_counted(self):
        """apply_fix prefers edits over a whole-file replacement for the same path."""
        out = fh.fingerprint({"P.java": "whole"}, {"P.java": [{"old_string": "a", "new_string": "b"}]})
        assert len(out) == 1 and out[0]["old"] != ""

    def test_different_replacement_text_is_a_different_proposal(self):
        a = fh.fingerprint(None, {"P.java": [{"old_string": "x", "new_string": "y"}]})
        b = fh.fingerprint(None, {"P.java": [{"old_string": "x", "new_string": "z"}]})
        assert a != b


class TestRender:

    def test_a_rejection_reaches_the_prompt_with_its_guard(self):
        """The whole point: attempt 2 must be told why attempt 1 was blocked."""
        out = fh.render([_rejected(1, "no_selector_broadening")])
        assert "no_selector_broadening" in out
        assert "NOTHING reached disk" in out
        assert "rc1" in out

    def test_empty_history_renders_nothing(self):
        assert fh.render([]) == ""

    def test_an_applied_attempt_is_not_described_as_rejected(self):
        out = fh.render([_applied(1)])
        assert "applied and the test still failed" in out


class TestPersistence:

    def test_append_accumulates_rather_than_overwriting(self, tmp_path):
        for n in (1, 2, 3):
            fh.append(tmp_path, _applied(n))
        history = fh.load(tmp_path)
        assert [e["attempt"] for e in history] == [1, 2, 3]

    def test_unreadable_history_degrades_to_empty(self, tmp_path):
        (tmp_path / fh.HISTORY_FILE).write_text("{not json")
        assert fh.load(tmp_path) == []

    def test_missing_history_is_empty(self, tmp_path):
        assert fh.load(tmp_path) == []
