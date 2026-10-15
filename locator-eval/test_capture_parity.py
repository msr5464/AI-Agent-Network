"""Java/Python capture parity — the sharpest risk in the integration.

The engine scores a failing page against fingerprints the Java framework recorded
on a green run. If the two sides disagree about what the page contains, nothing
breaks loudly: every similarity score is just quietly computed against a
different idea of the page, and the failure surfaces as bad heals much later.

Source drift is no longer possible — both stacks read the framework's single copy
of locator-capture.js. What identical source does NOT prove is identical output:
the two drive different Playwright versions, and so different Chromium builds.
That is what this checks.
"""
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright

from shared import browser_mode
from shared import workspace as workspace_helper
from shared import locator_capture as capture

HERE = pathlib.Path(__file__).parent
REPO_ROOT = HERE.parent
FIXTURE = HERE / "fixtures" / "v1" / "app.html"

# Same resolution order the agents use, so the test looks where the run looked:
# FRAMEWORK_DIR, else WORKSPACE_DIR/GITHUB_REPO_AUTOMATION, else a sibling
# checkout matched by shape. Skips when it is not there.
workspace_helper.load_repo_env()
FRAMEWORK = workspace_helper.resolve(
    os.environ.get("WORKSPACE_DIR") or REPO_ROOT.parent,
    os.environ.get("GITHUB_REPO_AUTOMATION", ""),
    exclude=REPO_ROOT)
JAVA_COPY = FRAMEWORK / "src" / "main" / "resources" / "locator-capture.js"

# Fields that carry identity. These must match exactly — they are what the
# scorer compares. Geometry is checked separately and may legitimately differ by
# a hair across browser builds.
IDENTITY = [
    "tag", "id", "name", "type", "class_list", "role", "accessible_name",
    "aria_label", "placeholder", "alt", "href", "title", "testid", "text",
    "attrs", "is_interactive", "is_visible", "is_enabled", "abs_xpath",
    "id_xpath", "sibling_index", "sibling_count", "neighbor_texts",
]
GEOMETRY_TOLERANCE = 1e-4      # a fraction of the viewport


def test_python_reads_the_frameworks_capture_script():
    """The engine must load the framework's copy, not one of its own."""
    if not JAVA_COPY.exists():
        pytest.skip(f"automation framework not present at {FRAMEWORK}")
    assert not (HERE.parent / "shared" / "locator_capture.js").exists(), (
        "a second copy of the capture script has reappeared in shared/. The "
        "framework owns it — two capture implementations are free to drift, and "
        "two that disagree silently corrupt every similarity score."
    )
    assert capture.script() == JAVA_COPY.read_text()


def _java_capture(tmp_path) -> dict | None:
    """Run the framework's CaptureDump, or None if it cannot be built here."""
    classes = FRAMEWORK / "target" / "test-classes" / "automation" / "tools" / "CaptureDump.class"
    if not classes.exists() or shutil.which("java") is None:
        return None
    cp_file = tmp_path / "cp.txt"
    build = subprocess.run(
        ["mvn", "-q", "-o", "dependency:build-classpath", f"-Dmdep.outputFile={cp_file}"],
        cwd=FRAMEWORK, capture_output=True, text=True, timeout=300)
    if build.returncode != 0 or not cp_file.exists():
        return None
    out = tmp_path / "java_capture.json"
    run = subprocess.run(
        ["java", "-cp",
         f"{FRAMEWORK}/target/test-classes:{FRAMEWORK}/target/classes:{cp_file.read_text()}",
         "automation.tools.CaptureDump", FIXTURE.resolve().as_uri(), str(out)],
        capture_output=True, text=True, timeout=300)
    if run.returncode != 0 or not out.exists():
        return None
    return json.loads(out.read_text())


def test_java_and_python_agree_on_a_live_page(tmp_path):
    java = _java_capture(tmp_path)
    if java is None:
        pytest.skip("framework not built (run `mvn test-compile` in the automation repo)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(FIXTURE.resolve().as_uri())
        python = capture.snapshot(page)
        browser.close()

    assert len(java["elements"]) == len(python["elements"]), (
        f"element counts differ: java={len(java['elements'])} python={len(python['elements'])}")
    assert java["landmarks"] == python["landmarks"]

    mismatches = []
    for i, (a, b) in enumerate(zip(java["elements"], python["elements"])):
        for key in IDENTITY:
            if a.get(key) != b.get(key):
                mismatches.append(f"element #{i} <{a.get('tag')}> {key}: "
                                  f"java={a.get(key)!r} python={b.get(key)!r}")
        for axis in ("x", "y", "w", "h"):
            if abs(a["bbox_norm"][axis] - b["bbox_norm"][axis]) > GEOMETRY_TOLERANCE:
                mismatches.append(f"element #{i} bbox.{axis}: "
                                  f"java={a['bbox_norm'][axis]} python={b['bbox_norm'][axis]}")
    assert not mismatches, "capture diverged:\n  " + "\n  ".join(mismatches[:10])
