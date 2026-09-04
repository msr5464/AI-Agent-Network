"""Tests for shared/mint_session.py.

Minting used to transcribe a login: scrape selectors out of a `LoginPage` page
object and replay them in Node. Asked for Naukri's — whose page object is called
`NaukriLoginPage` — it silently fell back to GitHub's and drove naukri.com with
`#login_field`. It now runs the module's own login helper instead, so what needs
pinning is the two ways that can still go wrong: reusing a session that was never
there, and calling a bounced login a success.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import mint_session


def test_stored_session_mode_reuses_the_file_and_mints_nothing(tmp_path):
    session = tmp_path / "src/test/resources/github/loginStorage/GitHubLoginStorage.json"
    session.parent.mkdir(parents=True)
    session.write_text('{"cookies": []}')
    entry = {"mode": "stored_session",
             "session": {"path": "src/test/resources/github/loginStorage/"
                                 "GitHubLoginStorage.json",
                         "file_name": "GitHubLoginStorage.json"}}
    outcome = mint_session.mint(tmp_path, "github", entry)
    assert outcome["ok"] is True and outcome["minted"] is False


def test_a_missing_stored_session_says_which_test_writes_it(tmp_path):
    """The old message told people to run any passing test. Only one writes it."""
    entry = {"mode": "stored_session",
             "session": {"path": "src/test/resources/github/loginStorage/"
                                 "GitHubLoginStorage.json",
                         "file_name": "GitHubLoginStorage.json"}}
    outcome = mint_session.mint(tmp_path, "github", entry)
    assert outcome["ok"] is False
    assert "storeCurrentSession" in outcome["reason"]


def test_mode_none_refuses_without_running_anything(tmp_path, monkeypatch):
    """No login path means no browser — and certainly no maven invocation."""
    def explode(*a, **k):
        raise AssertionError("should not have run a subprocess")
    monkeypatch.setattr(mint_session.subprocess, "run", explode)
    outcome = mint_session.mint(tmp_path, "naukari",
                                {"mode": "none", "reason": "the test never signs in"})
    assert outcome["ok"] is False
    assert outcome["reason"] == "the test never signs in"


@pytest.mark.parametrize("landed,login,expected", [
    ("https://www.naukri.com/nlogin/login?URL=//x/profile",
     "https://www.naukri.com/nlogin/login", True),      # bounced back
    ("https://www.naukri.com/mnjuser/homepage",
     "https://www.naukri.com/nlogin/login", False),     # moved on
    ("https://www.naukri.com/nlogin/login/",
     "https://www.naukri.com/nlogin/login", True),      # trailing slash
    ("https://www.naukri.com/mnjuser/homepage", "", False),   # no URL to check
])
def test_bounce_detection(landed, login, expected):
    """Cookies alone do not mean authenticated: a rejected login sets them too."""
    assert mint_session._bounced(landed, login) is expected


def test_a_bounced_login_is_a_failure_and_leaves_no_file(tmp_path, monkeypatch):
    """The saved state would look valid and drop the next browser onto a login."""
    (tmp_path / "parameters").mkdir()
    (tmp_path / "parameters" / "staging-sg.properties").write_text(
        "naukari.url=https://www.naukri.com/nlogin/login\n")
    out = tmp_path / "src/test/resources/naukari/loginStorage/NaukariLoginStorage.json"
    out.parent.mkdir(parents=True)
    out.write_text('{"cookies": [{"name": "pre-auth"}]}')

    class Result:
        stdout = ('MINT_RESULT {"ok":true,"degraded":true,"cookies":11,'
                  '"url":"https://www.naukri.com/nlogin/login?URL=//x","error":"x"}')
        stderr = ""
    monkeypatch.setattr(mint_session.subprocess, "run", lambda *a, **k: Result())

    outcome = mint_session.mint(tmp_path, "naukari",
                                {"mode": "credential", "helper": "H",
                                 "method": "doLogin", "arg_keys": ["a", "b"]})
    assert outcome["ok"] is False
    assert "did not authenticate" in outcome["reason"]
    # And the session that was already there — the one that demonstrably worked —
    # is still there. A failed mint cleans up after itself, not after everyone.
    assert out.exists()
    assert "pre-auth" in out.read_text()


def test_property_keys_are_passed_not_values(tmp_path, monkeypatch):
    """A password on a command line is a password in `ps` and in any parent log."""
    seen = {}

    class Result:
        stdout = ('MINT_RESULT {"ok":true,"degraded":false,"cookies":9,'
                  '"url":"https://app/home","error":""}')
        stderr = ""

    def capture(command, **kwargs):
        seen["command"] = command
        # SessionMinter writes the storage state to -Dmint.out and creates its
        # parent; stand in for that so the promotion step has something to move.
        target = Path([c for c in command if c.startswith("-Dmint.out=")][0]
                      .split("=", 1)[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"cookies": [{"name": "session"}]}')
        return Result()
    monkeypatch.setattr(mint_session.subprocess, "run", capture)

    outcome = mint_session.mint(tmp_path, "naukari",
                                {"mode": "credential", "helper": "H",
                                 "method": "doLogin",
                                 "arg_keys": ["naukari.username", "naukari.password"]})
    assert outcome["ok"] is True and outcome["minted"] is True
    joined = " ".join(seen["command"])
    assert "-Dmint.argKeys=naukari.username,naukari.password" in joined
    # The staging file was promoted to the name the framework reads.
    assert outcome["path"].name == "NaukariLoginStorage.json"
    assert outcome["path"].exists()


def _mint_capturing_command(tmp_path, monkeypatch):
    """Run a successful mint and hand back the maven command it built."""
    seen = {}

    class Result:
        stdout = ('MINT_RESULT {"ok":true,"degraded":false,"cookies":9,'
                  '"url":"https://app/home","error":""}')
        stderr = ""

    def capture(command, **kwargs):
        seen["command"] = command
        target = Path([c for c in command if c.startswith("-Dmint.out=")][0]
                      .split("=", 1)[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"cookies": [{"name": "session"}]}')
        return Result()
    monkeypatch.setattr(mint_session.subprocess, "run", capture)

    mint_session.mint(tmp_path, "naukari",
                      {"mode": "credential", "helper": "H", "method": "doLogin",
                       "arg_keys": ["naukari.username"]})
    return " ".join(seen["command"])


def test_minting_follows_playwright_headless_when_the_caller_says_nothing(
        tmp_path, monkeypatch):
    """A login is the browser step most worth watching when it goes wrong, so
    it must not be the one that ignores the switch everything else honours."""
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    assert "-Dheadless=false" in _mint_capturing_command(tmp_path, monkeypatch)


def test_an_explicit_headless_argument_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")
    seen = {}

    class Result:
        stdout = ('MINT_RESULT {"ok":true,"degraded":false,"cookies":9,'
                  '"url":"https://app/home","error":""}')
        stderr = ""

    def capture(command, **kwargs):
        seen["command"] = command
        target = Path([c for c in command if c.startswith("-Dmint.out=")][0]
                      .split("=", 1)[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"cookies": []}')
        return Result()
    monkeypatch.setattr(mint_session.subprocess, "run", capture)

    mint_session.mint(tmp_path, "naukari",
                      {"mode": "credential", "helper": "H", "method": "doLogin",
                       "arg_keys": ["naukari.username"]}, headless=True)
    assert "-Dheadless=true" in " ".join(seen["command"])
