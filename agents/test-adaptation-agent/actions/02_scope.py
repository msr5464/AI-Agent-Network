#!/usr/bin/env python3
"""
Step 02 — Scope

Works out which tests the change reaches, and freezes what each of them currently
proves. No model call, no network, no browser: everything here is a static read of
the automation repo, which makes it the one step that is fully unit-testable and
the one worth reading before anything expensive happens.

Two outputs, and the second is the more important one:

  * the blast radius — named tests, the shared surface that still passes today but
    would break when this ships, and what was excluded as infrastructure;
  * the **frozen intent contracts** — what each test proves, measured *before* any
    edit. Every later guard compares against this copy. Re-deriving it after the
    edit would produce a contract describing the edited code, and a conservation
    check against that approves whatever the edit did.

It also prints the cost of verification before exploration spends thirty minutes,
because the verify set holds the server's single global run slot and "14 tests
≈ 42 minutes" is a decision a human should get to make up front.

Reads:   $AUDIT_DIR/01-parse-change.json
Writes:  $AUDIT_DIR/02-scope.json + .md
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.log import log as _log
def log(msg): _log("scope", msg)

from shared import blast_radius, entry_path, intent, workspace as workspace_helper
from shared.assertion_graph import member_index
from shared.git import run_git

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", str(REPO_ROOT.parent))
GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "")
VERIFY_POLICY = os.environ.get("ADAPTATION_VERIFY_POLICY", "named_only")


def get_workspace():
    """The automation repo, cloned if it is not there yet.

    Both other agents already clone — healing in Python, authoring in its
    run.sh — so refusing to was this agent being the odd one out, not being
    careful. What it does *not* do is authoring's follow-up `checkout -f` +
    `pull`: that hard-resets the checkout, which would destroy the very
    uncommitted work the cleanliness gate below exists to protect.
    """
    return workspace_helper.ensure(
        WORKSPACE_DIR, GITHUB_REPO_AUTOMATION,
        org=os.environ.get("GITHUB_ORG", ""),
        token=os.environ.get("GITHUB_TOKEN", ""),
        branch=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
        exclude=REPO_ROOT, log=log)


def working_tree_is_clean(workspace: Path) -> tuple:
    """Refuse to run on a dirty checkout.

    Healing edits one line and gets away with it. This agent touches up to six
    files across several change items and then commits them, so somebody else's
    uncommitted work would be swept into the PR — or the branch checkout would
    fail in a way that is hard to read afterwards.
    """
    try:
        result = subprocess.run(["git", "status", "--porcelain"], cwd=str(workspace),
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not read git status: {exc}"
    if result.returncode != 0:
        return False, f"git status failed: {result.stderr.strip()[:200]}"
    # A login session the agent minted moments ago is runtime state, not
    # somebody's uncommitted work — and it lives inside the repo, so counting it
    # made minting a session self-defeating: get a session, become unable to run.
    dirty = [line for line in result.stdout.splitlines()
             if line.strip() and "loginStorage" not in line]
    if dirty:
        return False, (f"{len(dirty)} uncommitted change(s) in the automation repo "
                       f"— commit or stash them first: {dirty[0].strip()[:60]}")
    return True, ""


def write_skip(reason: str, infra: bool = False):
    (AUDIT_DIR / "02-scope.json").write_text(json.dumps(
        {"skipped": True, "reason": reason, "tiers": {"named": [], "shared_surface": [],
         "distant": []}, "budget": {"tests_to_verify": 0}}, indent=2))
    (AUDIT_DIR / "02-scope.md").write_text(f"# Scope\n\nSkipped — {reason}.\n")
    (AUDIT_DIR / ".fix-passed").write_text("skipped")
    (AUDIT_DIR / ".skip-reason").write_text("infra" if infra else "no-work")
    log(f"Skipped: {reason}")


def main():
    plan_path = AUDIT_DIR / "01-parse-change.json"
    if not plan_path.exists():
        log("ERROR: 01-parse-change.json missing")
        sys.exit(1)
    plan = json.loads(plan_path.read_text())

    workspace = get_workspace()
    if workspace is None:
        write_skip("automation repo workspace not found", infra=True)
        return
    log(f"Workspace: {workspace}")

    # Establish the base BEFORE the cleanliness gate. prepare_base only fetches
    # — it never moves HEAD or the tree — so it is safe on the far side of a
    # gate that exists to protect uncommitted work, and doing it here means a
    # branch that does not exist fails at step 2 of 5 rather than after the
    # expensive exploration in step 3.
    base_branch = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    prepared = workspace_helper.prepare_base(
        workspace, os.environ.get("GITHUB_ORG", ""), GITHUB_REPO_AUTOMATION,
        os.environ.get("GITHUB_TOKEN", ""), base_branch, log=log)
    if not prepared["ok"]:
        write_skip(f"could not prepare base branch — {prepared['reason']}", infra=True)
        return
    behind_out = run_git(["rev-list", "--count", f"HEAD..{prepared['sha']}"], workspace)
    behind = int(behind_out[1]) if behind_out[0] and behind_out[1].isdigit() else 0
    if behind:
        log(f"  note: this checkout is {behind} commit(s) behind {base_branch}")

    clean, why = working_tree_is_clean(workspace)
    if not clean:
        write_skip(f"automation repo is not clean — {why}", infra=True)
        return

    # Now that the gate has proved there is nothing to lose, move onto the base.
    # sync() refuses to reset precisely because it runs before this check; past
    # it, resetting honours the same intent instead of violating it — and
    # without this an "adapt against release/2.3" run would edit whatever HEAD
    # happened to be and then open a PR against release/2.3.
    moved = workspace_helper.checkout_base(
        workspace, prepared["branch"], prepared["sha"], log=log)
    if not moved["ok"]:
        write_skip(f"could not check out the base branch — {moved['reason']}", infra=True)
        return

    nouns = sorted({n for item in plan["items"] for n in (item.get("nouns") or [])})
    result = blast_radius.resolve(
        str(workspace), affects=plan.get("affects"),
        named_tests=plan.get("named_tests"), module=plan.get("module", ""),
        change_nouns=nouns)

    named = result["tiers"]["named"]
    shared = result["tiers"]["shared_surface"]
    log(f"Blast radius: {len(named)} named, {len(shared)} shared-surface, "
        f"{len(result['tiers']['distant'])} excluded as distant")
    for hub in result["hubs_suppressed"][:5]:
        log(f"  suppressed hub {hub['type']} (would have added "
            f"{hub['would_have_added']} file(s))")

    if not named and not shared:
        write_skip("the change note matched no tests — check Affects: or Module:")
        return

    # Freeze what each test proves, BEFORE anything is edited.
    # named_only  — just what the note named (default: the verify set holds the
    #               server's single global run slot)
    # tiered      — plus the tests that share the changed surface
    # all         — plus the ones excluded as reachable only through framework
    #               hubs, for when you would rather pay than wonder
    if VERIFY_POLICY == "named_only":
        verify_rows = named
    elif VERIFY_POLICY == "all":
        verify_rows = named + shared + result["tiers"]["distant"]
    else:
        verify_rows = named + shared
    log(f"Verify policy: {VERIFY_POLICY} → {len(verify_rows)} test(s) will be re-run")

    log("Measuring what each test proves (frozen before any edit)…")
    index = member_index(str(workspace))
    contracts = {}
    for row in named + shared:
        try:
            contracts[row["test"]] = intent.for_test(workspace, row["test"], index)
        except Exception as exc:
            log(f"  could not derive a contract for {row['test']}: {exc}")
    frozen = intent.freeze(contracts)
    authored = sum(1 for c in frozen.values() if c.get("source") == "authored")
    holes = sum(1 for c in frozen.values() if c.get("_unresolved"))
    log(f"  {len(frozen)} contract(s): {authored} authored, "
        f"{len(frozen) - authored} derived"
        + (f"; {holes} with unresolved calls (conservation is PLAUSIBLE there)"
           if holes else ""))

    budget = result["budget"]
    budget["tests_to_verify"] = len(verify_rows)
    budget["est_seconds"] = len(verify_rows) * blast_radius.SECONDS_PER_TEST
    budget["over_limit"] = len(verify_rows) > blast_radius.MAX_TESTS
    mins = round(budget["est_seconds"] / 60)
    log(f"Budget: {len(verify_rows)} test(s) ≈ {mins} min"
        + ("  ⚠️ over ADAPTATION_BLAST_MAX_TESTS — this change is bigger than one run"
           if budget["over_limit"] else ""))

    # How does the test under adaptation get itself signed in? It says so, and
    # reading it beats the repo-wide conventions exploration used to guess from —
    # the framework contradicts every one of them somewhere. Measured here, while
    # the tree is still untouched, alongside the frozen contracts.
    entry = entry_path.extract(workspace, verify_rows[0]["test"]) if verify_rows \
        else {"mode": "none", "reason": "no tests in scope"}
    log(f"Entry path: {entry_path.describe(entry)}")

    scope = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "workspace": str(workspace),
        # 05_ship cuts its branch from this exact SHA rather than re-resolving
        # origin/<base>, which by then may have moved under the step-04 edits.
        "base_branch": prepared["branch"],
        "base_sha": prepared["sha"],
        "module": plan.get("module", ""),
        "selection": result["selection"],
        "tiers": result["tiers"],
        "verify": [row["test"] for row in verify_rows],
        "verify_policy": VERIFY_POLICY,
        "not_verified": [row["test"] for row in (named + shared)
                         if row not in verify_rows],
        "edit_candidates": result["edit_candidates"],
        "page_object_candidates": result["page_object_candidates"],
        "hubs_suppressed": result["hubs_suppressed"],
        "hub_threshold": result["hub_threshold"],
        "total_classes": result["total_classes"],
        "intent_contracts": frozen,
        # Every call the assertion graph could not follow. A hole in the
        # guarantee has to be visible: without it "no assertion was lost"
        # quietly means "none that I managed to look at".
        "unresolved_receivers": sorted({
            call for contract in frozen.values()
            for call in (contract.get("_unresolved") or [])}),
        "entry_path": entry,
        "budget": budget,
    }
    (AUDIT_DIR / "02-scope.json").write_text(json.dumps(scope, indent=2))

    md = [f"# Scope — {plan.get('module','')}", "",
          blast_radius.describe(result), "",
          f"**Verify policy:** `{VERIFY_POLICY}` — {len(verify_rows)} test(s) re-run"]
    if scope["not_verified"]:
        md += ["", "Not re-run by this policy (list them in the PR so a human can "
               "run them in CI):"]
        md += [f"- `{t}`" for t in scope["not_verified"][:20]]
    md += ["", "## Edit candidates", ""]
    md += [f"- `{c['path']}` ({c['role']})" for c in scope["edit_candidates"]] or ["_none_"]
    md += ["", "## Intent contracts (frozen before any edit)", ""]
    for test, contract in list(frozen.items())[:10]:
        md += [intent.describe(contract), ""]
    (AUDIT_DIR / "02-scope.md").write_text("\n".join(md) + "\n")
    log(f"Wrote {AUDIT_DIR / '02-scope.json'}")


if __name__ == "__main__":
    main()
