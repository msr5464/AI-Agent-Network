"""What a run's own account of itself has to survive to be useful.

Two bugs, one source. The handoff carried `output[-4000:]`, and on a failing
maven build the last 4000 characters are entirely the stack-trace block — so
every `STEP:`/`ACTION:` line fell off the front and the diagnosis reported
"0 step(s), 0 action(s) completed" for a run that completed several of both. And
the framework decorates some lines with HTML that only the Studio UI renders, so
a terminal and a model prompt both got raw markup.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import narration, step_provenance

RUN = "\n".join(
    ["[09:36:56] <b style='font-size:13px;'>Description: verify the dot toggle</b>",
     "[09:36:56] STEP: Login to Naukri, toggle the trailing dot, and save",
     "[09:37:12] ACTION: Navigating to: https://www.naukri.com/nlogin/login",
     "[09:37:13] ACTION: Entering in 'Username field': someone@example.com",
     "[09:37:14] WARNING: Locator for 'Login button' matches more than one element",
     "[09:37:14] <a href='/out/shot.png' target='_blank'>&#128247; View Screenshot</a>"]
    + ["[INFO] filler line that pushes the narration out of a tail slice" for _ in range(120)]
    + ["java.lang.AssertionError:",
       "Failed to click on element 'Login button' with locator: Locator@button[type='submit']"])


class TestPlain:
    def test_an_anchor_keeps_its_text_and_its_href(self):
        rendered = narration.plain(
            "[09:37:14] <a href='/out/shot.png' target='_blank'>&#128247; View Screenshot</a>")
        assert rendered == "[09:37:14] 📷 View Screenshot (/out/shot.png)"

    def test_styling_tags_are_dropped_and_entities_decoded(self):
        assert narration.plain("<b style='x'>Description: a &amp; b</b>") == \
            "Description: a & b"

    def test_a_plain_line_is_untouched(self):
        assert narration.plain("[09:37:13] ACTION: Clicking: Login button") == \
            "[09:37:13] ACTION: Clicking: Login button"

    def test_empty_input_is_safe(self):
        assert narration.plain("") == ""


class TestForHandoff:
    def test_short_output_is_kept_whole(self):
        assert narration.for_handoff("[09:00:00] STEP: one") == "[09:00:00] STEP: one"

    def test_the_narration_survives_a_long_run(self):
        kept = narration.for_handoff(RUN)
        assert "STEP: Login to Naukri" in kept
        assert "ACTION: Entering in 'Username field'" in kept

    def test_the_failure_survives_too(self):
        # Both halves or neither: the diagnosis needs how far the flow got AND
        # how it stopped, and the old tail-only slice kept only the second.
        assert "Failed to click on element 'Login button'" in narration.for_handoff(RUN)

    def test_it_respects_the_budget(self):
        assert len(narration.for_handoff(RUN, limit=2000)) <= 2000 + 40

    def test_step_provenance_can_still_count_it(self):
        # The end this whole thing serves: the old slice made this report zeroes.
        summary = step_provenance.summarize(narration.for_handoff(RUN))
        assert summary["available"] is True
        assert summary["steps"] == 1
        assert summary["actions"] == 2

    def test_the_old_tail_slice_would_have_counted_nothing(self):
        summary = step_provenance.summarize(RUN[-4000:])
        assert summary["steps"] == 0 and summary["actions"] == 0
