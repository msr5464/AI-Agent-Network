"""Tests for shared/log.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import log as log_mod


class TestBlocked:
    """The prefix is a contract with the Studio console, not decoration."""

    def test_carries_the_prefix_the_console_matches_on(self):
        line = log_mod.blocked("push rejected", "no PR will be raised")
        assert line.startswith("BLOCKED:")

    def test_says_what_broke_and_what_it_costs(self):
        line = log_mod.blocked("push rejected", "no PR will be raised")
        assert "push rejected" in line and "no PR will be raised" in line

    def test_the_remedy_is_optional(self):
        assert "fix:" not in log_mod.blocked("a", "b")
        assert "fix: git status" in log_mod.blocked("a", "b", "git status")

    def test_survives_the_agent_timestamp_prefix(self):
        # The console strips "[HH:MM:SS] [step] " before matching, so the marker
        # has to be the first thing in the message the agent passes to log().
        message = log_mod.blocked("push rejected", "no PR will be raised")
        rendered = f"[15:52:54] [fix] {message}"
        bare = rendered.split("] ", 2)[-1]
        assert bare.startswith("BLOCKED:")

    def test_adds_no_markup_of_its_own(self):
        # The console builds text nodes from subprocess output rather than
        # markup; styling comes from classifying the line, so markup added here
        # would only show up as literal angle brackets.
        line = log_mod.blocked("push rejected", "no PR will be raised", "git status")
        assert "<" not in line and ">" not in line

    def test_passes_caller_text_through_unchanged(self):
        # git's own error text can contain angle brackets ("<branch>"); the
        # console renders the line as text either way, so nothing is escaped or
        # dropped on the way out.
        line = log_mod.blocked("cannot push <branch>", "no PR will be raised")
        assert "cannot push <branch>" in line
