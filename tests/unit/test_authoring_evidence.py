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

    def test_one_name_is_one_element(self, tmp_path, monkeypatch):
        """Three pages each asked for `amountDisplay`; the last report won and all
        three got the success screen's amount. A name the plan uses on several
        pages is asked for per page, and a repeat never replaces the first."""
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        pages = [{"class_name": "PopupPage", "locators_needed": ["amountDisplay", "payButton"]},
                 {"class_name": "BankPage", "locators_needed": ["amountDisplay", "otpField"]}]
        assert mod.qualified_locator_names(pages) == [
            "PopupPage.amountDisplay", "payButton", "BankPage.amountDisplay", "otpField"]
        selectors, _, _, _ = mod.parse_selector_output(
            "SELECTOR_FOUND: amountDisplay=.header-amount|count=1|visible=1\n"
            "SELECTOR_FOUND: amountDisplay=#txn_amount|count=1|visible=1")
        assert selectors == {"amountDisplay": ".header-amount"}

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


class TestPreferProven:
    """A passing test's locator is kept over a different one the model reported
    for the same name, when this run counted it at one visible element."""

    WANTED = ["amountField", "buyButton", "CartPage.total", "ResultPage.total"]

    def _run(self, mod, found, proven, rows):
        counts = {n: 1 for n in found}
        visibles = dict(counts)
        changed = mod.prefer_proven(found, counts, visibles, rows, proven, self.WANTED)
        return found, changed

    def test_the_proven_selector_replaces_the_reported_one(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        found, changed = self._run(mod, {"amountField": "div.cart input.amt"},
                                   {"amountField": "input.amt"},
                                   [{"known": [{"selector": "input.amt", "total": 1, "visible": 1}]}])
        assert found == {"amountField": "input.amt"}
        assert changed == [{"name": "amountField", "reported": "div.cart input.amt",
                            "proven": "input.amt"}]

    def test_a_name_never_reported_is_filled(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        found, _ = self._run(mod, {"CartPage.total": ".total"},
                             {"CartPage.total": ".total", "ResultPage.total": ".total"},
                             [{"checks": {".total": {"total": 1, "visible": 1}}}])
        assert found == {"CartPage.total": ".total", "ResultPage.total": ".total"}

    @pytest.mark.parametrize("readings", [
        [], [{"known": [{"selector": "a.buy", "total": 2, "visible": 2}]}],
        [{"known": [{"selector": "a.buy", "total": 1, "visible": 0}]}]])
    def test_a_proven_selector_not_counted_1_1_here_is_not_used(self, tmp_path, monkeypatch,
                                                                readings):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        found, changed = self._run(mod, {"buyButton": "button.buy"}, {"buyButton": "a.buy"},
                                   readings)
        assert found == {"buyButton": "button.buy"} and changed == []

    def test_a_name_this_plan_does_not_ask_for_is_ignored(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        found, _ = self._run(mod, {}, {"otherField": "#x"},
                             [{"known": [{"selector": "#x", "total": 1, "visible": 1}]}])
        assert found == {}

    def _proven_file(self, root, **overrides):
        d = root / "cache" / "user-a" / "checkout"
        d.mkdir(parents=True)
        data = {"web_base_url": "https://shop.test/", "input_file": "checkout.txt",
                "locators": [{"name": "buyButton", "selector": "a.buy"}], **overrides}
        (d / "04-proven-locators.json").write_text(json.dumps(data))

    @pytest.mark.parametrize("overrides,expected", [
        ({}, {"buyButton": "a.buy"}),
        ({"input_file": "login.txt"}, {}),
        ({"web_base_url": "https://other.test/"}, {}),
    ], ids=["this-test-case", "another-test-case", "another-site"])
    def test_only_this_test_cases_proof_is_read(self, tmp_path, monkeypatch, overrides, expected):
        mod = _load_action("02_validate_web.py", tmp_path / "audit", monkeypatch)
        monkeypatch.setattr(mod, "AGENT_DIR", tmp_path)
        monkeypatch.setenv("USER_ID", "user-a")
        monkeypatch.setenv("MODULE", "checkout")
        self._proven_file(tmp_path, **overrides)
        plan = {"web_base_url": "https://shop.test/", "_input_file": "/q/user-a/checkout.txt"}
        assert mod.proven_for_test_case(plan) == expected


class TestFailedLocators:
    """A reproducible failure on a step 02 selector is recorded for the step cache."""

    OUTPUT = ("[ERROR] Failed to click on element 'Close button' with locator: "
              "Locator@#frame >> internal:control=enter-frame >> div.close: Error {\n")

    def _record(self, tmp_path, monkeypatch, output, selectors):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        (tmp_path / "02-validate-web.json").write_text(json.dumps({"selectors": selectors}))
        plan = {"web_base_url": "https://shop.test/", "_input_file": "/q/checkout.txt",
                "feature_name": "shop"}
        return mod.record_failed_locators(output, "ShopWebTest#buy", plan)

    def test_the_failing_step_02_selector_is_recorded(self, tmp_path, monkeypatch):
        names = self._record(tmp_path, monkeypatch, self.OUTPUT,
                             {"closeButton": "#frame >> internal:control=enter-frame >> div.close",
                              "buyButton": "a.buy"})
        assert names == ["closeButton"]
        data = json.loads((tmp_path / "04-failed-locators.json").read_text())
        assert (data["input_file"], data["locators"][0]["element"]) == ("checkout.txt",
                                                                        "Close button")

    def test_a_later_failing_run_adds_to_what_was_recorded(self, tmp_path, monkeypatch):
        """A fix got past the first failure and reached another step 02 element."""
        selectors = {"closeButton": "#frame >> internal:control=enter-frame >> div.close",
                     "buyButton": "a.buy"}
        self._record(tmp_path, monkeypatch, self.OUTPUT, selectors)
        later = "Failed to click on element 'Buy' with locator: Locator@a.buy: Error {\n"
        assert self._record(tmp_path, monkeypatch, later, selectors) == ["buyButton"]
        assert self._record(tmp_path, monkeypatch, later, selectors) == []
        data = json.loads((tmp_path / "04-failed-locators.json").read_text())
        assert [e["name"] for e in data["locators"]] == ["closeButton", "buyButton"]

    def test_a_locator_step_02_never_gave_is_not_recorded(self, tmp_path, monkeypatch):
        assert self._record(tmp_path, monkeypatch, self.OUTPUT, {"buyButton": "a.buy"}) == []
        assert not (tmp_path / "04-failed-locators.json").exists()

    def test_a_failure_that_names_no_locator_is_not_recorded(self, tmp_path, monkeypatch):
        output = "✘ FAIL: total | Expected: '10' | Actual: '12'"
        assert self._record(tmp_path, monkeypatch, output, {"buyButton": "a.buy"}) == []


class TestPlanFiles:
    """The API enum has one entry per endpoint: a web test with none is not asked
    for one, since a model that declines to invent it aborted the whole step."""

    def _files(self, mod, test_type, endpoints):
        plan = {"web_pages": [{"class_name": "CartPage"}], "api_endpoints": endpoints}
        return mod._plan_files(plan, test_type, False, "", "", "Shop", "shop")

    def test_a_web_test_without_endpoints_gets_no_api_enum(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        files = self._files(mod, "web", [])
        assert not any(f.endswith("ShopApi.java") for f in files)
        assert "src/main/java/automation/modules/shop/web/CartPage.java" in files

    @pytest.mark.parametrize("test_type,endpoints", [
        ("api", []), ("both", []), ("web", [{"name": "GET_CART"}])])
    def test_an_api_test_or_one_with_endpoints_still_gets_it(self, tmp_path, monkeypatch,
                                                             test_type, endpoints):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        assert "src/main/java/automation/modules/shop/api/ShopApi.java" in self._files(
            mod, test_type, endpoints)


class TestFixResponseShapes:
    def test_targeted_edits_are_preferred_and_grouped_per_file(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        _, conf, files, edits, _ = mod.extract_fix_response({
            "root_cause": "ambiguous locator", "confidence": "high",
            "edits": [{"file": "A.java", "old_string": "x", "new_string": "y"},
                      {"file": "A.java", "old_string": "p", "new_string": "q"},
                      {"file": "B.java", "old_string": "m", "new_string": "n"}]})
        assert conf == "high" and files == {}
        assert {k: len(v) for k, v in edits.items()} == {"A.java": 2, "B.java": 1}

    def test_whole_file_shape_still_understood(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        _, _, files, edits, _ = mod.extract_fix_response(
            {"root_cause": "r", "files": {"A.java": "content"}})
        assert files == {"A.java": "content"} and edits == {}

    def test_bare_map_still_understood(self, tmp_path, monkeypatch):
        # An LLM does not always follow a structure change on the first try.
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        _, _, files, edits, _ = mod.extract_fix_response({"A.java": "content"})
        assert files == {"A.java": "content"} and edits == {}

    def test_a_reply_with_no_json_unpacks_to_five(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod.extract_fix_response(None) == ("", "", {}, {}, False)

    def test_an_empty_edit_list_is_not_a_file_map(self, tmp_path, monkeypatch):
        # Read as a flat map, this reply wrote files named `root_cause` and
        # `confidence` into the automation repo.
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        root, _, files, edits, defect = mod.extract_fix_response(
            {"root_cause": "matches the documented defect", "confidence": "high",
             "edits": [], "is_known_product_defect_matched": True})
        assert (files, edits, defect) == ({}, {}, True) and root
        _, _, files, _, _ = mod.extract_fix_response(
            {"root_cause": "r", "edits": [], "files": {"A.java": "x"}})
        assert files == {"A.java": "x"}, "a whole-file fallback beside an empty edit list still applies"

    def test_unclosed_fence_after_prose_with_braces(self, tmp_path, monkeypatch):
        # Observed reply: prose mentioning /cart/checkout/{uuid}, then ```json never closed.
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        reply = ('Lands on `/cart/checkout/{uuid}`.\n\n```json\n'
                 '{"root_cause": "r", "confidence": "high", "edits": '
                 '[{"file": "A.java", "old_string": "x", "new_string": "{y}"}]}')
        _, conf, _, edits, _ = mod.extract_fix_response(mod.extract_json(reply))
        assert conf == "high" and list(edits) == ["A.java"]
        # The same reply with no fence at all falls through to the brace scan.
        _, _, _, edits, _ = mod.extract_fix_response(mod.extract_json(reply.replace("```json", "")))
        assert list(edits) == ["A.java"]


class TestBaselinesStayInTheRunCheckout:
    def test_the_test_run_pins_the_baseline_dir_to_its_checkout(self, tmp_path, monkeypatch):
        """Unpinned, Baseline.java followed HEALING_BASELINE_DIR into the main
        checkout, ship read the worktree, and authoring PRs carried no baselines."""
        monkeypatch.setenv("FRAMEWORK_DIR", str(tmp_path / "wt"))
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        seen = {}

        class FakeProc:
            def __init__(self, cmd, **_):
                seen["cmd"], self.stdout, self.returncode = cmd, iter([]), 0

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(mod.subprocess, "Popen", FakeProc)
        mod.run_maven_test("DemoTest", "demo")
        expected = (tmp_path / "wt").resolve() / "src/main/resources/baselines"
        assert f"-Dbaseline.dir={expected}" in seen["cmd"]


class TestValidateApiCallsEveryEndpoint:
    def test_curl_runs_as_argv_and_a_bodyless_call_is_reachability_only(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_api.py", tmp_path, monkeypatch)
        ran, requested = [], []

        class Done:
            returncode, stdout, stderr = 0, '{"id": 7}\n201', ""

        class Resp:
            status_code = 400

            def json(self):
                return {"error": "body required"}

        def fake_run(argv, **kwargs):
            ran.append((argv, kwargs))
            return Done()

        def fake_request(method, url, **kwargs):
            requested.append(method)
            return Resp(), None

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod, "_request_with_retry", fake_request)
        checked, skipped = mod.check_safe_endpoints("https://api.x.io", [
            {"enum_name": "Create", "method": "POST", "path": "/pay", "expected_status": 201,
             "curl": "curl -X POST https://api.x.io/pay -d '{\"a\": 1}'; rm -rf /"},
            {"enum_name": "Order", "method": "POST", "path": "/order", "expected_status": 201},
            {"enum_name": "Clear", "method": "DELETE", "path": "/cart", "expected_status": 204},
        ], {}, None)

        argv, kwargs = ran[0]
        assert isinstance(argv, list) and argv[0] == "curl" and not kwargs.get("shell")
        assert "rm" in argv, "a ; inside the curl is a literal argument, never a second command"
        assert checked[0]["actual_status"] == 201 and checked[0]["response_keys"] == ["id"]
        assert checked[0]["body_sent"] is True and checked[0]["via"] == "curl"
        assert requested == ["POST", "DELETE"], "mutating calls without a curl are still made"
        assert [e["body_sent"] for e in checked[1:]] == [False, False]
        assert checked[1]["matched_expected"] is False and checked[1]["via"] == "requests"
        assert skipped == []

    def test_a_bodyless_status_is_not_advised_as_the_real_one(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        hint = mod._build_api_hint("api", {"auth": {}, "endpoints_checked": [
            {"method": "POST", "path": "/pay", "expected_status": 201, "actual_status": 400,
             "matched_expected": False, "response_keys": [], "error": None,
             "body_sent": False}]})
        assert "prefer the real observed status" not in hint
        assert "NOT its real status" in hint


class TestExistingHelperIsReused:
    def test_the_modules_own_helper_is_chosen(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        module = mod.AUTOMATION_FRAMEWORK_DIR / "src/main/java/automation/modules/naukari"
        module.mkdir(parents=True)
        # What the scenario-named feature_class left behind in the real framework.
        for name in ("NaukriProfileSummaryHelper", "NaukriHelper"):
            (module / f"{name}.java").write_text("class X {}")
        assert mod._find_existing_helper("naukari", "Naukari").endswith("/NaukriHelper.java")
        (module / "NaukariHelper.java").write_text("class X {}")
        assert mod._find_existing_helper("naukari", "Naukari").endswith("/NaukariHelper.java")
        assert mod._find_existing_helper("nomodule", "Nomodule") == ""


class TestCsvTestData:
    def test_credential_sheets_are_never_planned_for_codegen(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        csv_dir = mod.AUTOMATION_FRAMEWORK_DIR / "src/test/resources/saucedemo/csvFiles"
        csv_dir.mkdir(parents=True)
        # The sheets the real framework has: one credential sheet, two data sheets.
        (csv_dir / "users.csv").write_text("user_key,environment,username,password,role\n")
        (csv_dir / "products.csv").write_text("product_key,environment,slug,title\n")
        (csv_dir / "posts.csv").write_text("post_key,environment,postId,userId,title,body,limit\n")
        assert mod._plan_csv_files("saucedemo") == [
            "src/test/resources/saucedemo/csvFiles/posts.csv",
            "src/test/resources/saucedemo/csvFiles/products.csv"]
        assert mod._plan_csv_files("newmodule") == [
            "src/test/resources/newmodule/csvFiles/newmodule-data.csv"]

    @pytest.mark.parametrize("header,credential", [
        ("role,environment,username,password,description", True),
        ("user_key,apiKey,country", True),
        ("scenario,access_token", True),
        ("scenario,title,body,userId", False),
        ("product_key,footprint,notPresent", False),
    ])
    def test_credential_columns_are_recognised(self, tmp_path, monkeypatch, header, credential):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        assert mod._is_credential_csv(header) is credential

    def test_a_rewrite_may_add_rows_but_never_lose_one(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        existing = ("product_key,environment,slug\n"
                    "backpack,staging,sauce-labs-backpack\n"
                    "bike_light,staging,sauce-labs-bike-light\n")
        # Appended, or inserted beside its siblings: every row other tests read survives.
        assert mod._lost_csv_rows(existing, existing + "onesie,staging,sauce-labs-onesie\n") == []
        assert mod._lost_csv_rows(existing, existing.replace(
            "backpack,", "onesie,staging,sauce-labs-onesie\nbackpack,")) == []
        # Dropped or edited: named, so the write is refused.
        assert mod._lost_csv_rows(existing, existing.replace(
            "bike_light,staging,sauce-labs-bike-light\n", "")) == [
            "bike_light,staging,sauce-labs-bike-light"]
        assert mod._lost_csv_rows(existing, existing.replace("-bike-light", "-light")) == [
            "bike_light,staging,sauce-labs-bike-light"]
        assert mod._lost_csv_rows("", "product_key\nonesie\n") == []

    def test_a_rewrite_may_append_a_column(self, tmp_path, monkeypatch):
        """A new trailing column leaves every value other tests read where it was.
        Refusing it wrote the test that reads the column without the column, so the
        test failed on a null and step 04 paid for a fix that wrote this file back."""
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        existing = ("product_key,environment,title\n"
                    "backpack,staging,Sauce Labs Backpack\n"
                    "bike_light,staging,Sauce Labs Bike Light\n")
        assert mod._lost_csv_rows(existing, (
            "product_key,environment,title,price\n"
            "backpack,staging,Sauce Labs Backpack,$29.99\n"
            "bike_light,staging,Sauce Labs Bike Light,$9.99\n")) == []
        # A column slipped in before an existing one moves it: positional readers break.
        assert len(mod._lost_csv_rows(existing, (
            "product_key,price,environment,title\n"
            "backpack,$29.99,staging,Sauce Labs Backpack\n"
            "bike_light,$9.99,staging,Sauce Labs Bike Light\n"))) == 3
        # Renaming a column breaks readers that look it up by name.
        assert mod._lost_csv_rows(existing, existing.replace("title", "name")) == [
            "product_key,environment,title"]


class TestInterleavedStepLabels:
    def test_api_steps_are_kept_out_of_element_evidence(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        passed, failed, unverified = mod.parse_step_results(
            "STEP_PASSED: [WEB] Verify the cart badge shows 1\n"
            "STEP_PASSED: [API] Verify POST /cart returns 201\n"
            "STEP_UNVERIFIED: [API] Verify the order total is 42.00\n"
            "STEP_FAILED: [API] Create the order via POST /orders\n"
            "STEP_PASSED: Log in with the test credentials\n")
        assert passed == ["Verify the cart badge shows 1", "Log in with the test credentials"]
        assert unverified == [], "an API check can never produce a SELECTOR_FOUND"
        assert failed == ["Create the order via POST /orders"]


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
                                        "loginButton.click()")
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
        monkeypatch.setenv("AUTHORING_ENVIRONMENT", "staging")
        monkeypatch.setenv("AUTHORING_COUNTRY", "SG")
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

    def test_a_resume_restores_step_03_output_into_a_fresh_checkout(self, tmp_path, monkeypatch):
        """A resumed run's worktree is cut fresh from base; step 03's files are only
        in 03-generate.json. Unrestored, maven ran zero tests."""
        (tmp_path / "fw").mkdir()
        rel = "src/test/java/automation/x/FooTest.java"
        body = "public class FooTest {\n  @Test\n  public void generated(Config c) {}\n}\n"
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        assert mod.restore_generated_files({rel: body, "../escape.java": "x"}) == [rel]
        assert (tmp_path / "fw" / rel).read_text() == body
        assert not (tmp_path / "escape.java").exists()
        assert mod.restore_generated_files({rel: body}) == []


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
        assert out["kept_unmeasured"] == []
        page = plan["web_pages"][0]
        assert page["locators_needed"] == ["profileSummaryDisplayText", "saveButton",
                                           "successToast"]
        assert len(plan["web_test_methods"][0]["steps"]) == 3

    def test_a_pass_with_no_selector_is_not_blamed_on_the_product(self, tmp_path, monkeypatch):
        """Step 02 downgrades a pass that came with no selector, and step 03 used to
        report every kept check as "the product did not do this" — including an
        amount the model had just read off the page inside an iframe."""
        validate = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        _, downgraded = validate.enforce_verification_evidence([REAL_CHECK], [], {})
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        out = mod.prune_unverified_checks(
            self._plan(), {"selectors": {}, "steps_unverified": downgraded}, NAUKRI_INPUT)

        assert out["kept_unverified"] == [REAL_CHECK]
        assert out["kept_unmeasured"] == [REAL_CHECK]

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


class TestTheGeneratedCodeCompiles:
    """A wrong import is the cheapest failure in the pipeline and cost the most.

    The observed run generated `import automation.core.web.BasePage` — a package
    that has never existed; BasePage is `automation.core.BasePage`. Nothing in
    step 03 ran a compiler, so it reached step 04 intact and spent the initial
    maven run, the no-change re-run that rules out flakiness, and one of only two
    fix attempts. These pin the parser that feeds the repair pass.
    """

    # Verbatim from the failing run's maven output, including the duplicate block
    # maven prints under "Failed to execute goal".
    MAVEN_OUTPUT = """\
[INFO] Compiling 92 source files with javac [debug target 21] to target/classes
[ERROR] COMPILATION ERROR :
[ERROR] /tmp/qa-runs/x/src/main/java/automation/modules/naukari/web/NaukriLoginPage.java:[4,27] package automation.core.web does not exist
[ERROR] /tmp/qa-runs/x/src/main/java/automation/modules/naukari/web/NaukriLoginPage.java:[11,38] cannot find symbol
  symbol: class BasePage
[ERROR] /tmp/qa-runs/x/src/main/java/automation/modules/naukari/web/NaukriProfilePage.java:[5,27] package automation.core.web does not exist
[INFO] 3 errors
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.12.1:compile
[ERROR] /tmp/qa-runs/x/src/main/java/automation/modules/naukari/web/NaukriLoginPage.java:[4,27] package automation.core.web does not exist
[ERROR] /tmp/qa-runs/x/src/main/java/automation/modules/naukari/web/NaukriLoginPage.java:[11,38] cannot find symbol
[ERROR] -> [Help 1]
"""

    def test_it_names_the_files_to_repair_relative_to_the_repo(self, tmp_path, monkeypatch):
        gen = _load_action("03_generate.py", tmp_path, monkeypatch)
        errors = gen.compile_errors(self.MAVEN_OUTPUT, Path("/tmp/qa-runs/x"))
        assert sorted(errors) == [
            "src/main/java/automation/modules/naukari/web/NaukriLoginPage.java",
            "src/main/java/automation/modules/naukari/web/NaukriProfilePage.java",
        ]

    def test_maven_printing_every_error_twice_is_not_two_errors(self, tmp_path, monkeypatch):
        gen = _load_action("03_generate.py", tmp_path, monkeypatch)
        errors = gen.compile_errors(self.MAVEN_OUTPUT, Path("/tmp/qa-runs/x"))
        login = errors["src/main/java/automation/modules/naukari/web/NaukriLoginPage.java"]
        assert login == ["4: package automation.core.web does not exist",
                         "11: cannot find symbol"]

    def test_a_clean_build_reports_nothing(self, tmp_path, monkeypatch):
        gen = _load_action("03_generate.py", tmp_path, monkeypatch)
        assert gen.compile_errors("[INFO] BUILD SUCCESS\n", Path("/tmp/qa-runs/x")) == {}
        assert gen.compile_errors("", Path("/tmp/qa-runs/x")) == {}


class TestWhereTheBrowserActuallyWent:
    """steps_passed is prose the model chose to write; navigated_urls is the
    argument it passed. A run reported "Navigate to the profile page" and no key
    was ever minted for it, so the generated test navigated to null."""

    @staticmethod
    def _feed(decoder, name, inp):
        return decoder.feed(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
        }))

    def test_a_navigation_is_recorded_even_when_no_step_text_names_it(self):
        from shared.claude import _StreamJsonDecoder
        decoder = _StreamJsonDecoder()
        self._feed(decoder, "mcp__playwright__browser_navigate",
                   {"url": "https://www.naukri.com/mnjuser/profile"})
        assert decoder.navigated_urls == ["https://www.naukri.com/mnjuser/profile"]

    def test_other_tools_carrying_a_url_are_not_navigations(self):
        from shared.claude import _StreamJsonDecoder
        decoder = _StreamJsonDecoder()
        self._feed(decoder, "mcp__playwright__browser_click",
                   {"url": "https://example.com", "element": "Save"})
        self._feed(decoder, "WebFetch", {"url": "https://docs.example.com"})
        assert decoder.navigated_urls == []

    def test_revisiting_a_page_does_not_record_it_twice(self):
        from shared.claude import _StreamJsonDecoder
        decoder = _StreamJsonDecoder()
        for _ in range(3):
            self._feed(decoder, "mcp__playwright__browser_navigate",
                       {"url": "https://app.io/profile"})
        assert decoder.navigated_urls == ["https://app.io/profile"]

    def test_a_navigation_with_no_url_argument_is_ignored(self):
        from shared.claude import _StreamJsonDecoder
        decoder = _StreamJsonDecoder()
        self._feed(decoder, "mcp__playwright__browser_navigate", {})
        self._feed(decoder, "mcp__playwright__browser_navigate", {"url": "   "})
        self._feed(decoder, "browser_navigate", {"url": None})
        assert decoder.navigated_urls == []


class TestValuesObservedAndAsserted:
    """Vague English checks ("the name matches the one we filled") decided from what
    step 02 saw rather than from the word "matches". Values are the real ones from a
    checkout run."""

    OUTPUT = "\n".join([
        "INPUT_USED: nameField|Test User",
        "INPUT_USED: phoneField|081234567890",
        "INPUT_USED: nameField|second report ignored",
        "STEP_PASSED: Validate the customer name in the overlay matches the name entered",
        "VALUE_CHECK: Validate the customer name in the overlay matches the name entered"
        "|customerNameLabel|Test User|input:nameField|Test User",
        "STEP_PASSED: Validate the phone matches — +6281234567890",
        "VALUE_CHECK: Validate the phone matches|customerPhoneLabel|+6281234567890"
        "|input:phoneField|081234567890",
        "STEP_PASSED: Validate the recorded amount matches the expected purchase amount",
        "VALUE_CHECK: Validate the recorded amount matches the expected purchase amount"
        "|PaymentPage.amountDisplay|Rp20.000|literal|Rp 490.909",
    ])

    def test_step_02_records_what_it_typed_and_compared(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        assert mod.parse_inputs_used(self.OUTPUT) == {"nameField": "Test User",
                                                      "phoneField": "081234567890"}
        relations = [c["relation"] for c in mod.parse_value_checks(self.OUTPUT)]
        assert relations == ["equal", "phone", ""]

    def test_a_comparison_whose_values_do_not_match_is_downgraded(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        passed, _, _ = mod.parse_step_results(self.OUTPUT)
        kept, unverified = mod.enforce_value_checks(passed, [], mod.parse_value_checks(self.OUTPUT))
        assert unverified == ["Validate the recorded amount matches the expected purchase amount"]
        assert len(kept) == 2, "a step text with a trailing observation still matches its check"

    UNVERIFIED_PHONE = "\n".join([
        "INPUT_USED: phoneField|08123456789",
        "STEP_UNVERIFIED: Validate the customer phone matches the phone entered"
        "|exact match of \"08123456789\"|element shows \"+628123456789\"",
        "VALUE_CHECK: Validate the customer phone matches the phone entered"
        "|customerPhoneLabel|+628123456789|input:phoneField|08123456789",
    ])

    def test_an_unverified_comparison_whose_values_match_is_promoted(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        passed, _, unverified = mod.parse_step_results(self.UNVERIFIED_PHONE)
        checks = mod.parse_value_checks(self.UNVERIFIED_PHONE)
        inputs = mod.parse_inputs_used(self.UNVERIFIED_PHONE)
        kept, still = mod.promote_matched_values(
            passed, unverified, checks, {"customerPhoneLabel": "#phone"}, inputs)
        assert (kept, still) == (["Validate the customer phone matches the phone entered"], []), (
            "a country-code prefix is the same phone: the relation is measured, and a "
            "run judged it a mismatch in prose")

    def test_promotion_needs_a_measured_element_and_a_traced_expected_side(self, tmp_path,
                                                                          monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        _, _, unverified = mod.parse_step_results(self.UNVERIFIED_PHONE)
        checks = mod.parse_value_checks(self.UNVERIFIED_PHONE)
        assert mod.promote_matched_values([], unverified, checks, {},
                                          {"phoneField": "08123456789"}) == ([], unverified), (
            "no confirmed selector for the element: nothing was measured")
        assert mod.promote_matched_values([], unverified, checks, {"customerPhoneLabel": "#p"},
                                          {"phoneField": "0899"}) == ([], unverified), (
            "the expected side is not what INPUT_USED recorded as typed")

    def test_step_03_is_told_the_shape_and_the_comparison(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        web = {"inputs_used": {"nameField": "Test User"},
               "value_checks": [
                   {"check": "amount", "element": "amountDisplay", "rendered": "Rp20.000",
                    "source": "element:cartTotal", "expected": "20,000", "relation": "numeric"},
                   {"check": "unmatched", "element": "x", "rendered": "a", "source": "literal",
                    "expected": "b", "relation": ""}]}
        hint = mod.value_contracts_hint(web)
        assert "nameField = 'Test User'  (2 words)" in hint
        assert "Never one token" in hint and "never a whole-name generator" in hint
        assert "plain number text" in hint and "element:cartTotal" in hint
        assert "string equality assertion" in hint, (
            "a parsed long/double has no equality overload in every assertion helper")
        assert "unmatched" not in hint, "a check with no relation has no contract"
        assert mod.value_contracts_hint({}) == ""

    def test_an_invented_expected_value_is_untraced(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        files = {
            "src/test/resources/pg/csvFiles/pg-data.csv":
                "data_key,expected_amount,promo_text,environment\n"
                "checkout,Rp 490.909,Promo Flash Sale (Credit-Card),staging\n",
            "src/test/java/automation/pg/PgWebTest.java":
                'class PgWebTest { void t(Config config) {\n'
                '  AssertHelper.assertEquals(config, page.getAmount(), "20,000", "amount");\n'
                '  AssertHelper.assertContains(config, home.getMsg(), testData.get("thank_you"), "msg");\n'
                '}}\n'}
        raw = "5. validate amount on top of page is same as we passed earlier"
        web = {"steps_passed": ["Read and record the amount — Rp20.000"]}
        assert mod.untraced_expected_values(files, raw, web) == {
            "src/test/resources/pg/csvFiles/pg-data.csv": ["Rp 490.909"]}


class TestValueMismatchTriage:
    """Step 04 on this run's real failure: the demo appended a default last name."""

    FAILURE = ("✘ FAIL: Customer name in order details overlay should match the name "
               "entered in the checkout form | Expected: 'User_orrju' | Actual: "
               "'User_orrju sample_last_name'")
    TEST = ("public class PaymentGatewayWebTest {\n"
            "  @Test\n  public void completeCreditCardPayment(Config config) {\n"
            "    AssertHelper.assertEquals(config, overlay.getCustomerName(), data.getName(),\n"
            '        "Customer name in order details overlay should match the name entered '
            'in the checkout form");\n'
            "    AssertHelper.assertEquals(config, overlay.getCustomerPhone(), data.getPhone(),\n"
            '        "Customer phone in order details overlay should match");\n'
            "  }\n}\n")

    def _frozen(self, tmp_path, monkeypatch):
        rel = "src/test/java/automation/paymentgateway/PaymentGatewayWebTest.java"
        path = tmp_path / "fw" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.TEST)
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        mod.freeze_assertions("PaymentGatewayWebTest", "completeCreditCardPayment")
        return mod, rel

    def test_it_is_triaged_and_the_prompt_names_both_fixes(self, tmp_path, monkeypatch):
        mod, _ = self._frozen(tmp_path, monkeypatch)
        mismatch = mod.triage_value_mismatch(self.FAILURE)
        assert (mismatch["relation"], mismatch["expected"]) == ("words", "User_orrju")
        section = mod.value_mismatch_section(mismatch, {"nameField": "Test User"})
        assert "nameField = 'Test User'" in section
        assert "(a) If the test typed data of a different shape" in section
        assert "CONTAINS" in section
        assert "Never add interactions" in section

    def test_the_sanctioned_relax_passes_conservation_and_is_recorded(self, tmp_path, monkeypatch):
        mod, rel = self._frozen(tmp_path, monkeypatch)
        mismatch = mod.triage_value_mismatch(self.FAILURE)
        sanction = {"message": mismatch["message"], "relation": mismatch["relation"]}
        patched, _, rejections = mod.apply_fix({}, {rel: [{
            "old_string": "AssertHelper.assertEquals(config, overlay.getCustomerName()",
            "new_string": "AssertHelper.assertContains(config, overlay.getCustomerName()"}]},
            "PaymentGatewayWebTest", "completeCreditCardPayment", sanction)
        assert patched == [rel] and rejections == []
        assert sanction["relaxed"] and "assertContains" in sanction["relaxed"][0]

    def test_relaxing_the_other_check_is_still_rejected(self, tmp_path, monkeypatch):
        mod, rel = self._frozen(tmp_path, monkeypatch)
        mismatch = mod.triage_value_mismatch(self.FAILURE)
        sanction = {"message": mismatch["message"], "relation": mismatch["relation"]}
        patched, _, rejections = mod.apply_fix({}, {rel: [{
            "old_string": "AssertHelper.assertEquals(config, overlay.getCustomerPhone()",
            "new_string": "AssertHelper.assertContains(config, overlay.getCustomerPhone()"}]},
            "PaymentGatewayWebTest", "completeCreditCardPayment", sanction)
        assert patched == [] and "assertion_conservation" in rejections[-1]["reason"]

    def test_a_different_value_is_not_triaged(self, tmp_path, monkeypatch):
        mod, _ = self._frozen(tmp_path, monkeypatch)
        assert mod.triage_value_mismatch(
            self.FAILURE.replace("'User_orrju sample_last_name'", "'Budi'")) == {}


class TestLiteralValueMismatch:
    """The page rendered a quoted message without the space after its first sentence.
    The fix belongs in the literal: offered a comparator, a fix wrapped both sides in
    `.replaceAll("\\\\s+", "").toLowerCase()`."""

    FAILURE = ("✘ FAIL: Thank you message should be displayed after successful payment | "
               "Expected: 'Thank you for your purchase. Get a nice sleep.' | "
               "Actual: 'Thank you for your purchase.Get a nice sleep.'")
    REL = "src/test/java/automation/paymentgateway/PaymentGatewayWebTest.java"
    TEST = ("public class PaymentGatewayWebTest {\n  @Test\n  public void pay(Config config) {\n"
            "    AssertHelper.assertEquals(config, landing.getThankYouMessageText(),\n"
            '        "Thank you for your purchase. Get a nice sleep.",\n'
            '        "Thank you message should be displayed after successful payment");\n'
            "  }\n}\n")

    def _frozen(self, tmp_path, monkeypatch):
        path = tmp_path / "fw" / self.REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.TEST)
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        mod.freeze_assertions("PaymentGatewayWebTest", "pay")
        return mod

    def test_it_is_fixed_in_the_literal(self, tmp_path, monkeypatch):
        mod = self._frozen(tmp_path, monkeypatch)
        mismatch = mod.triage_value_mismatch(self.FAILURE)
        assert (mismatch["relation"], mismatch["literal"]) == ("formatting", True)
        section = mod.value_mismatch_section(mismatch, {"nameField": "Test User"})
        assert ("replace 'Thank you for your purchase. Get a nice sleep.' with exactly "
                "'Thank you for your purchase.Get a nice sleep.'") in section
        assert "compare with" not in section, "no comparator is offered"

    def test_the_literal_edit_passes_conservation_without_a_sanction(self, tmp_path, monkeypatch):
        mod = self._frozen(tmp_path, monkeypatch)
        patched, _, rejections = mod.apply_fix({}, {self.REL: [{
            "old_string": '"Thank you for your purchase. Get a nice sleep.",',
            "new_string": '"Thank you for your purchase.Get a nice sleep.",'}]},
            "PaymentGatewayWebTest", "pay", None)
        assert patched == [self.REL] and rejections == []

    def test_a_normalising_comparator_is_rejected(self, tmp_path, monkeypatch):
        mod = self._frozen(tmp_path, monkeypatch)
        patched, _, rejections = mod.apply_fix({}, {self.REL: [{
            "old_string": ('landing.getThankYouMessageText(),\n'
                           '        "Thank you for your purchase. Get a nice sleep.",'),
            "new_string": ('landing.getThankYouMessageText().replaceAll("\\\\s+", "").toLowerCase(),\n'
                           '        "Thank you for your purchase. Get a nice sleep.".replaceAll("\\\\s+", "").toLowerCase(),')}]},
            "PaymentGatewayWebTest", "pay", None)
        assert patched == [] and "assertion_conservation" in rejections[-1]["reason"]


class TestRetryAfterTimeout:
    """Attempt 1 of the 21:54 run timed out after confirming 19 selectors and 21 of 25
    steps; its retry was told it "produced no usable output" and started over."""

    STEPS = ["Navigate to https://demo.midtrans.com/",
             "Click the Buy Now button to open the shopping cart checkout form",
             "Validate the home page displays the message 'Thank you for your purchase.'"]

    def test_the_retry_reuses_what_was_confirmed_and_names_what_is_left(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        parsed = {"selectors": {"buyNowButton": "a.btn.buy"},
                  "steps_passed": ["Navigate to https://demo.midtrans.com/",
                                   "Click the Buy Now button to open the shopping cart "
                                   "checkout form — form visible"]}
        notes = "\n".join(mod.progress_notes("timed out after 1800s", parsed, self.STEPS))
        assert "no usable output" not in notes
        assert "buyNowButton=a.btn.buy" in notes and "do NOT search" in notes
        assert notes.split("Not yet confirmed:")[1].strip() == (
            "- Validate the home page displays the message 'Thank you for your purchase.'")

    def test_nothing_confirmed_keeps_the_old_note(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
        notes = mod.progress_notes("timed out", {"selectors": {}, "steps_passed": []}, self.STEPS)
        assert "produced no usable output" in notes[0]


class TestKnownSelectors:
    """Seeds merge every earlier run on the site. Taken from one run, whatever an
    older run confirmed and that run did not report was lost, and step 03 guessed it."""

    PLAN = {"web_base_url": "https://shop.test", "feature_name": "shop",
            "_input_file": "/queue/another-user/checkout.txt"}

    def _run(self, root, name, selectors, host="shop.test", status="ok", final=True,
             age=0, test_case="checkout.txt", module="shop", log=""):
        import os, time
        d = root / name
        d.mkdir(parents=True)
        (d / "01-parse.json").write_text(json.dumps({
            "web_base_url": f"https://{host}/", "feature_name": module,
            "_input_file": f"/queue/some-user/{test_case}"}))
        f = d / "02-validate-web.json"
        f.write_text(json.dumps({"selectors": selectors, "status": status,
                                 "final_attempt": final}))
        if log:
            (d / "stdout.log").write_text(log)
        stamp = time.time() - age
        os.utime(f, (stamp, stamp))

    def _known(self, mod, root, plan=None):
        sessions, entries = mod.known_selectors(plan or self.PLAN, roots=[root])
        return sessions, [(e["name"], e["selector"]) for e in entries]

    def test_every_run_on_the_site_contributes_newest_first(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "old", {"buy": "a.buy", "total": "#total"}, age=300)
        self._run(root, "new", {"pay": "#pay"}, age=100)
        self._run(root, "unfinished", {"otp": "#otp"}, status="timeout", final=False, age=50)
        self._run(root, "other-site", {"x": "#x"}, host="elsewhere.test", age=0)
        assert self._known(mod, root) == (
            ["unfinished", "new", "old"],
            [("otp", "#otp"), ("pay", "#pay"), ("buy", "a.buy"), ("total", "#total")]), (
            "each selector was measured 1/1 on its own, so an unfinished run counts")

    def test_this_test_case_comes_first_then_this_module(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "own", {"buy": "a.buy"}, age=300)
        self._run(root, "module", {"cart": "#cart"}, test_case="refund.txt", age=100)
        self._run(root, "other", {"login": "#login"}, test_case="login.txt",
                  module="auth", age=10)
        assert self._known(mod, root)[1] == [("buy", "a.buy"), ("cart", "#cart"),
                                             ("login", "#login")]
        monkeypatch.setattr(mod, "KNOWN_LIMIT", 2)
        assert self._known(mod, root)[1] == [("buy", "a.buy"), ("cart", "#cart")], (
            "the cap cuts the unrelated flow, never this one")

    def test_one_entry_per_selector_under_the_first_runs_name(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "own", {"amountText": ".amount"}, age=300)
        self._run(root, "other", {"totalLabel": ".amount"}, test_case="refund.txt",
                  module="billing", age=10)
        assert self._known(mod, root)[1] == [("amountText", ".amount")]

    def test_two_per_name_the_first_and_the_newest_other(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "own-old", {"buy": "a.old"}, age=300)
        self._run(root, "own-mid", {"buy": "a.mid"}, age=200)
        self._run(root, "other-new", {"buy": "a.new"}, test_case="login.txt",
                  module="auth", age=10)
        assert self._known(mod, root)[1] == [("buy", "a.mid"), ("buy", "a.new")], (
            "older runs of this test case cannot push out the newest confirmation")

    def test_page_prefixed_names_are_separate(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "own", {"BankPage.amount": "#txn", "SuccessPage.amount": ".headline",
                                "PopupPage.amount": ".header"})
        assert len(self._known(mod, root)[1]) == 3

    @pytest.mark.parametrize("log", [
        "[07:35:14] Step cache: restored 02-validate-web.json (x)",
        "[07:35:14] TESTING_MODE: restored 02-validate-web.json from cache (x)",
    ], ids=["current", "before-rename"])
    def test_the_current_and_a_cache_restored_session_are_skipped(self, tmp_path, monkeypatch,
                                                                 log):
        root = tmp_path / "audit"
        mod = _load_action("02_validate_web.py", root / "current", monkeypatch)
        self._run(root, "current", {"mine": "#mine"})
        self._run(root, "restored", {"stale": "#stale"}, age=0, log=log)
        self._run(root, "real", {"buy": "a.buy"}, age=500)
        assert self._known(mod, root) == (["real"], [("buy", "a.buy")])

    def test_nothing_known_is_nothing(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        assert mod.known_selectors({"web_base_url": "https://x.test"}, roots=[tmp_path]) == ([], [])
        assert mod.known_selectors({}, roots=[tmp_path]) == ([], [])

    def _proven(self, root, name, locators, host="shop.test", age=0,
                test_case="checkout.txt", module="shop"):
        import os, time
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "04-proven-locators.json"
        f.write_text(json.dumps({
            "status": "ok", "web_base_url": f"https://{host}/", "input_file": test_case,
            "feature_name": module,
            "locators": [{"name": n, "selector": s} for n, s in locators.items()]}))
        stamp = time.time() - age
        os.utime(f, (stamp, stamp))

    def test_a_step_04_replacement_drops_the_replaced_selector(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "run", {"buyNowButton": "button.guess", "total": "#total"}, age=100)
        self._proven(root, "run", {"buyNowButton": "a.buy"}, age=50)
        _, entries = mod.known_selectors(self.PLAN, roots=[root])
        assert [(e["name"], e["selector"], e["proven"]) for e in entries] == [
            ("buyNowButton", "a.buy", True), ("total", "#total", False)]

    def test_a_proven_selector_takes_the_first_place(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "newer", {"buy": "a.new"}, age=10)
        self._proven(root, "older", {"buy": "a.proven"}, age=300)
        self._run(root, "oldest", {"buy": "a.oldest"}, age=500)
        _, entries = mod.known_selectors(self.PLAN, roots=[root])
        assert {(e["selector"], e["proven"]) for e in entries} == {
            ("a.proven", True), ("a.new", False)}, (
            "the proven one and the newest other one; the third is skipped")

    def test_a_cache_folder_with_only_a_proven_file_is_read(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "cache"
        self._proven(root / "user", "checkout", {"buy": "a.buy"})
        self._proven(root / "user", "elsewhere", {"x": "#x"}, host="other.test")
        assert self._known(mod, root) == (["checkout"], [("buy", "a.buy")]), (
            "site and test case come from the file itself; another site is ignored")

    def test_a_list_of_alternatives_is_never_seeded(self, tmp_path, monkeypatch):
        mod = _load_action("02_validate_web.py", tmp_path / "current", monkeypatch)
        root = tmp_path / "audit"
        self._run(root, "run", {"buy": "button.buy, a.buy", "total": "#total"})
        self._proven(root, "proven", {"pay": "button.pay, a.pay"})
        assert self._known(mod, root)[1] == [("total", "#total")]


def test_an_unverified_action_is_not_a_check(tmp_path, monkeypatch):
    """A run counted a payment tab after clicking it and invented a step for the
    selector it could not report. Kept as a check, it made
    step 04 stop on the `defect` gate when the tab's guessed locator failed."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    unverified = [
        "Select Credit Card as the payment method (locator report)|checked "
        "a.list[href='#/credit-card'] after navigation|element no longer present",
        "Verify the amount decreases after applying the promo|compared before and after"
        "|amount stayed at Rp19.000",
        "The bank amount is the same as the cart total|compared|Rp19.000 vs Rp20.000",
    ]
    kept = mod.drop_unverified_actions(unverified)
    assert [u.split("|")[0] for u in kept] == [
        "Verify the amount decreases after applying the promo",
        "The bank amount is the same as the cart total"], (
        "a comparison is a claim even without a verifying verb")


def test_a_literal_read_off_the_page_is_not_a_check(tmp_path, monkeypatch):
    """The same session reported `Record the amount ...|literal|20,000`, and step 03
    generated assertEquals(amount, "20,000") from it."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    output = "\n".join([
        "VALUE_CHECK: Record the amount displayed on the order form before checkout"
        "|OrderFormPage.amountText|20,000|literal|20,000",
        "VALUE_CHECK: Verify the message is displayed|thankYouMessage"
        "|Thank you for your purchase.|literal|Thank you for your purchase.",
        "VALUE_CHECK: Verify the total|totalText|Rp 5.000|literal|Rp 5.000",
        "VALUE_CHECK: Verify the top amount matches the order form"
        "|topAmountText|Rp20.000|element:OrderFormPage.amountText|20,000",
    ])
    steps = ["Record the amount displayed on the order form before checkout",
             "Verify the message 'Thank you for your purchase.' is displayed",
             "Verify the total is 5,000"]
    kept = mod.drop_untraced_sources(mod.parse_value_checks(output), steps, {})
    assert [c["element"] for c in kept] == ["thankYouMessage", "totalText", "topAmountText"], (
        "a literal quoted by the test case stays, in any number format; an element source "
        "is not a literal")


def test_a_field_is_something_that_takes_typing(tmp_path, monkeypatch):
    """The plan's `amountField` was matched to a cart's read-only total,
    `td.amount`. It counted 1/1, and the model reported
    `INPUT_USED: amountField|20,000` for a value it had only read."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    found = {"nameField": "tr:nth-child(1) input", "amountField": "td.amount",
             "cardField": "#card", "otpField": "#otp"}
    counts, visibles, rejected = {k: 1 for k in found}, {k: 1 for k in found}, {}
    inputs = {"nameField": "Test User", "amountField": "20,000", "cardField": "4111",
              "otpField": "112233"}
    rows = [
        # What page.qa.check writes (Playwright's isEditable) and what a harvest writes.
        {"checks": {"tr:nth-child(1) input": {"total": 1, "visible": 1, "editable": True},
                    "td.amount": {"total": 1, "visible": 1, "editable": False}}},
        {"checks": {"td.amount": {"total": 1, "visible": 1, "tag": "td"},
                    "#card": {"total": 1, "visible": 1, "tag": "input"}}},
    ]
    kept = mod.enforce_typed_fields(inputs, found, counts, visibles, rejected, rows)
    assert kept == {"nameField": "Test User", "cardField": "4111", "otpField": "112233"}, (
        "an input by tag is typeable, and one never measured keeps the model's word")
    assert "amountField" not in found and "amountField" not in counts
    assert "takes no typing" in rejected["amountField"]
    checks = [{"check": "Verify the popup amount matches the cart amount", "element": "x",
               "rendered": "Rp20.000", "source": "input:amountField", "expected": "20,000",
               "relation": "numeric"}]
    assert mod.drop_untraced_sources(checks, [], kept) == [], (
        "a contract must not compare with test data nothing was typed into")


def test_an_input_used_must_be_something_the_browser_saw_typed(tmp_path, monkeypatch):
    """The helpers record every value typed into any field. A claim none of them
    matches is dropped; the field's selector stays, since only the claim is wrong."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    found = {"nameField": "#name", "cardField": "#card", "pwdField": "#pwd",
             "amountField": "input.text-right"}
    counts, visibles, rejected = {k: 1 for k in found}, {k: 1 for k in found}, {}
    inputs = {"nameField": "Test User", "cardField": "4111111111111111",
              "pwdField": "s3cret", "amountField": "20,000"}
    rows = [{"typed": {"sel": "#name", "value": "Test User", "password": False}},
            {"typed": {"sel": "#card", "value": "4111 1111 1111 1111", "password": False}},
            {"typed": {"sel": "#pwd", "value": None, "password": True}}]
    kept = mod.enforce_typed_fields(inputs, found, counts, visibles, rejected, rows)
    assert kept == {"nameField": "Test User", "cardField": "4111111111111111",
                    "pwdField": "s3cret"}, "a field that formats what it is given still counts"
    assert "amountField" in found and not rejected, "the claim is dropped, not the field"
    assert mod.enforce_typed_fields({"nameField": "x"}, {"nameField": "#name"}, {}, {}, {},
                                    []) == {"nameField": "x"}, (
        "no recording at all keeps the model's word")


def test_page_qa_scopes_a_whole_frame_and_counts_before_acting(tmp_path):
    """`scope: '#pay >> internal:control=enter-frame'` went to querySelector unsplit, so
    every harvest inside an embedded checkout was a SyntaxError; so did Playwright's
    `:visible` in a scope."""
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    from shared.mcp_config import INIT_PAGE
    script = """
const init = require(%s).default;
let present = true;
const harvest = at => async (fn, arg) => [{ tag: 'div', sel: '.x', total: 1, visible: 1, at,
  inner: Array.isArray(arg) ? arg[0] : undefined },
  // A field whose own selector is shared, with the text beside it.
  { tag: 'input', type: 'text', sel: "input[type='text']", total: 3, visible: 3,
    near: 'Name', box: 'tr', within: 'div.cart' }];
// Only the main frame has the scope: Playwright resolves it, so `:visible` works.
const scopeIn = n => () => ({ count: async () => n, first: () => ({ elementHandle: async () => 'ROOT' }) });
const main = { url: () => 'https://shop.test/', parentFrame: () => null, evaluate: harvest('main'),
  locator: scopeIn(1) };
const pay = { url: () => 'https://pay.test/', parentFrame: () => main, evaluate: harvest('pay'),
  locator: scopeIn(0), frameElement: async () => ({ evaluate: async () => '#pay' }) };
const loc = () => ({ count: async () => (present ? 1 : 0),
  nth: () => ({ isVisible: async () => present }),
  first: () => ({ innerText: async () => 'Tab', isEditable: async () => { throw new Error('not an <input>'); },
    isChecked: async () => true }) });
const page = { on() {}, url: () => 'https://shop.test/', frames: () => [main, pay],
  waitForTimeout: async () => {}, locator: loc, frameLocator: () => ({ locator: loc }) };
(async () => {
  await init({ page });
  const whole = await page.qa.harvest('#pay >> internal:control=enter-frame', {});
  const none = await page.qa.harvest('#other >> internal:control=enter-frame', {});
  const scoped = await page.qa.harvest('div.cart:visible', {});
  const out = await page.qa.step(async () => { present = false; },
    { before: { tab: '#tab' }, check: { tab: '#tab' }, harvest: false, quietMs: 0 });
  console.log(JSON.stringify({ whole, none, scoped, before: out.before, check: out.check }));
})().catch(e => { console.error(e); process.exit(1); });
""" % json.dumps(str(INIT_PAGE))
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert [(f["frame"], f["result"][0]["inner"], f["result"][0]["sel"]) for f in got["whole"]] == [
        ("https://pay.test/", None, "#pay >> internal:control=enter-frame >> .x")]
    assert "no frame is reached by #other" in got["none"][0]["result"]
    assert [(f["frame"], f["result"][0]["inner"]) for f in got["scoped"]] == [
        ("https://shop.test/", "ROOT")], "the scope is resolved by Playwright, in its own frame"
    assert got["scoped"][0]["result"][1]["sel"] == "div.cart tr:has-text('Name') input[type='text']", (
        "a field with only a shared selector is anchored on its label and counted")
    assert got["before"]["tab"]["total"] == 1 and got["check"]["tab"]["total"] == 0
    assert got["before"]["tab"]["editable"] is False, "isEditable throws on a non-field"
    assert got["before"]["tab"]["checked"] is True, "a selected option says so in the same count"


def test_step_02_calls_the_helpers_instead_of_carrying_code():
    source = (ROOT / "agents" / "test-authoring-agent" / "actions" / "02_validate_web.py").read_text()
    assert "page.qa.step(" in source and "page.qa.check(" in source
    assert "ONE browser_run_code_unsafe PER STEP" in source
    assert "const ATTRS = [" not in source, "the harvest snippet is preloaded, not pasted"


def test_only_a_sameness_claim_is_refuted_by_different_values(tmp_path, monkeypatch):
    """The 10:18 run downgraded "the amount has decreased" (its sides differ on
    purpose) and "the order ID is not null" (no second value at all)."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    checks = [{"check": c, "relation": ""} for c in (
        "Validate the displayed amount has decreased after applying the promo",
        "Validate the captured order ID from the success page is not null",
        "Validate the recorded amount matches the expected purchase amount")]
    passed = [c["check"] for c in checks]
    kept, unverified = mod.enforce_value_checks(passed, [], checks)
    assert unverified == ["Validate the recorded amount matches the expected purchase amount"]
    assert len(kept) == 2


def test_a_real_difference_on_test_data_shows_what_step_02_typed(tmp_path, monkeypatch):
    """'Alica Bednar MD' vs 'Alica Bednar' is no relation — not something to loosen —
    but the expected side is test data whose shape drifted from 'Test User'."""
    mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
    failure = ("✘ FAIL: Customer name should match | Expected: 'Alica Bednar MD' | "
               "Actual: 'Alica Bednar'")
    section = mod.typed_shape_section(failure, {"nameField": "Test User"})
    assert "'Alica Bednar MD' (3 words)" in section
    assert "nameField = 'Test User' (2 words)" in section
    assert mod.typed_shape_section("Failed to load Element", {"nameField": "x"}) == ""
    assert mod.typed_shape_section(failure, {}) == ""


def test_selector_found_is_held_to_what_the_helpers_measured(tmp_path, monkeypatch):
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    found = {"buyNow": "a.btn.buy", "name": "tr input", "toast": ".toast"}
    counts = {k: 1 for k in found}
    visibles = {k: 1 for k in found}
    rows = [{"checks": {"a.btn.buy": {"total": 1, "visible": 1},
                        "tr input": {"total": 4, "visible": 4}}},
            {"known": [{"selector": "a.btn.buy", "total": 0, "visible": 0}]}]
    found, counts, visibles, rejected, stats = mod.verify_with_evidence(
        found, counts, visibles, {}, rows)
    assert set(found) == {"buyNow", "toast"}, "claimed 1/1, measured 4/4: dropped"
    assert "measured 4 match(es)" in rejected["name"]
    assert stats == {"live": 1, "claimed": 1, "dropped": 1}


def test_the_test_cases_own_values_reach_the_browser_run():
    """Step 01 rewrote "fill dummy data … Address: Bangalore, India" as "fill the
    fields with dummy data", and step 02 typed an address it made up. Step 02 now
    reads the values from the test case itself."""
    from shared.test_case import given_values, is_given
    text = "\n".join([
        "Module: PaymentGateway",
        "Type: web",
        "URL: https://shop.test/",
        "Steps:",
        "1. Navigate to https://shop.test/",
        "3. Fill dummy data in all the fields and click Checkout:",
        "     Amount: 50000",
        "     Address: Bangalore, India",
        "     Enter card number: 4111 1111 1111 1111",
        "Open https://shop.test/help",
        "Validate the popup and everything else it says about the order: fine",
    ])
    assert given_values(text) == [("Amount", "50000"), ("Address", "Bangalore, India"),
                                      ("Enter card number", "4111 1111 1111 1111")], (
        "header fields, numbered step lines, a URL split at its scheme and a sentence "
        "with a colon are not values")
    assert is_given("4111111111111111", text) and not is_given("4811111111111114", text), (
        "a field's own formatting is the same value; another card is not")


def test_an_email_and_an_otp_are_credentials_only_in_a_flow_that_logs_in():
    from shared.credential_extraction import mentions_login
    assert mentions_login("2. Login as Admin user") and mentions_login("Sign in with email")
    assert not mentions_login("Fill the checkout form\nEmail: a@b.test\nEnter Bank OTP: 112233")


def test_a_value_the_test_case_gives_is_used_as_written(tmp_path, monkeypatch):
    """Told to randomise every typed field inside its shape, step 03 replaced the
    test case's card number and address with generated ones."""
    mod = _load_action("03_generate.py", tmp_path, monkeypatch)
    web = {"inputs_used": {"addressField": "Bangalore, India", "phoneField": "081234567890",
                           "cardNumberField": "4111 1111 1111 1111"}}
    raw = ("3. Fill the form and click Checkout:\n   Address: Bangalore, India\n"
           "   Enter card number: 4111 1111 1111 1111\n")
    hint = mod.value_contracts_hint(web, raw)
    assert "addressField = 'Bangalore, India'  (2 words)  GIVEN BY THE TEST CASE" in hint
    assert "cardNumberField = '4111 1111 1111 1111'  (4 words)  GIVEN BY THE TEST CASE" in hint
    assert "phoneField = '081234567890'  (1 word)\n" in hint, "a made-up value keeps the shape rule"
    assert "Test data for the other fields keeps that SHAPE" in hint


def test_a_clicked_locator_nobody_reported_is_confirmed_from_the_click(tmp_path, monkeypatch):
    """A run clicked a page's main button and never reported it, and step 03 guessed
    a `button` for what was a link. The browser records every click; the rows below
    are what it recorded for a link, a clickable div and a span inside a button."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    rows = [{"clicked": {"sel": "a.btn.buy", "total": 1, "visible": 1, "text": "BUY NOW"}},
            {"clicked": {"sel": "div.cart-checkout", "total": 1, "visible": 1, "text": "CHECKOUT"}},
            {"clicked": {"sel": "button.pay", "total": 1, "visible": 1, "text": "Pay now"}},
            {"clicked": {"sel": "a.promo", "total": 2, "visible": 2, "text": "Details"}}]
    found, counts, visibles = {"payButton": "button.pay"}, {"payButton": 1}, {"payButton": 1}
    recovered = mod.recover_clicked_locators(
        found, counts, visibles, rows,
        ["LandingPage.buyNowButton", "checkoutButton", "continueButton", "detailsIcon", "payButton"])
    assert recovered == ["LandingPage.buyNowButton", "checkoutButton"]
    assert found["LandingPage.buyNowButton"] == "a.btn.buy", (
        "two shared words beat the one `now` shares with 'Pay now'")
    assert "continueButton" not in found, "a click whose text does not name it is not it"
    assert "detailsIcon" not in found, "a click on an element that was not unique confirms nothing"


class TestProvenLocators:
    """Step 04 fixed a locator on the way to a passing test, and later runs never saw
    it: seeding read only step 02's map. The framework's baselines from the passing
    run say which locators matched one visible element while it passed."""

    REL = "src/main/java/automation/modules/shop/web/LandingPage.java"
    PAGE = ("public class LandingPage extends BasePage {\n"
            "    private final Locator buyNowButton;\n    private final Locator thankYou;\n"
            "    private final Locator rows;\n    private final Locator either;\n"
            "    public LandingPage(Config config) {\n        super(config);\n"
            "        buyNowButton = page.locator(\"a:has-text('Buy Now')\");\n"
            "        thankYou = page.locator(\"div.thanks\");\n"
            "        rows = page.locator(\"tr.row\");\n"
            "        either = page.locator(\"button.x, a.x\");\n    }\n}\n")
    PLAN = {"web_base_url": "https://shop.test/", "_input_file": "/queue/u/checkout.txt",
            "feature_name": "shop",
            "web_pages": [{"class_name": "LandingPage", "locators_needed": ["buyNowButton"]}]}

    def _setup(self, tmp_path, monkeypatch, recorded_at):
        fw = tmp_path / "fw"
        (fw / self.REL).parent.mkdir(parents=True)
        (fw / self.REL).write_text(self.PAGE)
        base = fw / "src/main/resources/baselines/shop"
        base.mkdir(parents=True)
        (base / "LandingPage.json").write_text(json.dumps({
            "pageObject": "LandingPage", "recordedAt": recorded_at,
            "coverage": {"buyNowButton": 1, "thankYou": 0, "rows": 3, "either": 1},
            "fingerprints": {"buyNowButton": {"is_visible": True},
                             "either": {"is_visible": True}}}))
        (tmp_path / "audit").mkdir()
        return _load_action("04_run_and_fix.py", tmp_path / "audit", monkeypatch,
                            workspace=tmp_path)

    def test_only_what_matched_one_visible_element_in_this_run(self, tmp_path, monkeypatch):
        import time
        from datetime import datetime
        mod = self._setup(tmp_path, monkeypatch, datetime.now().isoformat(timespec="seconds"))
        proven = mod.record_proven_locators([self.REL], self.PLAN, "LandingTest#buy",
                                            time.time() - 5)
        assert [(p["name"], p["selector"]) for p in proven] == [
            ("buyNowButton", "a:has-text('Buy Now')")], (
            "0 matches, 3 matches and a list of alternatives prove nothing")
        data = json.loads((tmp_path / "audit" / "04-proven-locators.json").read_text())
        assert (data["web_base_url"], data["input_file"], data["feature_name"]) == (
            "https://shop.test/", "checkout.txt", "shop"), "readable without the session"

    def test_names_follow_step_02s_qualified_form(self, tmp_path, monkeypatch):
        import time
        from datetime import datetime
        mod = self._setup(tmp_path, monkeypatch, datetime.now().isoformat(timespec="seconds"))
        plan = {**self.PLAN, "web_pages": [
            {"class_name": "LandingPage", "locators_needed": ["buyNowButton"]},
            {"class_name": "OtherPage", "locators_needed": ["buyNowButton"]}]}
        proven = mod.record_proven_locators([self.REL], plan, "t", time.time() - 5)
        assert [p["name"] for p in proven] == ["LandingPage.buyNowButton"]

    def test_a_baseline_from_before_this_run_proves_nothing(self, tmp_path, monkeypatch):
        import time
        mod = self._setup(tmp_path, monkeypatch, "2020-01-01T00:00:00")
        assert mod.record_proven_locators([self.REL], self.PLAN, "t", time.time()) == []


def test_an_either_or_selector_is_dropped_and_the_click_refills_it(tmp_path, monkeypatch):
    """`button:has-text("Buy Now"), a:has-text("Buy Now")` counted 1 because only the
    link matched, and named neither."""
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    found, counts, visibles, rejected = mod.parse_selector_output(
        'SELECTOR_FOUND: buyNowButton=button:has-text("Buy Now"), a:has-text("Buy Now")'
        "|count=1|visible=1\nSELECTOR_FOUND: total=td.total|count=1|visible=1")
    assert found == {"total": "td.total"} and "alternatives" in rejected["buyNowButton"]
    rows = [{"clicked": {"sel": "a.btn.buy", "total": 1, "visible": 1, "text": "BUY NOW"}}]
    assert mod.recover_clicked_locators(found, counts, visibles, rows, ["buyNowButton"]) == [
        "buyNowButton"] and found["buyNowButton"] == "a.btn.buy"
    hints = [{"type": "button", "name": "payButton", "selector": "button.pay, a.pay",
              "text": "Pay", "count": 1}]
    assert mod.reconcile_hints(hints, found) == [], "an unconfirmed either/or hint is dropped"


# ── Flow API, reuse ladder and option enums ──────────────────────────────────

def _load_parse(tmp_path, monkeypatch):
    input_file = tmp_path / "input.txt"
    input_file.write_text("Module: shop\nType: web\n")
    monkeypatch.setenv("INPUT_FILE", str(input_file))
    return _load_action("01_parse.py", tmp_path, monkeypatch)


class TestFlowPlan:
    """Step 01's plan names what it reuses, what it adds, and which choices are enums."""

    def test_an_option_control_is_added_to_the_page_it_is_picked_on(self, tmp_path, monkeypatch):
        mod = _load_parse(tmp_path, monkeypatch)
        plan = {"web_pages": [{"class_name": "PaymentPage", "locators_needed": ["payButton"]}],
                "option_enums": [{"name": "PaymentMethod", "page": "PaymentPage",
                                  "control": "paymentMethodOption", "exercised": ["CreditCard"]}]}
        notes = mod.settle_flow_plan(plan, False, {})
        assert plan["web_pages"][0]["locators_needed"] == ["payButton", "paymentMethodOption"]
        assert notes["controls_added"] == ["PaymentPage.paymentMethodOption"]
        assert plan["reuse"] == [] and plan["helper_web_methods"] == []

    def test_a_reuse_claim_for_a_method_that_does_not_exist_is_set_aside(self, tmp_path, monkeypatch):
        mod = _load_parse(tmp_path, monkeypatch)
        known = {"WaitHelper": {"waitForUrl"}, "ShopHelper": {"checkout"}}
        plan = {"reuse": [
            {"existing": "WaitHelper.waitForUrl(Config config, String url)", "how": "as_is"},
            {"existing": "ShopHelper.makePayment(PaymentMethod method)", "how": "extend",
             "change": "add the wallet case"},
            {"existing": "the login flow", "how": "as_is"}]}
        notes = mod.settle_flow_plan(plan, True, known)
        assert [e["existing"] for e in plan["reuse"]] == [
            "WaitHelper.waitForUrl(Config config, String url)", "the login flow"]
        assert [e["existing"] for e in plan["reuse_unknown"]] == [
            "ShopHelper.makePayment(PaymentMethod method)"]
        assert notes["reuse_unchecked"] == ["the login flow"]

    def test_a_new_operation_in_an_existing_module_must_say_why(self, tmp_path, monkeypatch):
        mod = _load_parse(tmp_path, monkeypatch)
        plan = {"helper_web_methods": [{"name": "checkout", "why_new": "nothing checks out yet"},
                                       {"name": "confirmOtp"}]}
        assert mod.settle_flow_plan(plan, True, {})["missing_why_new"] == ["confirmOtp"]
        new_module = {"helper_web_methods": [{"name": "confirmOtp"}]}
        assert mod.settle_flow_plan(new_module, False, {})["missing_why_new"] == []

    def test_provenance_is_read_from_a_business_steps_checks(self, tmp_path, monkeypatch):
        """Before this, the visitor ran a regex over every step and raised on a dict."""
        mod = _load_parse(tmp_path, monkeypatch)
        plan = {"web_test_methods": [{"method_name": "pay", "steps": [
            {"logstep": "Pay and verify the receipt  [source: user]", "call": "shop.pay()",
             "checks": ["Verify the receipt shows the total  [source: user]"]}]}]}
        mod.resolve_check_provenance(plan, "Pay, then verify the receipt shows the total")
        step = plan["web_test_methods"][0]["steps"][0]
        assert step["logstep"] == "Pay and verify the receipt"
        assert step["checks"] == ["Verify the receipt shows the total"]
        assert "Verify the receipt shows the total" in plan["check_provenance"]


SHOP_HELPER = '''package automation.modules.shop;

public class ShopHelper extends ApiHelper
{
    public String checkout(ShopData order)
    {
        CartPage cart = new CartPage(config);
        cart.fillDetails(order);
        cart.confirm();
        return cart.submit().getTotal();
    }

    public void refund(ShopData order)
    {
        new OrdersPage(config).refund(order.getId());
    }
}
'''


class TestFlowApiCodegen:
    def test_dropping_an_invented_check_keeps_its_business_step(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        shop_input = ("Module: shop\nType: web\n\nSteps:\n1. Open the profile page\n"
                      "2. Change the summary and save it\n"
                      "3. Verify the saved summary is shown after a reload\n")
        plan = {"web_pages": [{"class_name": "ProfilePage",
                               "locators_needed": ["saveButton", "successToast"],
                               "actions_needed": ["saveSummary", "isSuccessToastVisible"]}],
                "web_test_methods": [{"method_name": "editSummary", "steps": [
                    {"logstep": "Save the summary", "call": "shop.saveSummary(text)",
                     "checks": ["assertTrue isSuccessToastVisible on the returned ProfilePage"]}]}]}
        web = {"selectors": {"saveButton": "#save"},
               "steps_unverified": [f"{TOAST_CHECK}|no element confirmed|none"]}
        out = mod.prune_unverified_checks(plan, web, shop_input)
        assert out["dropped"] == [TOAST_CHECK]
        step = plan["web_test_methods"][0]["steps"][0]
        assert step["logstep"] == "Save the summary" and step["call"] == "shop.saveSummary(text)"
        assert step["checks"] == []

    def test_plan_files_add_the_enums_class_and_what_an_existing_module_extends(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRAMEWORK_DIR", raising=False)
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        module = mod.AUTOMATION_FRAMEWORK_DIR / "src/main/java/automation/modules/shop"
        (module / "web").mkdir(parents=True)
        (module / "ShopHelper.java").write_text(SHOP_HELPER)
        (module / "web" / "PaymentPage.java").write_text("public class PaymentPage {}")
        plan = {"web_pages": [], "data_fields": [{"name": "promo"}],
                "option_enums": [{"name": "PaymentMethod", "control": "paymentMethodOption"}],
                "reuse": [{"existing": "PaymentPage.choose(PaymentMethod method)", "how": "extend",
                           "change": "add the wallet case"},
                          {"existing": "WaitHelper.waitForUrl(Config config, String url)",
                           "how": "as_is"}]}
        files = mod._plan_files(plan, "web", True, "", "", "Shop", "shop")
        base = "src/main/java/automation/modules/shop/"
        for wanted in ("ShopEnums.java", "ShopData.java", "ShopBuilder.java", "web/PaymentPage.java"):
            assert base + wanted in files
        assert not any("WaitHelper" in f for f in files)      # shared code is never edited
        assert len(files) == len(set(files))
        assert mod._layer_of(base + "ShopEnums.java") < mod._layer_of(base + "ShopData.java") \
            < mod._layer_of(base + "web/PaymentPage.java") < mod._layer_of(base + "ShopHelper.java")

    def test_the_option_hint_lists_what_the_page_offered_and_how_to_select_any(self, tmp_path, monkeypatch):
        from shared import frames
        monkeypatch.setenv("AUTOMATION_FRAMEWORK", "playwright")
        mod = _load_action("03_generate.py", tmp_path, monkeypatch)
        plan = {"option_enums": [
            {"name": "PaymentMethod", "page": "PaymentPage", "control": "paymentMethodOption",
             "exercised": ["CreditCard"]},
            {"name": "Promo", "page": "PaymentPage", "control": "promoOption", "exercised": ["NoPromo"]}]}
        web = {"option_sets": {"paymentMethodOption": {
            "attribute": "data-option", "chosen": "card", "truncated": False,
            "template": frames.join(["#pay"], "a[data-option='{key}']"),
            "options": [{"key": "card", "label": "Card", "occurrences": 1},
                        {"key": "wallet", "label": "Wallet", "occurrences": 2}]}}}
        hint = mod.option_sets_hint(web, plan)
        assert 'key "wallet", label "Wallet" — shown 2 times' in hint
        assert '''locator("a[data-option='" + paymentMethod.getKey() + "']")''' in hint
        assert "Promo (PaymentPage.promoOption): no alternatives were recorded" in hint
        assert mod.option_sets_hint(web, {"option_enums": []}) == ""

    def test_the_existing_file_banner_points_at_the_reuse_ledger(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRAMEWORK_DIR", raising=False)
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        existing = mod.AUTOMATION_FRAMEWORK_DIR / "src/main/java/X.java"
        existing.parent.mkdir(parents=True)
        existing.write_text("public class X {}")
        banner = mod.read_existing_files_context(["src/main/java/X.java"])
        assert '"extend"' in banner and "do not remove or rewrite" not in banner

    def test_the_narration_repair_may_split_narration_but_not_change_calls(self, tmp_path, monkeypatch):
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        path = "src/test/java/automation/shop/ShopWebTest.java"
        original = '''public class ShopWebTest extends TestBase
{
    @Test(dataProvider = "getConfig")
    public void pay(Config config)
    {
        ShopHelper shop = new ShopHelper(config);
        config.logStep("Check out, pay by card and verify the receipt");
        String total = shop.checkout(order);
        ShopHelper.Receipt receipt = shop.makePayment(PaymentMethod.CreditCard, order);
        AssertHelper.assertEquals(config, receipt.getAmount(), total, "Receipt should charge the total");
    }
}
'''
        split_only = original.replace(
            '        config.logStep("Check out, pay by card and verify the receipt");\n'
            '        String total = shop.checkout(order);\n',
            '        config.logStep("Check out the order");\n'
            '        String total = shop.checkout(order);\n\n'
            '        config.logStep("Pay by card and verify the receipt charges the total");\n')
        unpacked = split_only.replace(
            "        ShopHelper.Receipt receipt = shop.makePayment(PaymentMethod.CreditCard, order);\n",
            "        CardPage card = new CardPage(config);\n        card.fillCardNumber(order.getCard());\n"
            "        ShopHelper.Receipt receipt = card.pay();\n")
        plan = {"web_test_methods": [{"method_name": "pay", "steps": [
            {"logstep": "Check out the order", "call": "shop.checkout(order) -> total", "checks": []},
            {"logstep": "Pay by card and verify the receipt", "call": "shop.makePayment(...)",
             "checks": ["assertEquals receipt.amount total"]}]}]}
        prompts = []

        def answer(content):
            def fake(prompt, label=""):
                prompts.append(prompt)
                return json.dumps({path: content})
            return fake

        monkeypatch.setattr(mod, "call_claude", answer(unpacked))
        files, _ = mod._repair_step_narration({path: original}, plan)
        assert files[path] == original, "a repair that unpacks an operation is rejected"
        assert "<support_files>" not in prompts[0]

        monkeypatch.setattr(mod, "call_claude", answer(split_only))
        files, _ = mod._repair_step_narration({path: original}, plan)
        assert files[path] == split_only

    def test_review_records_name_what_changed_in_existing_code(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRAMEWORK_DIR", raising=False)
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        helper = "src/main/java/automation/modules/shop/ShopHelper.java"
        enums = "src/main/java/automation/modules/shop/ShopEnums.java"
        test = "src/test/java/automation/shop/ShopWebTest.java"
        (mod.AUTOMATION_FRAMEWORK_DIR / helper).parent.mkdir(parents=True)
        (mod.AUTOMATION_FRAMEWORK_DIR / helper).write_text(SHOP_HELPER)
        after = SHOP_HELPER.replace("new OrdersPage(config).refund(order.getId());",
                                    "new OrdersPage(config).refund(order.getId(), true);").replace(
            "    public void refund(ShopData order)",
            "    public String checkoutExpress(ShopData order)\n    {\n"
            "        CartPage cart = new CartPage(config);\n        cart.fillDetails(order);\n"
            "        cart.confirm();\n        return cart.submit().getTotal();\n    }\n\n"
            "    public void refund(ShopData order)")
        written = {
            helper: after,
            enums: ('public class ShopEnums { public enum PaymentMethod { CreditCard("card", "Card"), '
                    'Cheque("cheque", "Cheque"); } }'),
            test: ('public class ShopWebTest { @Test public void pay(Config config) { '
                   'config.logStep("Check out"); shop.checkout(order); } }'),
        }
        plan = {"option_enums": [{"name": "PaymentMethod", "page": "PaymentPage",
                                  "control": "paymentMethodOption"}],
                "reuse": [{"existing": "ShopHelper.checkout(ShopData order)", "how": "as_is"},
                          {"existing": "WaitHelper.waitForUrl(Config config, String url)",
                           "how": "as_is"}]}
        web = {"option_sets": {"paymentMethodOption": {"options": [
            {"key": "card", "label": "Card", "occurrences": 1},
            {"key": "wallet", "label": "Wallet", "occurrences": 1}]}}}
        review = mod._review_records(written, {helper: SHOP_HELPER}, plan, web, "shop", "Shop")

        assert review["modified_existing"][helper] == {
            "changed": ["ShopHelper.refund(ShopData)"],
            "added": ["ShopHelper.checkoutExpress(ShopData)"], "removed": []}
        assert review["changed_existing_api"] == {}
        assert review["near_duplicates"] == [{"new": "ShopHelper.checkoutExpress(ShopData)",
                                              "like": "ShopHelper.checkout(ShopData)", "ratio": 1.0}]
        assert review["option_enum_gaps"] == {"PaymentMethod": {"missing_keys": ["wallet"],
                                                                "unobserved_constants": 0}}
        assert set(review["test_shape"]["methods"]) == {"ShopWebTest#pay"}
        assert review["reuse_unused"] == ["WaitHelper.waitForUrl(Config config, String url)"]


def test_step_02_writes_the_option_sets_it_found(tmp_path, monkeypatch):
    mod = _load_action("02_validate_web.py", tmp_path, monkeypatch)
    sets = {"paymentMethodOption": {
        "attribute": "data-option", "chosen": "card", "template": "", "truncated": False,
        "options": [{"key": "card", "label": "Card", "occurrences": 1},
                    {"key": "wallet", "label": "Wallet", "occurrences": 2}]}}
    mod._write_result({"paymentMethodOption": "a[data-option='card']"}, ["Pick the method"], [],
                      selector_counts={"paymentMethodOption": 1}, option_sets=sets)
    assert json.loads((tmp_path / "02-validate-web.json").read_text())["option_sets"] == sets
    md = (tmp_path / "02-validate-web.md").read_text()
    assert "## Option sets seen" in md and "Wallet (`wallet`, ×2)" in md


class TestRegressionReRun:
    """A slight change to an existing method is only safe if its existing callers
    still pass, and step 04 otherwise runs only the new test."""

    HELPER = '''package automation.modules.shop;

public class ShopHelper {
    public void pay() { }
    public void refund() { }
}
'''

    def _setup(self, tmp_path, monkeypatch):
        from shared import blast_radius
        monkeypatch.delenv("FRAMEWORK_DIR", raising=False)
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        root = mod.AUTOMATION_FRAMEWORK_DIR
        helper = "src/main/java/automation/modules/shop/ShopHelper.java"
        (root / helper).parent.mkdir(parents=True)
        (root / helper).write_text(self.HELPER.replace("public void pay() { }",
                                                       "public void pay() { confirm(); }"))
        tests = root / "src/test/java/automation/shop"
        tests.mkdir(parents=True)
        for name, method, call in (("PayTest", "pays", "pay"), ("RefundTest", "refunds", "refund")):
            (tests / f"{name}.java").write_text(
                f"package automation.shop;\n\npublic class {name} {{\n"
                f"    @Test public void {method}() {{ ShopHelper helper = new ShopHelper(); "
                f"helper.{call}(); }}\n}}\n")
        snapshot = tmp_path / "pre-run" / helper
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(self.HELPER)
        blast_radius._cache.clear()
        calls = []
        return mod, calls

    def test_only_tests_reaching_a_changed_method_rerun_with_their_own_baselines(self, tmp_path, monkeypatch):
        mod, calls = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "run_maven_test",
                            lambda k, m, baseline_dir=None: calls.append((k, m, baseline_dir)) or (True, ""))
        result = mod.regression_check(["ShopWebTest#newOne"])
        assert result["tests"] == ["PayTest#pays"]
        assert calls == [("PayTest", "pays", tmp_path / "regression-baselines")]
        saved = json.loads((tmp_path / "04-regression.json").read_text())
        assert saved["results"] == {"PayTest#pays": {"status": "passed", "first_error": ""}}

    def test_a_failure_is_retried_once_then_reported_with_its_first_error(self, tmp_path, monkeypatch):
        mod, calls = self._setup(tmp_path, monkeypatch)
        outputs = iter([(False, "[ERROR] flaky"),
                        (False, "[ERROR]   PayTest.pays:3 » AssertionError Expected 1 but got 2")])
        monkeypatch.setattr(mod, "run_maven_test",
                            lambda k, m, baseline_dir=None: calls.append(m) or next(outputs))
        result = mod.regression_check([])
        assert calls == ["pays", "pays"]
        assert result["results"]["PayTest#pays"]["status"] == "failed"
        assert "AssertionError" in result["results"]["PayTest#pays"]["first_error"]

    def test_over_the_cap_nothing_is_rerun_and_the_reason_is_recorded(self, tmp_path, monkeypatch):
        mod, calls = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "REGRESSION_MAX_TESTS", 0)
        monkeypatch.setattr(mod, "run_maven_test", lambda *a, **k: calls.append(a) or (True, ""))
        result = mod.regression_check([])
        assert calls == [] and result["not_run_reason"]

    def test_a_fix_keeps_the_first_pre_run_copy_and_skips_files_this_run_created(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch, workspace=tmp_path)
        (tmp_path / "03-generate.json").write_text(json.dumps({"created_files": ["src/main/java/New.java"]}))
        mod._snapshot_before_fix("src/main/java/Old.java", "before")
        mod._snapshot_before_fix("src/main/java/Old.java", "after a first fix")
        mod._snapshot_before_fix("src/main/java/New.java", "generated")
        assert (tmp_path / "pre-run/src/main/java/Old.java").read_text() == "before"
        assert not (tmp_path / "pre-run/src/main/java/New.java").exists()


class TestProgressIsNotARetry:
    """A fix that works and lets the test reach the next bug is progress, not a failed
    retry. Only attempts in a row that made none are charged to the budget."""

    def test_reaching_a_new_failure_is_progress_and_going_back_is_not(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        history = [{"targeted": "ShopWebTest.java:20", "failure_location": "ShopWebTest.java:31"}]
        seen = mod.seen_failures(history, "ShopWebTest.java:31")
        assert mod.made_progress(False, ["Cart.java"], "ShopWebTest.java:40", seen)
        assert not mod.made_progress(False, ["Cart.java"], "ShopWebTest.java:31", seen)  # same bug
        assert not mod.made_progress(False, ["Cart.java"], "ShopWebTest.java:20", seen)  # undone
        assert not mod.made_progress(False, [], "ShopWebTest.java:40", seen)            # nothing landed

    def test_the_streak_counts_only_trailing_attempts_without_progress(self, tmp_path, monkeypatch):
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod.no_progress_streak([{"progress": False}, {"progress": True},
                                       {"progress": False}, {}]) == 2
        assert mod.no_progress_streak([{"progress": True}]) == 0

    def test_the_verdict_charges_only_no_progress_and_keeps_a_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTHORING_FIX_RETRY_COUNT", "2")
        monkeypatch.setenv("AUTHORING_MAX_FIX_ATTEMPTS", "8")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        assert mod.retry_verdict("false", 0, 5) == "retry"       # five attempts, all progress
        assert mod.retry_verdict("false", 1, 1) == "retry"
        assert "no progress" in mod.retry_verdict("false", 2, 3)
        assert "ceiling" in mod.retry_verdict("false", 0, 8)
        for gate in ("true", "stuck", "defect", "skipped"):
            assert mod.retry_verdict(gate, 0, 1) == "stop"

    def test_the_gate_writes_the_verdict_from_the_history_on_disk(self, tmp_path, monkeypatch):
        from shared import fix_history
        monkeypatch.setenv("FIX_ATTEMPT", "3")
        monkeypatch.setenv("AUTHORING_FIX_RETRY_COUNT", "2")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        for progress in (False, True, True):
            fix_history.append(tmp_path, {"attempt": 1, "progress": progress})
        mod._write_gate("false")
        assert (tmp_path / ".fix-retry").read_text() == "retry"
        fix_history.append(tmp_path, {"attempt": 4, "progress": False})
        fix_history.append(tmp_path, {"attempt": 5, "progress": False})
        mod._write_gate("false")
        assert (tmp_path / ".fix-retry").read_text().startswith("stop: 2 fix attempt(s) in a row")

    def test_a_failing_initial_run_asks_for_the_first_fix_attempt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIX_ATTEMPT", "0")
        mod = _load_action("04_run_and_fix.py", tmp_path, monkeypatch)
        mod._write_gate("false")
        assert (tmp_path / ".fix-retry").read_text() == "retry"
        mod._write_gate("true")
        assert (tmp_path / ".fix-retry").read_text() == "stop"


class TestExistingTestsAreNotRewritten:
    """Extending a module rewrites its test class with a new method appended. The
    methods already there are shipped tests, and a repair pass once weakened one."""

    BEFORE = '''public class ShopWebTest extends TestBase
{
    @Test(dataProvider = "getConfig")
    public void pay(Config config)
    {
        config.logStep("Pay and verify the thank-you text");
        AssertHelper.assertEquals(config, shop.pay(), "Thanks.See you", "Thank-you text should match");
    }
}
'''
    NEW_METHOD = '''
    @Test(dataProvider = "getConfig")
    public void refund(Config config)
    {
        config.logStep("Refund and verify the refund text");
        AssertHelper.assertEquals(config, shop.refund(), "Refunded", "Refund text should match");
    }
}
'''

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRAMEWORK_DIR", raising=False)
        mod = _load_action("03_generate.py", tmp_path, monkeypatch, workspace=tmp_path)
        path = "src/test/java/automation/shop/ShopWebTest.java"
        (mod.AUTOMATION_FRAMEWORK_DIR / path).parent.mkdir(parents=True)
        (mod.AUTOMATION_FRAMEWORK_DIR / path).write_text(self.BEFORE)
        return mod, path

    def test_a_changed_existing_test_method_is_restored_and_new_ones_kept(self, tmp_path, monkeypatch):
        mod, path = self._setup(tmp_path, monkeypatch)
        weakened = self.BEFORE.replace(
            'AssertHelper.assertEquals(config, shop.pay(), "Thanks.See you", "Thank-you text should match");',
            'AssertHelper.assertTrue(config, shop.pay() != null, "Thank-you text should show");')
        generated = weakened.rstrip().rstrip("}") + self.NEW_METHOD
        files, restored = mod._restore_existing_tests({path: generated})
        assert restored == {path: ["pay"]}
        assert '"Thanks.See you"' in files[path] and "public void refund" in files[path]

    def test_reindenting_an_existing_test_is_not_a_change(self, tmp_path, monkeypatch):
        mod, path = self._setup(tmp_path, monkeypatch)
        reindented = self.BEFORE.replace("        config.logStep", "            config.logStep")
        assert mod._restore_existing_tests({path: reindented})[1] == {}

    def test_an_existing_tests_values_are_not_judged_against_this_runs_input(self, tmp_path, monkeypatch):
        mod, path = self._setup(tmp_path, monkeypatch)
        generated = self.BEFORE.rstrip().rstrip("}") + self.NEW_METHOD
        assert mod.untraced_expected_values({path: generated}, "Refund the order", {}) == {
            path: ["Refunded"]}
