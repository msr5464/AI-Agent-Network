"""Retry-loop tests: the paths the happy-path corpus never reaches.

R5 in particular is built but unused in normal operation (the deterministic path
clears every corpus case). Untested wiring is indistinguishable from broken
wiring, so it gets a stub model here.
"""
import json, pathlib, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import yaml
from playwright.sync_api import sync_playwright

from shared import browser_mode
from shared import locator_capture as capture
from shared import locator_resolve as heal_mod

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE.parent / "config" / "locator.yaml"
FIX = HERE / "fixtures"
BASE = HERE / "baselines"

# These fingerprint a real page, so they need the capture script — which the
# automation framework owns. Nothing here can stand in for it: a substitute walk
# is the exact thing keeping a single copy is meant to prevent.
pytestmark = pytest.mark.skipif(
    not capture.script_available(),
    reason=f"capture script not at {capture.script_path()} — set FRAMEWORK_DIR")


@pytest.fixture(scope="module")
def env():
    cfg = yaml.safe_load(CONFIG.read_text())
    man = json.loads((FIX / "manifest.json").read_text())
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        heal_mod.record_baselines(page, (FIX / "v1/app.html").as_uri(),
                                  man["locators"], BASE, cfg=cfg)
        yield cfg, man, browser, page
        browser.close()


def uri(rel):
    return (FIX / rel).as_uri()


def test_r5_llm_rescues_a_case_the_deterministic_path_refuses(env):
    """Force escalation by demanding an impossible score, then let a stub model
    pick. The model's choice must still pass execution-based verification."""
    cfg, man, browser, page = env
    cfg = json.loads(json.dumps(cfg))
    cfg["thresholds"]["accept"] = 0.99          # nothing will clear this
    baseline = heal_mod.load_baseline(BASE, "fixture", "LoginPage#usernameField")

    seen = {}

    def stub_llm(prompt: str):
        seen["prompt"] = prompt
        payload = json.loads(prompt.split(heal_mod.LLM_DATA_MARKER)[1])
        # pick the candidate whose accessible name matches the recorded one
        want = payload["target_was"]["accessible_name"]
        for c in payload["candidates"]:
            if c.get("accessible_name") == want:
                return {"widget_id": c["widget_id"], "why": "same accessible name"}
        return {"widget_id": -1}

    res = heal_mod.heal(page, baseline, cfg, uri("v2/id_renamed.html"), llm=stub_llm)
    assert "prompt" in seen, "R5 was never reached"
    assert "target_was" in seen["prompt"] and "candidates" in seen["prompt"]
    assert res.verdict == heal_mod.HEALED, res.reason
    assert res.picked_gt == "username"
    assert any(a["loop"] == "R5_llm" for a in res.attempts)


def test_r5_model_may_refuse(env):
    """A model answering -1 must not be coerced into a heal."""
    cfg, man, browser, page = env
    cfg = json.loads(json.dumps(cfg))
    cfg["thresholds"]["accept"] = 0.99
    baseline = heal_mod.load_baseline(BASE, "fixture", "LoginPage#usernameField")
    res = heal_mod.heal(page, baseline, cfg, uri("v2/id_renamed.html"),
                        llm=lambda p: {"widget_id": -1, "why": "none of these"})
    assert res.verdict == heal_mod.NO_HEAL


def test_r0_reports_a_flake_rather_than_healing(env):
    """If the locator resolves after waiting, that is a timing problem and the
    element must not be re-bound."""
    cfg, man, browser, page = env
    baseline = heal_mod.load_baseline(BASE, "fixture", "LoginPage#usernameField")
    res = heal_mod.heal(page, baseline, cfg, uri("v1/app.html"))
    assert res.verdict == heal_mod.NO_HEAL
    assert res.classification == "NOT_LOCATOR"


def test_run_cap_stops_a_mass_failure(env):
    """Twenty broken locators is a deploy failure, not twenty heals."""
    cfg, man, browser, page = env
    session = heal_mod.HealSession()
    session.heals = cfg["budgets"]["max_heals_per_run"]
    baseline = heal_mod.load_baseline(BASE, "fixture", "LoginPage#usernameField")
    res = heal_mod.heal(page, baseline, cfg, uri("v2/id_renamed.html"), session=session)
    assert res.classification == "RUN_CAP_REACHED"


def test_one_attempt_per_locator_per_run(env):
    cfg, man, browser, page = env
    session = heal_mod.HealSession()
    session.attempted.add("LoginPage#usernameField")
    baseline = heal_mod.load_baseline(BASE, "fixture", "LoginPage#usernameField")
    res = heal_mod.heal(page, baseline, cfg, uri("v2/id_renamed.html"), session=session)
    assert res.classification == "ALREADY_ATTEMPTED"


def test_claude_picker_parses_a_wrapped_reply(monkeypatch):
    """The model is asked for bare JSON and frequently wraps it in prose anyway."""
    from shared import claude
    monkeypatch.setattr(claude, "call_claude",
                        lambda *a, **k: 'Sure!\n{"widget_id": 2, "why": "same button"}\nHope that helps.')
    assert heal_mod.claude_picker()("prompt") == {"widget_id": 2, "why": "same button"}


def test_claude_picker_survives_an_unusable_reply(monkeypatch):
    """An empty or non-JSON answer must read as "no pick", never crash the run."""
    from shared import claude
    for reply in ("", "I could not determine the element.", "[1,2,3]"):
        monkeypatch.setattr(claude, "call_claude", lambda *a, **k: reply)
        assert heal_mod.claude_picker()("prompt") is None
