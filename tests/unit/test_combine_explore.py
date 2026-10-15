"""What the combined flow map has to carry into the adapt step.

The adapt step reads `03-explore.json`, not the web half, and the combined dict
names its keys explicitly — so anything not listed is silently dropped. The
element inventories were: page objects could not be measured from the combined
flow, selectors could not be recounted against it, and `matches_negative` was
handed an empty list of negatives and passed everything, which is exactly the
no-op it had been in every other agent before being given a real source.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AGENT = ROOT / "agents" / "test-adaptation-agent"

WEB = {
    "ran": True, "status": "ok",
    "flow": {
        "steps": [{"index": 0, "action": {"verb": "click", "target": {"name": "cart"}},
                   "result": {"outcome": "ok"}}],
        "pages": {"LoginPage": {"url": "https://x/", "title": "Swag Labs"},
                  "CartPage": {"url": "https://x/cart.html"}},
        "_inventories": {"LoginPage": [{"tag": "input", "id": "login-button"}],
                         "CartPage": [{"tag": "div", "class": "cart_quantity"}]},
        "unreachable": [], "refusals": [], "violations": [], "outcomes": [], "notes": [],
    },
}
API = {"ran": False, "status": "skipped", "steps": []}


def _run_combine(tmp_path, monkeypatch):
    (tmp_path / "03-explore-web.json").write_text(json.dumps(WEB))
    (tmp_path / "03-explore-api.json").write_text(json.dumps(API))
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DIR", str(AGENT))
    monkeypatch.setenv("REPO_ROOT", str(ROOT))
    monkeypatch.setattr(sys, "path", list(sys.path))
    sys.path.insert(0, str(AGENT))
    spec = importlib.util.spec_from_file_location(
        "combine_explore", AGENT / "actions" / "03_combine_explore.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    return json.loads((tmp_path / "03-explore.json").read_text())["flow"]


def test_the_combined_flow_keeps_the_page_inventories(tmp_path, monkeypatch):
    flow = _run_combine(tmp_path, monkeypatch)
    assert set(flow.get("_inventories") or {}) == {"LoginPage", "CartPage"}, (
        "the adapt step reads this file — an inventory dropped here is an "
        "inventory that does not exist as far as every guard is concerned")


def test_the_negatives_survive_the_round_trip(tmp_path, monkeypatch):
    """End to end for the guard: combined file → negatives → rejection."""
    flow = _run_combine(tmp_path, monkeypatch)
    # Three agents ship a `lib` package, and 04_adapt imports its own. Whichever
    # one an earlier test left in sys.modules would be the one imported here, so
    # this passed alone and failed in the suite.
    is_lib = lambda n: n == "lib" or n.startswith("lib.")
    for name in [n for n in sys.modules if is_lib(n)]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location(
        "adapt_for_negatives", AGENT / "actions" / "04_adapt.py")
    adapt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapt)
    for name in [n for n in sys.modules if is_lib(n)]:
        del sys.modules[name]
    from shared.edit_guards import matches_negative

    docs = adapt.negative_documents(flow)
    assert docs, "the login page is in the combined flow and is a page to refuse"
    assert matches_negative(["#login-button"], docs)[0] is False
    assert matches_negative([".cart_quantity"], docs)[0] is True
