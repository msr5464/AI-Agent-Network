"""Which session Locate replays with, and when it declines to use one.

The bug this pins: Locate globbed loginStorage/ for the newest file and validated
it by mtime. That answered "is there a session for this module?" when the question
is "how does THIS test sign in" — and it handed the replay a 12-hour-dead session
whose file was 5 hours old. The browser accepted it, the flow landed on a login
page, every locator looked missing, and the verdict was WRONG_STATE blaming a page
that was never examined.

`entry_path` and `session_state` already answered both questions; Locate just
never asked them.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(monkeypatch=None):
    import os
    os.environ.setdefault("AUDIT_DIR", str(ROOT / "tests" / "fixtures"))
    os.environ.setdefault("HANDOFF_FILE", str(ROOT / "tests" / "fixtures" / "none.json"))
    path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_locate.py"
    spec = importlib.util.spec_from_file_location("healing_locate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["healing_locate"] = mod
    spec.loader.exec_module(mod)
    return mod


loc = pytest.importorskip("bs4") and _load()

ISSUE = {"test_name": "automation.naukari.NaukriProfileSummaryWebTest"
                      ".toggleProfileSummaryDotAndVerify"}
CREDENTIAL = {"mode": "credential", "helper": "a.b.Helper", "method": "doThing",
              "arg_keys": ["u", "p"]}


@pytest.fixture
def wire(monkeypatch):
    """Stub the two modules Locate now delegates to."""
    def _wire(entry, usable, mint=None):
        monkeypatch.setattr(loc.entry_path, "extract", lambda w, t: entry)
        monkeypatch.setattr(loc.entry_path, "describe", lambda e: e.get("mode", ""))
        monkeypatch.setattr(loc.session_state, "usable",
                            lambda w, m, min_remaining_s=60: usable)
        monkeypatch.setattr(loc.mint_session, "mint",
                            lambda w, m, e, headless=True, log=None: mint or {})
        monkeypatch.setattr(loc, "STORAGE_STATE", "")
    return _wire


class TestStorageState:
    def test_a_live_session_is_used(self, wire):
        wire(CREDENTIAL, {"ok": True, "path": Path("/tmp/live.json")})
        assert loc._storage_state(Path("/ws"), ISSUE) == "/tmp/live.json"

    def test_an_expired_session_is_never_handed_to_the_replay(self, wire, monkeypatch):
        """The whole bug: an expired file parses fine and lands on a login page."""
        monkeypatch.setattr(loc, "MINT_SESSION", False)
        wire(CREDENTIAL, {"ok": False, "path": Path("/tmp/dead.json"),
                          "reason": "has expired: nauk_at"})
        assert loc._storage_state(Path("/ws"), ISSUE) is None

    def test_a_credential_test_mints_rather_than_reusing_a_dead_session(self, wire, monkeypatch):
        monkeypatch.setattr(loc, "MINT_SESSION", True)
        monkeypatch.setattr(loc, "_headless", lambda w: True)
        wire(CREDENTIAL, {"ok": False, "reason": "expired"},
             mint={"ok": True, "path": Path("/tmp/fresh.json")})
        assert loc._storage_state(Path("/ws"), ISSUE) == "/tmp/fresh.json"

    def test_a_failed_mint_replays_unauthenticated_rather_than_lying(self, wire, monkeypatch):
        monkeypatch.setattr(loc, "MINT_SESSION", True)
        monkeypatch.setattr(loc, "_headless", lambda w: True)
        wire(CREDENTIAL, {"ok": False, "reason": "expired"},
             mint={"ok": False, "reason": "login did not finish"})
        assert loc._storage_state(Path("/ws"), ISSUE) is None

    def test_minting_can_be_switched_off(self, wire, monkeypatch):
        monkeypatch.setattr(loc, "MINT_SESSION", False)
        wire(CREDENTIAL, {"ok": False, "reason": "expired"},
             mint={"ok": True, "path": Path("/tmp/fresh.json")})
        assert loc._storage_state(Path("/ws"), ISSUE) is None

    def test_a_stored_session_test_is_not_minted_for(self, wire, monkeypatch):
        """If the test names a session file, inventing one is not the fix."""
        monkeypatch.setattr(loc, "MINT_SESSION", True)
        wire({"mode": "stored_session"}, {"ok": False, "reason": "expired: nauk_at"},
             mint={"ok": True, "path": Path("/tmp/should-not-be-used.json")})
        assert loc._storage_state(Path("/ws"), ISSUE) is None

    def test_an_explicit_override_still_wins(self, monkeypatch, tmp_path):
        override = tmp_path / "mine.json"
        override.write_text("{}")
        monkeypatch.setattr(loc, "STORAGE_STATE", str(override))
        assert loc._storage_state(Path("/ws"), ISSUE) == str(override)


class TestTestIdNormalisation:
    @pytest.mark.parametrize("given,expected", [
        # The handoff writes Class.method; entry_path wants Class#method.
        ("automation.naukari.FooTest.barMethod", "automation.naukari.FooTest#barMethod"),
        # Already correct — left alone.
        ("automation.naukari.FooTest#barMethod", "automation.naukari.FooTest#barMethod"),
        # A bare class must not have its own name split off.
        ("automation.naukari.FooTest", "automation.naukari.FooTest"),
    ])
    def test_ids_reach_entry_path_in_the_shape_it_expects(self, monkeypatch, given, expected):
        seen = {}
        monkeypatch.setattr(loc.entry_path, "extract",
                            lambda w, t: seen.setdefault("id", t) and {} or {"mode": "none"})
        monkeypatch.setattr(loc.entry_path, "describe", lambda e: "")
        monkeypatch.setattr(loc.session_state, "usable",
                            lambda w, m, min_remaining_s=60: {"ok": False, "reason": "x"})
        monkeypatch.setattr(loc, "STORAGE_STATE", "")
        loc._storage_state(Path("/ws"), {"test_name": given})
        assert seen["id"] == expected


LOGIN_PAGE = '''
package automation.modules.naukari.web;
public class NaukriLoginPage extends BasePage {
    private final Locator usernameField = page.locator("[id='usernameField']");
    private final Locator passwordField = page.locator("[id='passwordField']");
    private final Locator loginButton   = page.locator("button.blue-btn[type='submit']");
}
'''

PROPS = {"naukari.username": "u@example.test", "naukari.password": "s3cret",
         "naukari.login.url": "https://site.test/nlogin/login"}


class TestLoginFieldDiscovery:
    """Found by shape, because repos name these differently every time."""

    @pytest.fixture
    def repo(self, tmp_path):
        d = tmp_path / "src" / "main" / "java" / "automation" / "modules" / "naukari"
        d.mkdir(parents=True)
        (d / "NaukriLoginPage.java").write_text(LOGIN_PAGE)
        return tmp_path

    def test_finds_username_password_and_submit(self, repo):
        assert loc._login_fields(repo, "naukari") == {
            "user": "[id='usernameField']",
            "password": "[id='passwordField']",
            "submit": "button.blue-btn[type='submit']"}

    def test_password_is_not_mistaken_for_the_username(self, repo):
        """`passwordField` matches /login|user/ on 'Field' alone if unguarded."""
        assert loc._login_fields(repo, "naukari")["user"] != "[id='passwordField']"

    def test_a_repo_with_no_login_page_returns_nothing(self, tmp_path):
        (tmp_path / "src" / "main" / "java").mkdir(parents=True)
        assert loc._login_fields(tmp_path, "naukari") == {}

    def test_an_incomplete_login_page_is_refused(self, tmp_path):
        """Two of three fields is a half-configured login — worse than refusing."""
        d = tmp_path / "src" / "main" / "java"
        d.mkdir(parents=True)
        (d / "PartialLoginPage.java").write_text(
            'private final Locator usernameField = page.locator("#u");\n'
            'private final Locator passwordField = page.locator("#p");\n')
        assert loc._login_fields(tmp_path, "naukari") == {}


class TestLoginReplay:
    @pytest.fixture
    def wired(self, monkeypatch, tmp_path):
        monkeypatch.setattr(loc.mint_session, "properties_path", lambda w, **k: tmp_path / "p")
        monkeypatch.setattr(loc.mint_session, "read_properties", lambda p: PROPS)
        monkeypatch.setattr(loc, "_login_fields", lambda w, m: {
            "user": "#u", "password": "#p", "submit": "#go"})
        return tmp_path

    def _entry(self, **kw):
        return {"mode": "credential",
                "arg_keys": ["naukari.username", "naukari.password"], **kw}

    def test_builds_a_replay_for_a_credential_test(self, wired):
        assert callable(loc._login_replay(wired, ISSUE, self._entry(), "https://site.test/p"))

    def test_the_replay_signs_in_then_goes_to_the_target(self, wired):
        calls = []

        class FakePage:
            url = "https://site.test/home"
            def goto(self, u): calls.append(("goto", u))
            def fill(self, sel, val): calls.append(("fill", sel, val))
            def click(self, sel): calls.append(("click", sel))
            def wait_for_url(self, matcher, timeout=0): calls.append(("wait",))

        loc._login_replay(wired, ISSUE, self._entry(), "https://site.test/p")(FakePage())
        assert calls[0] == ("goto", "https://site.test/nlogin/login")
        assert ("fill", "#u", "u@example.test") in calls
        assert ("fill", "#p", "s3cret") in calls
        assert ("click", "#go") in calls
        # The target must come last: going there before the redirect settles is
        # what aborts one of the two navigations.
        assert calls[-1] == ("goto", "https://site.test/p")

    def test_a_stored_session_test_gets_no_login_replay(self, wired):
        assert loc._login_replay(wired, ISSUE, {"mode": "stored_session"},
                                 "https://site.test/p") is None

    def test_missing_credentials_refuse_rather_than_half_login(self, wired, monkeypatch):
        monkeypatch.setattr(loc.mint_session, "read_properties",
                            lambda p: {"naukari.login.url": "https://site.test/l"})
        assert loc._login_replay(wired, ISSUE, self._entry(), "https://site.test/p") is None

    def test_missing_login_url_refuses(self, wired, monkeypatch):
        monkeypatch.setattr(loc.mint_session, "read_properties", lambda p: {
            "naukari.username": "u", "naukari.password": "p"})
        assert loc._login_replay(wired, ISSUE, self._entry(), "https://site.test/p") is None

    def test_no_target_url_refuses(self, wired):
        assert loc._login_replay(wired, ISSUE, self._entry(), "") is None
