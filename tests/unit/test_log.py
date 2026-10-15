"""Tests for shared/log.py."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from shared import log as log_mod  # noqa: E402


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


class TestSeverity:
    """One vocabulary, shared by the terminal here and the Studio console."""

    @pytest.mark.parametrize("message", [
        "ERROR: Login step detected but no credentials found in input file.",
        "error: lower case is the same line",
        "FATAL: out of budget",
        "Failed steps:",
        log_mod.blocked("push rejected", "no PR will be raised"),
    ])
    def test_error_lines(self, message):
        assert log_mod.severity(message) == "error"

    @pytest.mark.parametrize("message", ["WARNING: retrying", "Warning: retrying"])
    def test_warning_lines(self, message):
        assert log_mod.severity(message) == "warning"

    @pytest.mark.parametrize("message", [
        "Plan: NaukriProfileSummary | type=web",
        "the fix guard rejected a WARNING annotation",   # marker, but not leading
        "",
    ])
    def test_everything_else_is_info(self, message):
        assert log_mod.severity(message) == "info"

    def test_a_multi_line_message_is_classified_by_its_first_line(self):
        assert log_mod.severity("ERROR: no credentials\n  add Username:/Password:") == "error"


class TestColour:
    """Colour is for terminals. Agent stdout is a pipe under the server, and an
    escape sequence there would be literal junk in the console and stdout.log."""

    def _run(self, script: str, env: dict) -> str:
        return subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n{script}"],
            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env},
        ).stdout

    def test_a_piped_run_stays_plain_text(self):
        out = self._run("from shared.log import log; log('02-validate-web', 'ERROR: boom')", {})
        assert out == "" or "\033" not in out
        assert "\x1b" not in out and "ERROR: boom" in out

    def test_forced_colour_wraps_an_error_line_in_red(self):
        out = self._run("from shared.log import log; log('02-validate-web', 'ERROR: boom')",
                        {"QA_LOG_COLOR": "always"})
        assert out.startswith("\x1b[1;31m") and out.rstrip("\n").endswith("\x1b[0m")

    def test_an_ordinary_line_is_never_wrapped(self):
        out = self._run("from shared.log import log; log('01-parse', 'Plan: ready')",
                        {"QA_LOG_COLOR": "always"})
        assert "\x1b" not in out

    def test_no_color_disables_it(self):
        out = self._run("from shared.log import log; log('01-parse', 'ERROR: boom')",
                        {"NO_COLOR": "1", "QA_LOG_COLOR": "auto"})
        assert "\x1b" not in out

    def test_every_line_of_a_multi_line_error_is_highlighted(self):
        """The remedy printed under an error is part of the error, not an
        uncoloured orphan below it."""
        out = self._run(
            "from shared.log import log; log('02-validate-web', 'ERROR: boom\\n  add Username:')",
            {"QA_LOG_COLOR": "always"})
        lines = out.rstrip("\n").split("\n")
        assert len(lines) == 2
        assert all(l.startswith("\x1b[1;31m") and l.endswith("\x1b[0m") for l in lines)


class TestBashParity:
    """shared/session.sh classifies the same words the same way — an ERROR line
    from run.sh and one from a Python action must look identical."""

    def _bash_log(self, message: str, env_prefix: str = "QA_LOG_COLOR=always") -> str:
        script = f'source "{REPO_ROOT}/shared/session.sh"; {env_prefix} log "{message}"'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout

    def test_error_is_the_same_red(self):
        assert self._bash_log("ERROR: boom").startswith("\x1b[1;31m")

    def test_warning_is_the_same_yellow(self):
        assert self._bash_log("WARNING: carried on").startswith("\x1b[0;33m")

    def test_an_ordinary_line_is_never_wrapped(self):
        assert "\x1b" not in self._bash_log("Parse complete")

    def test_a_piped_run_stays_plain_text(self):
        assert "\x1b" not in self._bash_log("ERROR: boom", env_prefix="")
