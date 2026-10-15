"""Apply one change item atomically, or leave the repo exactly as it was.

Work is organised by change item rather than by file because a single item —
"a workspace picker now appears" — routinely spans a new page object, a helper
method and a call site. Applying those one file at a time leaves the repo
uncompilable between writes, and makes "roll back" ambiguous when the third write
fails.

    snapshot every target → apply all edits → guards → compile → verify
                          → on ANY failure, restore EVERY file

The snapshot is written to disk before the first write, not held in memory, so
`run.sh`'s ERR trap can restore it when the process dies partway through. The
automation checkout is shared with every other agent run on the machine; leaving
it in a state that is neither the original nor a working change is not an option.

Extracted from 04_adapt.py so the ordering can be tested without a model call:
the property that matters is that a failure at *any* stage restores *every* file,
and that is exactly the kind of thing that quietly stops being true.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from shared import edit_guards
from shared.code_analyzer import invalidate_file


class Transaction:
    """One change item's edits, applied together or not at all."""

    def __init__(self, audit_dir: Path, log: Callable[[str], None] = print):
        self.audit_dir = Path(audit_dir)
        self.log = log
        self.snapshots: Dict[str, str] = {}
        self.staged: Dict[str, str] = {}

    # ── staging ───────────────────────────────────────────────────────────────

    def stage(self, edits: List[dict]) -> str:
        """Compute the post-edit text for every file. Returns "" or an error.

        Nothing is written here. A bad edit in the third file must not leave the
        first two applied, so the whole item is computed before any of it lands.
        """
        by_file: Dict[str, List[dict]] = {}
        for edit in edits:
            path = edit.get("file") or ""
            if not path:
                return "an edit named no file"
            by_file.setdefault(str(Path(path).resolve()), []).append(edit)

        for path, file_edits in by_file.items():
            target = Path(path)
            if not target.exists():
                return f"target file does not exist: {path}"
            try:
                original = target.read_text(encoding="utf-8")
            except OSError as exc:
                return f"cannot read {target.name}: {exc}"
            updated, err = edit_guards.apply_edits(original, file_edits)
            if err:
                return f"{target.name}: {err}"
            self.snapshots[path] = original
            self.staged[path] = updated
        return ""

    def diff(self) -> str:
        return "\n".join(
            edit_guards.compute_diff(self.snapshots[p], self.staged[p], Path(p).name)
            for p in self.staged)

    # ── applying ──────────────────────────────────────────────────────────────

    def apply(self) -> None:
        """Write every staged file, recording the snapshot first."""
        (self.audit_dir / ".snapshots.json").write_text(json.dumps(self.snapshots))
        for path, updated in self.staged.items():
            Path(path).write_text(updated, encoding="utf-8")
            # The source cache would otherwise hand the next reader the text we
            # just replaced, so conservation would re-measure the old file.
            invalidate_file(Path(path))

    def rollback(self, why: str) -> None:
        for path, original in self.snapshots.items():
            try:
                Path(path).write_text(original, encoding="utf-8")
                invalidate_file(Path(path))
            except OSError as exc:
                self.log(f"  FAILED to restore {Path(path).name}: {exc}")
        (self.audit_dir / ".snapshots.json").unlink(missing_ok=True)
        self.log(f"  ↩︎ rolled back {len(self.snapshots)} file(s) — {why}")

    def commit(self) -> None:
        """Keep the changes; the snapshot is no longer needed for recovery."""
        (self.audit_dir / ".snapshots.json").unlink(missing_ok=True)

    # ── verification ──────────────────────────────────────────────────────────

    def compile(self, workspace: Path, command: str, timeout_s: int = 600) -> tuple:
        """Compile before running anything. Returns (ok, output).

        Not an optimisation. `00_reproduce.py` classifies "cannot find symbol" as
        INFRA_BUILD, which routes to *skip, don't call the model* — right when the
        repo arrived broken, and wrong when our own edit broke it. Compiling
        immediately after the edit is what tells the two apart, and it costs
        seconds where discovering it in a test run costs minutes.
        """
        _started = time.time()
        try:
            proc = subprocess.run(command.split(), cwd=str(workspace),
                                  capture_output=True, text=True, timeout=timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            self._record(command, time.time() - _started, "error")
            return False, f"could not run the compiler: {exc}"
        self._record(command, time.time() - _started,
                     "passed" if proc.returncode == 0 else "failed")
        if proc.returncode == 0:
            return True, ""
        return False, ((proc.stdout or "") + (proc.stderr or ""))[-3000:]

    @staticmethod
    def _record(command: str, elapsed_s: float, verdict: str) -> None:
        try:
            from shared import metrics
            metrics.record_tool("compile", command, elapsed_s, verdict)
        except Exception:
            pass
