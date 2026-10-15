"""A screen that shows briefly and closes by itself gets a record like any other.

Driven in a real browser, with no model: a local page whose button shows a spinner
for a moment, then a result panel for two seconds, then the page the flow is left
on. The helpers are loaded the way the browser server loads them
(`write_mcp_config`), so this is the code a run uses.

A walk that recorded such a screen left it with no inventory — the recording call
saved counts only, under no page — and a page with none is walked again from the
start and keeps the exploration out of the step cache.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import flow_map as fm
from shared.mcp_config import write_mcp_config

PAGE = """<!doctype html><html><head><title>Shop</title></head><body>
<h1>Order</h1>
<button id="pay">Pay</button>
<div id="app"></div>
<script>
// ?stay — the result panel never closes: nothing in the recording is brief.
const stay = location.search.includes('stay');
document.getElementById('pay').onclick = () => {
  const app = document.getElementById('app');
  setTimeout(() => { app.innerHTML = '<h3 class="spinner">Working</h3>'; }, 100);
  setTimeout(() => { app.innerHTML =
    '<div class="result"><h2>Payment accepted</h2><span class="paid-amount">500</span></div>'; }, 500);
  if (!stay) setTimeout(() => { app.innerHTML =
    '<div class="home"><h2>Welcome back</h2><p class="thanks">Thank you</p></div>'; }, 2500);
};
</script></body></html>
"""

KNOWN = [
    {"owner": "OrderPage", "path": "p/OrderPage.java", "name": "pay", "selector": "#pay"},
    {"owner": "ResultPage", "path": "p/ResultPage.java", "name": "amount",
     "selector": ".paid-amount"},
    {"owner": "ResultPage", "path": "p/ResultPage.java", "name": "heading",
     "selector": ".result h2"},
    {"owner": "HomePage", "path": "p/HomePage.java", "name": "thanks", "selector": ".thanks"},
]

DRIVER = """
const { chromium } = require(process.argv[2]);
(async () => {
  const [initScript, initPage, url] = process.argv.slice(3);
  let browser;
  try { browser = await chromium.launch({ headless: true }); }
  catch (e) { console.log(JSON.stringify({ skip: String(e.message || e).split('\\n')[0] })); return; }
  const context = await browser.newContext();
  await context.addInitScript({ path: initScript });
  const page = await context.newPage();
  await require(initPage).default({ page });
  await page.goto(url);
  await page.qa.step(null, { page: 'order' });
  await page.qa.step(null, { page: 'order-again' });
  const named = await page.qa.record(() => page.locator('#pay').click(), 3600, { page: 'result' });
  await page.qa.step(null, { page: 'home' });
  await page.goto(url + '?stay');
  await page.qa.record(() => page.locator('#pay').click(), 1500, { page: 'receipt' });
  await page.goto(url);
  const unnamed = await page.qa.record(() => page.locator('#pay').click(), 900);
  const texts = r => r.states.flatMap(s => s.els.map(e => e.text));
  console.log(JSON.stringify({
    named: { page: named.page || null, unnamed: named.unnamed || null, texts: texts(named) },
    unnamed: { page: unnamed.page || null, unnamed: unnamed.unnamed || null },
  }));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def walk(tmp_path_factory):
    node = shutil.which("node")
    playwright = ROOT / "node_modules" / "playwright"
    if not node or not playwright.is_dir():
        pytest.skip("node and the repo's playwright package are needed to drive a browser")
    tmp = tmp_path_factory.mktemp("brief")
    (tmp / "shop.html").write_text(PAGE)
    (tmp / "known.json").write_text(json.dumps(KNOWN))
    (tmp / "driver.js").write_text(DRIVER)
    evidence = tmp / "evidence.jsonl"
    server = json.loads(write_mcp_config(
        tmp, evidence_file=evidence, known_locators_file=tmp / "known.json"
    ).read_text())["mcpServers"]["playwright"]
    args = server["args"]
    import os
    done = subprocess.run(
        [node, str(tmp / "driver.js"), str(playwright),
         args[args.index("--init-script") + 1], args[args.index("--init-page") + 1],
         (tmp / "shop.html").as_uri()],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, **server["env"]})
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    if result.get("skip"):
        pytest.skip(f"no browser to drive: {result['skip']}")
    return result, fm.read_evidence(evidence)


def _brief(rows):
    return [r for r in rows if r.get("brief")]


def _texts(rows):
    return [e.get("text") for r in rows for e in r.get("inventory") or []]


def _counted(rows, name):
    return [e["total"] for r in rows for e in r.get("known") or [] if e["name"] == name]


def test_a_named_brief_screen_is_recorded_while_it_is_up(walk):
    _result, rows = walk
    brief = [r for r in _brief(rows) if r["page"] == "result"]
    assert brief, "filed under the name it was given"
    assert "Payment accepted" in _texts(brief), (
        "the inventory is taken while the screen is still on, not after it closed")
    assert max(_counted(brief, "amount")) == 1, (
        "the repo's locator for a thing only this screen shows is counted on it")


def test_only_what_came_and_went_is_filed_as_the_brief_screen(walk):
    _result, rows = walk
    brief = [r for r in _brief(rows) if r["page"] == "result"]
    # The spinner lasted under half a second: the flicker before the screen.
    assert "Working" not in _texts(brief)
    # The panel still showing when the recording ended is the next page. Filed
    # here, its locators would read as this screen's and its own page would have
    # nothing new to be recognised by.
    assert "Welcome back" not in _texts(brief)
    assert max(_counted(brief, "thanks")) == 0


def test_the_page_left_showing_is_measured_by_the_step_after(walk):
    _result, rows = walk
    home = [r for r in rows if r.get("page") == "home"]
    assert "Welcome back" in _texts(home)
    assert max(_counted(home, "thanks")) == 1, (
        "measuring it during the recording must not stop the next step counting "
        "the repo's locators on it")


def test_a_screen_that_never_closed_still_gets_the_record_it_was_named_for(walk):
    _result, rows = walk
    receipt = [r for r in _brief(rows) if r["page"] == "receipt"]
    assert "Payment accepted" in _texts(receipt), (
        "nothing came and went, so what was showing is the screen: a named page "
        "with no record sends the whole walk round again")


def test_recording_still_returns_every_state(walk):
    result, _rows = walk
    # Saving a record must not cost the watch a state: the screen that follows the
    # brief one appeared two seconds later and is still reported.
    assert "Payment accepted" in result["named"]["texts"]
    assert "Thank you" in result["named"]["texts"]
    assert result["named"]["page"] == "result" and result["named"]["unnamed"] is None


def test_a_screen_recorded_without_a_name_says_so_and_files_nothing(walk):
    result, rows = walk
    assert "page.qa.record(action, ms, { page:" in result["unnamed"]["unnamed"], (
        "a caller that names its pages is told at once, in the call's own result")
    assert {r.get("page") for r in _brief(rows)} == {"result", "receipt"}, (
        "nothing is filed for a screen nobody named: placed by address, it would "
        "land on whichever page shares that address")


def test_the_flow_map_treats_it_as_any_other_page(walk):
    _result, rows = walk
    markers = "\n".join([
        "PAGE_ENTER: order|file:///shop.html|Shop",
        "PAGE_ENTER: result|file:///shop.html|Shop",
        'FLOW_STEP: {"index": 1, "page": "result", "action": {"verb": "click", '
        '"target": {"name": "pay", "selector": "#pay", "accessible_name": "Pay"}}, '
        '"result": {"outcome": "ok"}}',
        "PAGE_ENTER: home|file:///shop.html|Shop",
    ])
    flow = fm.build(markers, rows)
    assert fm.pages_without_inventory(flow) == [], (
        "an unrecorded page is what sends the whole walk round again")
    fm.measure_page_objects(flow, [{"path": "p/ResultPage.java", "snippet": ""}])
    assert flow["pages"]["result"]["best_page_object"]["name"] == "ResultPage"
    check = flow["steps"][0]["selector_check"]
    assert (check["counted_against"], check["unique"]) == ("live", True), (
        "the button that opens the brief screen is gone from it, and was counted "
        "where it was clicked")


def test_a_state_measured_under_a_second_name_is_counted_for_it_too(walk):
    _result, rows = walk
    # Counted once per state whatever it was called, the second page had no counts
    # of its own and was matched to a page object from a sample instead.
    again = [r for r in rows if r.get("page") == "order-again"]
    assert max(_counted(again, "pay")) == 1
