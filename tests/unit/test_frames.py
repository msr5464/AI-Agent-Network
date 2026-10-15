"""Elements inside iframes, end to end through the pieces every agent shares.

The chain format is Playwright's own (`a >> internal:control=enter-frame >> b`),
so one string has to survive the page object, the failure snapshot, the guards,
the flow map and the healer. Each test below is one of those seams.
"""
import json
from pathlib import Path

import pytest
import yaml

from shared import dom_snapshot, edit_guards, flow_map, frames, page_identity
from shared.frameworks.playwright_plugin import PlaywrightCodeEngine
from shared.frameworks.selenium_plugin import SeleniumPlugin
from shared.locator_emit import candidates_for
from shared.locator_score import Volatility
from shared.mcp_config import CAPTURE_RULES, write_mcp_config

PAY, BANK = "iframe[name^='popup_']", "iframe[title='3ds']"
AMOUNT = frames.join([PAY, BANK], "#amount")


def _snapshot(tmp_path):
    """A top page with a nested payment iframe and two identical widget iframes."""
    records = [
        {"index": 1, "parent": 0, "ordinal": 0, "html": "<button>chat</button>"},
        {"index": 2, "parent": 0, "ordinal": 1, "html": "<button>chat</button>"},
        {"index": 3, "parent": 0, "ordinal": 2,
         "html": "<p>Pay</p><iframe title='3ds' src='/bank'></iframe>"},
        {"index": 4, "parent": 3, "ordinal": 0, "html": "<span id='amount'>19000.00</span>"},
    ]
    sidecar = tmp_path / "snap.frames.json"
    sidecar.write_text(json.dumps(records))
    html = (f'<!-- qa-agent-network:dom-snapshot url="https://shop.example.com" '
            f'frames="{sidecar}" -->\n<h1>Shop</h1><iframe class="w"></iframe>'
            f'<iframe class="w"></iframe><iframe name="popup_1790441440177"></iframe>')
    return page_identity.parse(html)


def test_a_page_object_field_inside_frames_reads_as_one_chain():
    source = '''
        private final Locator amount = page.locator("iframe[name^='popup_']").contentFrame()
            .locator("iframe[title='3ds']").contentFrame().locator("#amount");
        private final Locator pay = page.frameLocator("iframe[name^='popup_']").locator("#pay");
    '''
    found = {e["name"]: e["raw"] for e in PlaywrightCodeEngine().extract_locators(source)}
    # The iframe itself must never be recorded as the field's locator.
    assert found == {"amount": AMOUNT, "pay": frames.join([PAY], "#pay")}


def test_a_chain_is_written_back_as_frame_locator_code():
    code = PlaywrightCodeEngine().emit_locator(selector=AMOUNT)
    assert code["java"] == ('page.frameLocator("iframe[name^=\'popup_\']")'
                            '.frameLocator("iframe[title=\'3ds\']").locator("#amount")')
    role = PlaywrightCodeEngine().emit_locator(role="BUTTON", name="OK", frame_path=[PAY])
    assert "new FrameLocator.GetByRoleOptions()" in role["java"]
    # Selenium cannot enter a frame inside a By: no code beats wrong code.
    assert SeleniumPlugin().code.emit_locator(selector=AMOUNT)["java"] == ""


def test_a_chain_is_followed_through_the_captured_frames(tmp_path):
    soup = _snapshot(tmp_path)
    assert [n.get_text() for n in page_identity.select(soup, AMOUNT)] == ["19000.00"]
    assert page_identity.select(soup, frames.join(["#nothing"], "#amount")) == []
    # Two iframes match this hop: Playwright would refuse to act, so no count is honest.
    assert page_identity.select(soup, frames.join(["iframe.w"], "button")) is None
    assert dom_snapshot.selector_visibility(AMOUNT, soup, {}) == (1, 1)


def test_a_frame_fix_is_judged_inside_its_frame(tmp_path):
    soup = _snapshot(tmp_path)
    before = ('Locator amount = page.frameLocator("iframe[name^=\'popup_\']")'
              '.frameLocator("iframe[title=\'3ds\']").locator("#old");')
    ok, _ = edit_guards.validate_diagnosis_fit(
        before, before.replace("#old", "#amount"), "LOCATOR_STALE", soup, {})
    assert ok
    ok, reason = edit_guards.validate_diagnosis_fit(
        before, before.replace("#old", "#guess"), "LOCATOR_STALE", soup, {})
    assert not ok and "guess" in reason


def test_the_flow_map_recounts_a_chain_inside_its_frame_only():
    inventory = [{"tag": "span", "id": "amount", "frame": frames.join([PAY], "")},
                 {"tag": "span", "id": "amount"}]
    assert flow_map.count_in_inventory(frames.join([PAY], "#amount"), inventory) == 1
    assert flow_map.count_in_inventory("#amount", inventory) == 1
    assert flow_map.count_in_inventory(frames.join(["#other"], "#amount"), inventory) == 0


def test_a_healed_locator_keeps_its_frames():
    el = {"tag": "span", "id": "amount", "is_interactive": False}
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "locator.yaml").read_text())
    first = candidates_for(el, Volatility(cfg), frame_path=[PAY, BANK])[0]
    assert first["sel"] == AMOUNT
    assert first["java"].startswith('page.frameLocator("iframe[name^=\'popup_\']")')


def test_the_iframe_selector_rule_is_preloaded_not_pasted(tmp_path):
    # A model asked to paste the rule retyped it without the title rule, and
    # found no way into a 3-D Secure iframe identified only by its title.
    config = json.loads(write_mcp_config(tmp_path).read_text())
    args = config["mcpServers"]["playwright"]["args"]
    script = Path(args[args.index("--init-script") + 1])
    assert script.read_text().startswith(frames.INIT_JS) and frames.LINK_JS in frames.INIT_JS
    assert "page.qa" in CAPTURE_RULES and "const LINK" not in CAPTURE_RULES


def test_the_measuring_helpers_are_preloaded_not_pasted(tmp_path, monkeypatch):
    """A 15-minute validation spent a third of its output retyping the harvest, the
    count, the frame loop and the recorder in nearly every call. Both halves are
    loaded by the MCP server now, and the prompts only call them."""
    monkeypatch.delenv("PLAYWRIGHT_MCP_BROWSER", raising=False)
    args = json.loads(write_mcp_config(tmp_path).read_text())["mcpServers"]["playwright"]["args"]
    assert "window.__qa = " in Path(args[args.index("--init-script") + 1]).read_text()
    init_page = Path(args[args.index("--init-page") + 1])
    assert init_page.exists() and "exports.default" in init_page.read_text()
    assert args[args.index("--browser") + 1] == "chromium", (
        "installed Chrome held a renderer at 100% CPU after every load; Chromium is "
        "also the engine the generated tests run on")
    for rule in (CAPTURE_RULES,):
        assert "page.qa.record(" in rule and "const harvest" not in rule


def test_the_evidence_files_reach_the_browser_server(tmp_path):
    """page.qa writes what it measures to QA_EVIDENCE_FILE and counts every locator in
    QA_KNOWN_LOCATORS; a caller that passes neither (healing) gets no env at all."""
    args = write_mcp_config(tmp_path, evidence_file=tmp_path / "ev.jsonl",
                            known_locators_file=tmp_path / "known.json")
    server = json.loads(args.read_text())["mcpServers"]["playwright"]
    assert server["env"] == {"QA_EVIDENCE_FILE": str(tmp_path / "ev.jsonl"),
                             "QA_KNOWN_LOCATORS": str(tmp_path / "known.json")}
    plain = json.loads(write_mcp_config(tmp_path, cdp_endpoint="http://x:9222").read_text())
    assert "env" not in plain["mcpServers"]["playwright"]


def test_the_page_helpers_load_in_node():
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    from shared.mcp_config import INIT_PAGE
    done = subprocess.run([node, "-e", f"require({json.dumps(str(INIT_PAGE))}).default"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


class TestAssembledLiterals:
    """A selector literal the code finishes at runtime is not the locator.

    Counted as written, `#item-` matches nothing, and the explorer's live check
    reported a working page object as broken.
    """

    def test_playwright_concatenation_is_approximate(self):
        found = PlaywrightCodeEngine().extract_locators(
            'Locator item(String n) { return page.locator("#item-" + n); }\n'
            'Locator cart = page.locator("#cart");\n')
        by_raw = {loc["raw"]: loc["approx"] for loc in found}
        assert by_raw == {"#item-": True, "#cart": False}

    def test_selenium_format_template_is_approximate(self):
        found = SeleniumPlugin().code.extract_locators(
            'WebElement row = driver.findElement(By.cssSelector(String.format("tr[data-id=\'%s\']", id)));\n'
            'WebElement cart = driver.findElement(By.cssSelector("#cart"));\n'
            'WebElement item = driver.findElement(By.id("item-" + n));\n')
        by_raw = {loc["raw"]: loc["approx"] for loc in found}
        assert by_raw.get("#cart") is False
        assert by_raw.get("item-") is True
