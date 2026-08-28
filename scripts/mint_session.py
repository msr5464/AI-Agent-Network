#!/usr/bin/env python3
"""Establish a login session for a module, so exploration can start signed in.

    python3 scripts/mint_session.py --module naukari
    python3 scripts/mint_session.py --module naukari --headed
    python3 scripts/mint_session.py --test automation.github.GitHubLoginTest#storeFirstTimeLoginOnGitHub

The session is established the way the *test* establishes it: `shared/entry_path`
reads the test's own setup, and either reuses the storage state it loads or runs
the very login helper it calls. Nothing here guesses at a login form.

Credentials come from the automation repo's own
`parameters/{environment}-{country}.properties`, resolved inside the JVM from
property keys — no value crosses a command line, a prompt or a log.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import entry_path, mint_session, session_state

_TEST_METHOD = re.compile(r"@Test\b[\s\S]{0,400}?public\s+void\s+(\w+)\s*\(")


def _load_repo_env() -> None:
    for candidate in (Path(__file__).resolve().parents[1] / "config" / ".env",
                      Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def discover_entry(workspace: Path, module: str):
    """A test for the module that actually declares a login, and its entry path.

    Every test method is considered, not just the first one found: a module's
    tests are not all web tests, and picking alphabetically lands on
    `GitHubApiTest` — which signs in nowhere — while `GitHubLoginTest` sitting
    right next to it declares two different login paths. What is wanted here is
    a test that has an entry path, so that is the search.
    """
    roots = sorted((workspace / "src" / "test" / "java").rglob("*Test.java"))
    owned = [p for p in roots if module.lower() in {q.lower() for q in p.parts}]
    fallback = ("", {"mode": "none", "reason": f"no test found for module {module!r}"})
    for path in owned:
        source = path.read_text(encoding="utf-8", errors="ignore")
        parts = path.parts
        fqcn = ".".join(parts[parts.index("java") + 1:]).removesuffix(".java")
        for method in _TEST_METHOD.findall(source):
            test = f"{fqcn}#{method}"
            entry = entry_path.extract(workspace, test)
            if entry["mode"] != "none":
                return test, entry
            if not fallback[0]:
                fallback = (test, entry)
    return fallback


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", default="")
    ap.add_argument("--test", default="",
                    help="fqcn#method whose entry path to reuse; discovered "
                         "from --module when omitted")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--headed", action="store_true", help="watch the login happen")
    args = ap.parse_args()

    if not (args.module or args.test):
        print("give --module or --test", file=sys.stderr)
        return 2

    _load_repo_env()
    workspace = args.workspace
    if not workspace:
        parent = os.environ.get("WORKSPACE_DIR", "")
        name = os.environ.get("GITHUB_REPO_AUTOMATION", "")
        workspace = str(Path(parent) / name) if parent and name else ""
    if not workspace or not Path(workspace).is_dir():
        print(f"automation repo not found: {workspace}", file=sys.stderr)
        return 2
    workspace = Path(workspace)

    module = args.module or args.test.split("#")[0].split(".")[1]
    if args.test:
        test, entry = args.test, entry_path.extract(workspace, args.test)
    else:
        test, entry = discover_entry(workspace, module)
    if not test:
        print(f"no test found for module {module!r} — pass --test explicitly",
              file=sys.stderr)
        return 2

    print(f"Test:       {test}")
    print(f"Entry path: {entry_path.describe(entry)}")

    existing = session_state.usable(workspace, module)
    if existing["ok"]:
        print(f"A valid session already exists: {existing['path']}")
        print("Delete it first if you want a fresh one.")
        return 0

    result = mint_session.mint(workspace, module, entry,
                               headless=not args.headed, log=print)
    if not result["ok"]:
        print(f"FAILED: {result['reason']}", file=sys.stderr)
        return 1
    if not result.get("minted"):
        print(f"Reused the session the test loads: {result['path']}")
        return 0

    if result.get("degraded"):
        print(f"NOTE: authenticated, but the landing page did not load — "
              f"{result['post_login_error']}")
    check = session_state.usable(workspace, module)
    print(f"Signed in, landed on {result.get('landed_on')}")
    print(f"Saved {result['path']}")
    print(f"Verified: {check['report'].get('cookies')} cookie(s), "
          f"valid={check['report'].get('valid')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
