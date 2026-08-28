#!/usr/bin/env python3
"""What a change to one area of the automation repo reaches.

Useful on its own, with no agent involved: point it at a package or a page object
and it tells you which tests exercise it, which it excluded as infrastructure,
and what verifying the set would cost.

    python3 scripts/blast_radius.py --affects 'automation.checkout.*'
    python3 scripts/blast_radius.py --test SauceDemoWebTest#loginAndVerifyProductsPage
    python3 scripts/blast_radius.py --module saucedemo --json
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import blast_radius


def _load_repo_env() -> None:
    """Read config/.env the way shared/load_env.sh does for the agents.

    Without this the CLI auto-detects "the first directory with a src/", which on
    a machine with several checkouts is reliably the wrong one.
    """
    for candidate in (Path(__file__).resolve().parents[1] / "config" / ".env",
                      Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _default_workspace() -> str:
    _load_repo_env()
    parent = os.environ.get("WORKSPACE_DIR") or str(Path(__file__).resolve().parents[2])
    name = os.environ.get("GITHUB_REPO_AUTOMATION", "")
    if name and (Path(parent) / name).exists():
        return str(Path(parent) / name)
    for candidate in sorted(Path(parent).iterdir()):
        if candidate.is_dir() and (candidate / "src").exists():
            return str(candidate)
    return parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=None, help="automation repo (auto-detected)")
    ap.add_argument("--affects", action="append", default=[],
                    help="glob over <package>.<Class>#<method>, repeatable")
    ap.add_argument("--test", action="append", default=[], help="explicit test, repeatable")
    ap.add_argument("--module", default="", help="fallback when --affects is absent")
    ap.add_argument("--hops", type=int, default=blast_radius.MAX_HOPS)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    workspace = args.workspace or _default_workspace()
    if not Path(workspace).is_dir():
        print(f"automation repo not found: {workspace}", file=sys.stderr)
        return 2

    result = blast_radius.resolve(workspace, affects=args.affects,
                                  named_tests=args.test, module=args.module,
                                  max_hops=args.hops)
    if args.json:
        print(json.dumps(result, indent=2, default=lambda o: sorted(o)
                         if isinstance(o, set) else str(o)))
    else:
        print(f"Workspace: {workspace}\n")
        print(blast_radius.describe(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
