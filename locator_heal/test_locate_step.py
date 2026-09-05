"""End-to-end test of the Locate step, as the agent actually runs it.

Builds the world 01_locate expects — a workspace with page objects, a baseline
recorded from a green page, and a failure-time DOM snapshot with its fingerprint
sidecar — then runs the step as a subprocess and reads its output. Everything
below the subprocess boundary is the real thing.
"""
import datetime as dt
import json, os, pathlib, subprocess, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright

from shared import browser_mode
from shared import locator_capture as capture

REPO = pathlib.Path(__file__).resolve().parent.parent
HERE = REPO / "locator_heal"

# Fingerprints a real page, so it needs the framework's capture script.
pytestmark = pytest.mark.skipif(
    not capture.script_available(),
    reason=f"capture script not at {capture.script_path()} — set FRAMEWORK_DIR")
FIX = HERE / "fixtures"
STEP = REPO / "agents" / "test-healing-agent" / "actions" / "01_locate.py"

PAGE_OBJECT = """package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;

public class LoginPage {

    private final Page page;

    public LoginPage(Page page) {
        this.page = page;
    }

    private final Locator loginButton = page.locator("button#login-button");
}
"""


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A workspace, a green baseline, and a failing page with its sidecar."""
    root = tmp_path_factory.mktemp("locate")
    workspace = root / "ws" / "automation-repo"
    (workspace / "src/main/java/automation/modules").mkdir(parents=True)
    (workspace / "src/main/java/automation/modules/LoginPage.java").write_text(PAGE_OBJECT)
    (workspace / "src/main/resources").mkdir(parents=True, exist_ok=True)
    if capture.script_available():
        (workspace / "src/main/resources/locator-capture.js").write_text(capture.script())

    baselines = root / "baselines"
    baselines.mkdir()
    dom_dir = root / "dom"
    dom_dir.mkdir()

    good = (FIX / "v1/app.html").resolve().as_uri()
    drifted = (FIX / "v2/tag_swapped.html").resolve()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # The green run: what Baseline.java records.
        page.goto(good)
        snap = capture.snapshot(page)
        _, fingerprint = capture.find_by_locator(page, "button#login-button", snap=snap)
        recorded_at = (dt.datetime.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        (baselines / "LoginPage.json").write_text(json.dumps({
            "pageObject": "LoginPage", "recordedAt": recorded_at,
            "urlShape": good, "title": "Swag Shop", "bodyClass": "",
            "coverage": {"loginButton": 1},
            "fingerprints": {"loginButton": fingerprint},
        }, indent=2))

        # The failing run: what BrowserHelper.captureDomSnapshot writes.
        page.goto(drifted.as_uri())
        failing = capture.snapshot(page)
        sidecar = dom_dir / "loginTest_120000.fingerprints.json"
        sidecar.write_text(json.dumps(failing))
        html = dom_dir / "loginTest_120000.html"
        html.write_text(
            f'<!-- qa-agent-network:dom-snapshot test="loginTest" '
            f'url="{drifted.as_uri()}" capturedAt="2026-09-02T12:00:00" '
            f'fingerprints="{sidecar}" -->\n' + page.content())
        browser.close()

    handoff = root / "00-handoff.json"
    handoff.write_text(json.dumps({
        "build_tag": "LocateTest-1",
        "automation_issues": [{
            "test_name": "automation.LoginTest.login",
            "class_name": "LoginTest", "method_name": "login",
            "failed_selector": "button#login-button",
            "dom_snapshot": str(html),
            "failure_url": drifted.as_uri(),
            "error_message": "Failed to click on element 'Login button'",
            "root_cause_category": "ELEMENT_NOT_FOUND",
        }],
    }))
    return {"root": root, "workspace": workspace, "baselines": baselines,
            "handoff": handoff}


def run_step(world, audit, mode="shadow"):
    env = {**os.environ,
           "AUDIT_DIR": str(audit),
           "HANDOFF_FILE": str(world["handoff"]),
           "REPO_ROOT": str(REPO),
           "WORKSPACE_DIR": str(world["workspace"].parent),
           "GITHUB_REPO_AUTOMATION": world["workspace"].name,
           "HEALING_BASELINE_DIR": str(world["baselines"]),
           "HEALING_LOCATE_MODE": mode}
    done = subprocess.run([sys.executable, str(STEP)], env=env,
                          capture_output=True, text=True, timeout=300)
    out = audit / "01-locate.json"
    return done, (json.loads(out.read_text()) if out.exists() else None)


def test_locate_finds_and_proves_the_moved_element(world, tmp_path):
    audit = tmp_path / "session"; audit.mkdir()
    done, payload = run_step(world, audit)

    assert done.returncode == 0, done.stderr[-2000:]
    assert payload is not None, "01-locate.json was not written"
    assert payload["attempted"] == 1
    assert payload["located"] == 1, payload["resolutions"]

    resolution = payload["resolutions"][0]
    assert resolution["verdict"] == "HEALED"
    assert resolution["locator_id"] == "LoginPage#loginButton"
    assert resolution["classification"] == "LOCATOR_DRIFT"
    assert resolution["verification"] in ("STRONG", "WEAK")
    assert "getByRole" in resolution["new_expression"], resolution["new_expression"]
    assert resolution["score"] >= 0.75


def test_locate_writes_a_readable_report(world, tmp_path):
    audit = tmp_path / "session2"; audit.mkdir()
    run_step(world, audit)
    report = (audit / "01-locate.md").read_text()
    assert "LoginPage#loginButton" in report
    assert "LOCATOR_DRIFT" in report


def test_locate_never_aborts_the_run_on_a_broken_handoff(tmp_path):
    """The step is an optimisation; a bad input must cost a model call, not the run."""
    audit = tmp_path / "session3"; audit.mkdir()
    handoff = tmp_path / "bad.json"
    handoff.write_text("{ this is not json")
    env = {**os.environ, "AUDIT_DIR": str(audit), "HANDOFF_FILE": str(handoff),
           "REPO_ROOT": str(REPO), "HEALING_LOCATE_MODE": "shadow"}
    done = subprocess.run([sys.executable, str(STEP)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]


def _load_fix_module(tmp_path, mode, resolutions):
    """Import 01_fix with the environment it expects, to exercise its gate."""
    import importlib.util
    audit = tmp_path / "audit"; audit.mkdir(exist_ok=True)
    (audit / "01-locate.json").write_text(json.dumps({"mode": mode,
                                                      "resolutions": resolutions}))
    handoff = tmp_path / "h.json"; handoff.write_text(json.dumps({"automation_issues": []}))
    os.environ.update({"AUDIT_DIR": str(audit), "HANDOFF_FILE": str(handoff),
                       "REPO_ROOT": str(REPO), "HEALING_LOCATE_MODE": mode})
    path = REPO / "agents" / "test-healing-agent" / "actions" / "01_fix.py"
    spec = importlib.util.spec_from_file_location(f"fix_{mode}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RESOLUTION = {"verdict": "HEALED", "failed_selector": "button#login-button",
              "new_expression": 'page.getByRole(AriaRole.BUTTON)',
              "page_object": "LoginPage", "field": "loginButton",
              "score": 0.86, "margin": 0.2, "tier": "T1_identity",
              "verification": "STRONG", "strategy": "role+name"}


def test_shadow_mode_records_but_does_not_replace_the_model(tmp_path):
    """The whole point of shadow: measure the engine without letting it decide."""
    fix = _load_fix_module(tmp_path, "shadow", [RESOLUTION])
    assert fix.load_locate_resolutions() == [RESOLUTION]     # recorded
    assert fix.locate_resolution({"failed_selector": "button#login-button"}) is None


def test_enforce_mode_uses_the_located_answer(tmp_path):
    fix = _load_fix_module(tmp_path, "enforce", [RESOLUTION])
    found = fix.locate_resolution({"failed_selector": "button#login-button"})
    assert found is not None and found["field"] == "loginButton"
    # A different selector must not pick it up.
    assert fix.locate_resolution({"failed_selector": "#something-else"}) is None


def test_enforce_ignores_an_unproven_resolution(tmp_path):
    """Only HEALED counts. A refusal must never be turned into an edit."""
    refused = {**RESOLUTION, "verdict": "NO_HEAL", "classification": "FEATURE_REMOVED"}
    fix = _load_fix_module(tmp_path, "enforce", [refused])
    assert fix.locate_resolution({"failed_selector": "button#login-button"}) is None


def test_located_fix_has_the_same_shape_as_a_model_fix(tmp_path, world):
    """A deterministic fix earns no exemptions: it goes through the same guards."""
    fix = _load_fix_module(tmp_path, "enforce", [RESOLUTION])
    po = world["workspace"] / "src/main/java/automation/modules/LoginPage.java"
    ctx = {"page_objects": [{"path": str(po)}]}
    fix_json, error = fix.build_located_fix(RESOLUTION, ctx, world["workspace"])
    assert not error, error
    assert fix_json["fixable"] is True
    assert fix_json["target_file"] == str(po)
    assert len(fix_json["edits"]) == 1
    assert "loginButton" in fix_json["edits"][0]["old_string"]
    assert "getByRole" in fix_json["edits"][0]["new_string"]
    assert "verified by performing the step" in fix_json["fix_description"]
