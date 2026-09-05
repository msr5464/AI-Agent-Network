"""Tests for the authoring agent's selector hygiene, evidence and fix guards.

These exist because of a specific failure that was invisible at every stage that
could have caught it. Step 02 recorded Playwright-MCP snapshot handles
(`[ref=e71]`, `generic[ref=f2e585]`) as "confirmed selectors"; step 03 wrote them
into page objects as `page.locator("[ref='f2e585']")`, which compiles and can
never match; step 04 then spent its whole fix budget on a stack trace, while the
DOM, the trace and the framework's own failure context sat unread on disk.

Each test below pins one link in that chain.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import credential_properties, edit_guards  # noqa: E402
from shared.page_identity import is_dom_selector       # noqa: E402


def _load_action(name, tmp_path, monkeypatch, workspace=None):
    """Load an action script by path. They read env at import, so set it first.

    Loaded by path rather than by package import: the agent action directories
    are not packages, and three agents ship same-named modules.
    """
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("REPO_ROOT", str(ROOT))
    monkeypatch.setenv("AGENT_DIR", str(ROOT / "agents" / "test-authoring-agent"))
    if workspace:
        monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
        monkeypatch.setenv("GITHUB_REPO_AUTOMATION", "fw")
    path = ROOT / "agents" / "test-authoring-agent" / "actions" / name
    spec = importlib.util.spec_from_file_location(f"authoring_{name[:2]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSelectorHygiene:
    """A locator that cannot match at runtime must never be called 'confirmed'."""

    # Exactly what step 02 recorded on the run that produced the broken test.
    MCP_REFS = ["[ref=e71]", "generic[ref=f2e585]", "img[ref=f2e589]",
                "textbox[ref=f2e736]", "aria-ref=f2e750", "f2e750"]

    @pytest.mark.parametrize("selector", MCP_REFS)
    def test_mcp_snapshot_handles_are_rejected(self, selector):
        assert not is_dom_selector(selector)

    def test_text_pseudo_attribute_is_rejected(self):
        # Valid CSS syntax, matches nothing — it survives any "does this parse?"
        # check, which is why it needs naming explicitly.
        assert not is_dom_selector("button[text='Save']")

    @pytest.mark.parametrize("selector", [
        "button.blue-btn", "[id='usernameField']", "[data-cy='save']",
        'textarea[placeholder*="compelling"]', 'button:has-text("Login")',
        "a[href='/profile']",   # contains "ref=" inside href — must not trip the check
        "dd", "h1", "#main .btn", "div > span.x",
    ])
    def test_real_selectors_are_kept(self, selector):
        assert is_dom_selector(selector)

    def test_blank_is_not_a_selector(self):
        assert not is_dom_selector("") and not is_dom_selector("   ")


class TestStepO2Parsers:
    def test_selector_markers_carrying_refs_are_dropped(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        raw = "\n".join([
            "SELECTOR_FOUND: usernameField = [ref=e71]|count=1",
            "SELECTOR_FOUND: profileSummarySection = generic[ref=f2e585]|count=1",
            "SELECTOR_FOUND: loginButton = button.blue-btn|count=1",
        ])
        selectors, _, _, _ = mod.parse_selector_output(raw)
        assert selectors == {"loginButton": "button.blue-btn"}

    def test_a_selector_matching_several_elements_is_dropped(self, tmp_path, monkeypatch):
        """The exact failure: button[type='submit'] matched 2 elements, was recorded
        as confirmed, and killed the generated test with a strict mode violation."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, counts, _, _ = mod.parse_selector_output(
            "SELECTOR_FOUND: loginButton = button[type='submit']|count=2")
        assert selectors == {} and counts == {}

    def test_a_selector_matching_nothing_is_dropped(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, _, _, _ = mod.parse_selector_output(
            "SELECTOR_FOUND: ghost = .no-such-thing|count=0")
        assert selectors == {}

    def test_a_unique_selector_is_kept_with_its_count(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, counts, _, _ = mod.parse_selector_output(
            "SELECTOR_FOUND: loginButton = button.blue-btn|count=1")
        assert selectors == {"loginButton": "button.blue-btn"}
        assert counts == {"loginButton": 1}

    def test_an_unreported_count_is_dropped(self, tmp_path, monkeypatch):
        """An unmeasured selector used to be kept-and-flagged, to avoid zeroing out
        a selector map step 03 aborts on. That trade was a bad one: nothing
        downstream reads the flag at codegen time, so the only thing it bought was
        a log line explaining, after the fact, why the generated test died of a
        strict mode violation. Confirmed now means measured."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, counts, _, _ = mod.parse_selector_output(
            "SELECTOR_FOUND: loginButton = button.blue-btn")
        assert selectors == {} and counts == {}

    def test_a_selector_containing_a_pipe_survives(self, tmp_path, monkeypatch):
        """The count is read from the END of the line, so a literal | in the
        selector is safe — the same trap that forced INTERACTION_HINT onto JSON."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, counts, _, _ = mod.parse_selector_output(
            'SELECTOR_FOUND: odd = [data-x="a|b"]|count=1')
        assert selectors == {"odd": '[data-x="a|b"]'} and counts == {"odd": 1}

    def test_interaction_hints_get_the_same_filter(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        raw = "\n".join([
            'INTERACTION_HINT: {"type":"input","name":"user","selector":"[id=\'u\']","text":"U"}',
            'INTERACTION_HINT: {"type":"other","name":"sum","selector":"[ref=f2e590]","text":"S"}',
        ])
        hints = mod.parse_interaction_hints(raw)
        assert [h["name"] for h in hints] == ["user"]


class TestHintsAreHeldToTheUniquenessBar:
    """Step 03 generates locators from INTERACTION_HINTs as readily as from
    SELECTOR_FOUNDs, but only the latter ever had to prove it matched one element."""

    def test_a_stale_hint_defers_to_the_confirmed_selector(self, tmp_path, monkeypatch):
        """Observed on the profile-summary flow: the model hinted the edit icon as
        img[alt='PencilSimple'], found clicking it did nothing, moved up to the
        parent span and confirmed THAT — leaving a hint pointing at the element
        that does not work beside a selector pointing at the one that does."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors = {"editButton": "#summary span.cursor-pointer"}
        hints = mod.reconcile_hints(
            [{"type": "button", "name": "editButton",
              "selector": "#summary img[alt='PencilSimple']", "text": "edit",
              "count": None}],
            selectors)
        assert hints[0]["selector"] == selectors["editButton"]

    def test_an_unbacked_hint_without_a_count_is_dropped(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        hints = mod.reconcile_hints(
            [{"type": "button", "name": "save", "selector": "button",
              "text": "Save", "count": None}], {})
        assert hints == []

    def test_an_unbacked_hint_that_measured_itself_survives(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        hints = mod.reconcile_hints(
            [{"type": "button", "name": "save", "selector": "[id='s']",
              "text": "Save", "count": 1}], {})
        assert hints[0]["selector"] == "[id='s']"
        assert "count" not in hints[0], "the internal count key must not reach disk"

    def test_the_count_survives_the_json_round_trip(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        parsed = mod.parse_interaction_hints(
            'INTERACTION_HINT: {"type":"button","name":"s","selector":"[id=\'s\']",'
            '"text":"S","count":1}')
        assert mod.reconcile_hints(parsed, {})[0]["name"] == "s"


class TestStepO3Scan:
    def test_generated_ref_locators_are_reported(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        java = '''
        private final Locator a = page.locator("[ref='f2e585']");
        private final Locator b = page.locator("img[ref='f2e589']");
        private final Locator c = page.locator("button.blue-btn");
        '''
        assert mod.unusable_locators(java) == ["[ref='f2e585']", "img[ref='f2e589']"]

    def test_clean_page_object_reports_nothing(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        assert mod.unusable_locators('page.locator("button[type=\'submit\']")') == []


class TestFixResponseShapes:
    def test_targeted_edits_are_preferred_and_grouped_per_file(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        _, conf, files, edits = mod.extract_fix_response({
            "root_cause": "ambiguous locator", "confidence": "high",
            "edits": [{"file": "A.java", "old_string": "x", "new_string": "y"},
                      {"file": "A.java", "old_string": "p", "new_string": "q"},
                      {"file": "B.java", "old_string": "m", "new_string": "n"}]})
        assert conf == "high" and files == {}
        assert {k: len(v) for k, v in edits.items()} == {"A.java": 2, "B.java": 1}

    def test_whole_file_shape_still_understood(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        _, _, files, edits = mod.extract_fix_response(
            {"root_cause": "r", "files": {"A.java": "content"}})
        assert files == {"A.java": "content"} and edits == {}

    def test_bare_map_still_understood(self, tmp_path, monkeypatch):
        # An LLM does not always follow a structure change on the first try.
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        _, _, files, edits = mod.extract_fix_response({"A.java": "content"})
        assert files == {"A.java": "content"} and edits == {}


class TestThirdPartyNoise:
    def test_only_first_party_request_failures_survive(self, tmp_path, monkeypatch):
        """Ad and analytics beacons abort on every page load and cost prompt budget."""
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        kept = mod._first_party_errors([
            "FAILED GET https://googleads.g.doubleclick.net/pagead/x (net::ERR_ABORTED)",
            "FAILED POST https://www.google.com/rmkt/collect (net::ERR_ABORTED)",
            "FAILED GET https://api.naukri.com/v1/profile (500)",
        ], "www.naukri.com")
        assert len(kept) == 1 and "api.naukri.com" in kept[0]

    def test_a_sibling_subdomain_api_is_not_discarded(self, tmp_path, monkeypatch):
        # api.example.com failing for a page on www.example.com is the single most
        # useful line in the whole section; an exact-host rule would drop it.
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        kept = mod._first_party_errors(
            ["FAILED GET https://api.example.com/v1/thing (500)"], "www.example.com")
        assert len(kept) == 1


class TestFixGuardsInAuthoring:
    ORIGINAL = (
        "package x;\n"
        "public class LoginPage extends BasePage {\n"
        "    private final Locator loginButton = page.locator(\"button[type='submit']\");\n"
        "    private final Locator user = page.locator(\"[id='usernameField']\");\n"
        "    public LoginPage(Config config) { super(config); }\n"
        "    public void doLogin(String u, String p) { click(loginButton, \"Login\"); }\n"
        "}\n"
    )
    REL = "src/main/java/automation/modules/x/web/LoginPage.java"

    def _guards(self, tmp_path, monkeypatch):
        return _load_action("04_run_and_fix.py", tmp_path, monkeypatch)._run_guards

    def test_a_targeted_locator_fix_is_allowed(self, tmp_path, monkeypatch):
        updated = self.ORIGINAL.replace("button[type='submit']", "button.blue-btn")
        ok, reason = self._guards(tmp_path, monkeypatch)(self.ORIGINAL, updated, self.REL)
        assert ok, reason

    def test_dropping_a_method_is_rejected(self, tmp_path, monkeypatch):
        updated = self.ORIGINAL.replace(
            "    public void doLogin(String u, String p) { click(loginButton, \"Login\"); }\n", "")
        ok, reason = self._guards(tmp_path, monkeypatch)(self.ORIGINAL, updated, self.REL)
        assert not ok and "removed method" in reason

    def test_raw_driver_calls_are_rejected(self, tmp_path, monkeypatch):
        updated = self.ORIGINAL.replace("click(loginButton, \"Login\")",
                                        "driver.findElement(By.id(\"x\"))")
        ok, reason = self._guards(tmp_path, monkeypatch)(self.ORIGINAL, updated, self.REL)
        assert not ok and "raw driver" in reason

    def test_broadening_a_selector_is_rejected(self, tmp_path, monkeypatch):
        # Broadening is how a wrong-page failure gets papered over into a pass.
        updated = self.ORIGINAL.replace("[id='usernameField']", "input")
        ok, reason = self._guards(tmp_path, monkeypatch)(self.ORIGINAL, updated, self.REL)
        assert not ok and "broadens" in reason

    def test_an_empty_file_is_rejected(self, tmp_path, monkeypatch):
        ok, reason = self._guards(tmp_path, monkeypatch)(self.ORIGINAL, "", self.REL)
        assert not ok


class TestSharedGuardExtraction:
    """no_selector_broadening was split out of validate_diagnosis_fit for reuse."""

    def test_it_stands_alone_without_a_verdict(self):
        before = "page.locator(\"[id='usernameField']\")"
        after = "page.locator(\"input\")"
        ok, reason = edit_guards.no_selector_broadening(before, after)
        assert not ok and "broadens" in reason

    def test_healing_still_gets_the_same_rule_through_the_verdict_path(self):
        before = "page.locator(\"[id='usernameField']\")"
        after = "page.locator(\"input\")"
        ok, _ = edit_guards.validate_diagnosis_fit(before, after, "LOCATOR_STALE")
        assert not ok


class TestCredentialPrecondition:
    def _props(self, tmp_path, body):
        (tmp_path / "parameters").mkdir(parents=True, exist_ok=True)
        path = tmp_path / "parameters" / "staging-sg.properties"
        path.write_text(body)
        return path

    def _write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOCREATE_ENVIRONMENT", "staging")
        monkeypatch.setenv("AUTOCREATE_COUNTRY", "SG")
        return credential_properties.write_credential_property(
            tmp_path, "naukari", {"username": "realuser", "password": "realpass"})

    def test_missing_keys_are_written(self, tmp_path, monkeypatch):
        path = self._props(tmp_path, "other.username=x\n")
        assert self._write(tmp_path, monkeypatch) == "written"
        assert "naukari.username=realuser" in path.read_text()

    def test_a_present_but_empty_value_is_filled(self, tmp_path, monkeypatch):
        """`naukari.username=` hands the test "" — the login form is filled with
        nothing and the failure surfaces as a locator error somewhere later."""
        path = self._props(tmp_path, "naukari.username=\nnaukari.password=\n")
        assert self._write(tmp_path, monkeypatch) == "written"
        body = path.read_text()
        assert "naukari.username=realuser" in body
        assert body.count("naukari.username") == 1, "must fill in place, not duplicate"

    def test_a_real_value_is_left_alone(self, tmp_path, monkeypatch):
        path = self._props(tmp_path, "naukari.username=human\nnaukari.password=choice\n")
        assert self._write(tmp_path, monkeypatch) == "already present"
        assert "naukari.username=human" in path.read_text()


class TestRuntimeEvidence:
    """The framework writes a DOM and a failure context on every failure."""

    def _fixture(self, tmp_path):
        fw = tmp_path / "fw"
        dom_dir = fw / "test-output" / "dom"
        dom_dir.mkdir(parents=True)
        (dom_dir / "myTest_120000.html").write_text(
            '<!-- qa-agent-network:dom-snapshot test="myTest" '
            'url="https://example.com/login" capturedAt="2026-01-01T12:00:00" -->\n'
            '<html><body><button type="submit">Login</button>'
            '<button type="submit">Use OTP</button></body></html>')
        (dom_dir / "myTest_120000.context.json").write_text(json.dumps({
            "schema": 1, "test": "x.MyTest.myTest",
            "failure": {"kind": "PAGE_NOT_LOADED", "pageObject": "ProfilePage",
                        "anchors": [{"selector": "[ref='f1']", "count": 0}]},
            "page": {"url": "https://example.com/login", "title": "Login",
                     "readyState": "complete"},
            "pageObjectCoverage": {"ProfilePage": {"matched": 0, "evaluable": 6,
                                                   "details": {"header": 0}}},
        }))
        return fw

    def test_dom_and_context_reach_the_prompt(self, tmp_path, monkeypatch):
        fw = self._fixture(tmp_path)
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        monkeypatch.setattr(mod, "TEST_RESULTS_DIR", fw / "test-output")
        out = mod.gather_runtime_evidence("myTest")

        assert "example.com/login" in out["dom_section"], "the page reached must be named"
        assert "0 of 6 locators matched" in out["context_section"]
        # The single most useful sentence: it redirects the fixer upstream instead
        # of letting it rewrite locators on a page the test never reached.
        assert "never reached this page" in out["context_section"]

    def test_artefacts_from_an_earlier_run_are_ignored(self, tmp_path, monkeypatch):
        """Observed live: a run where no test executed wrote no artefacts, so the
        glob picked the newest file from the PREVIOUS night and showed the fixer a
        DOM and failing selector from a different failure entirely."""
        import os, time
        fw = self._fixture(tmp_path)
        stale = time.time() - 3600
        for f in (fw / "test-output" / "dom").iterdir():
            os.utime(f, (stale, stale))

        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        monkeypatch.setattr(mod, "TEST_RESULTS_DIR", fw / "test-output")

        fresh = mod.gather_runtime_evidence("myTest", newer_than=time.time() - 60)
        assert fresh["dom_snapshot_path"] == ""
        assert fresh["dom_section"] == "" and fresh["context_section"] == ""

        # Unbounded, the same artefacts are still available — the bound is what
        # rejects them, not their absence.
        anyway = mod.gather_runtime_evidence("myTest")
        assert anyway["dom_snapshot_path"] != ""

    def test_a_missing_artefact_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        monkeypatch.setattr(mod, "TEST_RESULTS_DIR", tmp_path / "nope")
        out = mod.gather_runtime_evidence("noSuchTest")
        assert out == {"dom_section": "", "trace_section": "", "context_section": "",
                       "dom_snapshot_path": "", "trace_path": ""}


class TestUniquenessReachesCodegen:
    """Step 02 measures uniqueness; step 03 must not lose that signal."""

    def test_a_selector_with_no_recorded_count_is_flagged(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        assert mod.unverified_selectors({"loginButton": "button.blue-btn"}, {}) == ["loginButton"]

    def test_a_verified_selector_is_not_flagged(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        assert mod.unverified_selectors({"loginButton": "b"}, {"loginButton": 1}) == []

    def test_a_cache_predating_the_count_protocol_still_generates(self, tmp_path, monkeypatch):
        """Old caches have no counts. Every selector is flagged, but none dropped —
        emptying the map would trip step 03's own guard and abort the run."""
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        selectors = {"a": "sel-a", "b": "sel-b"}
        assert mod.unverified_selectors(selectors, None) == ["a", "b"]
        assert len(selectors) == 2, "flagging must not remove anything"


class TestGuardFalsePositives:
    """A guard that blocks a valid fix costs a whole attempt, so its inputs matter."""

    def test_a_quoted_human_label_is_not_treated_as_a_selector(self):
        # Observed live: isElementDisplayed(toast, "Success Toast") had its LABEL
        # paired against the replacement selector, the comma rule fired, and a
        # correct fix was rejected — burning fix attempt 2 of 2.
        assert edit_guards._selectors_in('click(loginButton, "Login button")') == []
        assert edit_guards._selectors_in('isElementDisplayed(t, "Success Toast")') == []

    def test_real_selectors_are_still_extracted(self):
        assert edit_guards._selectors_in('page.locator("[id=\'u\']")') == ["[id='u']"]
        assert edit_guards._selectors_in('page.locator("button.blue-btn")') == ["button.blue-btn"]

    def test_replacing_a_toast_selector_is_no_longer_blocked(self):
        before = 'private final Locator toast = page.locator("[class*=\'toast\']");\n' \
                 'boolean ok = isElementDisplayed(toast, "Success Toast");\n'
        after = 'private final Locator toast = page.locator("[class*=\'msgBlock\']");\n' \
                'boolean ok = isElementDisplayed(toast, "Success Toast");\n'
        ok, reason = edit_guards.no_selector_broadening(before, after)
        assert ok, reason

    def test_adding_alternatives_to_an_already_alternated_selector_is_caught(self):
        """Observed live: a toast selector that matched nothing had two more
        alternatives bolted on. Widening the net until something matches is how a
        test goes green for the wrong reason."""
        before = 'page.locator("[class*=\'toast\'], [class*=\'msgBlock\']")'
        after = 'page.locator("[class*=\'toast\'], [class*=\'msgBlock\'], [role=\'alert\']")'
        ok, reason = edit_guards.no_selector_broadening(before, after)
        assert not ok and "broadens" in reason

    def test_swapping_a_selector_for_an_equally_tight_one_is_allowed(self):
        before = 'page.locator("[class*=\'toast\'], [class*=\'msgBlock\']")'
        after = 'page.locator("[class*=\'alertBar\'], [class*=\'notify\']")'
        ok, reason = edit_guards.no_selector_broadening(before, after)
        assert ok, reason

    def test_a_genuine_broadening_is_still_caught(self):
        before = 'page.locator("[id=\'saveBtn\']")'
        after = 'page.locator("[id=\'saveBtn\'], [role=\'button\']")'
        ok, reason = edit_guards.no_selector_broadening(before, after)
        assert not ok and "broadens" in reason


class TestZeroTestsIsNotAPass:
    """A build that executes no test must never ship as APPROVED.

    Observed live: -Dtest named a method the generated class did not declare, so
    surefire ran 0 tests, exited 0, step 04 reported PASS and step 05 opened an
    APPROVED PR for a test that never executed.
    """

    def test_zero_tests_run_is_not_a_pass(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod._tests_actually_ran(
            "[INFO] Tests run: 0, Failures: 0, Errors: 0, Skipped: 0\n"
            "[INFO] BUILD SUCCESS") is False

    def test_a_real_run_is_recognised(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod._tests_actually_ran(
            "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0") is True

    def test_a_build_that_never_reached_surefire_stays_unknown(self, tmp_path, monkeypatch):
        """A compile error must remain a plain failure, not be relabelled
        'nothing ran' — the distinction changes what the fix step is told."""
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod._tests_actually_ran("[ERROR] COMPILATION ERROR") is None

    def test_the_pass_decision_itself_rejects_a_zero_test_build(self, tmp_path, monkeypatch):
        """The wiring, not just the helper: exit 0 + zero tests must be False."""
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        zero = "[INFO] Tests run: 0, Failures: 0\n[INFO] BUILD SUCCESS"
        assert mod.build_passed(0, zero) is False
        assert mod.build_passed(0, "[INFO] Tests run: 2, Failures: 0") is True
        assert mod.build_passed(1, "[INFO] Tests run: 1, Failures: 1") is False
        # A compile error never reached surefire: still a failure, via exit code.
        assert mod.build_passed(1, "[ERROR] COMPILATION ERROR") is False

    def test_the_highest_reported_count_wins(self, tmp_path, monkeypatch):
        # Surefire prints a per-class line and a summary line; a 0 in one of them
        # must not mask a class that genuinely ran.
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod._tests_actually_ran(
            "Tests run: 0, Failures: 0\nTests run: 3, Failures: 1") is True


class TestGeneratedMethodNameWins:
    """Resolution must work through the REAL call signature.

    The first version of these tests passed a relative path as the class argument,
    while the production call site passes _infer_test_class(...) — a bare class
    STEM. The lookup missed, fell back to the planned name, and the bug shipped
    again. So these mirror the caller exactly.
    """

    REL = "src/test/java/automation/naukari/NaukriProfileSummaryWebTest.java"
    CLASS_NAME = "NaukriProfileSummaryWebTest"   # what _infer_test_class returns

    def test_the_generated_name_is_used_over_the_planned_one(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        src = ("public class NaukriProfileSummaryWebTest extends TestBase {\n"
               '    @Test(description = "d", dataProvider = "getConfig")\n'
               "    @TestVariables(automatedBy = QA.Mukesh)\n"
               "    public void toggleDotInProfileSummaryAndVerify(Config config) {}\n"
               "}\n")
        plan = {"web_test_methods": [{"method_name": "toggleDotAndVerifyProfileSummary"}]}
        assert mod._resolve_test_method(plan, "web", {self.REL: src}, self.CLASS_NAME) == \
            "toggleDotInProfileSummaryAndVerify"

    def test_it_agrees_with_what_infer_test_class_produces(self, tmp_path, monkeypatch):
        """Pins the two halves together so they cannot drift apart again."""
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        written = [self.REL]
        class_name = mod._infer_test_class(written, "web")
        assert class_name == self.CLASS_NAME
        src = ("public class NaukriProfileSummaryWebTest extends TestBase {\n"
               "    @Test\n    public void actuallyGenerated(Config c) {}\n}\n")
        plan = {"web_test_methods": [{"method_name": "planned"}]}
        assert mod._resolve_test_method(plan, "web", {self.REL: src}, class_name) == \
            "actuallyGenerated"

    def test_the_planned_name_is_kept_when_it_really_exists(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        src = ("public class NaukriProfileSummaryWebTest extends TestBase {\n"
               "    @Test\n    public void helper(Config c) {}\n"
               "    @Test\n    public void plannedName(Config c) {}\n}\n")
        plan = {"web_test_methods": [{"method_name": "plannedName"}]}
        assert mod._resolve_test_method(plan, "web", {self.REL: src}, self.CLASS_NAME) == \
            "plannedName"

    def test_it_falls_back_to_the_plan_when_the_source_is_unavailable(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        plan = {"web_test_methods": [{"method_name": "plannedName"}]}
        assert mod._resolve_test_method(plan, "web", {}, self.CLASS_NAME) == "plannedName"


class TestStaleTestMethodIsCorrected:
    """Step 04 must not trust a recorded method name it can check against disk.

    03-generate.json can be stale — a resume, or re-running step 04 alone, never
    revisits it. `mvn -Dtest=Class#gone` runs ZERO tests and reports BUILD SUCCESS,
    so an unchecked name is silently invisible.
    """

    def _class_file(self, tmp_path, body):
        rel = "src/test/java/automation/x/FooTest.java"
        path = tmp_path / "fw" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return rel

    def test_a_name_absent_from_the_class_is_replaced(self, tmp_path, monkeypatch):
        rel = self._class_file(tmp_path, "public class FooTest {\n"
                                         "  @Test\n  public void actuallyGenerated(Config c) {}\n}\n")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        assert mod.resolve_test_method("FooTest", "planeOldStaleName", [rel]) == "actuallyGenerated"

    def test_a_valid_name_is_left_alone(self, tmp_path, monkeypatch):
        rel = self._class_file(tmp_path, "public class FooTest {\n"
                                         "  @Test\n  public void alpha(Config c) {}\n"
                                         "  @Test\n  public void beta(Config c) {}\n}\n")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        assert mod.resolve_test_method("FooTest", "beta", [rel]) == "beta"

    def test_it_finds_the_class_even_when_not_in_files_written(self, tmp_path, monkeypatch):
        """A resumed run may have an empty files_written; disk is the source of truth."""
        self._class_file(tmp_path, "public class FooTest {\n"
                                   "  @Test\n  public void onlyOne(Config c) {}\n}\n")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        assert mod.resolve_test_method("FooTest", "stale", []) == "onlyOne"

    def test_the_wiring_reconciles_before_anything_runs(self, tmp_path, monkeypatch):
        """Covers the CALL, not just the function: removing the reconciliation from
        the load path must fail a test, which testing resolve_test_method alone
        does not achieve."""
        rel = self._class_file(tmp_path, "public class FooTest {\n"
                                         "  @Test\n  public void actuallyGenerated(Config c) {}\n}\n")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        gen = {"test_class": "FooTest", "test_method": "staleName", "files_written": [rel]}
        test_class, test_method, files = mod.load_run_target(gen)
        assert (test_class, test_method) == ("FooTest", "actuallyGenerated")
        assert files == [rel]

    def test_a_missing_class_leaves_the_recorded_name_untouched(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        assert mod.resolve_test_method("NoSuchTest", "recorded", []) == "recorded"


class TestSharedTestMethodExtraction:
    def test_it_is_comment_aware(self):
        from shared.test_catalog import test_methods_in
        src = ("public class FooTest {\n"
               "  // we should add @Test to this one day\n"
               "  public void notATest(Config c) {}\n"
               "  @Test\n  public void realTest(Config c) {}\n}\n")
        assert test_methods_in(src) == ["realTest"]


class TestNavigationSettleScan:
    """net::ERR_ABORTED is the most common runtime failure in generated web code.

    Clicking Login starts a navigation; navigating again before it settles makes
    Playwright abort the first. Codegen rule 6c asks for a wait in between — this
    reports when the model skipped it, because a rule it can silently ignore is
    not a guarantee.
    """

    def _scan(self, tmp_path, monkeypatch):
        return _load_action("03_generate.py", tmp_path, monkeypatch).unsettled_navigations

    def test_click_then_navigate_is_flagged(self, tmp_path, monkeypatch):
        src = ('        click(loginButton, "Login button");\n'
               '        page.navigate(PROFILE_URL);\n')
        hits = self._scan(tmp_path, monkeypatch)(src)
        assert len(hits) == 1
        assert hits[0][0] == 1 and hits[0][2] == 2   # action line, nav line

    def test_an_intervening_wait_clears_it(self, tmp_path, monkeypatch):
        src = ('        click(loginButton, "Login button");\n'
               '        WaitHelper.waitForPageLoad(config);\n'
               '        BrowserHelper.navigateTo(config, PROFILE_URL);\n')
        assert self._scan(tmp_path, monkeypatch)(src) == []

    def test_network_idle_also_counts_as_settling(self, tmp_path, monkeypatch):
        src = ('        click(saveButton, "Save");\n'
               '        WaitHelper.waitForNetworkIdle(config);\n'
               '        BrowserHelper.navigateTo(config, PROFILE_URL);\n')
        assert self._scan(tmp_path, monkeypatch)(src) == []

    def test_a_comment_between_them_does_not_hide_the_wait(self, tmp_path, monkeypatch):
        src = ('        click(loginButton, "Login");\n'
               '        // let the post-login redirect settle\n'
               '        WaitHelper.waitForNetworkIdle(config);\n'
               '        BrowserHelper.navigateTo(config, PROFILE_URL);\n')
        assert self._scan(tmp_path, monkeypatch)(src) == []

    def test_navigation_with_no_preceding_action_is_fine(self, tmp_path, monkeypatch):
        src = ('    public void open() {\n'
               '        BrowserHelper.navigateTo(config, LOGIN_URL);\n'
               '    }\n')
        assert self._scan(tmp_path, monkeypatch)(src) == []

    def test_bare_page_navigate_is_covered_too(self, tmp_path, monkeypatch):
        # Rule 6b bans page.navigate() outright, but the scan must catch it either
        # way — navigateTo waits AFTER navigating, which does not prevent the abort.
        src = ('        submit(form, "Login form");\n'
               '        page . navigate(PROFILE_URL);\n')
        assert len(self._scan(tmp_path, monkeypatch)(src)) == 1


# ── The toast that shipped a green test with its assertion deleted ────────────
#
# PR #60 generated `assertTrue(profilePage.isSuccessToastVisible())` against a
# guessed locator, the toast never existed, and the fix replaced the assertion
# with `if (!visible) logWarning(...)`. The PR then reported "✅ Passed".
#
# Four separate gaps had to line up. One test each.

NAUKRI_INPUT = """Module: Naukari
Type: web

Steps:
1. Navigate to https://www.naukri.com/nlogin/login
2. Do login by using the credentials given below
3. Then navigate to the profile page and modify the "Profile summary" section by
   adding a dot (.) at the end
4. But if a dot (.) is already present at the end, then remove the dot (.)
5. Save the profile
6. Wait for 2 seconds
7. Now, go again to the profile page and validate that the profile is updated and
   the recent changes are reflected
"""

TOAST_CHECK = "Verify a success confirmation toast or message appears"
REAL_CHECK = ("Assert that the displayed Profile Summary text matches the "
              "modified summary saved in the previous step")


class TestCheckProvenance:
    """Which checks the author actually asked for, measured rather than claimed."""

    def test_a_check_the_input_never_mentions_is_droppable(self):
        from shared import check_provenance as sp
        assert sp.droppable(TOAST_CHECK, NAUKRI_INPUT)

    def test_the_authors_own_check_is_never_droppable(self):
        """The failure mode that matters most. Dropping this would delete an
        assertion the author asked for — the exact harm the whole change exists
        to prevent — so it must survive even though it is worded very differently
        from step 7 of the input."""
        from shared import check_provenance as sp
        assert not sp.droppable(REAL_CHECK, NAUKRI_INPUT)
        assert not sp.droppable("assertEquals refreshedSummary to modifiedSummary "
                                "— confirms the change persisted", NAUKRI_INPUT)

    def test_a_partly_traceable_check_survives(self):
        """`clearly_invented` is deliberately stricter than `derive`: one word in
        common with the author is enough to keep a check, because keeping a
        doubtful check costs a red test while dropping a real one costs silence."""
        from shared import check_provenance as sp
        assert sp.derive("Verify the success toast on the profile page appears",
                         NAUKRI_INPUT) == sp.INFERRED
        assert not sp.droppable("Verify the success toast on the profile page appears",
                                NAUKRI_INPUT)

    def test_an_action_is_never_droppable_however_invented(self):
        """"Save it" names an outcome, not a button. An action whose control does
        not exist is a mechanism to discover (rule 2e), never a check to delete."""
        from shared import check_provenance as sp
        assert sp.shape("Save the profile") == sp.ACTION
        assert not sp.droppable("Click the Frobnicate widget", NAUKRI_INPUT)

    def test_a_verification_riding_on_an_action_is_still_a_verification(self):
        """Step 7 is "go again to the profile page AND validate ..." — the proof
        is on the tail of an action, not at the front of the sentence."""
        from shared import check_provenance as sp
        assert sp.shape("Now, go again to the profile page and validate that the "
                        "profile is updated") == sp.VERIFICATION

    def test_framework_plumbing_does_not_launder_an_invented_check(self):
        """`assertTrue isSuccessToastVisible on the returned NaukriProfilePage`
        drags in naukri/profile from the class name, both of which trace back to
        the input. The check is still about a toast nobody asked for."""
        from shared import check_provenance as sp
        assert sp.droppable("assertTrue isSuccessToastVisible on the returned "
                            "NaukriProfilePage", NAUKRI_INPUT)

    def test_a_mistagged_check_is_overruled_by_the_text(self):
        from shared import check_provenance as sp
        # Model claims the author wanted it; the author's words say otherwise.
        assert sp.reconcile("user", sp.INFERRED) == sp.USER      # keeps it
        assert sp.reconcile("inferred", sp.INFERRED) == sp.INFERRED
        # And the drop decision does not consult the claim at all.
        assert sp.droppable(TOAST_CHECK, NAUKRI_INPUT)


class TestVisibleOnlySelectors:
    """A locator for something nobody can see is not a confirmed locator."""

    def test_a_hidden_unique_match_is_dropped_with_a_reason(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, _, _, rejected = mod.parse_selector_output(
            "SELECTOR_FOUND: successToast = [class*='toast']|count=1|visible=0")
        assert selectors == {}
        assert "successToast" in rejected and "visible" in rejected["successToast"]

    def test_a_visible_unique_match_is_kept(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, counts, visibles, rejected = mod.parse_selector_output(
            "SELECTOR_FOUND: loginButton = button.blue|count=1|visible=1")
        assert selectors == {"loginButton": "button.blue"}
        assert counts == {"loginButton": 1} and visibles == {"loginButton": 1}
        assert rejected == {}

    def test_an_unmeasured_visibility_is_kept_not_dropped(self, tmp_path, monkeypatch):
        """A cached run predating the visibility protocol must not empty the
        selector map and abort codegen — the same reasoning that kept
        unverified_selectors() reporting rather than dropping."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, _, visibles, _ = mod.parse_selector_output(
            "SELECTOR_FOUND: legacy = #old|count=1")
        assert selectors == {"legacy": "#old"}
        assert visibles == {"legacy": None}

    def test_a_pipe_in_the_selector_still_survives_both_suffixes(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        selectors, _, _, _ = mod.parse_selector_output(
            'SELECTOR_FOUND: odd = [data-x="a|b"]|count=1|visible=1')
        assert selectors == {"odd": '[data-x="a|b"]'}


class TestStepOutcomeHonesty:
    """Step outcomes were the one self-report nothing ever checked."""

    def test_a_verification_passed_with_no_selector_is_downgraded(self, tmp_path, monkeypatch):
        """The single check that would have caught PR #60 on its own. The run
        reported the toast step passed on the strength of a 200 response."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        kept, unverified = mod.enforce_verification_evidence(
            [TOAST_CHECK], [], {"saveButton": "#save", "profileSummaryText": "#sum"})
        assert kept == []
        assert len(unverified) == 1 and TOAST_CHECK in unverified[0]

    def test_a_verification_backed_by_a_selector_is_kept(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        kept, unverified = mod.enforce_verification_evidence(
            [REAL_CHECK], [], {"profileSummaryDisplayText": "#sum"})
        assert kept == [REAL_CHECK] and unverified == []

    def test_an_action_step_is_never_downgraded(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        kept, unverified = mod.enforce_verification_evidence(
            ["Click the Save button", "Wait 2 seconds"], [], {})
        assert len(kept) == 2 and unverified == []

    def test_the_third_state_is_parsed(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        passed, failed, unverified = mod.parse_step_results(
            "STEP_PASSED: Click Save\n"
            "STEP_UNVERIFIED: Verify a toast appears|looked for [role=alert]|never in DOM\n"
            "STEP_FAILED: Click Next|category=timeout|gave up")
        assert passed == ["Click Save"] and len(failed) == 1
        assert unverified and unverified[0].startswith("Verify a toast appears")

    def test_a_discovered_mechanism_is_parsed(self, tmp_path, monkeypatch):
        """Naukri autosaves ~1s after the last keystroke. "Save it" does not mean
        "press a Save button", and a missing button is not a dead end."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        mechs = mod.parse_mechanisms(
            "MECHANISM_FOUND: saveProfileSummary|autosave|blur the textarea|"
            "value persists after reload")
        assert mechs["saveProfileSummary"]["kind"] == "autosave"
        assert "blur" in mechs["saveProfileSummary"]["trigger"]

    def test_an_unknown_mechanism_kind_is_ignored(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        assert mod.parse_mechanisms("MECHANISM_FOUND: save|telepathy|think hard") == {}


class TestAssertionConservation:
    """The guard that would have rejected the PR #60 fix outright.

    Java fixtures rather than mocks: `conserved` walks the real call graph, and
    the bug it missed lived in how an assertion moved between two files.
    """

    TEST_BEFORE = """package automation.naukari;
public class NaukriProfileSummaryWebTest extends TestBase {
    @Test
    public void toggleDotInProfileSummaryAndVerify(Config config) {
        NaukriProfilePage profilePage = helper.toggle(config);
        config.logStep("Save the profile summary and verify the success toast appears");
        profilePage.saveProfileSummary();
        AssertHelper.assertTrue(config, profilePage.isSuccessToastVisible(),
            "Success toast should appear after saving the profile summary");
        String refreshedSummary = profilePage.refreshAndGetProfileSummaryText();
        AssertHelper.assertEquals(config, refreshedSummary, modifiedSummary,
            "Profile summary after page refresh should match the saved modified summary");
    }
}
"""

    def _fingerprints(self, tmp_path, source, name="after"):
        from shared import assertion_graph
        root = tmp_path / name / "src" / "test" / "java" / "automation" / "naukari"
        root.mkdir(parents=True, exist_ok=True)
        (root / "NaukriProfileSummaryWebTest.java").write_text(source)
        index = assertion_graph.member_index(str(tmp_path / name))
        return assertion_graph.fingerprints(
            "NaukriProfileSummaryWebTest", "toggleDotInProfileSummaryAndVerify", index)

    def _verdict(self, tmp_path, after_source):
        from shared import assertion_graph
        before = self._fingerprints(tmp_path, self.TEST_BEFORE, "before")
        after = self._fingerprints(tmp_path, after_source, "after")
        return assertion_graph.conserved(before, after)

    def test_the_real_pr60_edit_is_rejected(self, tmp_path):
        """Verbatim shape of what shipped: the assertion replaced by an `if` and a
        logWarning, and the test then reported as passed."""
        after = self.TEST_BEFORE.replace(
            '''        AssertHelper.assertTrue(config, profilePage.isSuccessToastVisible(),
            "Success toast should appear after saving the profile summary");''',
            '''        if (!profilePage.isSuccessToastVisible()) {
            config.logWarning("Success toast not detected — Naukri may suppress it");
        }''')
        report = self._verdict(tmp_path, after)
        assert not report["ok"]
        assert report["lost"], "the deleted assertTrue must be named"
        assert "assertTrue" in report["reason"]

    def test_a_ladder_downgrade_is_rejected(self, tmp_path):
        """assertEquals -> assertNotNull still leaves a call behind, so nothing
        that counts assertions would notice."""
        after = self.TEST_BEFORE.replace(
            "AssertHelper.assertEquals(config, refreshedSummary, modifiedSummary,",
            "AssertHelper.assertNotNull(config, refreshedSummary,")
        report = self._verdict(tmp_path, after)
        assert not report["ok"]
        assert report["weakened"] or report["lost"]

    def test_wrapping_an_assertion_in_a_condition_is_rejected(self, tmp_path):
        """An assertion that runs only when it would pass proves nothing."""
        after = self.TEST_BEFORE.replace(
            '''        AssertHelper.assertTrue(config, profilePage.isSuccessToastVisible(),
            "Success toast should appear after saving the profile summary");''',
            '''        if (profilePage.isSuccessToastVisible()) {
            AssertHelper.assertTrue(config, profilePage.isSuccessToastVisible(),
                "Success toast should appear after saving the profile summary");
        }''')
        report = self._verdict(tmp_path, after)
        assert not report["ok"]
        assert report["conditionalised"] or report["lost"]

    def test_a_legitimate_fix_is_accepted(self, tmp_path):
        """The other half of the same PR — a real wait fix, changing nothing about
        what the test proves. A guard that blocked this would be useless."""
        after = self.TEST_BEFORE.replace(
            "profilePage.saveProfileSummary();",
            "profilePage.saveProfileSummary();\n        WaitHelper.waitForNetworkIdle(config);")
        report = self._verdict(tmp_path, after)
        assert report["ok"], report["reason"]

    def test_renaming_a_variable_is_accepted(self, tmp_path):
        after = self.TEST_BEFORE.replace("refreshedSummary", "reloadedSummary")
        assert self._verdict(tmp_path, after)["ok"]


class TestUnverifiedCheckMatrix:
    """Who asked for the check decides what happens to it.

    Both branches matter and they pull in opposite directions: an invented check
    is dropped so it cannot fail and tempt a fix, and the author's check is kept
    so the test fails honestly instead.
    """

    def _plan(self):
        return {
            "web_pages": [{
                "class_name": "NaukriProfilePage",
                "locators_needed": ["profileSummaryDisplayText", "saveButton",
                                    "successToast"],
                "actions_needed": ["getProfileSummaryText", "saveProfileSummary",
                                   "isSuccessToastVisible"],
            }],
            "web_test_methods": [{
                "method_name": "toggle",
                "steps": ["call helper.toggle(...)",
                          "assertTrue isSuccessToastVisible on the returned NaukriProfilePage",
                          "assertEquals refreshedSummary to modifiedSummary"],
            }],
        }

    def test_an_invented_unverified_check_is_stripped_entirely(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        plan = self._plan()
        web = {"selectors": {"saveButton": "#save",
                             "profileSummaryDisplayText": "#sum"},
               "steps_unverified": [f"{TOAST_CHECK}|no element confirmed|none"]}
        out = mod.prune_unverified_checks(plan, web, NAUKRI_INPUT)

        assert out["dropped"] == [TOAST_CHECK]
        page = plan["web_pages"][0]
        assert "successToast" not in page["locators_needed"]
        assert "isSuccessToastVisible" not in page["actions_needed"]
        steps = plan["web_test_methods"][0]["steps"]
        assert not any("isSuccessToastVisible" in s for s in steps)
        # ...and the author's own assertion is untouched.
        assert any("assertEquals refreshedSummary" in s for s in steps)
        assert "profileSummaryDisplayText" in page["locators_needed"]

    def test_a_requested_unverified_check_is_kept_so_the_test_fails(self, tmp_path, monkeypatch):
        """The other branch. The product does not do what was asked; the test must
        say so, not quietly stop asking."""
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        plan = self._plan()
        web = {"selectors": {"saveButton": "#save"},
               "steps_unverified": [f"{REAL_CHECK}|read it back|value unchanged"]}
        out = mod.prune_unverified_checks(plan, web, NAUKRI_INPUT)

        assert out["dropped"] == []
        assert out["kept_unverified"] == [REAL_CHECK]
        page = plan["web_pages"][0]
        assert page["locators_needed"] == ["profileSummaryDisplayText", "saveButton",
                                           "successToast"]
        assert len(plan["web_test_methods"][0]["steps"]) == 3

    def test_nothing_is_touched_when_everything_was_observed(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        plan = self._plan()
        before = json.dumps(plan, sort_keys=True)
        out = mod.prune_unverified_checks(plan, {"selectors": {}, "steps_unverified": []},
                                          NAUKRI_INPUT)
        assert out == {"dropped": [], "kept_unverified": []}
        assert json.dumps(plan, sort_keys=True) == before

    def test_a_locator_gap_is_named_one_by_one(self, tmp_path, monkeypatch):
        """The rung between "confirmed nothing" and "this whole page is empty":
        5-of-6 confirmed used to pass both guards and guess the sixth."""
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        gaps = mod.unconfirmed_locators(
            self._plan()["web_pages"],
            {"saveButton": "#save", "profileSummaryDisplayText": "#sum"},
            [], {})
        assert gaps == {"NaukriProfilePage": ["successToast"]}

    def test_a_mechanism_covers_a_missing_locator(self, tmp_path, monkeypatch):
        """An autosave page has no Save button, and that is not a gap."""
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        gaps = mod.unconfirmed_locators(
            [{"class_name": "P", "locators_needed": ["saveButton"]}],
            {}, [], {"saveButton": {"kind": "autosave"}})
        assert gaps == {}


class TestFixRollback:
    """A rejected fix must leave nothing behind.

    Conservation is checked once, after every file in the fix is written, because
    an assertion can move between a test and a page object and neither file looks
    wrong alone. That makes rollback part of the guard: half of a rejected fix
    left on disk is a weakened test the next attempt inherits and never re-checks.
    """

    TEST_SRC = TestAssertionConservation.TEST_BEFORE

    def _framework(self, tmp_path):
        fw = tmp_path / "fw"
        d = fw / "src" / "test" / "java" / "automation" / "naukari"
        d.mkdir(parents=True)
        (d / "NaukriProfileSummaryWebTest.java").write_text(self.TEST_SRC)
        return fw

    REL = "src/test/java/automation/naukari/NaukriProfileSummaryWebTest.java"

    def _mod(self, tmp_path, monkeypatch):
        fw = self._framework(tmp_path)
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        monkeypatch.setattr(mod, "AUTOMATION_FRAMEWORK_DIR", fw)
        monkeypatch.setattr(mod, "AUDIT_DIR", tmp_path)
        return mod, fw

    def test_a_weakening_fix_is_rolled_back(self, tmp_path, monkeypatch):
        mod, fw = self._mod(tmp_path, monkeypatch)
        mod.freeze_assertions("NaukriProfileSummaryWebTest",
                              "toggleDotInProfileSummaryAndVerify")
        weakened = self.TEST_SRC.replace(
            '''        AssertHelper.assertTrue(config, profilePage.isSuccessToastVisible(),
            "Success toast should appear after saving the profile summary");''',
            '''        if (!profilePage.isSuccessToastVisible()) {
            config.logWarning("toast missing");
        }''')
        patched, contents, rejections = mod.apply_fix(
            {self.REL: weakened}, {},
            "NaukriProfileSummaryWebTest", "toggleDotInProfileSummaryAndVerify")

        assert patched == [] and contents == {}
        assert any("assertion_conservation" in r["reason"] for r in rejections)
        # The file on disk is the original, not the weakened version.
        assert (fw / self.REL).read_text() == self.TEST_SRC

    def test_a_legitimate_fix_is_applied(self, tmp_path, monkeypatch):
        mod, fw = self._mod(tmp_path, monkeypatch)
        mod.freeze_assertions("NaukriProfileSummaryWebTest",
                              "toggleDotInProfileSummaryAndVerify")
        fixed = self.TEST_SRC.replace(
            "profilePage.saveProfileSummary();",
            "profilePage.saveProfileSummary();\n        WaitHelper.waitForNetworkIdle(config);")
        patched, _, rejections = mod.apply_fix(
            {self.REL: fixed}, {},
            "NaukriProfileSummaryWebTest", "toggleDotInProfileSummaryAndVerify")

        assert patched == [self.REL] and rejections == []
        assert "waitForNetworkIdle" in (fw / self.REL).read_text()

    def test_without_a_freeze_the_guard_abstains(self, tmp_path, monkeypatch):
        """A missing baseline must not block a legitimate compile-error fix — the
        guard only ever rejects on a measured loss."""
        mod, fw = self._mod(tmp_path, monkeypatch)
        ok, reason = mod.check_conservation("NaukriProfileSummaryWebTest",
                                            "toggleDotInProfileSummaryAndVerify")
        assert ok and reason == ""

    def test_force_applies_it_anyway(self, tmp_path, monkeypatch):
        """Matching the escape hatch test-healing-agent already has, for a human
        who has looked at the diff and decided otherwise."""
        mod, fw = self._mod(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "FORCE", True)
        mod.freeze_assertions("NaukriProfileSummaryWebTest",
                              "toggleDotInProfileSummaryAndVerify")
        weakened = self.TEST_SRC.replace(
            'AssertHelper.assertTrue(config, profilePage.isSuccessToastVisible(),\n'
            '            "Success toast should appear after saving the profile summary");',
            '')
        patched, _, _ = mod.apply_fix(
            {self.REL: weakened}, {},
            "NaukriProfileSummaryWebTest", "toggleDotInProfileSummaryAndVerify")
        assert patched == [self.REL]


class TestEvidenceSurvivesARejectedAttempt:
    """An attempt whose every fix was rejected must not blind the attempt after it.

    Observed in a real run: attempt 1 was rejected by `no_selector_broadening`, so it
    wrote a result carrying no failure context — and `04-run-and-fix.json` is overwritten
    wholesale. Attempt 2 then loaded `failure_location=""` (blanking its structured
    evidence and making the `stuck` check unreachable) and `run_started_at=0.0`, which
    disables `gather_runtime_evidence`'s freshness gate: `not newer_than` short-circuits.
    That attempt duly read a DOM context file timestamped hours earlier, from a previous
    session, and was asked to fix a failure it was not looking at.
    """

    REJECTED = {
        "attempt": 1, "test_class": "T", "test_method": "m", "passed": False,
        "test_output": "same as before", "fixes_applied": [],
        "fix_rejections": [{"file": "P.java", "reason": "no_selector_broadening: wider"}],
        "skipped_rerun": True,
    }

    def _mod(self, tmp_path, monkeypatch):
        (tmp_path / "fw").mkdir(exist_ok=True)
        return _load_action("04_run_and_fix.py", tmp_path, monkeypatch,
                            workspace=tmp_path), tmp_path

    def test_failure_context_is_carried_forward(self, tmp_path, monkeypatch):
        mod, audit = self._mod(tmp_path, monkeypatch)
        (audit / "04-run-and-fix.json").write_text(json.dumps({
            "attempt": 0, "failure_location": "T.java:45",
            "failure_message": "toast never appeared",
            "screenshot_path": "/shots/m_110924.png",
            "summary_lines": ["[ERROR] Tests run: 1"],
            "run_started_at": 1757000000.0,
        }))
        mod._write_result(dict(self.REJECTED), [], 1)
        after = json.loads((audit / "04-run-and-fix.json").read_text())
        assert after["failure_location"] == "T.java:45"
        assert after["failure_message"] == "toast never appeared"
        assert after["screenshot_path"] == "/shots/m_110924.png"
        assert after["summary_lines"] == ["[ERROR] Tests run: 1"]
        # The one that silently disabled stale-artefact filtering.
        assert after["run_started_at"] == 1757000000.0

    def test_a_real_new_result_still_wins(self, tmp_path, monkeypatch):
        """Carrying forward must never overwrite an attempt's own fresh evidence."""
        mod, audit = self._mod(tmp_path, monkeypatch)
        (audit / "04-run-and-fix.json").write_text(json.dumps({
            "failure_location": "T.java:45", "run_started_at": 1757000000.0,
        }))
        mod._write_result({"attempt": 2, "passed": False, "fixes_applied": ["P.java"],
                           "failure_location": "Other.java:9",
                           "run_started_at": 1757009999.0}, [], 2)
        after = json.loads((audit / "04-run-and-fix.json").read_text())
        assert after["failure_location"] == "Other.java:9"
        assert after["run_started_at"] == 1757009999.0

    def test_nothing_to_carry_forward_is_not_an_error(self, tmp_path, monkeypatch):
        """The initial run has no previous file at all."""
        mod, audit = self._mod(tmp_path, monkeypatch)
        mod._write_result({"attempt": 0, "passed": True, "fixes_applied": []}, [], 0)
        assert json.loads((audit / "04-run-and-fix.json").read_text())["passed"] is True
