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

from shared import blast_radius, workspace


def _default_workspace() -> str:
    """The automation repo, resolved the way the agents resolve it."""
    workspace.load_repo_env()
    return str(workspace.resolve(
        os.environ.get("WORKSPACE_DIR") or Path(__file__).resolve().parents[2],
        os.environ.get("GITHUB_REPO_AUTOMATION", ""),
        exclude=Path(__file__).resolve().parents[1]))


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
