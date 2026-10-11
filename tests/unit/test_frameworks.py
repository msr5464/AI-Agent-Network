"""Contract tests for the framework plugins.

There were none. The plugin suite shipped with two implementations, one of which
silently produced invalid or empty output for several inputs — invalid CSS from
unquoted attribute values, a `:contains()` selector handed to a Playwright
locator, and an empty snippet for every `role=` request — because nothing ever
asserted that a plugin's output was usable.

These are parity tests: whatever a plugin claims to support, it must return
something non-empty and syntactically plausible for.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from shared.frameworks import detect, get_plugin

ALL = pytest.mark.parametrize("framework", detect.SUPPORTED)

# One case per branch a CodeEngine is expected to handle.
EMIT_CASES = [
    ("testid", {"testid": "submit-btn"}),
    ("testid with a space", {"testid": "my testid"}),
    ("testid with a quote", {"testid": 'say "hi"'}),
    ("role+name", {"role": "button", "name": "Sign in"}),
    ("role only", {"role": "link"}),
    ("placeholder", {"placeholder": "Email address"}),
    ("label", {"label": "Username"}),
    ("alt", {"alt": "Company logo"}),
    ("title", {"title": "Close"}),
    ("text exact", {"text": "Log out", "exact": True}),
    ("text loose", {"text": "Log out"}),
    ("id selector", {"selector": "#login_field"}),
    ("css selector", {"selector": ".btn.btn-primary"}),
]


@ALL
@pytest.mark.parametrize("label,kwargs", EMIT_CASES, ids=[c[0] for c in EMIT_CASES])
def test_emit_locator_never_returns_an_empty_snippet(framework, label, kwargs):
    """An unhandled branch used to fall through to {"python": "", "java": ""}.

    A silently empty locator is worse than a crash: it reaches the prompt as a
    blank, and the model writes something plausible instead.
    """
    emitted = get_plugin(framework).code.emit_locator(**kwargs)
    assert emitted.get("python"), f"{framework} produced no python for {label}"
    assert emitted.get("java"), f"{framework} produced no java for {label}"


@ALL
def test_attribute_values_are_quoted(framework):
    """`[data-testid=my testid]` is not valid CSS.

    Values were interpolated bare, so anything with a space, a quote or a
    leading digit produced a selector that could not match.
    """
    emitted = get_plugin(framework).code.emit_locator(testid="my testid")
    for snippet in (emitted["python"], emitted["java"]):
        if "data-testid" not in snippet:
            continue        # framework used a dedicated accessor, nothing to quote
        attribute = re.search(r"\[data-testid=([^\]]+)\]", snippet)
        assert attribute, snippet
        value = attribute.group(1)
        assert value[0] in "'\"", f"{framework} emitted an unquoted value: {snippet}"


@ALL
def test_has_text_selector_is_valid_for_the_instrument(framework):
    """The result is handed to a live browser locator, not to BeautifulSoup.

    Selenium's returned `:contains(...)`, which no browser accepts, so every
    scoped-by-neighbor candidate silently failed its uniqueness check.
    """
    selector = get_plugin(framework).code.build_has_text_selector("div", "Total", "span")
    assert selector
    assert ":contains(" not in selector, f"{framework} emitted a non-browser selector"


@ALL
def test_diagnostics_recognise_their_own_ambiguity_error(framework):
    """Each framework must recognise the phrasing IT actually emits."""
    engine = get_plugin(framework).diagnostics
    samples = {
        "playwright": "Error: strict mode violation: locator('.btn') resolved to 3 elements",
        "selenium": "Found multiple elements matching locator .btn",
    }
    assert engine.is_ambiguous_locator(samples[framework])
    assert not engine.is_ambiguous_locator("net::ERR_CONNECTION_REFUSED")


@ALL
def test_diagnostics_recognise_their_own_locator_failures(framework):
    """Healing and element-name extraction ask the plugin this
    instead of matching one framework's exception names themselves."""
    engine = get_plugin(framework).diagnostics
    samples = {
        "playwright": ["TimeoutError: locator.click: Timeout 30000ms exceeded.",
                       "  - waiting for locator('#login-button')"],
        "selenium": ["org.openqa.selenium.NoSuchElementException: no such element: "
                     "Unable to locate element: {\"method\":\"css selector\"}",
                     "org.openqa.selenium.StaleElementReferenceException: stale element "
                     "reference: element is not attached to the page document"],
    }
    for message in samples[framework]:
        assert engine.is_locator_resolution_failure(message), message
    assert not engine.is_locator_resolution_failure("AssertionError: expected [3] but found [2]")


@ALL
def test_code_engine_declares_its_repo_conventions(framework):
    """Element types, selector-taking calls and raw driver calls come from the
    plugin, so the edit guards and code analyser name no framework themselves."""
    code = get_plugin(framework).code
    assert code.ELEMENT_TYPES and code.LOCATOR_CALLS and code.RAW_DRIVER_CALLS
    for pattern, label in code.RAW_DRIVER_CALLS:
        assert pattern.pattern and label


@ALL
def test_optional_telemetry_members_default_safely(framework, tmp_path):
    """A framework with no network log returns [] rather than raising."""
    telemetry = get_plugin(framework).telemetry
    assert telemetry.read_network(tmp_path / "missing") == []
    assert isinstance(telemetry.NOISE_ACTIONS, frozenset)


@ALL
def test_plugin_exposes_the_four_contract_seams(framework):
    plugin = get_plugin(framework)
    for seam in ("telemetry", "runner", "diagnostics", "code"):
        assert getattr(plugin, seam) is not None, f"{framework} is missing {seam}"


@ALL
def test_mcp_is_not_a_plugin_concern(framework):
    """Browser inspection is the agents' instrument, not a repo convention.

    Both implementations of the old MCPProvider returned the same Playwright
    server and the same allowed-tools list, so the abstraction bought nothing.
    """
    assert not hasattr(get_plugin(framework), "mcp")


def test_unknown_framework_is_refused_loudly():
    with pytest.raises(ValueError, match="Unsupported"):
        get_plugin("cypress")


# ── Detection ─────────────────────────────────────────────────────────────────
def test_detects_playwright_from_a_maven_pom(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>com.microsoft.playwright</groupId><artifactId>playwright</artifactId>"
        "</dependency></dependencies></project>")
    assert detect.detect_from_repo(tmp_path) == detect.PLAYWRIGHT


def test_detects_selenium_from_a_maven_pom(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.seleniumhq.selenium</groupId><artifactId>selenium-java</artifactId>"
        "</dependency></dependencies></project>")
    assert detect.detect_from_repo(tmp_path) == detect.SELENIUM


def test_playwright_wins_when_a_repo_carries_both(tmp_path):
    """Appium pulls in selenium-remote-driver, so a Playwright repo can declare
    both. The framework the TESTS are written against is the one that counts."""
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies>"
        "<dependency><groupId>com.microsoft.playwright</groupId></dependency>"
        "<dependency><groupId>org.seleniumhq.selenium</groupId>"
        "<artifactId>selenium-remote-driver</artifactId></dependency>"
        "</dependencies></project>")
    assert detect.detect_from_repo(tmp_path) == detect.PLAYWRIGHT


def test_explicit_override_wins_but_is_reported(tmp_path, monkeypatch, capsys):
    """An override that contradicts the repo is the exact misconfiguration that
    silently disabled locator extraction — so it must be said out loud."""
    (tmp_path / "pom.xml").write_text("<project>com.microsoft.playwright</project>")
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", "selenium")
    framework, why = detect.resolve(tmp_path)
    assert (framework, why) == ("selenium", "AUTOMATION_FRAMEWORK")
    assert "WARNING" in capsys.readouterr().out


def test_falls_back_to_playwright_when_nothing_is_knowable(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOMATION_FRAMEWORK", raising=False)
    assert detect.resolve(tmp_path)[0] == detect.PLAYWRIGHT


def test_real_target_repos_resolve_correctly(monkeypatch):
    """The repos this network is actually pointed at, if they are checked out."""
    monkeypatch.delenv("AUTOMATION_FRAMEWORK", raising=False)
    expected = {
        Path("/Users/mukesh/msr5464/Jarvis"): detect.PLAYWRIGHT,
        Path("/Users/mukesh/msr5464/Selenium-Automation-Framework"): detect.SELENIUM,
    }
    checked = 0
    for repo, want in expected.items():
        if not repo.is_dir():
            continue
        assert detect.detect_from_repo(repo) == want, repo
        checked += 1
    if not checked:
        pytest.skip("neither target repo is checked out here")


# ── Telemetry ─────────────────────────────────────────────────────────────────
def test_selenium_telemetry_is_reachable(tmp_path, monkeypatch):
    """The whole Selenium evidence chain, which used to be impossible.

    Every discovery site globbed traces/<method>_*.zip — Playwright's layout —
    while SeleniumTelemetryParser accepted only .jsonl, so it could never be
    handed a path it would take. Its telemetry was unreachable by construction.
    """
    import json
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", "selenium")
    monkeypatch.setenv("FRAMEWORK_DIR", "")

    log_dir = tmp_path / "telemetry"
    log_dir.mkdir()
    (log_dir / "testCheckout_20260909.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"command": "get", "url": "https://shop.example/login"},
        {"command": "click", "locator": "By.css: .checkout",
         "exception": "NoSuchElementException: no such element"},
    ]))

    from shared import telemetry
    found = telemetry.discover(tmp_path, "testCheckout")
    assert found, "Selenium telemetry is still undiscoverable"

    actions = telemetry.read_actions(found[0])
    assert len(actions) == 2
    failed = telemetry.failing_action(actions)
    assert failed and failed["selector"] == "By.css: .checkout"


@ALL
def test_parsers_return_the_declared_schema(framework, tmp_path, monkeypatch):
    """Consumers index these keys directly; a parser returning its raw records
    raised KeyError inside prompt construction."""
    import json
    from shared.frameworks.base import TelemetryParser

    monkeypatch.setenv("AUTOMATION_FRAMEWORK", framework)
    monkeypatch.setenv("FRAMEWORK_DIR", "")
    parser = get_plugin(framework).telemetry

    if framework == "selenium":
        artifact = tmp_path / "run.jsonl"
        artifact.write_text(json.dumps({"command": "click", "locator": "#a"}) + "\n")
        actions = parser.read_actions(artifact)
    else:
        actions = [parser.normalise({"action": "click", "selector": "#a"})]

    assert actions
    for action in actions:
        for key in TelemetryParser.ACTION_KEYS:
            assert key in action, f"{framework} omitted {key!r}"


@ALL
def test_format_for_prompt_survives_sparse_records(framework, monkeypatch):
    """Fields were read as direct subscripts, so a record missing any one of
    them crashed the prompt builder at the moment the evidence was needed."""
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", framework)
    monkeypatch.setenv("FRAMEWORK_DIR", "")
    from shared import telemetry
    rendered = telemetry.format_for_prompt([{"action": "click"},
                                            {"selector": "#only-a-selector"}])
    assert isinstance(rendered, str)


def test_the_values_a_test_typed_reach_the_prompt(tmp_path, monkeypatch):
    """Observed: shifted test data typed "India" as a card number. The timeline
    showed selectors only, and a keystroke-typed value was not even parsed, so
    three fix attempts never saw it."""
    import json, zipfile
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", "playwright")
    monkeypatch.setenv("FRAMEWORK_DIR", "")
    events = [{"type": "before", "callId": str(i), "class": "Frame", "method": m,
               "params": {"selector": s, **p}, "startTime": i}
              for i, (m, s, p) in enumerate([
                  ("fill", "#address", {"value": "Bangalore"}),
                  ("type", "#card-number", {"text": "India"}),
                  ("fill", "#password", {"value": "Secret1"}),
                  ("click", "label[for='690']", {})])]
    trace = tmp_path / "t.zip"
    with zipfile.ZipFile(trace, "w") as z:
        z.writestr("trace.trace", "\n".join(json.dumps(e) for e in events))
    from shared import telemetry
    rendered = telemetry.format_for_prompt(telemetry.read_actions(trace))
    assert "#card-number  <- 'India'" in rendered
    assert "#address  <- 'Bangalore'" in rendered
    assert "#password  <- 'Secret1'" in rendered, "a wrong password is a cause a fixer must see"


@ALL
def test_discover_is_quiet_when_there_is_nothing(framework, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_FRAMEWORK", framework)
    monkeypatch.setenv("FRAMEWORK_DIR", "")
    assert get_plugin(framework).telemetry.discover(tmp_path, "nothingHere") == []
    assert get_plugin(framework).telemetry.discover(tmp_path / "missing", "x") == []


# ── A wait that polls until it gives up ───────────────────────────────────────
def _poll(selector, action="Frame.isVisible"):
    return {"action": action, "selector": selector, "error": ""}


def test_a_polled_wait_is_named_through_the_evidence_recorded_after_it():
    """Nothing throws when a wait polls until its budget runs out, so the failing
    selector is inferred from the streak. A framework that then records why the
    wait failed — counting its anchor, and every other locator on the page
    object — makes those calls into the same trace, after the polling. Reading
    only the very tail saw a streak of one and named nothing.
    """
    telemetry = get_plugin("playwright").telemetry
    actions = ([{"action": "Frame.click", "selector": "#checkout", "error": ""}]
               + [_poll("div.order-total") for _ in range(12)]
               + [_poll("div.order-total", "Frame.queryCount"),
                  _poll("div.order-total >> nth=0"),
                  _poll("div.details", "Frame.queryCount"),
                  _poll("div.phone", "Frame.queryCount"),
                  _poll("div.order-total", "Frame.queryCount")])
    failed = telemetry.failing_action(actions)
    assert failed and failed["selector"] == "div.order-total"
    assert failed["inferred"] is True


def test_a_streak_the_flow_moved_on_from_is_not_the_failure():
    """Polling that was followed by another interaction succeeded: the flow went
    on, so that selector is not what the test died waiting for."""
    telemetry = get_plugin("playwright").telemetry
    actions = ([_poll("div.spinner") for _ in range(6)]
               + [{"action": "Frame.click", "selector": "#next", "error": ""}])
    assert telemetry.failing_action(actions) is None


def test_an_action_that_threw_still_wins_over_any_streak():
    telemetry = get_plugin("playwright").telemetry
    actions = ([_poll("div.order-total") for _ in range(5)]
               + [{"action": "Frame.fill", "selector": "#card-code",
                   "error": "Timeout 30000ms exceeded."}])
    assert telemetry.failing_action(actions)["selector"] == "#card-code"


# ── The code emitted for a selector has to BE that selector ───────────────────
def test_an_exact_attribute_is_emitted_as_an_exact_builder():
    """What is proved unique is the selector; what is written into the page object
    is the snippet. The attribute builders match by substring unless told
    otherwise, so `[placeholder='123']` — one field — was emitted as a builder
    that also matched the field whose placeholder was "1234 1234 1234 1234", and
    an answer reported as proved failed its first run on an ambiguous locator.
    """
    code = get_plugin("playwright").code
    for key, builder in (("placeholder", "getByPlaceholder"), ("alt", "getByAltText"),
                         ("title", "getByTitle"), ("label", "getByLabel")):
        emitted = code.emit_locator(**{key: "123"}, exact=True)
        assert emitted["java"].startswith(f'page.{builder}("123", new Page.')
        assert emitted["java"].endswith("Options().setExact(true))")
        assert "exact=True" in emitted["python"]


def test_an_exact_builder_inside_an_iframe_uses_the_frame_options_type():
    emitted = get_plugin("playwright").code.emit_locator(
        placeholder="123", exact=True, frame_path=["#pay-frame"])
    assert emitted["java"] == (
        'page.frameLocator("#pay-frame").getByPlaceholder("123", '
        'new FrameLocator.GetByPlaceholderOptions().setExact(true))')


def test_a_caller_that_did_not_ask_for_exact_gets_the_plain_builder():
    emitted = get_plugin("playwright").code.emit_locator(label="Email")
    assert emitted["java"] == 'page.getByLabel("Email")'


def test_the_ladder_asks_for_exact_wherever_its_selector_is_exact():
    """Every rung pairs a selector with a snippet; the pair must match the same
    elements, or proving one says nothing about the other."""
    import yaml
    from shared import locator_emit
    from shared.locator_score import Volatility

    cfg = yaml.safe_load((Path(__file__).resolve().parents[2]
                          / "config" / "locator.yaml").read_text())
    element = {"tag": "input", "role": None, "accessible_name": None,
               "placeholder": "123", "alt": "card back", "title": "Security code",
               "id": None, "name": None, "text": "", "is_interactive": True,
               "class_list": []}
    by_strategy = {c["strategy"]: c for c in
                   locator_emit.candidates_for(element, Volatility(cfg))}
    for strategy in ("placeholder", "alt", "title"):
        assert "setExact(true)" in by_strategy[strategy]["java"], strategy
    assert by_strategy["placeholder"]["sel"] == "[placeholder='123']"


def test_an_unsettled_hint_takes_its_rung_and_the_name_built_from_it_off_the_ladder():
    """A text field's accessible name IS its placeholder when it has no label, so
    getByRole(TEXTBOX, "MM/YY") reads the same value through another door."""
    import yaml
    from shared import locator_emit
    from shared.locator_score import Volatility

    cfg = yaml.safe_load((Path(__file__).resolve().parents[2]
                          / "config" / "locator.yaml").read_text())
    element = {"tag": "input", "role": "textbox", "accessible_name": "MM/YY",
               "placeholder": "MM/YY", "title": "Expiry date", "id": "expiry",
               "name": None, "text": "", "is_interactive": True, "class_list": []}

    def rungs(**kwargs):
        return [c["strategy"] for c in
                locator_emit.candidates_for(element, Volatility(cfg), **kwargs)]

    assert rungs()[:2] == ["role+name", "placeholder"]
    assert rungs(unsettled=("placeholder",)) == ["title", "id"]
    # Named by something else, the role rung has nothing to do with the hint.
    element["accessible_name"] = "Expiry"
    assert rungs(unsettled=("placeholder",)) == ["role+name", "title", "id"]


# ── A getBy builder is a declared locator wherever it is written ──────────────
CARD_FORM = '''public class CardFormPage extends BasePage {
    private final Locator code;
    private final Locator number;
    private final Locator pay;
    private final Locator logo;
    private final Locator help;
    private final Locator submit;
    public CardFormPage(Config config) {
        code = page.frameLocator("#pay-frame").getByPlaceholder("123",
                new FrameLocator.GetByPlaceholderOptions().setExact(true));
        number = page.getByPlaceholder("1234 1234");
        pay = page.frameLocator("#pay-frame").getByRole(AriaRole.BUTTON,
                new FrameLocator.GetByRoleOptions().setName("Pay now").setExact(true));
        logo = this.page.getByAltText("Bank logo");
        help = page.frameLocator("#pay-frame").frameLocator("iframe[title='3ds']").getByTitle("Help");
        submit = page.frameLocator("#pay-frame").getByTestId("submit");
    }
}'''
HOP = " >> internal:control=enter-frame >> "


def _declared(source=CARD_FORM):
    return {d["name"]: d for d in get_plugin("playwright").code.extract_locators(source)}


def test_a_builder_inside_an_iframe_is_read_as_a_locator():
    """A repair writes whichever builder proved unique, inside a frame as readily
    as outside one. Only `.locator()` chains were read back, so a field repaired
    with a builder in a frame stopped being one of its page object's locators:
    it dropped out of the coverage count, and the next time it broke nothing
    could say which field declared it."""
    declared = _declared()
    assert set(declared) == {"code", "number", "pay", "logo", "help", "submit"}
    assert declared["code"]["selector"] == f"#pay-frame{HOP}[placeholder='123']"
    assert declared["code"]["frame_path"] == ["#pay-frame"]
    assert declared["submit"]["selector"] == f"#pay-frame{HOP}[data-testid='submit']"


def test_every_hop_of_a_nested_iframe_is_kept():
    declared = _declared()
    assert declared["help"]["frame_path"] == ["#pay-frame", "iframe[title='3ds']"]
    assert declared["help"]["selector"] == (
        f"#pay-frame{HOP}iframe[title='3ds']{HOP}[title='Help']")


def test_a_role_builder_in_an_iframe_keeps_its_name_and_its_frame():
    declared = _declared()
    assert declared["pay"]["kind"] == "role"
    assert declared["pay"]["accessible_name"] == "Pay now"
    assert declared["pay"]["frame_path"] == ["#pay-frame"]


def test_a_builder_outside_any_iframe_reads_as_before():
    declared = _declared()
    assert declared["number"]["selector"] == "[placeholder='1234 1234']"
    assert "frame_path" not in declared["number"]
    # The two builders a repair can emit and nothing used to read.
    assert declared["logo"]["selector"] == "[alt='Bank logo']"


def test_the_runtime_form_of_a_builder_is_the_declared_form():
    """A failure quotes a builder the way the runtime holds it; the declaration
    reads as the attribute it stands for. Unless both come out the same, a failing
    locator never matches the field that declares it."""
    code = get_plugin("playwright").code
    declared = _declared()
    runtime = f'#pay-frame{HOP}internal:attr=[placeholder="123"s]'
    assert code.normalize_selector(runtime) == declared["code"]["selector"]
    assert code.normalize_selector('Locator@internal:attr=[placeholder="1234 1234"i]') == \
        declared["number"]["selector"]
    assert code.normalize_selector('internal:testid=[data-testid="submit"s]') == \
        "[data-testid='submit']"
