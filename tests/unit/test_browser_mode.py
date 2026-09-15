"""Tests for shared/browser_mode.py and the runners that consult it.

HEADLESS_BROWSER used to be read by four steps with four copies of the same
expression, and ignored by every other browser in the network — the reproduce
run, the verification runs, the probes, the locate replay and the session mint
all stayed invisible when you asked to watch. What is pinned here is the rule
that replaced that: one switch, no per-step override to argue with it, and —
the part that is easy to regress — *nothing* emitted when the switch is unset,
so a Maven build keeps following the framework's parameters/config.properties.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import browser_mode, mcp_config, test_runner


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start from an unset switch — the machine running the tests has one."""
    monkeypatch.delenv("HEADLESS_BROWSER", raising=False)


@pytest.mark.parametrize("raw, expected", [
    ("false", False), ("FALSE", False), ("0", False), ("no", False), ("off", False),
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    (" false ", False),
])
def test_recognised_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("HEADLESS_BROWSER", raw)
    assert browser_mode.headless() is expected


@pytest.mark.parametrize("raw", ["", "   ", "headed", "yep"])
def test_an_unreadable_value_is_treated_as_unset(monkeypatch, raw):
    """A typo must not quietly flip the mode — it must read as "not asked"."""
    monkeypatch.setenv("HEADLESS_BROWSER", raw)
    assert browser_mode.configured() is None
    assert browser_mode.headless() is True
    assert browser_mode.maven_properties() == {}


def test_unset_is_distinguishable_from_asking_for_headless():
    """The whole Maven fallback rests on this distinction."""
    assert browser_mode.configured() is None
    assert browser_mode.headless() is True
    assert browser_mode.headless(default=False) is False


def test_there_is_exactly_one_switch(monkeypatch):
    """A per-step override is the surprise this module exists to remove: set it
    false and every browser goes headed, with nothing left to argue back."""
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    assert browser_mode.configured() is False
    assert browser_mode.headless() is False
    assert browser_mode.maven_properties() == {"headless": "false"}


def test_maven_properties_emits_the_exact_strings_the_framework_parses(monkeypatch):
    """BrowserHelper does `!"false".equalsIgnoreCase(...)`, so anything that is
    not literally "false" means headless. Only "true"/"false" may be sent."""
    monkeypatch.setenv("HEADLESS_BROWSER", "0")
    assert browser_mode.maven_properties() == {"headless": "false"}
    monkeypatch.setenv("HEADLESS_BROWSER", "yes")
    assert browser_mode.maven_properties() == {"headless": "true"}


# ── The runners ──────────────────────────────────────────────────────────────

def test_a_maven_run_carries_the_switch(monkeypatch):
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    cmd, props = test_runner._apply_browser_mode(
        ["mvn", "test", "-Dtest=LoginTest"], {"traceMode": "on"})
    assert props == {"traceMode": "on", "headless": "false"}
    assert cmd == ["mvn", "test", "-Dtest=LoginTest"]


def test_a_gradle_run_carries_it_too(monkeypatch):
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    _, props = test_runner._apply_browser_mode(
        ["./gradlew", "test", "--tests", "*.LoginTest"], {})
    assert props == {"headless": "false"}


def test_a_node_playwright_run_gets_the_flag_it_understands(monkeypatch):
    """`npx playwright test` has no -D, and headless is already its default —
    so headed is a flag and headless is silence."""
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    cmd, props = test_runner._apply_browser_mode(
        ["npx", "playwright", "test", "--grep", "login"], {})
    assert cmd[-1] == "--headed" and props == {}

    monkeypatch.setenv("HEADLESS_BROWSER", "true")
    cmd, _ = test_runner._apply_browser_mode(
        ["npx", "playwright", "test", "--grep", "login"], {})
    assert "--headed" not in cmd and not any(a.startswith("-D") for a in cmd)


def test_an_unset_switch_leaves_the_build_command_untouched():
    """Then the framework's parameters/config.properties still decides, which
    is what every caller had before the switch reached them."""
    cmd, props = test_runner._apply_browser_mode(
        ["mvn", "test", "-Dtest=LoginTest"], {"traceMode": "on"})
    assert props == {"traceMode": "on"}
    assert cmd == ["mvn", "test", "-Dtest=LoginTest"]


def test_an_explicit_caller_property_is_never_overridden(monkeypatch):
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    _, props = test_runner._apply_browser_mode(["mvn", "test"], {"headless": "true"})
    assert props == {"headless": "true"}


def test_a_custom_runner_that_is_not_a_browser_build_gets_nothing(monkeypatch):
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    cmd, props = test_runner._apply_browser_mode(["pytest", "-k", "login"], {})
    assert cmd == ["pytest", "-k", "login"] and props == {}


# ── Playwright MCP ───────────────────────────────────────────────────────────

def test_mcp_config_follows_the_switch_when_no_caller_says_otherwise(tmp_path,
                                                                    monkeypatch):
    monkeypatch.setenv("HEADLESS_BROWSER", "false")
    written = mcp_config.write_mcp_config(tmp_path)
    assert "--headless" not in written.read_text()

    monkeypatch.setenv("HEADLESS_BROWSER", "true")
    written = mcp_config.write_mcp_config(tmp_path)
    assert "--headless" in written.read_text()


def test_mcp_config_still_honours_an_explicit_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADLESS_BROWSER", "true")
    written = mcp_config.write_mcp_config(tmp_path, headless=False)
    assert "--headless" not in written.read_text()


def test_attaching_over_cdp_never_argues_about_headless(tmp_path, monkeypatch):
    """--headless describes how to LAUNCH a browser and conflicts with
    connecting to one that is already running on the failing page."""
    monkeypatch.setenv("HEADLESS_BROWSER", "true")
    written = mcp_config.write_mcp_config(
        tmp_path, cdp_endpoint="http://localhost:9222")
    assert "--headless" not in written.read_text()
