"""What a test proves, kept separate from how it proves it.

A locator repair is safe because the shape of the edit constrains it: one string,
on a page we proved we were on. Once an edit may add or remove steps, shape
constrains nothing, and "the test passes" is close to worthless as evidence — a
test that asserts nothing passes fastest of all.

So the mechanism becomes mutable and the proof does not. An intent contract is
that proof written down: the assertions that must survive, the pages that must be
reached, and the things no repair may ever do.

Three sources, and a missing contract never blocks a run:

  1. **authored** — checked into the automation repo beside the tests, where it
     belongs: it describes those tests and has to move with them.
  2. **derived** — computed from the pre-edit source. Stronger than it sounds,
     because CONVENTIONS.md §10 requires every test step to carry a
     `logStep(testConfig, "...")` stating the action and its expected outcome, so
     a derived contract has real prose in it and not just call signatures.
  3. **absent** — the run continues with assertion conservation alone.

The load-bearing rule is **freeze before edit**. A contract is derived from the
original source and stored; every guard compares against that stored copy. Deriving
it again afterwards would let an edit that deleted an assertion produce a contract
that no longer expects one, and the guard would approve its own violation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from shared import assertion_graph

# Where authored contracts live inside the automation repo.
CONTRACT_DIR = Path("src") / "test" / "resources" / "intents"

# Things no repair may do, whatever the change note says. Stated positively in
# the contract so they survive into the prompt and the PR body.
NEVER = [
    "remove, weaken, or make conditional any assertion",
    "wrap a verification in try/catch",
    "reach the expected outcome by skipping a step rather than performing it",
    "replace a page-identity check with one that also passes on another page",
]

MUTABLE = ["locators", "waits", "navigation steps", "intermediate pages"]


def path_for(workspace, test: str) -> Path:
    """`pkg.Class#method` → the contract file that would describe it."""
    safe = test.replace("#", "_").replace(".", "_")
    return Path(workspace) / CONTRACT_DIR / f"{safe}.json"


def load(workspace, test: str) -> Optional[Dict]:
    """An authored contract, or None. Never raises."""
    path = path_for(workspace, test)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        data.setdefault("source", "authored")
        return data
    return None


def derive(workspace, test: str, index: Optional[Dict] = None) -> Dict:
    """Compute a contract from the current source. Cheap, mechanical, no model."""
    simple_class, _, method = test.replace("#", ".").rpartition(".")
    simple_class = simple_class.rsplit(".", 1)[-1]
    if index is None:
        index = assertion_graph.member_index(str(workspace))

    fingerprints = assertion_graph.fingerprints(simple_class, method, index)
    identity = sorted({
        info["site"] for info in fingerprints["asserts"].values()
        if "assertPageLoaded" in info["callee"]})

    return {
        "test": test,
        "source": "derived",
        "proves": fingerprints["log_steps"],
        "invariants": [
            {"kind": "assertion", "fingerprint": fp, "callee": info["callee"],
             "site": info["site"], "literals": info["literals"],
             "must_remain": True}
            for fp, info in sorted(fingerprints["asserts"].items())
        ],
        "identity": identity,
        "mutable": list(MUTABLE),
        "never": list(NEVER),
        "unresolved": fingerprints["unresolved"],
        "_fingerprints": fingerprints,
    }


def for_test(workspace, test: str, index: Optional[Dict] = None) -> Dict:
    """The best contract available for one test, always something."""
    authored = load(workspace, test)
    derived = derive(workspace, test, index)
    if authored:
        # An authored contract states intent; the derived fingerprints are what
        # the guards actually compare. Keep both rather than choosing.
        authored["_fingerprints"] = derived["_fingerprints"]
        authored.setdefault("invariants", derived["invariants"])
        authored.setdefault("identity", derived["identity"])
        authored.setdefault("never", list(NEVER))
        return authored
    return derived


def freeze(contracts: Dict[str, Dict]) -> Dict[str, Dict]:
    """Strip the un-serialisable working state before a contract is stored.

    What gets written to 02-scope.json is the thing every later guard compares
    against, so it has to be plain JSON and it has to be written once, before any
    edit. Re-deriving after an edit is how a conservation guard ends up approving
    its own violation.
    """
    frozen = {}
    for test, contract in contracts.items():
        copy = {k: v for k, v in contract.items() if k != "_fingerprints"}
        fps = contract.get("_fingerprints") or {}
        copy["_asserts"] = fps.get("asserts", {})
        copy["_unresolved"] = fps.get("unresolved", [])
        frozen[test] = copy
    return frozen


def thaw(frozen: Dict) -> Dict:
    """A frozen contract back into the shape assertion_graph.conserved expects."""
    return {"asserts": frozen.get("_asserts") or {},
            "unresolved": frozen.get("_unresolved") or [],
            "log_steps": frozen.get("proves") or []}


def verifies(contract: Dict) -> List[str]:
    """The human-readable half of each assertion the test must keep making.

    `AssertHelper.assertEquals(config, cart.getCount(), "1", "Cart badge should
    show 1 after adding a product")` records both literals; the message is the
    last one, and it is the only half worth showing a human. Literals keep the
    quotes they were captured with, and a bare expected value like `"1"` is not
    a sentence — hence the strip and the space test.
    """
    out: List[str] = []
    seen = set()
    for invariant in contract.get("invariants") or []:
        literals = invariant.get("literals") or []
        if not literals:
            continue
        message = str(literals[-1]).strip().strip('"').strip()
        if " " not in message or message in seen:
            continue
        seen.add(message)
        out.append(message)
    return out


def describe(contract: Dict) -> str:
    """The contract as markdown, for the scope report and the PR body."""
    lines = [f"**{contract.get('test','?')}** — contract `{contract.get('source','?')}`"]
    if contract.get("proves"):
        lines.append("\nProves:")
        lines += [f"- {step}" for step in contract["proves"]]
    invariants = contract.get("invariants") or []
    lines.append(f"\n{len(invariants)} assertion invariant(s) must survive.")
    if contract.get("identity"):
        lines.append("Page-identity checks: " + ", ".join(contract["identity"]))
    if contract.get("unresolved"):
        lines.append(f"\n⚠️ {len(contract['unresolved'])} call(s) could not be "
                     f"resolved — conservation is PLAUSIBLE, not CONFIRMED here.")
    return "\n".join(lines)
