"""Tests for the healing → adaptation handoff.

The case this exists for is one the healing agent currently gets wrong with HIGH
confidence: a page rebuilt in place looks identical to a page never reached, and
the remediation sends a human to investigate navigation that is not broken.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import adaptation_handoff as ah

ISSUE = {"test_name": "automation.profile.ProfileWebTest#updateSummary",
         "failure_url": "https://app.example.com/profile/42"}


def _evidence(mismatches, vanished, still_present):
    return {"expected_page_object": "ProfilePage",
            "facts": {"title": "Profile · Example", "url": ISSUE["failure_url"]},
            "baseline_diff": {"available": True, "mismatches": mismatches,
                              "vanished": vanished, "still_present": still_present}}


WRONG_PAGE = {"verdict": "WRONG_PAGE", "confidence": "HIGH"}


class TestDiscrimination:
    def test_same_route_everything_vanished_is_a_rebuild(self):
        evidence = _evidence(["body class lost logged-in"], ["avatar", "bio"], [])
        assert ah.looks_restructured(evidence, WRONG_PAGE) is True

    def test_a_changed_route_stays_a_genuine_wrong_page(self):
        evidence = _evidence(["url shape /profile/{id} -> /login"], ["avatar"], [])
        assert ah.looks_restructured(evidence, WRONG_PAGE) is False, (
            "everything vanished AND we are somewhere else is exactly what "
            "WRONG_PAGE is for; only a matching route makes it a rebuild")

    def test_surviving_locators_mean_it_is_not_a_rebuild(self):
        evidence = _evidence([], ["avatar"], ["header"])
        assert ah.looks_restructured(evidence, WRONG_PAGE) is False

    def test_without_a_baseline_it_declines_to_guess(self):
        evidence = {"baseline_diff": {"available": False}}
        assert ah.looks_restructured(evidence, WRONG_PAGE) is False, (
            "no baseline means no comparison; the honest answer is 'cannot "
            "tell', which leaves healing's existing behaviour alone")

    def test_other_verdicts_are_untouched(self):
        evidence = _evidence([], ["avatar"], [])
        assert ah.looks_restructured(evidence, {"verdict": "LOCATOR_STALE"}) is False


class TestDraft:
    def test_the_note_is_marked_a_draft(self, tmp_path):
        evidence = _evidence([], ["avatar", "bio"], [])
        path = ah.write_draft(tmp_path, ISSUE, evidence, WRONG_PAGE)
        text = path.read_text()
        assert text.startswith("# DRAFT"), (
            "nobody should run this automatically — a human still has to decide "
            "whether the product changed or broke")
        assert "Module: profile" in text
        assert "avatar" in text and "ProfilePage" in text

    def test_an_existing_draft_is_never_clobbered(self, tmp_path):
        evidence = _evidence([], ["avatar"], [])
        first = ah.write_draft(tmp_path, ISSUE, evidence, WRONG_PAGE)
        first.write_text("a human has been editing this")
        assert ah.write_draft(tmp_path, ISSUE, evidence, WRONG_PAGE) is None
        assert first.read_text() == "a human has been editing this", (
            "a nightly run that overwrote yesterday's edits every night would be "
            "worse than not writing one at all")
