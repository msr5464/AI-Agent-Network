"""Telling "the run failed" apart from "the run finished" — twice over.

Both signals were found by one real adaptation run. The CLI answered a usage cap
with prose on stdout and exit code 0, so the adapt step reported "could not parse
the model's response as JSON" for every change item; and the step wrote a valid
04-adapt.json in which every item had failed, which the server read as a healthy
step and the UI drew green.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest                                                    # noqa: E402

from shared.claude import ClaudeResult, usage_limit              # noqa: E402
from qa_agents_server.audit_reader import (_step_has_error,      # noqa: E402
                                           _step_status)


class TestUsageLimit:
    @pytest.mark.parametrize("text", [
        "You've hit your session limit · resets 11:50am (Asia/Calcutta)",
        "You have hit your usage limit, resets at 4pm",
    ])
    def test_a_cap_is_recognised(self, text):
        assert usage_limit(text) == text.strip()

    @pytest.mark.parametrize("text", [
        '{"adaptable": true, "edits": []}',
        "",
        "   ",
    ])
    def test_an_answer_is_not_a_cap(self, text):
        assert usage_limit(text) == ""

    @pytest.mark.parametrize("text", [
        '{"adaptable": false, "unadaptable_reason": "the rate limit resets hourly"}',
        '[{"limit": "resets at noon"}]',
    ])
    def test_a_json_answer_is_never_a_cap(self, text):
        # Short, and inside both word tests — but it is the model answering.
        assert usage_limit(text) == ""

    def test_a_multi_line_answer_is_never_a_cap(self):
        assert usage_limit("The limit resets hourly.\nSo the test should retry.") == ""

    def test_a_long_answer_that_discusses_limits_is_not_a_cap(self):
        # The guard is length-bounded precisely so a test about rate limiting
        # does not read as the account being out of budget.
        prose = ("The endpoint has a rate limit of 50 requests per second which "
                 "resets every second, so the test asserts the retry backs off. ") * 4
        assert usage_limit(prose) == ""


class TestResultStatus:
    """Callers read the status, never the text. The text wrapper returns "" for
    every non-ok status, so a capped call looked exactly like a model that
    answered with something unparseable."""

    def _result(self, status, stdout=""):
        return ClaudeResult(stdout=stdout, stderr="", returncode=0, status=status,
                            timed_out=False, duration_s=0.6)

    def test_a_cap_describes_itself_as_a_cap(self):
        capped = self._result(
            "usage_limit", "You've hit your session limit · resets 1:40pm (Asia/Calcutta)")
        described = capped.describe()
        assert "usage cap" in described and "1:40pm" in described

    def test_a_cap_is_not_ok(self):
        assert self._result("usage_limit", "hit your limit, resets 1pm").status != "ok"

    def test_a_cap_mid_run_that_exits_1_is_still_a_cap(self):
        # Observed: six turns in, the CLI exited 1 with the cap only in the final
        # result event, and the log blamed a stderr trust warning instead.
        import io, json
        from unittest import mock
        from shared.claude import call_claude_ex
        cap = "You've hit your session limit · resets 2:20am (Asia/Calcutta)"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Reading the test file first."}]}},
            {"type": "result", "is_error": True, "api_error_status": 429,
             "result": cap},
        ]
        with mock.patch("shared.claude.subprocess.Popen") as popen:
            popen.return_value.stdout = io.StringIO(
                "".join(json.dumps(e) + "\n" for e in events))
            popen.return_value.stderr = io.StringIO(
                "Ignoring 3 permissions.allow entries from .claude/settings.json\n")
            popen.return_value.wait.return_value = 1
            popen.return_value.poll.return_value = 1
            popen.return_value.returncode = 1
            result = call_claude_ex(prompt="p", model="m", cwd=".", timeout=5,
                                    stream_json=True)
        assert result.status == "usage_limit"
        assert "2:20am" in result.describe()


class TestStepHasError:
    def test_an_adapt_step_whose_items_all_failed_is_a_failure(self):
        data = {"attempt": 1, "applied_mode": False, "items": [
            {"index": 1, "kind": "field_added", "status": "failed",
             "reason": "could not parse the model's response as JSON"},
            {"index": 2, "kind": "coverage_added", "status": "failed"},
        ]}
        assert _step_has_error(data) is True

    def test_a_deliberate_escalation_is_not_a_failure(self):
        # Escalating is the design working — it must stay distinguishable from
        # the step falling over.
        data = {"items": [{"index": 1, "status": "escalated", "reason": "outcome_changed"},
                          {"index": 2, "status": "declined"}]}
        assert _step_has_error(data) is False

    def test_a_change_notes_items_are_not_step_results(self):
        # 01-parse-change.json also has an "items" list, with no status at all.
        data = {"module": "SauceDemo", "items": [
            {"index": 1, "kind": "coverage_added", "escalate_only": False}]}
        assert _step_has_error(data) is False


class TestARetriedStepHasNoOutcomeYet:
    """A step run.sh will retry is not finished, so it has nothing to report.

    The bug this pins: 01_fix.py rewrites 01-fix.json after every attempt, and a
    mid-run attempt legitimately ends with the gate false — some tests repaired,
    the rest still being worked on. The UI read that file the moment it changed,
    saw `fix_gate: "false"`, and painted the Fix step red while attempt 2 was
    starting. Nothing had failed; the step had not finished.
    """

    def test_a_failing_gate_mid_retry_reports_running(self):
        data = {"fix_gate": "false", "final_attempt": False, "failed": 2}
        assert _step_status(data) == "running"
        assert _step_has_error(data) is True, \
            "the gate really is false — it is the timing that makes it unreportable"

    def test_the_last_attempt_still_reports_failed(self):
        data = {"fix_gate": "false", "final_attempt": True, "failed": 2}
        assert _step_status(data) == "failed"

    def test_a_gate_that_passed_is_final_even_mid_loop(self):
        # run.sh stops retrying the moment the gate passes, so this IS the last
        # attempt however many were allowed.
        data = {"fix_gate": "true", "final_attempt": True}
        assert _step_status(data) == "done"

    def test_a_snapshot_left_by_an_exited_process_is_final(self):
        # Cancelled or crashed between attempts: no retry is coming, so the
        # post-exit sweep and replay must not leave the chip spinning.
        data = {"status": "ok", "final_attempt": False}
        assert _step_status(data, finished=True) == "done"
        assert _step_status({"fix_gate": "false", "final_attempt": False},
                            finished=True) == "failed"

    def test_a_capped_validate_web_is_red(self):
        # 02_validate_web.py's usage-cap write; it used to be "skipped" (grey)
        # blamed on missing MCP tools.
        assert _step_status({"status": "error", "skipped": False}) == "failed"

    def test_a_step_that_never_retries_is_unaffected(self):
        # Only 01-fix.json writes final_attempt; every other step's file must
        # keep being judged the moment it appears.
        assert _step_status({"ship_status": "push_failed"}) == "failed"
        assert _step_status({"status": "skipped"}) == "skipped"
        assert _step_status({"resolutions": []}) == "done"
