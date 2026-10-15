"""A step's selector is counted by the call that uses it.

Driven in a real browser, with no model: a local page with a form whose fields
have nothing of their own to select by, a styled checkbox whose input only looks
hidden, and a button that appears once the box is ticked. The helpers are loaded
the way the browser server loads them (`write_mcp_config`), so this is the code a
run uses.

A walk acted on every one of these with its own `page.locator(...)` and reported
steps under selectors nothing had counted. Its fields were recorded under the
class they wear while focused, its checkbox as two clicks, and the change items
they belonged to were declined for want of a verified step.
"""

import json
import os
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

PAGE = """<!doctype html><html><head><title>Portal</title><style>
.box input { opacity: 0; position: absolute; width: 16px; height: 16px; }
.box span { display: inline-block; width: 16px; height: 16px; border: 1px solid #333;
            position: relative; z-index: 1; }
label { cursor: pointer; }
</style></head><body>
<h1>Members</h1>
<form class="card" onsubmit="return false">
  <div class="group form-field"><div><label class="lbl">Other Id</label></div>
    <div><input class="text text--active"></div></div>
  <div class="group form-field"><div><label class="lbl">Passport Number</label></div>
    <div><input class="text text--active"></div></div>
  <div class="group form-field"><div><label class="lbl">Passport Expiry</label></div>
    <div><input class="text text--active" placeholder="yyyy-mm-dd"></div></div>
  <div class="actions"><button type="submit" class="btn">Save</button></div>
</form>
<form class="card" onsubmit="return false">
  <div class="group form-field"><div><label class="lbl">Blood Group</label></div>
    <div><input class="text text--error"></div></div>
  <div class="actions"><button type="submit" class="btn">Save</button></div>
</form>
<table>
  <tr class="head"><th><div class="box"><label><input type="checkbox">
    <span class="mark"></span></label></div></th><th>Name</th></tr>
  <tr><td class="pick"><div class="box"><label><input type="checkbox" value="0">
    <span class="mark"></span></label></div></td><td>Asha</td></tr>
</table>
<button class="btn danger" id="remove" style="display:none">Remove Selected</button>
<p id="count"></p>
<script>
document.querySelectorAll('input.text').forEach(i => {
  i.onfocus = () => i.classList.add('text--focus');
  i.onblur = () => i.classList.remove('text--focus');
});
document.querySelector('td.pick input').onchange = e => {
  document.getElementById('remove').style.display = e.target.checked ? '' : 'none';
  document.getElementById('count').textContent = e.target.checked ? '1 selected' : '';
};
document.getElementById('remove').onclick = () => {
  document.getElementById('count').textContent = 'Removed';
};
</script></body></html>
"""

KNOWN = [{"owner": "MembersPage", "path": "p/MembersPage.java", "name": "nameHeader",
          "selector": "th:has-text('Name')"}]

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
  const first = await page.qa.step(null, { page: 'members' });
  const controls = first.frames.flatMap(fr => fr.result);
  const passport = controls.find(e => e.tag === 'input' && /Passport Number/.test(e.sel || ''));
  const filled = await page.qa.step([
    { fill: passport ? passport.sel : 'input.none', value: 'P-1' },
    { fill: "input[placeholder='yyyy-mm-dd']", value: '2031-01-01' },
  ], { page: 'members', harvest: false });
  const ticked = await page.qa.step({ click: 'td.pick span.mark' }, { page: 'members' });
  const removed = await page.qa.record(
    { click: "button:has-text('Remove Selected')" }, 600, { page: 'members' });
  const loose = await page.qa.step({ click: 'input.text' },
                                   { page: 'members', harvest: false, maxMs: 1500 });
  const some = (await page.qa.step(null, { page: 'members', limit: 3 })).frames[0];
  console.log(JSON.stringify({
    selectors: controls.map(e => e.sel).filter(Boolean),
    saves: controls.filter(e => e.tag === 'button' && e.text === 'Save')
      .map(e => ({ sel: e.sel, total: e.total })),
    filledHarvest: 'frames' in filled,
    whole: { listed: controls.length, unlisted: first.frames[0].unlisted || 0 },
    some: { listed: some.result.length, unlisted: some.unlisted || 0 },
    passport: passport ? { sel: passport.sel, total: passport.total } : null,
    filled: { ok: filled.ok, used: filled.used },
    ticked: { ok: ticked.ok, used: ticked.used },
    removed: { action: removed.action, used: removed.used },
    loose: { ok: loose.ok, used: loose.used, error: loose.error || '' },
    count: await page.locator('#count').innerText(),
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
    tmp = tmp_path_factory.mktemp("declared")
    (tmp / "portal.html").write_text(PAGE)
    (tmp / "known.json").write_text(json.dumps(KNOWN))
    (tmp / "driver.js").write_text(DRIVER)
    evidence = tmp / "evidence.jsonl"
    server = json.loads(write_mcp_config(
        tmp, evidence_file=evidence, known_locators_file=tmp / "known.json"
    ).read_text())["mcpServers"]["playwright"]
    args = server["args"]
    done = subprocess.run(
        [node, str(tmp / "driver.js"), str(playwright),
         args[args.index("--init-script") + 1], args[args.index("--init-page") + 1],
         (tmp / "portal.html").as_uri()],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, **server["env"]})
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    if result.get("skip"):
        pytest.skip(f"no browser to drive: {result['skip']}")
    return result, fm.read_evidence(evidence)


def _clicks(rows):
    return [r["clicked"] for r in rows if "clicked" in r]


def _typed(rows):
    return [r["typed"] for r in rows if "typed" in r]


def test_a_field_with_nothing_of_its_own_is_anchored_on_its_label(walk):
    result, _rows = walk
    assert result["passport"] is not None and result["passport"]["total"] == 1
    assert ":has-text('Passport Number')" in result["passport"]["sel"]
    # Anchored on the wrapper's class, not its bare tag: `div:has-text(...)` is
    # every ancestor of the label, and no field under it is the only one.
    assert "div.group.form-field:has-text(" in result["passport"]["sel"]


def test_a_class_that_names_a_passing_state_is_never_part_of_a_selector(walk):
    result, rows = walk
    offered = result["selectors"] + [t["sel"] for t in _typed(rows) if t.get("sel")]
    assert offered and not [s for s in offered
                            if "--focus" in s or "--active" in s or "--error" in s], (
        "a field's keystrokes were recorded under its focus class, unique for "
        "exactly as long as it was being typed into")


def test_a_declared_action_counts_its_selector_before_using_it(walk):
    result, rows = walk
    assert result["filled"]["ok"] is True
    used = result["filled"]["used"]
    assert [(c["total"], c["visible"]) for c in used.values()] == [(1, 1), (1, 1)]
    assert "uid" not in json.dumps(used), "element identities are for the evidence file"
    filed = {sel: c for r in rows for sel, c in (r.get("checks") or {}).items()}
    for selector in used:
        assert filed[selector]["total"] == 1 and filed[selector].get("uid"), (
            "the count is filed with the step's evidence, on the element it was used on")
    assert [t["value"] for t in _typed(rows)][-2:] != [], "and the keystrokes are recorded"
    assert {"P-1", "2031-01-01"} <= {t["value"] for t in _typed(rows)}


def test_a_selector_several_elements_share_is_refused_with_its_count(walk):
    result, _rows = walk
    assert result["loose"]["ok"] is False
    assert result["loose"]["used"]["input.text"]["total"] == 4
    assert "strict mode violation" in result["loose"]["error"]


def test_two_controls_with_one_label_are_told_apart_by_their_section(walk):
    # Two Save buttons under one selector: a walk took the second by position,
    # saved the other section, and nothing it had typed persisted.
    result, _rows = walk
    assert result["saves"] == [
        {"sel": "form.card:has-text('Other Id') button[type='submit']", "total": 1},
        {"sel": "form.card:has-text('Blood Group') button[type='submit']", "total": 1}]


def test_a_list_cut_at_its_limit_says_how_much_it_left_out(walk):
    # Unsaid, a control past the cut reads as one that is not on the page: a walk
    # was never offered the button it had to click, and wrote its own selector.
    result, _rows = walk
    assert result["whole"]["unlisted"] == 0
    assert result["some"]["listed"] == 3
    assert result["some"]["unlisted"] == result["whole"]["listed"] + 1 - 3, (
        "everything the whole list held, and the button the walk has shown since")


def test_a_call_that_only_fills_reads_no_page_back(walk):
    result, rows = walk
    assert result["filledHarvest"] is False
    filled = [r for r in rows if "P-1" in json.dumps(r.get("inventory") or [])]
    assert filled, "the page is still measured for the evidence file"


def test_a_label_and_the_checkbox_behind_it_are_recorded_as_one_click(walk):
    result, rows = walk
    assert result["ticked"]["ok"] is True and result["count"] == "Removed"
    label = next(c for c in _clicks(rows) if c.get("sel") == "td.pick label")
    assert (label["total"], label["visible"]) == (1, 1), (
        "a bare label is still the only one of its tag under the cell")
    handed_on = [c for c in _clicks(rows) if c.get("via")]
    assert [c["via"] for c in handed_on] == [label["uid"]], (
        "the input only looks hidden, so its own click cannot be told by its size")
    actions = [a for a in fm.recorded_actions(rows) if a["kind"] == "click"]
    assert [a["sel"] for a in actions][:1] == ["td.pick label"]
    assert len(actions[0]["uids"]) == 2


def test_every_step_reported_with_the_selector_it_passed_is_verified(walk):
    result, rows = walk
    def step(index, verb, name, selector, label="", value=None):
        return "FLOW_STEP: " + json.dumps({
            "index": index, "page": "members",
            "action": {"verb": verb, "value": value, "target": {
                "name": name, "selector": selector, "accessible_name": label}},
            "result": {"outcome": "ok"}})
    markers = "\n".join([
        "PAGE_ENTER: members|file:///portal.html|Portal",
        step(1, "fill", "passportNumber", result["passport"]["sel"], "Passport Number", "P-1"),
        step(2, "fill", "passportExpiry", "input[placeholder='yyyy-mm-dd']", "Passport Expiry",
             "2031-01-01"),
        step(3, "click", "rowCheckbox", "td.pick span.mark"),
        step(4, "click", "removeSelected", "button:has-text('Remove Selected')",
             "Remove Selected"),
    ])
    flow = fm.build(markers, rows, sandbox=True)
    checks = [s["selector_check"] for s in flow["steps"]]
    assert [(c["counted_against"], c["unique"]) for c in checks] == [("live", True)] * 4
    assert not any("reported_selector" in s["action"]["target"] for s in flow["steps"]), (
        "a selector that was counted is the step's own, and nothing replaces it")
    assert flow["unreported_actions"] == [], (
        "the tick is one action, and the step that reported it accounts for it")
    table = fm.describe(flow, selectors=True)
    assert "rowCheckbox | `td.pick span.mark` |" in table
    assert "| P-1 |" in table
