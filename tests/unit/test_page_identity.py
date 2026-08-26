"""Unit tests for shared/page_identity.py.

The invariant that carries the most weight here is that an unevaluable selector
is never counted as an unmatched one. Conflating them turns "we could not tell"
into "the page is wrong", which is exactly the kind of invented evidence the
diagnosis engine exists to avoid.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import page_identity as pi
from tests import fixtures as fx


class TestNormalizeSelector:
    @pytest.mark.parametrize("raw,expected", [
        ("img[class*='avatar']", "img[class*='avatar']"),
        ("img[class*='avatar'] >> nth=0", "img[class*='avatar']"),
        ("Locator@img[class*='avatar'] >> nth=0", "img[class*='avatar']"),
        ("#login >> visible=true >> nth=0", "#login"),
        ("a, .b", "a, .b"),
    ])
    def test_css_survives_playwright_suffixes(self, raw, expected):
        assert pi.normalize_selector(raw) == expected

    @pytest.mark.parametrize("raw", [
        "xpath=//div[@id='x']", "//div[@id='x']", 'text=Sign in',
        'a:has-text("Sign in")', "div >> internal:label=Name", "", None,
    ])
    def test_unevaluable_returns_none(self, raw):
        assert pi.normalize_selector(raw) is None


class TestPageFacts:
    def test_reads_identity_from_a_snapshot(self):
        facts = pi.page_facts(fx.DASHBOARD_LOGGED_OUT)
        assert facts["url"] == "https://app.example.com/"
        assert facts["title"] == "Example · Build things"
        assert "logged-out" in facts["body_class"]
        assert "The future of building" in facts["headings"]

    def test_unparseable_input_reports_error_not_silence(self):
        assert pi.page_facts("")["error"]


class TestExtractLocators:
    def test_reads_page_locator_declarations_with_field_names(self):
        found = {loc["name"]: loc for loc in pi.extract_locators(fx.DASHBOARD_PAGE_SOURCE)}
        assert found["avatarWidget"]["raw"] == "img[class*='avatar']"
        assert found["userMenu"]["selector"]

    def test_getbyrole_is_extracted_not_dropped(self):
        # A page object built from getByRole must not look like a file with no
        # locators — that is indistinguishable from "not a page object" and would
        # silently disable coverage for whole modules.
        found = pi.extract_locators(fx.HOME_PAGE_SOURCE)
        assert len(found) == 1
        assert found[0]["kind"] == "role"
        assert found[0]["accessible_name"] == "Sign in"
        assert found[0]["approx"] is True

    def test_xpath_locators_are_extracted_but_unevaluable(self):
        found = pi.extract_locators(fx.OPAQUE_PAGE_SOURCE)
        assert len(found) == 2
        assert all(not loc["selector"] for loc in found)


class TestCoverage:
    def _soup(self, snapshot):
        return pi.parse(snapshot)

    def test_expected_page_present(self):
        coverage = pi.locator_coverage(
            pi.extract_locators(fx.DASHBOARD_PAGE_SOURCE), self._soup(fx.DASHBOARD_OK))
        assert (coverage["matched"], coverage["evaluable"]) == (2, 2)

    def test_expected_page_absent(self):
        coverage = pi.locator_coverage(
            pi.extract_locators(fx.DASHBOARD_PAGE_SOURCE),
            self._soup(fx.DASHBOARD_LOGGED_OUT))
        assert (coverage["matched"], coverage["evaluable"]) == (0, 2)

    def test_partial_coverage_isolates_the_broken_locator(self):
        coverage = pi.locator_coverage(
            pi.extract_locators(fx.DASHBOARD_PAGE_SOURCE),
            self._soup(fx.DASHBOARD_RENAMED))
        assert (coverage["matched"], coverage["evaluable"]) == (1, 2)
        broken = [d for d in coverage["details"] if d["count"] == 0]
        assert len(broken) == 1 and "avatar" in broken[0]["raw"]

    def test_unevaluable_locators_do_not_count_as_absent(self):
        coverage = pi.locator_coverage(
            pi.extract_locators(fx.OPAQUE_PAGE_SOURCE), self._soup(fx.DASHBOARD_OK))
        assert coverage["evaluable"] == 0
        assert coverage["matched"] == 0
        assert all(detail["count"] is None for detail in coverage["details"])

    def test_getbyrole_matches_by_accessible_name(self):
        coverage = pi.locator_coverage(
            pi.extract_locators(fx.HOME_PAGE_SOURCE),
            self._soup(fx.DASHBOARD_LOGGED_OUT))
        assert (coverage["matched"], coverage["evaluable"]) == (1, 1)

    def test_getbyrole_does_not_match_a_page_without_it(self):
        coverage = pi.locator_coverage(
            pi.extract_locators(fx.HOME_PAGE_SOURCE), self._soup(fx.DASHBOARD_OK))
        assert coverage["matched"] == 0

    def test_page_object_coverage_ranks_best_match_first(self):
        reports = pi.page_object_coverage(
            [{"path": "DashboardPage.java", "snippet": fx.DASHBOARD_PAGE_SOURCE},
             {"path": "HomePage.java", "snippet": fx.HOME_PAGE_SOURCE}],
            self._soup(fx.DASHBOARD_LOGGED_OUT))
        assert reports[0]["name"] == "HomePage"
        assert reports[0]["ratio"] == 1.0
        assert reports[-1]["name"] == "DashboardPage"


class TestStateMarkers:
    def test_body_class_and_signin_affordance(self):
        soup = pi.parse(fx.DASHBOARD_LOGGED_OUT)
        markers = {m["marker"] for m in pi.state_markers(pi.page_facts(fx.DASHBOARD_LOGGED_OUT, soup), soup)}
        assert "logged-out" in markers
        assert "sign in" in markers

    def test_healthy_page_has_no_markers(self):
        soup = pi.parse(fx.DASHBOARD_OK)
        assert pi.state_markers(pi.page_facts(fx.DASHBOARD_OK, soup), soup) == []
