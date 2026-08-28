"""Run the one targeted re-run that confirms or refutes a diagnosis.

The policy — which verdicts are worth probing and what result would confirm each
— lives in `shared/diagnosis.py`, next to the rules that produce them. Only the
execution lives here, because running a test needs the agent's runner and
`shared/` must not depend on an agent.

Every probe returns one of: "passed", "failed", "same_dom", "different_dom",
"inconclusive". Anything that goes wrong is "inconclusive", never a guess — a
probe that could not run has to leave the verdict exactly as it found it.
"""

import hashlib
from pathlib import Path
from typing import Callable, Optional

from shared.dom_snapshot import find_snapshot

from shared.test_runner import run_test, split_test_name

# What "give it more time" means for a probe. Large enough that a genuinely slow
# element appears, small enough that a hung page still ends the run.
_EXTENDED_BUDGET_S = 120


def _fingerprint(path: Path) -> str:
    """A stable digest of a captured page, ignoring the per-run header line."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    body = text.split("-->", 1)[-1] if text.startswith("<!--") else text
    return hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()


def run(kind: str, test_name: str, workspace: Path, results_dir: Path,
        before_snapshot: Optional[str] = None, log: Callable = print,
        timeout_s: int = 600) -> str:
    """Execute one probe and report what it observed."""
    _, _, method = split_test_name(test_name)
    properties = {"traceMode": "on"}

    if kind == "rerun_extended_budget":
        # The framework reads this as its element wait, so the whole question
        # "would it have appeared with more time?" is one property away.
        properties["ObjectWaitTime"] = str(_EXTENDED_BUDGET_S)
        log(f"  probe: re-running with a {_EXTENDED_BUDGET_S}s element budget")
    else:
        log(f"  probe: re-running {test_name}")

    try:
        status, _ = run_test(test_name, workspace, extra_properties=properties,
                             timeout_s=timeout_s, log=lambda _m: None)
    except Exception as exc:
        log(f"  probe could not run ({exc}) — leaving the verdict unchanged")
        return "inconclusive"

    if status == "unverified":
        return "inconclusive"

    if kind != "rerun_compare_dom":
        return status

    # A wrong page reached twice, byte for byte, is deterministic rather than
    # flaky — which is what separates "the test never gets there" from "it
    # usually gets there".
    if status == "passed":
        return "different_dom"
    if not before_snapshot or not Path(before_snapshot).exists():
        return "inconclusive"
    after = find_snapshot(results_dir, method)
    if not after:
        return "inconclusive"
    before_digest = _fingerprint(Path(before_snapshot))
    after_digest = _fingerprint(after)
    if not before_digest or not after_digest:
        return "inconclusive"
    return "same_dom" if before_digest == after_digest else "different_dom"
