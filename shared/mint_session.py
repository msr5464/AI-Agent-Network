"""Produce a login session by running the test's own login code.

`session_state.usable()` hard-stops without a storage state, which is right —
exploring without one lands on a login page and "discovers" that the whole flow
changed. But the file it wanted only existed if a module's helper happened to
call `BrowserHelper.storeSession()`, and that is keyed on the `ProjectName` enum
with exactly one caller. Most modules could never produce one.

The first answer to that was to *transcribe* the login: scrape selectors out of a
`LoginPage` page object with a regex and replay them in Node. It resolved
GitHub's `LoginPage` when asked for Naukri's — whose page object is called
`NaukriLoginPage` — and typed a Naukri password into `#login_field`. Widening the
glob would have fixed that one case and left the design intact: every convention
it relied on is already contradicted somewhere in the framework.

So this runs the login instead. `shared/entry_path` reads the test under
adaptation and reports what *it* does; `automation.core.SessionMinter` then
invokes that same helper method through Maven. Whatever the module's login does —
its own navigation, post-submit steps, dismissed modals — happens because the
module's own code is what ran.

The credential still never enters a prompt, a model context, or a log, and now it
never crosses a command line either: `SessionMinter` receives property *keys* and
resolves them to values inside the JVM.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from shared import browser_mode

# The one line of SessionMinter's output that is contractual. Framework logging
# emits plenty of other braces, so the marker is what makes this parseable.
_MARKER = "MINT_RESULT "

MINT_TIMEOUT_S = int(os.environ.get("ADAPT_MINT_TIMEOUT_S", "420"))


def properties_path(workspace, environment: str = "", country: str = "") -> Path:
    environment = (environment or os.environ.get("ADAPT_ENVIRONMENT", "staging")).lower()
    country = (country or os.environ.get("ADAPT_COUNTRY", "SG")).lower()
    return Path(workspace) / "parameters" / f"{environment}-{country}.properties"


def read_properties(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def entry_url(workspace, module: str) -> str:
    """The module's configured entry URL, for the bounce check below only.

    Best effort by design: the framework spells this `saucedemo.url`,
    `naukari.url` and `githubUrl`, so there is no key to rely on. A miss costs
    the check, not the mint.
    """
    props = read_properties(properties_path(workspace))
    for key in (f"{module.lower()}.url", f"{module.lower()}Url", f"{module}Url"):
        if props.get(key):
            return props[key]
    return ""


def _bounced(landed: str, login_url: str) -> bool:
    """Did the browser end up back on the page it was signing in from?

    A login that is rejected — or raced, as Naukri's is by navigating away before
    the submit completes — redirects back to the login form, usually with the
    intended destination in the query string. The context then holds cookies and
    looks authenticated while being nothing of the kind, which is exactly the
    file that makes a later explorer report that the whole flow changed.
    """
    if not landed or not login_url:
        return False
    return urlparse(landed).path.rstrip("/") == urlparse(login_url).path.rstrip("/")


def _parse(stdout: str) -> Optional[Dict]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(_MARKER):
            try:
                return json.loads(line[len(_MARKER):])
            except ValueError:
                return None
    return None


def mint(workspace, module: str, entry: Dict, headless: Optional[bool] = None,
         log=lambda m: None) -> Dict:
    """Run the entry path the test uses. Returns {ok, path, reason}.

    `headless` unset means "whatever this run asked for" — HEADLESS_BROWSER,
    then headless. A login is the one browser step worth watching when it goes
    wrong, so it must not be the step that ignores the switch.
    """
    workspace = Path(workspace)
    if headless is None:
        headless = browser_mode.headless()
    mode = entry.get("mode")

    if mode == "stored_session":
        # Nothing to mint: the test names a session file. If it is missing or
        # stale, the fix is to run whatever test writes it, not to invent one.
        path = workspace / entry["session"]["path"]
        if path.exists():
            return {"ok": True, "path": path, "reason": "", "minted": False}
        return {"ok": False, "path": None, "minted": False,
                "reason": f"the test loads {entry['session']['path']}, which does "
                          f"not exist. Run the test that writes it "
                          f"(the one calling storeCurrentSession) first."}

    if mode != "credential":
        return {"ok": False, "path": None, "minted": False,
                "reason": entry.get("reason", "the test states no login path")}

    out_dir = workspace / "src" / "test" / "resources" / module.lower() / "loginStorage"
    out_path = out_dir / f"{module.capitalize()}LoginStorage.json"
    # Mint to one side and promote on success. A failed mint that cleans up its
    # own output would otherwise delete whatever session was already there — and
    # the session already there is, by definition, one that worked.
    staging = out_dir / f".{out_path.name}.minting"

    log(f"  running {entry['helper'].rsplit('.', 1)[-1]}.{entry['method']}"
        f"({', '.join(entry['arg_keys'])}) — the same call the test makes")
    log(f"  credentials resolve inside the JVM from property keys; no value "
        f"crosses the command line")

    command = [
        "mvn", "-q", "compile", "exec:java@mint",
        f"-Dheadless={'true' if headless else 'false'}",
        f"-Dmint.helper={entry['helper']}",
        f"-Dmint.method={entry['method']}",
        f"-Dmint.argKeys={','.join(entry['arg_keys'])}",
        f"-Dmint.out={staging}",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=MINT_TIMEOUT_S, cwd=str(workspace))
    except subprocess.TimeoutExpired:
        return {"ok": False, "path": None, "minted": False,
                "reason": f"the login did not finish within {MINT_TIMEOUT_S}s"}
    except OSError as exc:
        return {"ok": False, "path": None, "minted": False,
                "reason": f"could not run maven: {exc}"}

    outcome = _parse(result.stdout)
    if outcome is None:
        staging.unlink(missing_ok=True)
        tail = (result.stderr or result.stdout or "")[-400:]
        return {"ok": False, "path": None, "minted": False,
                "reason": f"the login run produced no result line: {tail}"}

    if not outcome.get("ok"):
        staging.unlink(missing_ok=True)
        return {"ok": False, "path": None, "minted": False,
                "reason": outcome.get("error", "login failed")}

    landed = outcome.get("url", "")
    login_url = entry_url(workspace, module)
    if _bounced(landed, login_url):
        staging.unlink(missing_ok=True)
        return {"ok": False, "path": None, "minted": False, "landed_on": landed,
                "reason": f"the login ran but did not authenticate: it ended back "
                          f"on {landed}. The test's own login path is what failed "
                          f"here, which is a finding about the module rather than "
                          f"about minting."}

    staging.replace(out_path)
    return {"ok": True, "path": out_path, "reason": "", "minted": True,
            "landed_on": landed, "cookies": outcome.get("cookies", 0),
            "degraded": bool(outcome.get("degraded")),
            "post_login_error": outcome.get("error", "")}
