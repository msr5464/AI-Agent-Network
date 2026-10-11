"""Which selector a failure is filed under when no trace names one.

A trace names the failing selector only when an action on it threw. A wait that
polls until its budget runs out throws nothing, so for that whole class of
failure `failed_selector` stayed empty — and the offline locate, which looks a
failure up by its selector, reported that it had nothing to work on while the
diagnosis two lines above it said the locator was stale.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import run_artifacts

PAGE_LOAD = ("Failed to load Element Locator@#pay-frame >> internal:control=enter-frame "
             ">> div.order-total in CheckoutPage")
CHAIN = "#pay-frame >> internal:control=enter-frame >> div.order-total"


def _context(tmp_path, anchors):
    path = tmp_path / "pays_101500.context.json"
    path.write_text(json.dumps({"schema": 1, "failure": {
        "kind": "PAGE_NOT_LOADED", "pageObject": "CheckoutPage", "anchors": anchors}}))
    return str(path)


class TestNamingTheFailingSelector:
    def test_the_failure_message_names_it(self):
        issue = {"error_message": PAGE_LOAD, "failed_selector": ""}
        assert run_artifacts.name_failing_selector(issue) == CHAIN
        assert issue["failed_selector"] == CHAIN

    def test_the_recorded_anchor_names_it_when_the_message_does_not(self, tmp_path):
        issue = {"error_message": "BUILD FAILURE", "failed_selector": "",
                 "failure_context": _context(
                     tmp_path, [{"selector": "Locator@div.cart tr", "count": 0}])}
        assert run_artifacts.name_failing_selector(issue) == "div.cart tr"

    def test_a_selector_the_trace_named_is_left_alone(self):
        issue = {"error_message": PAGE_LOAD, "failed_selector": "#from-the-trace"}
        assert run_artifacts.name_failing_selector(issue) == "#from-the-trace"
        assert issue["failed_selector"] == "#from-the-trace"

    def test_nothing_named_stays_empty(self, tmp_path):
        issue = {"error_message": "BUILD FAILURE", "failed_selector": "",
                 "failure_context": _context(tmp_path, [])}
        assert run_artifacts.name_failing_selector(issue) == ""
        assert issue["failed_selector"] == ""

    def test_it_says_where_the_selector_came_from(self):
        said = []
        run_artifacts.name_failing_selector(
            {"error_message": PAGE_LOAD, "failed_selector": ""}, log=said.append)
        assert said and "from the failure message" in said[0]


class TestAttach:
    def test_a_run_with_no_trace_still_files_the_failure_under_its_selector(self, tmp_path):
        issue = {"error_message": PAGE_LOAD, "failed_selector": ""}
        from_trace = run_artifacts.attach(issue, tmp_path, "pays")
        # What the trace said, which is nothing — the caller reads this as trace
        # evidence, and the message is evidence of a different kind.
        assert from_trace == ""
        assert issue["failed_selector"] == CHAIN
