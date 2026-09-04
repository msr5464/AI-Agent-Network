#!/usr/bin/env python3
"""Commit element-fingerprint baselines after a green suite.

Runs at the end of a passing CI run, in the workspace clone. Four rules, and the
third is the one that needs care:

  * **Path-scoped.** Only the baselines directory is ever staged. This job has
    write access to the repo; it has no business touching source.
  * **Green only.** The caller decides — the Java side already discards anything
    a failing test recorded, so by the time we get here the files on disk are a
    good run's or nothing.
  * **No-op when unchanged.** Measured with `recordedAt` excluded. Two
    consecutive green runs produce byte-identical fingerprints and a different
    timestamp, so a naive "did the file change" check commits every baseline on
    every build. The timestamp cannot simply be dropped: baseline.load()'s
    staleness guard uses it to reject a record written by the failing run itself.
  * **Never fails the build.** A missing baseline lowers confidence in a later
    diagnosis. It is not worth failing a green suite over.

Usage: commit_baselines.py <workspace> [--branch <name>] [--push]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.git import run_git

BASELINE_PATH = "src/main/resources/baselines"
VOLATILE_KEYS = ("recordedAt",)


def _substance(path: Path) -> str:
    """The file's content with per-run noise removed, for comparison only."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return path.read_text(errors="ignore") if path.exists() else ""
    if isinstance(data, dict):
        for key in VOLATILE_KEYS:
            data.pop(key, None)
    return json.dumps(data, sort_keys=True)


def _committed_version(workspace: Path, relative: str) -> str:
    ok, out, _ = run_git(["show", f"HEAD:{relative}"], workspace)
    if not ok:
        return ""
    try:
        data = json.loads(out)
    except ValueError:
        return out
    if isinstance(data, dict):
        for key in VOLATILE_KEYS:
            data.pop(key, None)
    return json.dumps(data, sort_keys=True)


def changed_baselines(workspace: Path) -> list[Path]:
    """Baseline files whose substance differs from what is committed."""
    root = workspace / BASELINE_PATH
    if not root.is_dir():
        return []
    changed = []
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(workspace).as_posix()
        if _substance(path) != _committed_version(workspace, relative):
            changed.append(path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--branch", default="")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--push-url", default="")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    if not (workspace / ".git").exists():
        print(f"not a git working tree: {workspace}")
        return 0

    changed = changed_baselines(workspace)
    if not changed:
        print("baselines unchanged — nothing to commit")
        return 0

    print(f"{len(changed)} baseline(s) changed:")
    for path in changed[:20]:
        print(f"  {path.relative_to(workspace)}")

    if args.branch:
        run_git(["checkout", "-B", args.branch], workspace)

    # Path-scoped: never `git add -A` in a job that holds a write token.
    ok, _, err = run_git(["add", "--", BASELINE_PATH], workspace)
    if not ok:
        print(f"could not stage baselines: {err}")
        return 0

    ok, _, err = run_git(
        ["commit", "-m",
         f"chore(baselines): refresh {len(changed)} locator fingerprint(s)\n\n"
         f"Recorded from a passing suite. Fingerprints describe what each page\n"
         f"object locator matched while it worked, so a later drift can be\n"
         f"diagnosed by comparison rather than by guesswork."],
        workspace)
    if not ok:
        print(f"nothing committed: {err}")
        return 0
    print("committed")

    if args.push:
        target = ["push", "origin", args.branch or "HEAD"]
        ok, _, err = run_git(target, workspace, push_url=args.push_url or None)
        print("pushed" if ok else f"push failed (not fatal): {err}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # noqa: BLE001 - never fail a green build
        print(f"baseline commit skipped ({type(exc).__name__}: {exc})")
        sys.exit(0)
