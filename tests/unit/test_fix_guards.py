"""Tests for the fix-integrity guards in 01_fix.py.

These exist because verification-by-rerun cannot catch a fix built on a wrong
diagnosis: the easiest way to make a page assertion pass is to weaken it. The
cases below are the edits the agent really produced against
GitHubLoginTest.verifyLoginOnGitHubUsingStoredSession, recorded in
agents/test-healing-agent/audit/20260826-000021-fix-GitHubLoginTest/01-fix.md.
All three were reverted only after maven had run them.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agents" / "test-healing-agent"
sys.path.insert(0, str(ROOT))

from shared import diagnosis, page_identity
from tests import fixtures as fx


def diagnosis_stop():
    """Parametrise over the live list, so a new stop verdict is covered by
    this test the moment it is added rather than whenever someone remembers."""
    return diagnosis.STOP


@pytest.fixture(scope="module")
def fix(tmp_path_factory):
    """Import 01_fix.py with its own `lib` package (both agents ship one)."""
    tmp = tmp_path_factory.mktemp("fix")
    os.environ.setdefault("AUDIT_DIR", str(tmp))
    os.environ.setdefault("HANDOFF_FILE", str(tmp / "handoff.json"))

    saved_path, saved_modules = list(sys.path), {
        n: m for n, m in sys.modules.items() if n == "lib" or n.startswith("lib.")}
    for name in saved_modules:
        del sys.modules[name]
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(AGENT))
    try:
        spec = importlib.util.spec_from_file_location(
            "fix_step", AGENT / "actions" / "01_fix.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in [n for n in sys.modules if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


ORIGINAL = '''    public DashboardPage(Config config)
    {
        super(config);
        avatarWidget = page.locator("img[class*='avatar']").first();
        userMenu     = page.locator("summary[aria-label*='View profile']");
        assertPageLoaded(avatarWidget);
    }
'''

# Attempt 2 of the recorded run: the load anchor is moved off the avatar onto a
# link the logged-out page also has. This is the dangerous one — it makes
# assertPageLoaded succeed on the wrong page.
WEAKENED_ANCHOR = """    public DashboardPage(Config config)
    {
        super(config);
        pageAnchor   = page.locator("a[aria-label='Homepage']");
        avatarWidget = page.locator("[data-login]").first();
        userMenu     = page.locator("summary[aria-label*='View profile']");
        assertPageLoaded(pageAnchor);
    }
"""

# Attempt 1: a plausible replacement selector that exists nowhere on the page
# the test actually reached.
GUESSED_SELECTOR = ORIGINAL.replace("img[class*='avatar']",
                                    ".AppHeader-user, [data-login]")

# What a real locator fix looks like: same tightness, and it matches the page.
GENUINE_FIX = ORIGINAL.replace("img[class*='avatar']", "img[class*='user-photo']")


class TestIdentityAssertionGuard:
    def test_weakened_page_anchor_is_rejected_on_a_stop_verdict(self, fix):
        ok, reason = fix.validate_diagnosis_fit(ORIGINAL, WEAKENED_ANCHOR, "WRONG_PAGE")
        assert ok is False
        assert "page-load assertion" in reason

    def test_weakened_page_anchor_is_rejected_when_undiagnosed(self, fix):
        ok, _ = fix.validate_diagnosis_fit(ORIGINAL, WEAKENED_ANCHOR, "")
        assert ok is False

    def test_a_confirmed_stale_locator_may_touch_the_anchor(self, fix):
        # When the page really is right, editing the anchor's selector is the fix.
        ok, _ = fix.validate_diagnosis_fit(ORIGINAL, WEAKENED_ANCHOR, "LOCATOR_STALE")
        assert ok is True


class TestBroadeningGuard:
    def test_adding_comma_alternatives_is_rejected(self, fix):
        ok, reason = fix.validate_diagnosis_fit(ORIGINAL, GUESSED_SELECTOR, "WRONG_PAGE")
        assert ok is False
        assert "broaden" in reason or "page-load assertion" in reason

    def test_collapsing_to_a_bare_tag_is_broader(self, fix):
        assert fix._is_broader("img[class*='avatar']", "img") is True

    def test_an_equally_tight_selector_is_not_broader(self, fix):
        assert fix._is_broader("img[class*='avatar']", "img[class*='user-photo']") is False

    def test_identical_selectors_are_not_broader(self, fix):
        assert fix._is_broader("#a", "#a") is False


class TestReplacementMustExist:
    def _soup(self, snapshot):
        return page_identity.parse(snapshot)

    def test_the_recorded_guess_is_rejected(self, fix):
        # The recorded attempt proposed ".AppHeader-user, [data-login]". Two
        # independent rules condemn it — it is broader than what it replaces, and
        # it matches nothing on the page. Either is enough; the point is that
        # maven took a minute to discover what this settles in microseconds.
        ok, reason = fix.validate_diagnosis_fit(
            ORIGINAL, GUESSED_SELECTOR, "LOCATOR_STALE",
            self._soup(fx.DASHBOARD_LOGGED_OUT))
        assert ok is False
        assert reason

    def test_a_tight_selector_matching_nothing_is_still_rejected(self, fix):
        # Isolates the third rule: same tightness as the original, so the
        # broadening check passes it through, but it exists on no page we saw.
        guess = ORIGINAL.replace("img[class*='avatar']", "img[class*='gravatar']")
        ok, reason = fix.validate_diagnosis_fit(
            ORIGINAL, guess, "LOCATOR_STALE", self._soup(fx.DASHBOARD_LOGGED_OUT))
        assert ok is False
        assert "matches nothing" in reason

    def test_a_replacement_present_on_the_page_is_accepted(self, fix):
        ok, reason = fix.validate_diagnosis_fit(
            ORIGINAL, GENUINE_FIX, "LOCATOR_STALE", self._soup(fx.DASHBOARD_RENAMED))
        assert ok is True, reason

    def test_no_snapshot_means_no_opinion(self, fix):
        # Absent evidence must not become a rejection.
        ok, _ = fix.validate_diagnosis_fit(ORIGINAL, GENUINE_FIX, "LOCATOR_STALE", None)
        assert ok is True

    def test_unevaluable_replacements_are_not_judged(self, fix):
        xpath_fix = ORIGINAL.replace("img[class*='avatar']", "xpath=//img[@id='av']")
        ok, _ = fix.validate_diagnosis_fit(
            ORIGINAL, xpath_fix, "LOCATOR_STALE", self._soup(fx.DASHBOARD_LOGGED_OUT))
        assert ok is True


class TestAbstentionIsStillGuarded:
    """Abstention falls through for the *gate*, but not for identity assertions.

    The asymmetry is deliberate: a blocked fix is visible and retryable, while a
    weakened page assertion ships a permanently green broken test. FORCE is the
    documented way past it.
    """

    def test_identity_edit_is_blocked_when_the_engine_abstained(self, fix):
        ok, reason = fix.validate_diagnosis_fit(
            ORIGINAL, WEAKENED_ANCHOR, "INSUFFICIENT_EVIDENCE")
        assert ok is False
        assert "FORCE=true" in reason

    def test_an_ordinary_selector_edit_still_passes_on_abstention(self, fix):
        ok, reason = fix.validate_diagnosis_fit(
            ORIGINAL, GENUINE_FIX, "INSUFFICIENT_EVIDENCE")
        assert ok is True, reason


class TestPipelineGating:
    """The asymmetry between the two entry points, stated as a test.

    Probes run on the standalone path only, so a verdict reached in the fix step
    has never been measured. It gates at HIGH alone; standalone gates at MEDIUM
    because a probe stands behind it. The invariant both share: nothing blocks
    work unless it was measured or corroborated.
    """

    def _v(self, verdict, confidence="HIGH"):
        return {"verdict": verdict, "confidence": confidence, "reasons": ["r"]}

    def test_high_confidence_stop_gates_the_pipeline(self, fix):
        gate, note = fix.should_gate(self._v("WRONG_PAGE"), "enforce", False)
        assert gate is True and note == ""

    def test_medium_confidence_reports_but_does_not_gate(self, fix):
        # Unmeasured on this path — reporting is honest, blocking would be a guess.
        gate, note = fix.should_gate(self._v("WRONG_PAGE", "MEDIUM"), "enforce", False)
        assert gate is False
        assert "unprobed on this path" in note

    def test_shadow_never_gates_even_at_high(self, fix):
        gate, note = fix.should_gate(self._v("WRONG_PAGE"), "shadow", False)
        assert gate is False
        assert "would have stopped" in note

    def test_force_never_gates(self, fix):
        gate, note = fix.should_gate(self._v("WRONG_PAGE"), "enforce", True)
        assert gate is False and "FORCE" in note

    def test_an_actionable_verdict_never_gates(self, fix):
        gate, note = fix.should_gate(self._v("LOCATOR_STALE"), "enforce", False)
        assert (gate, note) == (False, "")

    def test_abstention_never_gates(self, fix):
        gate, _ = fix.should_gate(self._v("INSUFFICIENT_EVIDENCE", "LOW"),
                                  "enforce", False)
        assert gate is False

    def test_a_missing_diagnosis_never_gates(self, fix):
        assert fix.should_gate({}, "enforce", False) == (False, "")
        assert fix.should_gate(None, "enforce", False) == (False, "")

    @pytest.mark.parametrize("verdict", sorted(diagnosis_stop()))
    def test_every_stop_verdict_gates_at_high_in_enforce(self, fix, verdict):
        gate, _ = fix.should_gate(
            {"verdict": verdict, "confidence": "HIGH"}, "enforce", False)
        assert gate is True


class TestClaudeInvocation:
    """The agent's call_claude() wrapper, and how it reaches shared.claude.

    Tier 1 and tier 3 both drive a browser, so both pass their own
    allowed_tools. The wrapper had that argument hardcoded from artifact_dir and
    swallowed the caller's in **kwargs, so every browser inspection died with
    "got multiple values for keyword argument 'allowed_tools'" — after the test
    had already been re-run with the browser parked.
    """

    @pytest.fixture
    def recorded(self, fix, monkeypatch):
        calls = []

        def spy(prompt, model, cwd, **kwargs):
            calls.append(kwargs)
            return "ok"

        monkeypatch.setattr(fix, "_call_claude", spy)
        return calls

    def test_browser_call_keeps_its_own_tools(self, fix, recorded, tmp_path):
        out = fix.call_claude("p", tmp_path, use_system_prompt=False,
                              allowed_tools=["mcp__playwright__*"],
                              mcp_config="x.json", strict_mcp_config=True,
                              stream_json=True, partial_on_timeout=True)
        assert out == "ok"
        assert recorded[0]["allowed_tools"] == ["mcp__playwright__*"]
        assert recorded[0]["mcp_config"] == "x.json"

    def test_artifact_dir_grants_read_and_adds_the_dir(self, fix, recorded, tmp_path):
        fix.call_claude("p", tmp_path, artifact_dir="/shots")
        assert recorded[0]["allowed_tools"] == ["Read"]
        assert recorded[0]["add_dir"] == "/shots"

    def test_both_together_merge(self, fix, recorded, tmp_path):
        fix.call_claude("p", tmp_path, artifact_dir="/shots",
                        allowed_tools=["mcp__playwright__*"])
        assert recorded[0]["allowed_tools"] == ["mcp__playwright__*", "Read"]

    def test_a_plain_call_grants_nothing(self, fix, recorded, tmp_path):
        fix.call_claude("p", tmp_path)
        assert recorded[0]["allowed_tools"] is None
        assert recorded[0]["add_dir"] is None


class TestReapParkedBrowser:
    """Closing the browser a repair run parked.

    The framework detaches it on purpose, so nothing else ever closes it. The
    reaper read a browserPid the framework never wrote, killed nothing, and
    deleted the session file anyway — leaving a Chromium holding the CDP port,
    which makes every later run skip repair mode.
    """

    def _session(self, tmp_path, **extra):
        f = tmp_path / ".repair-session.json"
        f.write_text("{}")
        return {"_path": str(f), "cdpEndpoint": "http://localhost:9222", **extra}

    def test_pid_in_the_session_is_killed(self, fix, monkeypatch, tmp_path):
        monkeypatch.setattr(fix.time, "sleep", lambda s: None)
        killed = []
        monkeypatch.setattr(fix.os, "kill",
                            lambda pid, sig: killed.append((pid, sig)))
        fix._reap_parked_browser(self._session(tmp_path, browserPid=4242))
        assert killed[0] == (4242, fix.signal.SIGTERM)

    def test_missing_pid_falls_back_to_the_cdp_port(self, fix, monkeypatch, tmp_path):
        monkeypatch.setattr(fix, "_pid_listening_on", lambda endpoint: 777)
        monkeypatch.setattr(fix.time, "sleep", lambda s: None)
        killed = []
        monkeypatch.setattr(fix.os, "kill",
                            lambda pid, sig: killed.append((pid, sig)))
        fix._reap_parked_browser(self._session(tmp_path))
        assert killed[0] == (777, fix.signal.SIGTERM)

    def test_the_session_file_is_always_cleared(self, fix, monkeypatch, tmp_path):
        monkeypatch.setattr(fix, "_pid_listening_on", lambda endpoint: 0)
        session = self._session(tmp_path)
        fix._reap_parked_browser(session)
        assert not Path(session["_path"]).exists()

    def test_a_browser_that_survives_is_reported_not_assumed(self, fix, monkeypatch,
                                                             tmp_path, capsys):
        monkeypatch.setattr(fix.os, "kill", lambda pid, sig: None)  # never dies
        monkeypatch.setattr(fix.time, "sleep", lambda s: None)
        fix._reap_parked_browser(self._session(tmp_path, browserPid=4242))
        out = capsys.readouterr().out
        assert "ignored SIGTERM" in out and "closed" not in out
