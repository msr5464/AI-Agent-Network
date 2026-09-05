#!/usr/bin/env python3
"""
Step 00 — Reproduce  (standalone mode only)

Runs a named test locally, reproduces its failure, and writes the same handoff
file that test-triaging-agent would have produced. Everything downstream — the
clustering, DOM grounding, safety guard, verification and PR — then runs exactly
as it does for a pipeline-fed run, because it cannot tell the difference.

Reads:   TEST_NAME (Class#method | Class.method | pkg.Class.method | Class)
Outputs: audit/<session>/00-handoff.json + 00-reproduce.json + .md
         .fix-passed=skipped when there is nothing to fix

Exits 0 in every non-crash case. "The test passes" and "this is not a locator
failure" are both legitimate outcomes, not errors.
"""

import os, sys, json, re, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → shared.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
def log(msg): _log("reproduce", msg)

import warnings, urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
import logging
logging.basicConfig(level=logging.WARNING)

from shared.test_runner import run_test, split_test_name
from lib import probes
from shared.code_analyzer import CodeAnalyzer
from shared.dom_snapshot import find_snapshot, parse_header
from shared.playwright_trace import read_actions, failing_action
from shared import diagnosis
from shared import workspace as workspace_helper
from shared.git import run_git
from shared import failure_context as _failure_context
from shared import narration, run_artifacts

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
TEST_NAME = os.environ.get("TEST_NAME", "").strip()

GITHUB_REPO_AUTOMATION = os.environ.get("GITHUB_REPO_AUTOMATION", "")
GITHUB_ORG             = os.environ.get("GITHUB_ORG", "")
GITHUB_TOKEN           = os.environ.get("GITHUB_TOKEN", "")
GITHUB_DEFAULT_BRANCH  = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
# Mirrors 01_fix.py: false means "let me look at this first", and nothing may
# move the user's checkout under them.
AUTO_PUSH              = os.environ.get("AUTO_PUSH", "true").lower() != "false"
WORKSPACE_DIR          = os.environ.get("WORKSPACE_DIR", str(REPO_ROOT.parent))
TEST_RESULTS_DIR_NAME  = os.environ.get("TEST_RESULTS_DIR_NAME", "test-output")

REPAIR = os.environ.get("REPAIR", "false").lower() == "true"
FORCE  = os.environ.get("FORCE", "false").lower() == "true"
REPRODUCE_TIMEOUT_S = int(os.environ.get("AUTOFIX_REPRODUCE_TIMEOUT_S", "900"))

# shadow: run the diagnosis and log it, but let the old behaviour decide.
# enforce: a stop verdict actually stops the run before any model call.
# Shadow is the default because this gate can refuse work the agent used to
# do successfully, and that risk deserves a measurement rather than a leap.
DIAGNOSIS_MODE = os.environ.get("DIAGNOSIS_MODE", "shadow").strip().lower()
# A probe costs one test run. Off by default in the reproduce step only when
# explicitly disabled, since the run has already paid for a workspace and a
# build here and the marginal cost is the run itself.
PROBE_ENABLED = os.environ.get("DIAGNOSIS_PROBE", "true").strip().lower() != "false"

# ── Failure-shape detection ───────────────────────────────────────────────────
#
# Deciding this before calling Claude is what stops the agent confidently
# "fixing" a locator when the real problem is a wrong expected value or a dead
# database. Signals are taken from what these frameworks actually emit.

_LOCATOR_SIGNALS = [
    # The Playwright framework's own wrappers. BasePage.assertPageLoaded raises a
    # java.lang.AssertionError, so this must be matched BEFORE the assertion
    # signals below or a genuine broken locator is dismissed as a bad expectation.
    "element not visible after timeout",
    "failed to load element",
    # The Selenium/Thanos wrapper phrasing
    "is not visible", "is not clickable", "is not displayed", "is not present",
    # Playwright
    "waiting for locator", "waiting for selector", "strict mode violation",
    "locator.click", "locator.fill", "locator resolved to",
    # Selenium
    "nosuchelementexception", "elementnotinteractableexception",
    "staleelementreferenceexception", "elementclickinterceptedexception",
    "unable to locate element",
]

# Checked BEFORE the locator signals: a Playwright timeout looks locator-shaped
# but an API/DB timeout is not something a locator edit can fix.
_INFRA_SIGNALS = {
    "INFRA_BUILD": ["there is no pom in this directory", "requires a project to execute",
                    "could not find artifact", "compilation failure", "cannot find symbol"],
    "INFRA_DB": ["communications link failure", "no suitable driver found",
                 "could not connect to database", "jdbc:mysql://<"],
    "INFRA_USER": ["failed to get free user after", "no free user available", "userquery["],
    "INFRA_CREDENTIALS": ["value: expected string, got undefined"],
    "INFRA_API_AUTH": ["401 unauthorized", "403 forbidden", "statuscode=401", "statuscode=403"],
}

_ASSERTION_SIGNALS = [
    "assertionerror", "assertion failed", "expected [", "expected:", "but found",
    "but was:", "did not equal", "assertequals", "asserttrue", "assertfalse",
]


def classify_failure_shape(text: str, trace_selector: str = "",
                           trace_selector_inferred: bool = False) -> tuple:
    """Return (shape, reason). shape is LOCATOR / ASSERTION / INFRA_* / UNKNOWN."""
    blob = (text or "").lower()

    for shape, signals in _INFRA_SIGNALS.items():
        for signal in signals:
            if signal in blob:
                return shape, f"matched infrastructure signal: {signal!r}"

    # A trace whose failing action genuinely errored with a selector is strong
    # evidence of a locator problem. One *inferred* by `_polled_to_death` is not:
    # that fires whenever a wait loop ran out of patience, which is equally what
    # a page that never loaded looks like. Inferred selectors fall through to the
    # signal lists below rather than short-circuiting them.
    if trace_selector and not trace_selector_inferred:
        return "LOCATOR", f"the failing action in the trace used selector {trace_selector!r}"

    for signal in _LOCATOR_SIGNALS:
        if signal in blob:
            return "LOCATOR", f"matched locator signal: {signal!r}"

    for signal in _ASSERTION_SIGNALS:
        if signal in blob:
            return "ASSERTION", f"matched assertion signal: {signal!r}"

    return "UNKNOWN", "no recognisable locator, assertion or infrastructure signal"

# ── Workspace + report parsing ────────────────────────────────────────────────

def get_workspace() -> Path | None:
    """The automation checkout: FRAMEWORK_DIR, else WORKSPACE_DIR/repo, else by shape."""
    return workspace_helper.find(WORKSPACE_DIR, GITHUB_REPO_AUTOMATION,
                                 exclude=REPO_ROOT)


def align_workspace_to_base(workspace: Path) -> None:
    """Reproduce against the branch the fix will be based on, not whatever HEAD is.

    These used to be different branches and nobody could see it: this step ran
    the test against the checkout as found, while 01_fix cut its branch from
    origin/<base>. With one shared default that was invisible; once a run may
    name its own base it means diagnosing failures in code the PR will not
    contain.

    Two things are deliberately not done. On AUTO_PUSH=false nothing moves —
    that is the documented "let me look at this first" contract, and dragging
    someone's checkout onto another branch would break it — but the divergence
    is logged rather than left silent. And a dirty tree is never force-checked-
    out; uncommitted work outranks the alignment.
    """
    _, current, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
    current = current.strip()
    if not AUTO_PUSH:
        if current and current != GITHUB_DEFAULT_BRANCH:
            log(f"AUTO_PUSH=false — reproducing on {current}; a PR would be "
                f"based on {GITHUB_DEFAULT_BRANCH}")
        return

    ok, status, _ = run_git(["status", "--porcelain"], workspace)
    # loginStorage is a session the agent minted moments ago — runtime state
    # that lives inside the repo, not somebody's work. Same exclusion the
    # adaptation agent's cleanliness gate uses.
    dirty = [ln for ln in status.splitlines() if ln.strip() and "loginStorage" not in ln]
    if not ok or dirty:
        log(f"Checkout has uncommitted changes — reproducing on {current or 'HEAD'} "
            f"as it stands rather than moving to {GITHUB_DEFAULT_BRANCH}")
        return

    prepared = workspace_helper.prepare_base(
        workspace, GITHUB_ORG, GITHUB_REPO_AUTOMATION, GITHUB_TOKEN,
        GITHUB_DEFAULT_BRANCH, log=log)
    if not prepared["ok"]:
        log(f"Could not prepare {GITHUB_DEFAULT_BRANCH} ({prepared['reason']}) — "
            f"reproducing on {current or 'HEAD'}")
        return
    moved = workspace_helper.checkout_base(
        workspace, prepared["branch"], prepared["sha"], log=log)
    if not moved["ok"]:
        log(f"{moved['reason']} — reproducing on {current or 'HEAD'}")


def read_report_entries(results_dir: Path, class_simple: str, method: str) -> list:
    """Failing entries from the framework's own report.json.

    JsonTestReporter writes this specifically so an agent does not have to parse
    HTML reports. Its screenshotPath is always empty (the framework never
    cross-fills it), so artefacts are located by convention instead.
    """
    report_path = results_dir / "report.json"
    if not report_path.exists():
        log(f"  No report.json at {report_path} — falling back to raw output")
        return []
    try:
        entries = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f"  Could not read report.json ({e}) — falling back to raw output")
        return []
    if not isinstance(entries, list):
        return []

    matched = [
        e for e in entries
        if isinstance(e, dict)
        and (e.get("className", "").split(".")[-1] == class_simple
             or e.get("className") == class_simple)
        and (not method or e.get("testName") == method)
    ]
    return [e for e in matched if (e.get("status") or "").upper() not in ("PASSED", "PASS")]


def _from_this_run(paths: list, not_before: float | None) -> list:
    """Drop artifacts written before the run started."""
    return run_artifacts.from_this_run(paths, not_before)


def _find_class_file(class_simple: str, workspace: Path) -> str:
    """The source file declaring this test class, for a run with no method name."""
    for ext in ("java", "kt"):
        for found in workspace.rglob(f"{class_simple}.{ext}"):
            return str(found.relative_to(workspace))
    return ""


def attach_artifacts(issue: dict, results_dir: Path, method_name: str,
                     not_before: float | None = None) -> str:
    """Point the issue at the DOM snapshot and trace this run just produced.

    The body lives in `shared/run_artifacts.py`: the fix step needs the same
    thing after a verification run, to tell a repaired element apart from the
    next one that fails.
    """
    return run_artifacts.attach(issue, results_dir, method_name,
                                not_before=not_before, log=log)


def wait_budget_seconds(workspace: Path) -> int | None:
    """The framework's configured element timeout, so a wait that ran the clock
    out can be told apart from one that gave up early."""
    for name in ("parameters/config.properties", "config.properties"):
        candidate = workspace / name
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "ObjectWaitTime":
                    return int(value.strip())
        except (OSError, ValueError):
            return None
    return None



def relabel(issue: dict, verdict: dict, mode: str) -> None:
    """Let an enforced verdict rename the failure. Shadow mode changes nothing.

    `root_cause_category` is not a label — the fix step keys off it, and
    `extract_element_names` used to return nothing for any value outside
    ELEMENT_NOT_FOUND/TIMEOUT. So a shadow-mode verdict of WRONG_PAGE, which is
    supposed to be inert, still reached the model as the classification *and*
    switched off the page-object lookup that would have contradicted it. A
    verdict that is not allowed to stop the run is not allowed to rewrite the
    failure either.
    """
    if mode != "enforce":
        return
    if verdict.get("verdict") == "LOCATOR_STALE":
        issue["root_cause_category"] = "ELEMENT_NOT_FOUND"
    elif verdict.get("verdict") and verdict["verdict"] != diagnosis.ABSTAIN:
        issue["root_cause_category"] = verdict["verdict"]
        issue["recommended_action"] = (verdict.get("action")
                                       or verdict.get("remediation", ""))


def gate_decision(shape: str, reason: str, verdict: dict, mode: str,
                  force: bool) -> tuple:
    """Fold a diagnosis into the failure shape. Returns (shape, reason, note).

    Shadow mode is deliberately inert: it reports what it would have done and
    changes nothing, so the verdicts can be measured against real outcomes before
    they are allowed to refuse work the agent used to do successfully.
    """
    if not verdict or verdict.get("verdict") not in diagnosis.STOP:
        return shape, reason, ""
    if force:
        return shape, reason, (f"diagnosis says {verdict['verdict']}, but FORCE=true "
                               f"— attempting a fix anyway")
    if mode != "enforce":
        return shape, reason, (f"shadow mode: would have stopped here — "
                               f"{verdict['verdict']}. Set DIAGNOSIS_MODE=enforce "
                               f"to act on it.")
    return (verdict["verdict"],
            verdict["reasons"][0] if verdict.get("reasons") else reason, "")


# ── Output helpers ────────────────────────────────────────────────────────────

def finish(status: str, headline: str, detail: str = "", issues: list | None = None,
           verdict: dict | None = None) -> None:
    """Write the audit files and exit. Never raises."""
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp": ts, "test_name": TEST_NAME, "status": status,
        "headline": headline, "detail": detail,
        "repair_mode": REPAIR, "forced": FORCE,
        "issues_found": len(issues or []),
    }
    # The remediation is the sentence a person actually needs, so it has to be a
    # field rather than prose inside the headline — the UI reads this, and a run
    # that says only "not a locator problem" has told them nothing they can act on.
    if verdict:
        result["diagnosis"] = {
            "verdict": verdict.get("verdict", ""),
            "confidence": verdict.get("confidence", ""),
            "reasons": verdict.get("reasons") or [],
            "remediation": verdict.get("remediation", ""),
            "actionable": verdict.get("actionable", False),
        }
    (AUDIT_DIR / "00-reproduce.json").write_text(json.dumps(result, indent=2))

    lines = [f"# Reproduce — {TEST_NAME}", "", f"**Status:** `{status}`  ",
             f"**Result:** {headline}", ""]
    if detail:
        lines += ["```", detail[-3000:], "```", ""]
    if issues:
        lines += ["## Failures queued for fixing", ""]
        lines += [f"- `{i['test_name']}` — {i.get('error_type', '')}: "
                  f"{(i.get('error_message') or '')[:120]}" for i in issues]
    (AUDIT_DIR / "00-reproduce.md").write_text("\n".join(lines) + "\n")

    if status != "queued":
        (AUDIT_DIR / ".fix-passed").write_text("skipped")
        if status.startswith("INFRA"):
            reason = "infra"
        elif status in diagnosis.STOP:
            reason = diagnosis.skip_reason(status)
        else:
            reason = "no-work"
        (AUDIT_DIR / ".skip-reason").write_text(reason)
    log(headline)
    sys.exit(0)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TEST_NAME:
        log("ERROR: TEST_NAME not set")
        sys.exit(1)

    full_class, class_simple, method = split_test_name(TEST_NAME)
    log(f"Test: {full_class}" + (f"#{method}" if method else " (whole class)"))

    workspace = get_workspace()
    if not workspace:
        finish("INFRA_WORKSPACE", "Automation repo workspace not found — "
               "set WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")
    log(f"Workspace: {workspace}")
    align_workspace_to_base(workspace)

    # Resolve the fully-qualified name so the handoff carries a package, which is
    # what CodeAnalyzer needs later to find the test file again.
    if "." not in full_class:
        try:
            # `find_test_file` needs a class AND a method that a @Test declares.
            # A whole-class run has no method, and the placeholder "x" that used
            # to be passed matched nothing — so package resolution silently never
            # worked for a class-level run, and warned about a test named ".x".
            found = (CodeAnalyzer().find_test_file(f"{class_simple}.{method}",
                                                   str(workspace)) if method
                     else _find_class_file(class_simple, workspace))
            if found:
                package = CodeAnalyzer()._extract_package(
                    (workspace / found).read_text(encoding="utf-8", errors="ignore"))
                if package:
                    full_class = f"{package}.{class_simple}"
                    log(f"Resolved: {full_class}")
        except Exception as e:
            log(f"  Could not resolve package ({e}) — continuing with the simple name")

    # ── Run it ────────────────────────────────────────────────────────────────
    properties = {"traceMode": "on"}
    if REPAIR:
        # Explicit opt-in only. Normally the fix step decides this for itself and
        # parks a browser on retry, so the common path stays fast and headless.
        properties["repairMode"] = "true"
        log("REPAIR=true — parking the browser on the failing page")

    log("Running the test to reproduce the failure...")
    # Anything older than this belongs to an earlier run, whatever it is named.
    # A second of slack absorbs filesystem timestamp granularity.
    run_started = time.time() - 1
    status, output = run_test(TEST_NAME, workspace, extra_properties=properties,
                              timeout_s=REPRODUCE_TIMEOUT_S, log=log)

    if status == "passed":
        finish("passing", "Test passes — nothing to fix.")
    if status == "unverified":
        finish("INFRA_NO_RUNNER", "No test runner could be found, so the test was "
               "never executed.", output)

    log("Test failed as expected — collecting evidence")

    results_dir = workspace / TEST_RESULTS_DIR_NAME

    # ── Parse the failures ────────────────────────────────────────────────────
    entries = read_report_entries(results_dir, class_simple, method)
    if not entries:
        # No structured report: still fixable from the raw output for the single
        # named test, but not for a whole class (we cannot tell which failed).
        if not method:
            finish("UNKNOWN", "The class failed but report.json is missing, so the "
                   "individual failing tests could not be identified.", output)
        entries = [{"testName": method, "className": full_class,
                    "failureMessage": output[-2000:], "failureLocation": ""}]
        log("  Built a single failure entry from raw output")

    log(f"{len(entries)} failing test(s) in this run")

    budget_s = wait_budget_seconds(workspace)

    issues, shapes, diagnoses = [], [], {}
    for entry in entries:
        entry_method = entry.get("testName") or method
        entry_class = entry.get("className") or full_class
        message = entry.get("failureMessage") or ""
        location = entry.get("failureLocation") or ""

        issue = {
            "test_name": f"{entry_class}.{entry_method}",
            "classification": "AUTOMATION_ISSUE",
            "confidence": "HIGH",
            "root_cause_category": "ELEMENT_NOT_FOUND",
            "root_cause": message[:400],
            "failure_signature": f"{entry.get('status', 'FAILED')}: {message[:120]}",
            "recommended_action": "Update the broken locator",
            "error_type": (message.split(":")[0][:80] if ":" in message else "TestFailure"),
            "error_message": message[:2000],
            "stack_trace": location,
            # The tail alone is the maven stack-trace block, which contains no
            # STEP:/ACTION: lines at all — so every consumer that reads how far
            # the flow got was answering from an empty record.
            "execution_log": narration.for_handoff(output),
            "class_name": entry_class,
            "method_name": entry_method,
            "full_name": f"{entry_class}.{entry_method}",
            "dom_snapshot": "", "failure_url": "", "trace_path": "", "failed_selector": "",
            "screenshot": "",
            "cause_group_key": "", "cause_group_size": 1,
        }
        trace_selector = attach_artifacts(issue, results_dir, entry_method,
                                          not_before=run_started)

        # Ask why the element was missing before assuming the locator is at fault.
        # Everything this reads was already on disk; it costs no model call.
        verdict = {}
        try:
            evidence = diagnosis.collect(issue, workspace=workspace, budget_s=budget_s,
                                         audit_dir=AUDIT_DIR.parent,
                                         not_before=run_started)
            verdict = diagnosis.diagnose(evidence)
            for line in diagnosis.describe(verdict, evidence):
                log(f"  {line}")
            # Measure anything short of HIGH before acting on it. One targeted
            # re-run costs less than the model call plus speculative edit plus
            # verification run plus revert that acting on a wrong verdict does.
            if PROBE_ENABLED and diagnosis.needs_probe(verdict):
                kind = diagnosis.PROBES[verdict["verdict"]]["kind"]
                outcome = probes.run(kind, TEST_NAME, workspace, results_dir,
                                     issue.get("dom_snapshot"), log=log)
                verdict = diagnosis.apply_probe(verdict, outcome)
                log(f"  probe result: {outcome} → {verdict['verdict']} "
                    f"({verdict['confidence']})")

            # anchor_state travels with the verdict: it is measured in the live
            # page and it is the one fact that separates an element that was
            # renamed from one that was hidden from one that was there all along.
            # Dropping it here left the fix step to re-infer it from saved markup,
            # which cannot tell those apart.
            issue["diagnosis"] = {k: verdict[k] for k in
                                  ("verdict", "confidence", "reasons", "remediation",
                                   "action", "actionable", "rule", "anchor_state")}
            issue["diagnosis"]["probe"] = verdict.get("probe", {})
            diagnoses[issue["test_name"]] = verdict
        except Exception as exc:
            log(f"  Diagnosis failed ({exc}) — falling back to signal matching")

        shape, reason = classify_failure_shape(
            f"{message}\n{narration.for_handoff(output)}", trace_selector,
            bool(issue.get("failed_selector_inferred")))

        shape, reason, note = gate_decision(shape, reason, verdict,
                                            DIAGNOSIS_MODE, FORCE)
        if note:
            log(f"  ({note})")

        relabel(issue, verdict, DIAGNOSIS_MODE)

        shapes.append((issue["test_name"], shape, reason))
        if shape == "LOCATOR" or FORCE:
            issues.append(issue)
        # The diagnosis above already said what broke, in more detail and with
        # its evidence. Repeating the shape classifier's one-liner underneath it
        # reads as a second, competing verdict; keep it only when there was no
        # diagnosis to print.
        if verdict.get("verdict") and verdict["verdict"] != diagnosis.ABSTAIN:
            log(f"  {entry_method}: queued as {shape}")
        else:
            log(f"  {entry_method}: {shape} — {reason}")

    # ── Gate on failure shape ─────────────────────────────────────────────────
    if not issues:
        parts = []
        for name, shape, reason in shapes:
            parts.append(f"{name}: {shape} — {reason}")
            verdict = diagnoses.get(name) or {}
            for extra in (verdict.get("reasons") or [])[1:]:
                parts.append(f"    {extra}")
            if verdict.get("remediation"):
                parts.append(f"    REMEDIATION: {verdict['remediation']}")
        summary = "\n".join(parts)
        worst = next((s for _, s, _ in shapes if s.startswith("INFRA")), shapes[0][1])
        if worst in diagnosis.STOP:
            headline = (f"{worst} — this is not something a locator edit can fix. "
                        f"Stopping before any model call. Re-run with FORCE=true "
                        f"to attempt one anyway.")
        else:
            headline = (f"Not a locator failure ({worst}) — stopping before any fix "
                        f"is attempted. Re-run with FORCE=true to try anyway.")
        worst_verdict = next((diagnoses[name] for name, shape, _ in shapes
                              if shape == worst and name in diagnoses), None)
        finish(worst, headline, summary, verdict=worst_verdict)

    if FORCE and any(shape != "LOCATOR" for _, shape, _ in shapes):
        log("FORCE=true — attempting a fix despite the failure not looking locator-shaped")

    # ── Write the handoff ─────────────────────────────────────────────────────
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", f"{class_simple}-{method}" if method else class_simple)
    handoff = {
        "build_tag": f"local-{safe}",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_session": os.environ.get("SESSION_ID", ""),
        "source_audit_dir": str(AUDIT_DIR),
        "origin": "standalone",
        "automation_issues": issues,
    }
    (AUDIT_DIR / "00-handoff.json").write_text(json.dumps(handoff, indent=2))
    log(f"Handoff written: {len(issues)} issue(s) → 00-handoff.json")

    # Record the verdict on the way through as well as on the way out. A soak that
    # only sees the runs that stopped can measure false stops and nothing else —
    # and the costlier mistake is a fix attempted on a page the test never reached.
    proceeding = next((diagnoses[name] for name, _, _ in shapes if name in diagnoses), None)
    finish("queued", f"{len(issues)} failing test(s) queued for fixing", "", issues,
           verdict=proceeding)


if __name__ == "__main__":
    main()
