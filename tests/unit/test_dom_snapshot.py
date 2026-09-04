"""Tests for reading the failure-time DOM and the fingerprints beside it.

The case these exist for: a healing run replaced a broken locator with
`button:has-text('Profile summary')`, which matched a real button that was not
visible. The edit guard could not evaluate `:has-text()` at all, so it passed
vacuously and the fix cost a 90-second Maven run to disprove. Visibility is only
knowable from the capture — the saved markup has no layout engine.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import dom_snapshot as ds

pytest.importorskip("bs4")
from bs4 import BeautifulSoup  # noqa: E402

BODY = """
<div id="profile-section-profile-summary">
  <button class="collapsed">Profile summary</button>
  <span class="flex"><img alt="PencilSimple" src="/pencil.svg"></span>
</div>
"""

# What LocatorCapture records: the button is in the document but not visible.
ELEMENTS = [
    {"tag": "button", "id": None, "testid": None, "alt": None, "aria_label": None,
     "text": "Profile summary", "is_visible": False},
    {"tag": "img", "id": None, "testid": None, "alt": "PencilSimple",
     "aria_label": None, "text": "", "is_visible": True},
]


@pytest.fixture
def soup():
    return BeautifulSoup(BODY, "html.parser")


@pytest.fixture
def prints():
    return {"url": "https://example.test/profile", "elements": ELEMENTS}


class TestSelectorVisibility:
    @pytest.mark.parametrize("selector,expected", [
        # The exact fix that shipped and failed: one match, none of them visible.
        ("#profile-section-profile-summary button:has-text('Profile summary')", (1, 0)),
        # The element the test actually wanted.
        ('#profile-section-profile-summary img[alt="PencilSimple"]', (1, 1)),
        # Java escapes its inner quotes; the backslashes are not part of the
        # selector and used to make it unevaluable.
        ('#profile-section-profile-summary img[alt=\\"PencilSimple\\"]', (1, 1)),
        ("#profile-section-profile-summary img[alt='PencilSimple']", (1, 1)),
        # Evaluated, matched nothing — distinct from "could not evaluate".
        ("#edit-section-profile-summary", (0, 0)),
        ("#profile-section-profile-summary img[alt='mukesh']", (0, 0)),
        # :text-is is the same family and must not slip through either.
        ("#profile-section-profile-summary button:text-is('Profile summary')", (1, 0)),
    ])
    def test_counts_matches_and_visible_matches(self, soup, prints, selector, expected):
        assert ds.selector_visibility(selector, soup, prints) == expected

    @pytest.mark.parametrize("selector", ["", "xpath=//button", "//button", "text=Save"])
    def test_undecidable_is_none_not_zero(self, soup, prints, selector):
        """Cannot-evaluate must never be reported as matched-nothing."""
        assert ds.selector_visibility(selector, soup, prints) is None

    def test_narrowing_suffixes_are_stripped_not_refused(self, soup, prints):
        """`>> nth=0` narrows a selector; the base is still evaluable."""
        assert ds.selector_visibility(
            '#profile-section-profile-summary img[alt="PencilSimple"] >> nth=0',
            soup, prints) == (1, 1)

    def test_no_soup_is_undecidable(self, prints):
        assert ds.selector_visibility("button", None, prints) is None

    def test_unknown_element_is_not_reported_visible(self, soup):
        """A capture that never saw the element proves nothing about visibility."""
        assert ds.selector_visibility(
            '#profile-section-profile-summary img[alt="PencilSimple"]', soup, {}) == (1, 0)


class TestLoadFingerprints:
    def _write(self, tmp_path, sidecar_ref):
        sidecar = tmp_path / "t_120000.fingerprints.json"
        sidecar.write_text(json.dumps({"elements": ELEMENTS}))
        snap = tmp_path / "t_120000.html"
        ref = str(sidecar) if sidecar_ref else ""
        header = (f'<!-- qa-agent-network:dom-snapshot test="t" '
                  f'url="https://example.test/p" fingerprints="{ref}" -->')
        snap.write_text(header + "<html><body>" + BODY + "</body></html>")
        return snap

    def test_reads_the_sidecar_named_in_the_header(self, tmp_path):
        assert len(ds.load_fingerprints(self._write(tmp_path, True))["elements"]) == 2

    @pytest.mark.parametrize("path", ["", "/nonexistent/x.html"])
    def test_missing_snapshot_is_empty(self, path):
        assert ds.load_fingerprints(path) == {}

    def test_header_without_a_sidecar_is_empty(self, tmp_path):
        assert ds.load_fingerprints(self._write(tmp_path, False)) == {}

    def test_unreadable_sidecar_is_empty(self, tmp_path):
        snap = self._write(tmp_path, True)
        (tmp_path / "t_120000.fingerprints.json").write_text("{not json")
        assert ds.load_fingerprints(snap) == {}


class TestDistillUnchanged:
    def test_still_works_without_element_names(self):
        """test-authoring-agent calls distill() with no names; that must not change."""
        out = ds.distill("<html><body>" + BODY + "</body></html>")
        assert out["likely_matches"] == []
        assert out["total_elements"] >= 1
        assert all("suggested_selector" in e for e in out["elements"])


class TestCandidatesFromFingerprints:
    """Why the HTML path was not enough.

    On the page this was written against, distill() offered the model exactly
    three candidates — all wrong — and could not represent the right one at all:
    the edit control is an <img> carrying nothing but an `alt`, and neither `img`
    nor `alt` is in its lists. Three runs in a row the model picked the invisible
    button it was handed. The capture has every element with computed visibility.
    """

    FAILED = "#summary img[alt='mukesh']"

    PRINTS = {"url": "https://example.test/profile", "elements": [
        # The target: right region, right element type, but only an `alt`.
        {"tag": "img", "alt": "PencilSimple", "role": "img", "is_visible": True,
         "is_interactive": False, "area_norm": 0.0003, "text": "",
         "accessible_name": "PencilSimple",
         "ancestor_chain": [{"id": "summary", "testid": None}]},
        # Same icon elsewhere on the page — out of scope, must not win.
        {"tag": "img", "alt": "PencilSimple", "role": "img", "is_visible": True,
         "is_interactive": False, "area_norm": 0.0003, "text": "",
         "ancestor_chain": [{"id": "education", "testid": None}]},
        # The trap: in scope and full of matching words, but not visible.
        {"tag": "button", "text": "Profile summary", "is_visible": False,
         "is_interactive": True, "area_norm": 0.002,
         "ancestor_chain": [{"id": "summary", "testid": None}]},
        # In scope, visible, wordy — a container, not a click target.
        {"tag": "div", "text": "Profile summary", "is_visible": True,
         "is_interactive": False, "area_norm": 0.4,
         "ancestor_chain": [{"id": "summary", "testid": None}]},
    ]}

    NAMES = ["Edit Profile Summary button", "clickEditProfileSummary"]

    def _build(self, **kw):
        return ds.candidates_from_fingerprints(
            kw.pop("prints", self.PRINTS), self.NAMES, kw.pop("failed", self.FAILED))

    def test_the_right_element_ranks_first(self):
        top = self._build()["likely_matches"][0]
        assert top["suggested_selector"] == '#summary img[alt="PencilSimple"]'

    def test_hidden_elements_never_appear(self):
        out = self._build()
        rendered = str(out["likely_matches"] + out["elements"])
        assert "Profile summary" not in rendered or "button" not in rendered
        assert all(e["tag"] != "button" for e in out["elements"])

    def test_only_visible_elements_are_counted(self):
        assert self._build()["total_elements"] == 3

    def test_scope_beats_an_identical_element_elsewhere(self):
        """Seven identical icons on the real page; scoping is what disambiguates."""
        assert self._build()["likely_matches"][0]["in_failing_scope"] is True

    def test_likely_matches_are_confined_to_the_failing_scope(self):
        assert all(e.get("in_failing_scope") for e in self._build()["likely_matches"])

    def test_element_type_of_the_broken_selector_is_preferred(self):
        """An <img> that stopped matching is likelier replaced by another <img>."""
        assert self._build()["likely_matches"][0]["tag"] == "img"

    def test_without_a_scope_it_still_ranks_rather_than_failing(self):
        out = self._build(failed="img[alt='mukesh']")
        assert out["likely_matches"] and not out.get("error")

    @pytest.mark.parametrize("prints", [{}, {"elements": []},
                                        {"elements": [{"tag": "div", "is_visible": False}]}])
    def test_no_visible_capture_reports_an_error_not_a_crash(self, prints):
        assert ds.candidates_from_fingerprints(prints, self.NAMES, self.FAILED)["error"]

    def test_prompt_rendering_says_the_candidates_were_visible(self):
        text = ds.format_for_prompt(self._build())
        assert "VISIBLE" in text
        assert 'img[alt="PencilSimple"]' in text


class TestViewportParity:
    """The engine must open every context at the framework's viewport.

    bbox_norm and area_norm are normalised by the live viewport, so a baseline
    recorded by a 1920x1080 maven run and a candidate captured at 1280x900
    disagree on `location` and `area` for the very same element — an identical
    200x50 button differs by about 1.8x on area alone. Responsive layouts make it
    worse: the two widths can sit on opposite sides of a breakpoint.

    The Java baseline does not record the viewport it used, so nothing detects
    this at runtime; pinning it here is the only guard.
    """

    FRAMEWORK = {"width": 1920, "height": 1080}   # BrowserHelper.java, every path

    def test_engine_viewport_matches_the_framework(self):
        from shared import locator_capture
        assert locator_capture.VIEWPORT == self.FRAMEWORK

    def test_no_module_opens_a_context_at_its_own_size(self):
        """One definition, or it drifts again — which is how this started."""
        import re
        root = Path(__file__).resolve().parents[2]
        offenders = []
        for rel in ("shared/locator_resolve.py", "shared/locator_patch.py",
                    "agents/test-healing-agent/actions/01_locate.py"):
            text = (root / rel).read_text()
            for match in re.finditer(r'viewport\s*=\s*\{[^}]*\}', text):
                offenders.append(f"{rel}: {match.group(0)[:60]}")
        assert not offenders, "hardcoded viewport(s): " + "; ".join(offenders)

    def test_area_normalisation_is_why_this_matters(self):
        """A 200x50 element scores differently under the two viewports."""
        el = 200 * 50
        at_framework = el / (1920 * 1080)
        at_old_engine = el / (1280 * 900)
        assert round(at_old_engine / at_framework, 1) == 1.8
