"""How does the test under adaptation get itself signed in? Read it, don't guess.

The adaptation agent edits an *existing* test. That test already states its own
entry path — which credentials, which login method, or which saved session — so
deriving login from repo-wide conventions was always answering a question the
source had already answered.

Conventions lost, repeatedly, because the framework has no single convention:

  * the login page object is `LoginPage` in two modules and `NaukriLoginPage` in
    a third;
  * the URL key is `saucedemo.url` and `naukari.url` — but `githubUrl`;
  * `GitHubLoginTest` logs in two different ways in one file: one test calls
    `loginWithStoredSession()`, the next reads `github.username` / `.password`
    and calls `doLogin(u, p)`.

So `extract()` reads the test's setup prefix and reports one of three modes:

  `stored_session` — the test loads a saved storage state. Reuse that exact file.
  `credential`     — the test reads named properties and calls a login method.
                     Use *those* keys and *that* method.
  `none`           — the test never signs in. Explore unauthenticated rather than
                     hard-stopping on a session it was never going to need.

This is the same guess-then-measure rule the agent already applies to page
objects, finally applied to login as well.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

# `String username = config.getRunTimeProperty("naukari.username");`
_PROPERTY_READ = re.compile(
    r"(?:String\s+)?(\w+)\s*=\s*\w+\s*\.\s*getRunTimeProperty\s*\(\s*\"([^\"]+)\"")

# `NaukriProfileSummaryHelper naukri = new NaukriProfileSummaryHelper(config);`
_HELPER_NEW = re.compile(r"(\w+)\s+(\w+)\s*=\s*new\s+(\w+)\s*\(")

# `NaukriProfilePage profilePage = naukri.doLogin(username, password);`
# also bare `github.loginWithStoredSession();`
_CALL = re.compile(r"(?:\w+\s+\w+\s*=\s*)?(\w+)\s*\.\s*(\w+)\s*\(([^)]*)\)\s*;")

# `import automation.modules.naukari.NaukriProfileSummaryHelper;`
_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+)\s*;", re.MULTILINE)

# `BrowserHelper.initBrowserWithStoredSession(config, ProjectName.GitHub, SESSION_FILE);`
_STORED_SESSION = re.compile(
    r"initBrowserWithStoredSession\s*\(\s*\w+\s*,\s*(?:\w+\s*\.\s*)?(\w+)\s*,\s*(\w+)")

# `private static final String SESSION_FILE = "GitHubLoginStorage.json";`
_STRING_CONST = re.compile(
    r"static\s+final\s+String\s+(\w+)\s*=\s*\"([^\"]+)\"")

# Method names worth following when the call's arguments do not identify it.
_LOGIN_HINTS = ("login", "signin", "authenticate", "session")


def test_source(workspace, test_id: str) -> Optional[Path]:
    """`automation.naukari.FooTest#bar` → the .java file that declares it."""
    fqcn = test_id.split("#", 1)[0]
    relative = Path(*fqcn.split(".")).with_suffix(".java")
    for root in ("src/test/java", "src/main/java"):
        candidate = Path(workspace) / root / relative
        if candidate.exists():
            return candidate
    return None


def method_body(source: str, method: str) -> str:
    """The body of one method, brace-matched from its signature."""
    start = re.search(r"\b" + re.escape(method) + r"\s*\([^)]*\)\s*\{", source)
    if not start:
        return ""
    depth, index = 0, start.end() - 1
    for index in range(start.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                break
    return source[start.end():index]


def _resolve(simple_name: str, source: str, workspace) -> Optional[Path]:
    """A simple class name → its file, via the importing file's own imports."""
    for _static, imported in _IMPORT.findall(source):
        if imported.rsplit(".", 1)[-1] == simple_name:
            path = Path(workspace) / "src" / "main" / "java" / Path(
                *imported.split(".")).with_suffix(".java")
            if path.exists():
                return path
    matches = list((Path(workspace) / "src" / "main").rglob(f"{simple_name}.java"))
    return matches[0] if matches else None


def _stored_session_path(workspace, helper_source: str, method: str) -> Optional[Dict]:
    """If this helper method loads a saved session, where that session lives."""
    body = method_body(helper_source, method)
    found = _STORED_SESSION.search(body)
    if not found:
        return None
    project, file_ref = found.group(1), found.group(2)
    constants = dict(_STRING_CONST.findall(helper_source))
    file_name = constants.get(file_ref, file_ref.strip('"'))
    return {
        "project": project,
        "file_name": file_name,
        "path": str(Path("src/test/resources") / project.lower()
                    / "loginStorage" / file_name),
    }


def extract(workspace, test_id: str) -> Dict:
    """The entry path the named test uses. See the module docstring for modes."""
    result: Dict = {"mode": "none", "test": test_id, "reason": "",
                    "helper": "", "method": "", "arg_keys": [], "session": None}
    near_miss = ""

    source_path = test_source(workspace, test_id)
    if source_path is None:
        result["reason"] = f"could not find the source for {test_id}"
        return result
    source = source_path.read_text(encoding="utf-8", errors="ignore")

    method = test_id.split("#", 1)[1] if "#" in test_id else ""
    body = method_body(source, method) if method else source
    if not body:
        result["reason"] = f"could not read the body of {test_id}"
        return result

    # variable -> property key, and variable -> helper type
    properties = dict(_PROPERTY_READ.findall(body))
    helpers = {var: cls for cls, var, ctor in _HELPER_NEW.findall(body) if cls == ctor}

    for receiver, called, raw_args in _CALL.findall(body):
        if receiver not in helpers:
            continue
        args = [a.strip() for a in raw_args.split(",") if a.strip()]
        credential_args = [a for a in args if a in properties]
        looks_like_login = any(h in called.lower() for h in _LOGIN_HINTS)
        if not credential_args and not looks_like_login:
            continue

        helper_path = _resolve(helpers[receiver], source, workspace)
        helper_source = (helper_path.read_text(encoding="utf-8", errors="ignore")
                         if helper_path else "")
        fqcn = ""
        if helper_path:
            parts = helper_path.parts
            if "java" in parts:
                after = parts[parts.index("java") + 1:]
                fqcn = ".".join(after).removesuffix(".java")

        # A saved session beats running a login: it is what the test itself does,
        # it costs nothing, and it cannot be raced by a broken login flow.
        if helper_source:
            session = _stored_session_path(workspace, helper_source, called)
            if session:
                return {**result, "mode": "stored_session", "helper": fqcn,
                        "method": called, "session": session,
                        "reason": f"{test_id.split('#')[0].rsplit('.', 1)[-1]} "
                                  f"calls {called}(), which loads "
                                  f"{session['file_name']}"}

        if credential_args and len(credential_args) == len(args):
            return {**result, "mode": "credential", "helper": fqcn,
                    "method": called,
                    "arg_keys": [properties[a] for a in args],
                    "reason": f"the test calls {called}() with "
                              f"{', '.join(properties[a] for a in args)}"}

        if credential_args:
            # A login call whose arguments are not all property-backed — GitHub's
            # OTP test passes a hardcoded "123456". Minting resolves property KEYS
            # inside the JVM precisely so no value crosses a command line, and it
            # has nowhere to get this one. Say which argument, rather than claiming
            # the test never signs in.
            unresolved = [a for a in args if a not in properties]
            near_miss = (f"the test calls {called}(), but "
                         f"{', '.join(repr(a) for a in unresolved)} "
                         f"{'is' if len(unresolved) == 1 else 'are'} not read from "
                         f"a property — minting has no way to supply "
                         f"{'it' if len(unresolved) == 1 else 'them'}")

    result["reason"] = near_miss or (
        "the test never signs in — no stored session and no credential-taking "
        "login call in its body")
    return result


def describe(entry: Dict) -> str:
    mode = entry.get("mode")
    if mode == "stored_session":
        return f"stored session — {entry['session']['path']}"
    if mode == "credential":
        return (f"credential — {entry['helper'].rsplit('.', 1)[-1]}"
                f".{entry['method']}({', '.join(entry['arg_keys'])})")
    return f"no login — {entry.get('reason', '')}"
