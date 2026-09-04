#!/usr/bin/env python3
"""
Step 04 — Run & Fix
Two-phase design driven by FIX_ATTEMPT (set by run.sh):

  FIX_ATTEMPT=0  (initial run)
    Runs the generated test as-is. No Claude call. Writes gate=true/false.
    If the test passes, the fix loop in run.sh is skipped entirely.

  FIX_ATTEMPT>=1  (fix attempt N)
    Loads the previous failure output, calls Claude for a fix, applies it,
    THEN runs the test. Each fix attempt is an atomic (fix + verify) unit.
    run.sh counts only these attempts against MAX_FIX_ATTEMPTS.

Reads:  $AUDIT_DIR/03-generate.json
        $AUDIT_DIR/04-run-and-fix.json  (previous attempt's test output)
Writes: $AUDIT_DIR/04-run-and-fix.json
        $AUDIT_DIR/04-run-and-fix.md
        $AUDIT_DIR/04-run-and-fix-attempt-{N}.json  (fix attempts only, for per-step commits)
        $AUDIT_DIR/.fix-passed          gate: true / false / skipped
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

from shared import workspace as workspace_helper

from shared import browser_mode

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR    = Path(os.environ["AUDIT_DIR"])
AGENT_DIR    = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT    = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
AUTOMATION_FRAMEWORK_DIR    = workspace_helper.resolve(
    WORKSPACE_DIR, os.environ.get("GITHUB_REPO_AUTOMATION", ""),
    exclude=REPO_ROOT)

MODEL        = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
ENVIRONMENT  = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging")
COUNTRY      = os.environ.get("AUTOCREATE_COUNTRY", "SG")
FIX_ATTEMPT  = int(os.environ.get("FIX_ATTEMPT", "1"))
MAX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))
MAVEN_TEST_TIMEOUT_S = int(os.environ.get("MAVEN_TEST_TIMEOUT_S", "300"))
# Wall-clock budget for the fix call. This step used to pass no timeout at all and
# silently inherit call_claude_ex's 300s default — too tight for a fix that has to
# emit COMPLETE file contents, so the call was killed mid-response and reported as
# an empty one.
FIX_TIMEOUT_S = int(os.environ.get("FIX_TIMEOUT_S", "900"))
# Diff budget for one fix. edit_guards defaults to 40, which is healing's *locator*
# budget; authoring legitimately repairs compile errors, imports and helper calls,
# so it needs more room while still rejecting a whole-file regeneration.
FIX_MAX_DIFF_LINES = int(os.environ.get("FIX_MAX_DIFF_LINES", "200"))

# Where the Java framework's TestListener/JsonTestReporter write machine-readable
# results — built specifically "for AI agents to read... without parsing HTML
# reports" per JsonTestReporter's own docstring, but nothing here read it before.
# Defaults match Config.resultsDirectory's own default (user.dir/test-output),
# and user.dir for the mvn subprocess below is AUTOMATION_FRAMEWORK_DIR.
TEST_RESULTS_DIR = AUTOMATION_FRAMEWORK_DIR / os.environ.get("TEST_RESULTS_DIR_NAME", "test-output")

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("04-run-and-fix", msg)

from shared.claude import call_claude_ex as _call_claude_ex
def call_claude(prompt: str) -> str:
    """Run the fix call, reporting *why* it produced nothing when it does.

    The legacy call_claude() collapses timeout / non-zero exit / genuinely-empty
    into the same empty string, so a fix killed at the timeout was indistinguishable
    from one the model declined to make — and the step went on to re-run the test
    unfixed, reporting only "did not return a valid fix map".
    """
    # The decoder turns a finished text block into one progress line per line of
    # text, and this step's text block IS the fix map — echoing it would dump whole
    # Java files into the run console. Surface only genuine progress signals.
    _PROGRESS_PREFIXES = ("API retry", "MCP server", "\u2192 ")

    def _on_output(label: str, line: str) -> None:
        if label == "stdout" and line.startswith(_PROGRESS_PREFIXES):
            log(f"  {line[:200]}")

    result = _call_claude_ex(
        prompt=prompt,
        model=MODEL,
        cwd=str(REPO_ROOT),
        timeout=FIX_TIMEOUT_S,
        on_output=_on_output,
        log_dir=str(AUDIT_DIR),   # raw transcript survives for post-mortem
        stream_json=True,
        # Generating a fix is pure text-in/text-out — no MCP server is needed, and
        # inheriting the user's global config just pays connection cost per attempt.
        strict_mcp_config=True,
    )
    if not result.ok:
        log(f"ERROR: Claude fix call {result.describe()}")
        # A timeout still carries whatever arrived before the kill; handing it back
        # lets extract_json() salvage a complete object when the model had already
        # finished and was only idling on the wire.
        return result.stdout if result.status == "timeout" else ""
    return result.stdout

from shared.credential_properties import write_credential_property
from shared.test_catalog import test_methods_in
from shared import properties_file, url_properties
# Evidence readers shared with test-healing-agent. The framework already writes a
# DOM snapshot, a structured failure context and a Playwright trace on every
# failure; before this, step 04 read none of them and asked Claude to fix a test
# from a stack trace alone.
from shared import diagnosis as _diagnosis
from shared import failure_context as _failure_context
from shared.dom_snapshot import (find_snapshot, distill as distill_dom,
                                 format_for_prompt as format_dom)
from shared.playwright_trace import (read_actions, failing_action,
                                     format_for_prompt as format_trace)
# Mechanical guards, shared with test-healing-agent and test-adaptation-agent.
# Deliberately NOT imported: validate_diagnosis_fit (rejects any edit touching a
# page-load assertion unless the verdict is LOCATOR_STALE — which would refuse
# every compile-error fix this step exists to make), matches_negative (needs a
# negatives list authoring has no source for) and steps_justified (adaptation's
# flow contract).
from shared.edit_guards import (apply_edits, compute_diff, log_edits,
                                logstep_present, no_new_swallowing,
                                no_selector_broadening, validate_fix,
                                wrapper_compliance)


def _record_build(cmd, elapsed_s: float, verdict: str) -> None:
    """Maven time for the metrics rollup. This step runs its own Maven rather
    than shared/test_runner.py, so it needs its own record call."""
    try:
        from shared import metrics
        metrics.record_tool("build", " ".join(cmd), elapsed_s, verdict)
    except Exception:
        pass


def extract_json(text: str):
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


_TESTS_RUN = re.compile(r"Tests run:\s*(\d+)", re.I)


def _tests_actually_ran(output: str):
    """Did surefire execute at least one test? True / False / None if unknown.

    None matters: a build that fell over before surefire reported anything at all
    (a compile error) must stay a plain failure, not be re-labelled "nothing ran".
    """
    counts = [int(m.group(1)) for m in _TESTS_RUN.finditer(output or "")]
    if not counts:
        return None
    return max(counts) > 0


def build_passed(returncode: int, output: str) -> bool:
    """Whether the build represents a genuine pass.

    Exit code alone is not enough: surefire reports BUILD SUCCESS when -Dtest
    matches no method, so a run that executed nothing exits 0. Kept separate from
    run_maven_test so the rule is testable without shelling out to maven.
    """
    return returncode == 0 and _tests_actually_ran(output) is not False


def run_maven_test(test_class: str, test_method: str) -> tuple:
    """Run mvn test with real-time line-by-line streaming. Returns (passed, output)."""
    test_arg = f"{test_class}#{test_method}" if test_method else test_class

    cmd = [
        "mvn", "test",
        f"-Dtest={test_arg}",
        f"-Denvironment={ENVIRONMENT}",
        f"-Dcountry={COUNTRY}",
        # Only when PLAYWRIGHT_HEADLESS actually says so. Passing nothing lets
        # the framework's own parameters/config.properties decide, which is the
        # same rule shared/test_runner applies to every other agent's build.
        *(f"-D{key}={value}" for key, value in browser_mode.maven_properties().items()),
        "--no-transfer-progress",
    ]
    # Same build markers the healing agent emits, so the dashboard can fold the
    # build output for either agent with one rule.
    log(f"[build:start] {' '.join(cmd)}")
    _build_started = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into the same stream
            text=True,
            cwd=str(AUTOMATION_FRAMEWORK_DIR),
        )
    except FileNotFoundError:
        log("ERROR: mvn not found in PATH")
        return False, "ERROR: mvn command not found. Is Maven installed and in PATH?"

    all_lines: list = []
    timed_out = False

    def _stream() -> None:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            all_lines.append(line)
            if line.strip():            # skip blank separator lines
                log(f"  {line}")

    reader = threading.Thread(target=_stream, daemon=True)
    reader.start()

    try:
        proc.wait(timeout=MAVEN_TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()

    reader.join(timeout=5)

    if timed_out:
        log(f"[build:end] timed out in {int(time.time() - _build_started)}s")
        _record_build(cmd, time.time() - _build_started, "timed out")
        log(f"ERROR: mvn test timed out ({MAVEN_TEST_TIMEOUT_S}s)")
        return False, "\n".join(all_lines) + f"\nERROR: Maven test timed out after {MAVEN_TEST_TIMEOUT_S} seconds."

    output_text = "\n".join(all_lines)
    passed = build_passed(proc.returncode, output_text)
    if proc.returncode == 0 and not passed:
        # Surefire reports BUILD SUCCESS when -Dtest matches no method: the suite
        # "passed" having executed nothing. Treated as a pass, that ships an
        # APPROVED PR for a test that never ran — the single worst outcome this
        # pipeline can produce, and indistinguishable from a real pass by exit
        # code alone.
        log("ERROR: the build succeeded but ZERO tests ran — the -Dtest filter "
            "matched no method. This is NOT a pass.")
        log(f"  → check that {test_arg} names a real @Test method in the "
            f"generated class.")
    log(f"[build:end] {'passed' if passed else 'failed'} in "
        f"{int(time.time() - _build_started)}s")
    _record_build(cmd, time.time() - _build_started, "passed" if passed else "failed")
    log(f"Test exit code: {proc.returncode} ({'PASS' if passed else 'FAIL'})")
    # Return the full captured output (last 6000 chars keeps tail for Claude context)
    return passed, output_text[-6000:]


def read_generated_files(files_written: list) -> dict:
    """Read the content of all generated Java files."""
    contents = {}
    for rel_path in files_written:
        full = AUTOMATION_FRAMEWORK_DIR / rel_path
        if full.exists():
            try:
                contents[rel_path] = full.read_text()
            except Exception:
                pass
    return contents


def extract_fix_response(fix_map) -> tuple:
    """Unpack into (root_cause, confidence, files_map, edits_map).

    Understands three shapes, most preferred first:
      {"root_cause", "confidence", "edits": [{file, old_string, new_string}]}
      {"root_cause", "confidence", "files": {path: full_content}}
      {path: full_content}                       (bare, no metadata)

    Exactly one of files_map / edits_map is ever non-empty.
    """
    if not isinstance(fix_map, dict):
        return "", "", {}
    root  = str(fix_map.get("root_cause", ""))
    conf  = str(fix_map.get("confidence", ""))

    # Preferred shape — targeted search/replace, the same contract the healing
    # agent uses. Grouped per file so each file is read, patched and guarded once.
    edits_map: dict = {}
    for edit in (fix_map.get("edits") or []):
        if not isinstance(edit, dict):
            continue
        rel = str(edit.get("file", "")).strip()
        if rel:
            edits_map.setdefault(rel, []).append(edit)
    if edits_map:
        return root, conf, {}, edits_map

    if "files" in fix_map and isinstance(fix_map["files"], dict):
        return root, conf, fix_map["files"], {}
    # Neither shape: treat the whole object as a flat {file: content} map. An LLM
    # does not always follow a structure change on the first try, and a fix that
    # still applies correctly should not be discarded over missing metadata.
    return "", "", fix_map, {}


def _run_guards(original: str, updated: str, rel_path: str) -> tuple:
    """Mechanical checks a re-run cannot do for us. Returns (ok, reason).

    The verification loop cannot catch a fix built on a wrong diagnosis, because
    the easiest way to make an assertion pass is to weaken it. These run before
    maven does, so a fix that could only pass by weakening the test never reaches
    a runner at all.
    """
    is_test = Path(rel_path).name.endswith(("Test.java", "Tests.java", "Test.kt"))
    checks = (
        ("size/integrity",  lambda: validate_fix(original, updated,
                                                 Path(rel_path).name, FIX_MAX_DIFF_LINES)),
        ("no_new_swallowing",     lambda: no_new_swallowing(original, updated)),
        ("wrapper_compliance",    lambda: wrapper_compliance(original, updated)),
        ("logstep_present",       lambda: logstep_present(original, updated, is_test)),
        ("no_selector_broadening", lambda: no_selector_broadening(original, updated)),
        # A fix is the other way a literal URL gets into the repo: step 03's
        # guard cannot see what step 04 writes afterwards.
        ("no_hardcoded_url",      lambda: url_properties.no_hardcoded_url(original, updated)),
    )
    for name, run in checks:
        try:
            ok, reason = run()
        except Exception as exc:      # pragma: no cover - a guard must never break a fix
            log(f"  guard {name} errored, ignoring: {exc}")
            continue
        if not ok:
            return False, f"{name}: {reason}"
    return True, ""


def apply_fix(files_map: dict, edits_map: dict = None) -> tuple:
    """Apply a fix to the framework repo, guarded.

    Prefers targeted edits (edits_map) over whole-file replacement (files_map):
    a search/replace that must match exactly once cannot silently drop the rest of
    a file the model never saw.

    Returns (patched_paths, patched_contents, rejections).
    """
    edits_map = edits_map or {}
    patched, rejections = [], []
    patched_contents: dict = {}

    targets = list(edits_map.keys()) + [k for k in files_map if k not in edits_map]
    for rel_path in targets:
        full = AUTOMATION_FRAMEWORK_DIR / rel_path
        # Safety: only ever write inside the framework repo.
        try:
            full.resolve().relative_to(AUTOMATION_FRAMEWORK_DIR.resolve())
        except ValueError:
            log(f"  BLOCKED: {rel_path} escapes repo root")
            rejections.append({"file": rel_path, "reason": "path escapes repo root"})
            continue

        original = full.read_text() if full.exists() else ""

        if rel_path in edits_map:
            if not original:
                log(f"  Cannot patch {rel_path}: file does not exist")
                rejections.append({"file": rel_path, "reason": "file does not exist"})
                continue
            updated, edit_err = apply_edits(original, edits_map[rel_path])
            if not updated:
                # apply_edits refuses an old_string that is missing or ambiguous —
                # guessing which occurrence was meant is how an autofix corrupts a file.
                log(f"  Cannot apply edits to {rel_path}: {edit_err}")
                rejections.append({"file": rel_path, "reason": edit_err})
                continue
        else:
            updated = files_map.get(rel_path) or ""
            if not updated.strip():
                continue

        if original:
            ok, reason = _run_guards(original, updated, rel_path)
            if not ok:
                log(f"  REJECTED {rel_path} — {reason}")
                rejections.append({"file": rel_path, "reason": reason,
                                   "diff": compute_diff(original, updated,
                                                        Path(rel_path).name)})
                continue

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(updated)
        patched.append(rel_path)
        patched_contents[rel_path] = updated
        log(f"  Fixed: {rel_path}")
        if rel_path in edits_map:
            # The prose root_cause says WHY; without this nobody can see WHAT.
            log_edits(full, original, edits_map[rel_path], log)
    return patched, patched_contents, rejections


def _extract_failure_summary(output: str) -> list:
    """Extract the most actionable failure lines from Maven/TestNG output.

    Returns the list rather than only logging it — this used to be a log-only
    side effect, so the curated signal it computes (vs. the raw last-N-chars
    tail truncation) never reached the Claude fix prompt, only a human reading
    the console.
    """
    summary = []
    for line in output.splitlines():
        s = line.strip()
        # TestNG failure header: [ERROR]   ClassName.method:N » ErrorType
        if s.startswith("[ERROR]") and ("»" in s or ("Failures" in s and s != "[ERROR] Failures:")):
            summary.append(s)
        # Timeout exceeded line
        elif "exceeded" in s and ("Timeout" in s or "timeout" in s):
            summary.append(s)
        # "waiting for locator(...)" — what the test got stuck on
        elif "waiting for locator" in s:
            summary.append(f"  stuck waiting for: {s.split('waiting for locator')[-1].strip()}")
        # Assertion mismatch
        elif ("Expected" in s and ("but got" in s or "was" in s)) or "AssertionError" in s:
            summary.append(s)
        # Java compile errors
        elif "cannot find symbol" in s or ("error:" in s and ".java" in s):
            summary.append(s)
        # Maven timeout (from our subprocess)
        elif "Maven test timed out" in s:
            summary.append(s)

    if summary:
        return summary[:10]
    # Fallback: last few [ERROR] lines
    return [l.strip() for l in output.splitlines() if "[ERROR]" in l][-5:]


def _log_failure_summary(output: str) -> list:
    """Log the curated failure summary and return it (see _extract_failure_summary)."""
    summary = _extract_failure_summary(output)
    if summary:
        log("Failure summary:")
        for ln in summary:
            log(f"  {ln}")
    return summary


def read_json_test_report(test_class: str, test_method: str) -> dict:
    """Read the structured failure entry from the Java framework's own
    test-output/report.json, written by JsonTestReporter specifically "for AI
    agents to read... without parsing HTML reports" (its own docstring) — a
    precise failureLocation (file:line) instead of hunting through a raw
    stack trace, that nothing here consulted before this change.

    Returns {} if the report doesn't exist, isn't valid JSON, or has no entry
    for this class/method (e.g. a compile error prevented the suite from
    running at all, so TestNG never invoked the listener).
    """
    report_path = TEST_RESULTS_DIR / "report.json"
    if not report_path.exists():
        return {}
    try:
        entries = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(entries, list):
        return {}

    # JsonTestReporter writes the FULLY QUALIFIED class name
    # ("automation.naukari.NaukriProfileSummaryWebTest") while step 03 hands us the
    # simple one ("NaukriProfileSummaryWebTest"), so an equality test never matched
    # and this returned {} on every run — silently costing the fix prompt the
    # failureMessage, and the diagnosis engine the page object it reasons from.
    def _same_class(recorded: str) -> bool:
        return bool(recorded) and (recorded == test_class
                                   or recorded.endswith("." + test_class)
                                   or test_class.endswith("." + recorded))

    candidates = [e for e in entries
                  if isinstance(e, dict) and _same_class(e.get("className", ""))]
    if test_method:
        method_matches = [e for e in candidates if e.get("testName") == test_method]
        if method_matches:
            candidates = method_matches
    if not candidates:
        return {}
    # Prefer the most recent non-passed entry (retries can produce multiple
    # entries for the same test); fall back to the last entry of any status.
    failed = [e for e in candidates if e.get("status") != "PASSED"]
    return (failed or candidates)[-1]


def find_latest_screenshot(test_method: str, newer_than: float = 0.0) -> str:
    """Best-effort screenshot lookup via glob, NOT via report.json's own
    screenshotPath field — that field is always written as an empty string
    by JsonTestReporter (verified against the framework source: the comment
    even says "populated by TestListener" but nothing actually cross-fills
    it). TestListener.onTestFailure DOES call BrowserHelper.takeScreenshot,
    which writes to test-output/screenshots/{testcaseName}_{HHmmss}.png —
    config.testcaseName is set to the TestNG method name, matching test_method.

    newer_than (epoch seconds) filters to screenshots from THIS run, so a
    stale screenshot left over from a previous run of the same test method
    isn't mistaken for current evidence.
    """
    if not test_method:
        return ""
    screenshots_dir = TEST_RESULTS_DIR / "screenshots"
    if not screenshots_dir.exists():
        return ""
    matches = sorted(
        (p for p in screenshots_dir.glob(f"{test_method}_*.png") if p.stat().st_mtime >= newer_than),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return ""
    try:
        return str(matches[0].resolve().relative_to(AUTOMATION_FRAMEWORK_DIR.resolve()))
    except ValueError:
        return str(matches[0])


def build_failure_context(test_class: str, test_method: str, test_output: str,
                          run_started_at: float) -> dict:
    """Consolidate every failure signal available after a test run: the
    structured JsonTestReporter entry, a screenshot (best-effort glob), and
    the curated Maven/TestNG summary — one place both the human log and the
    Claude fix prompt draw from, instead of two independently-computed views
    of "what failed" that only the human-facing one was ever complete.
    """
    report_entry = read_json_test_report(test_class, test_method)
    screenshot = find_latest_screenshot(test_method, newer_than=run_started_at)
    summary_lines = _log_failure_summary(test_output)

    if report_entry:
        log(f"Structured report: {report_entry.get('failureLocation') or '(no location)'} "
            f"— {(report_entry.get('failureMessage') or '')[:200]}")
    if screenshot:
        log(f"Screenshot: {screenshot}")

    return {
        "failure_location":  report_entry.get("failureLocation", ""),
        "failure_message":   report_entry.get("failureMessage", ""),
        "retry_count":       report_entry.get("retryCount"),
        "screenshot_path":   screenshot,
        "summary_lines":     summary_lines,
        # Lets the next attempt bound its evidence lookup to THIS run's artefacts.
        "run_started_at":    run_started_at,
    }


_URL_IN_TEXT = re.compile(r"https?://[^\s)]+")


def _registrable(host: str) -> str:
    """Last two labels of a host — good enough to tell first- from third-party.

    Not Public-Suffix-List accurate (it treats example.co.uk as co.uk), which only
    ever makes the check more permissive: the failure mode is keeping one extra
    line of evidence, never dropping a real one.
    """
    labels = (host or "").strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else (host or "")


def _first_party_errors(errors: list, page_host: str, max_len: int = 140) -> list:
    """Keep only request failures on the page's own host, condensed.

    Third-party tracker noise (doubleclick, googleads, analytics beacons) aborts
    routinely on a normal page load and is never the cause of a test failure.
    """
    if not page_host:
        return []
    kept = []
    for err in errors:
        match = _URL_IN_TEXT.search(str(err))
        if not match:
            continue
        host = _extract_host(match.group(0))
        # Compare the registrable domain, not the exact host: an API on
        # api.example.com failing for a page on www.example.com is exactly the
        # evidence worth keeping, and an exact/subdomain match would discard it.
        # The cost is that a same-company analytics beacon survives too, which the
        # cap below bounds to a few condensed lines.
        if _registrable(host) and _registrable(host) == _registrable(page_host):
            condensed = str(err)
            if len(condensed) > max_len:
                condensed = condensed[:max_len] + " …"
            kept.append(condensed)
    return kept


def advisory_diagnosis(test_class: str, test_method: str, dom_snapshot_path: str,
                       failed_selector: str, failure_message: str,
                       failure_location: str) -> str:
    """Run the shared rule engine and render its verdict as ADVICE, never a gate.

    test-healing-agent lets this verdict decide what it may edit, because it only
    ever repairs a locator in a test that used to pass. Authoring is a different
    job: the code is newly generated and may not even compile, so a verdict of
    "this is not a stale locator" must never stop the fix. The rules are still
    worth running — they read the same live-page evidence and are good at spotting
    that a flow never arrived at the page it was asserting against — so the output
    goes into the prompt as context and nothing more.
    """
    try:
        issue = {
            "test_name":      f"{test_class}.{test_method}" if test_class else test_method,
            "dom_snapshot":   dom_snapshot_path,
            "failed_selector": failed_selector,
            "error_message":  failure_message,
            "stack_trace":    failure_location,
        }
        evidence = _diagnosis.collect(issue, workspace=AUTOMATION_FRAMEWORK_DIR)
        verdict = _diagnosis.diagnose(evidence)
    except Exception as e:                        # pragma: no cover - defensive
        log(f"Evidence: diagnosis unavailable ({e})")
        return ""

    name = verdict.get("verdict", "")
    if not name:
        return ""
    log(f"Evidence: diagnosis {name} ({verdict.get('confidence', '')})")
    lines = [f"Verdict: {name} (confidence {verdict.get('confidence', 'UNKNOWN')})"]
    lines += [f"  - {r}" for r in (verdict.get("reasons") or [])]
    if verdict.get("remediation"):
        lines.append(f"  Suggested: {verdict['remediation']}")
    return (
        "\n## DIAGNOSIS (ADVISORY — from the shared rule engine)\n"
        "This is a hint from evidence, not an instruction. It is tuned for repairing\n"
        "locators in tests that used to pass; this test is newly generated, so a\n"
        "compile error, a wrong helper call or a bad assertion is equally likely.\n"
        "Use it if it fits the evidence above, and ignore it if it does not.\n"
        + "\n".join(lines) + "\n"
    )


def gather_runtime_evidence(test_method: str, newer_than: float = 0.0) -> dict:
    """Read the DOM, failure context and trace the framework wrote at failure.

    `newer_than` bounds the lookup to artefacts this run actually produced. A run
    where the test never executed writes none, and without the bound the newest
    matching file from a PREVIOUS session is picked up instead — showing the fixer
    a DOM and a failing selector from an entirely different failure, which is worse
    than showing it nothing.

    Every lookup is independently best-effort: a missing or unreadable artefact
    must degrade the fix prompt, never break the fix path.
    """
    out = {"dom_section": "", "trace_section": "", "context_section": "",
           "dom_snapshot_path": "", "trace_path": ""}
    if not test_method:
        return out

    def _fresh(path) -> bool:
        try:
            return not newer_than or Path(path).stat().st_mtime >= newer_than
        except OSError:
            return False

    # ── DOM at the moment of failure ──────────────────────────────────────────
    try:
        snap = find_snapshot(TEST_RESULTS_DIR, test_method)
        if snap and not _fresh(snap):
            log(f"Evidence: ignoring stale DOM snapshot {Path(snap).name} — it "
                f"predates this run, so it describes a different failure")
            snap = None
        if snap:
            out["dom_snapshot_path"] = str(snap)
            distilled = distill_dom(snap.read_text(errors="ignore"))
            body = format_dom(distilled)
            if body.strip():
                out["dom_section"] = (
                    "\n## DOM AT FAILURE (captured in the real browser, at the "
                    "failing step)\n"
                    "This is the page the test was actually on. A locator that "
                    "matches nothing here is wrong, and one that matches several "
                    "elements is what raises Playwright's strict-mode violation.\n"
                    f"{body}\n"
                )
            log(f"Evidence: DOM snapshot {snap.name}")
    except Exception as e:                       # pragma: no cover - defensive
        log(f"Evidence: DOM snapshot unavailable ({e})")

    # ── Structured failure context written next to the snapshot ───────────────
    try:
        ctx_path = (_failure_context.beside_snapshot(out["dom_snapshot_path"])
                    if out["dom_snapshot_path"]
                    else _failure_context.find(TEST_RESULTS_DIR, test_method))
        # The fallback lookup is not time-bounded, so it will happily return the
        # context file next to a snapshot that was just rejected as stale.
        if ctx_path and not _fresh(ctx_path):
            ctx_path = None
        if ctx_path:
            ctx = _failure_context.load(ctx_path)
            # describe() covers readyState / DOM volatility / anchor counts / JS
            # errors. The fields it leaves out — which page we were on and how much
            # of the page object matched — are the ones that say whether the flow
            # even arrived, so compose them here rather than change a formatter
            # test-healing-agent shares.
            lines = []
            if ctx.get("url"):
                lines.append(f"Page at failure: {ctx['url']}")
            if ctx.get("title"):
                lines.append(f"Page title: {ctx['title']}")
            cov = _failure_context.self_coverage(ctx)
            if cov:
                lines.append(
                    f"{cov['name']}: {cov['matched']} of {cov['evaluable']} locators "
                    f"matched in the live page")
                for name, hits in (cov.get("details") or {}).items():
                    lines.append(f"    {name}: {hits} match(es)")
                if cov["evaluable"] and not cov["matched"]:
                    lines.append(
                        "    → NOT ONE locator matched. The flow almost certainly "
                        "never reached this page, so the bug is in an EARLIER step "
                        "(login, navigation) rather than in these selectors.")
            described = _failure_context.describe(ctx)
            if described.strip():
                lines.append(described)
            # Only first-party failures. A page like this logs a dozen aborted
            # requests to ad and analytics hosts on every load; they are never why
            # a test failed, and unfiltered they cost several KB of prompt to say
            # nothing. A failed call to the app's OWN host is worth every character.
            page_host = _extract_host(ctx.get("url", ""))
            for err in _first_party_errors(ctx.get("http_errors") or [], page_host)[:3]:
                lines.append(f"HTTP error (first-party): {err}")
            if lines:
                out["context_section"] = (
                    "\n## FAILURE CONTEXT (recorded by the framework, in the live page)\n"
                    + "\n".join(lines) + "\n"
                )
            log(f"Evidence: failure context {Path(ctx_path).name}")
    except Exception as e:                       # pragma: no cover - defensive
        log(f"Evidence: failure context unavailable ({e})")

    # ── What the test actually did, selector by selector ──────────────────────
    try:
        traces = [t for t in sorted((TEST_RESULTS_DIR / "traces").glob(f"{test_method}_*.zip"),
                                    key=lambda f: f.stat().st_mtime, reverse=True)
                  if _fresh(t)]
        if traces:
            out["trace_path"] = str(traces[0])
            actions = read_actions(traces[0])
            body = format_trace(actions)
            if body.strip():
                out["trace_section"] = (
                    "\n## WHAT THE TEST ACTUALLY DID (Playwright trace)\n"
                    f"{body}\n"
                )
            failed = failing_action(actions)
            if failed:
                log(f"Evidence: failing action {failed.get('action')} "
                    f"{failed.get('selector')!r}")
    except Exception as e:                       # pragma: no cover - defensive
        log(f"Evidence: trace unavailable ({e})")

    return out


# ── Infrastructure helpers ────────────────────────────────────────────────────

def _extract_host(url: str) -> str:
    """Return 'host' or 'host:port' from a URL, or '' if unparseable/empty."""
    if not url:
        return ""
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""


def classify_failure(output: str, api_base_url: str = "") -> str:
    """Returns 'INFRA_BUILD' | 'INFRA_DB' | 'INFRA_USER' | 'INFRA_CREDENTIALS' |
    'INFRA_API_CONNECTION' | 'INFRA_API_AUTH' | 'CODE_ERROR'."""
    infra_build_signals = [
        "There is no POM in this directory",
        "requires a project to execute",
        "Could not find artifact",
    ]
    infra_db_signals = [
        "Communications link failure",
        "Connection refused",
        "No suitable driver found",
        "Unable to connect",
        "db.thanos.url",
        "Could not connect to database",
        "<host>",
        "<dbname>",
        "jdbc:mysql://<",
    ]
    infra_user_signals = [
        "Failed to get free user after",
        "getUserWithRetry",
        "UserQuery[",
        "No free user available",
    ]
    # A missing {feature}.username/.password property (see try_fix_infra_credentials)
    # surfaces as a null being handed to a Playwright fill() call — this exact
    # Playwright-Java error text was confirmed against a real failure caused by
    # exactly that gap, which two separate Claude fix attempts misdiagnosed as a
    # locator/timing bug because nothing connected the null value back to its source.
    infra_credentials_signals = [
        "value: expected string, got undefined",
    ]
    # Common REST-client phrasings for an auth rejection. Kept multi-word/prefixed
    # (never a bare "401"/"403") so this can't collide with an unrelated line
    # number or byte offset elsewhere in Maven output.
    infra_api_auth_signals = [
        "401 Unauthorized", "HTTP/1.1 401", "statusCode=401", "\"status\":401",
        "status code: 401", "status code 401", "Response status:401",
        "403 Forbidden", "HTTP/1.1 403", "statusCode=403", "\"status\":403",
        "status code: 403", "status code 403", "Response status:403",
    ]

    # "Connection refused" is ambiguous — it fires for BOTH a dead local DB and
    # an unreachable REST API. Disambiguate using api_base_url's own host so an
    # unreachable API is never misrouted into try_fix_infra_db() (which would
    # attempt to auto-configure a local MySQL — never the right fix here).
    # Checked before the generic infra_db_signals loop below.
    api_host = _extract_host(api_base_url)
    if api_host and "Connection refused" in output and api_host in output:
        return "INFRA_API_CONNECTION"

    for signal in infra_api_auth_signals:
        if signal in output:
            return "INFRA_API_AUTH"
    for signal in infra_build_signals:
        if signal in output:
            return "INFRA_BUILD"
    for signal in infra_db_signals:
        if signal in output:
            return "INFRA_DB"
    for signal in infra_user_signals:
        if signal in output:
            return "INFRA_USER"
    for signal in infra_credentials_signals:
        if signal in output:
            return "INFRA_CREDENTIALS"
    return "CODE_ERROR"


def _find_mysql() -> str:
    """Return path to mysql binary, or empty string if not found."""
    for candidate in ["/usr/local/mysql/bin/mysql", "/opt/homebrew/bin/mysql"]:
        if Path(candidate).exists():
            return candidate
    return shutil.which("mysql") or ""


def try_fix_infra_db() -> bool:
    """Auto-configure local MySQL in system.properties if not already set. Returns True if fixed."""
    mysql_bin = _find_mysql()
    if not mysql_bin:
        log("DB auto-repair: mysql binary not found")
        return False

    result = subprocess.run([mysql_bin, "-u", "root", "-e", "SELECT 1;"],
                            capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        log("DB auto-repair: could not connect to local MySQL as root (no password)")
        return False

    sys_props = AUTOMATION_FRAMEWORK_DIR / "parameters" / "system.properties"
    if not sys_props.exists():
        sys_props.parent.mkdir(parents=True, exist_ok=True)
        sys_props.write_text("")

    content = sys_props.read_text()
    if "<host>" not in content and "db.thanos.url" in content:
        log("DB auto-repair: system.properties already has a real DB URL — skipping")
        return False

    # Remove any placeholder lines and append real values
    lines = [ln for ln in content.splitlines()
             if not ln.strip().startswith("db.thanos.") or "<host>" not in ln]
    lines += [
        "",
        "# Auto-configured by test-authoring-agent",
        "db.thanos.url=jdbc:mysql://localhost:3306/thanos",
        "db.thanos.username=root",
        "db.thanos.password=",
    ]
    sys_props.write_text("\n".join(lines) + "\n")
    log("DB auto-repair: wrote local MySQL config to system.properties")
    return True


def _mysql_escape(value: str) -> str:
    """Minimal correct MySQL string escaping for values embedded via the
    `mysql -e` CLI (no parameterized-query API is available at this layer —
    the mysql binary itself doesn't support bound parameters).

    Order matters: backslashes MUST be escaped before quotes. The previous
    .replace("'", "\\'") escaped quotes only — a value ending in a literal
    backslash (e.g. "foo\\") turned that lone \\' into \\\\' , which MySQL
    reads as an escaped backslash followed by an UNESCAPED quote, breaking
    out of the string.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def try_fix_infra_user(plan: dict) -> bool:
    """Insert demo user into the user pool table if missing. Returns True if action taken."""
    creds = plan.get("demo_credentials", {})
    if not creds.get("username") or not creds.get("password"):
        log("User auto-repair: no demo_credentials in plan")
        return False

    mysql_bin = _find_mysql()
    if not mysql_bin:
        log("User auto-repair: mysql binary not found")
        return False

    environment  = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging")
    table        = f"users_{environment}"
    country      = plan.get("country", "SG")
    feature_enum = plan.get("feature_enum", "CARD")
    username     = _mysql_escape(creds["username"])
    password     = _mysql_escape(creds["password"])
    otp          = _mysql_escape(creds.get("otp", ""))

    def run_mysql(sql: str):
        return subprocess.run(
            [mysql_bin, "-u", "root", "thanos", "-e", sql],
            capture_output=True, text=True, timeout=10
        )

    # Check if user already exists
    check = run_mysql(f"SELECT id FROM `{table}` WHERE username='{username}' LIMIT 1;")
    if check.returncode != 0:
        log(f"User auto-repair: could not query {table}: {check.stderr[:200]}")
        return False

    if username in check.stdout:
        run_mysql(f"UPDATE `{table}` SET usageStatus='FREE', testcaseName=NULL "
                  f"WHERE username='{username}';")
        log(f"User auto-repair: reset existing user to FREE: {username}")
        return True

    sql = (
        f"INSERT INTO `{table}` "
        f"(isActive, userType, poolUser, feature, usageStatus, username, password, otp, country) "
        f"VALUES (1, 'Admin', 'YES', '{feature_enum}', 'FREE', "
        f"'{username}', '{password}', '{otp}', '{country}');"
    )
    result = run_mysql(sql)
    if result.returncode == 0:
        log(f"User auto-repair: inserted demo user into {table}: {username}")
        return True
    log(f"User auto-repair: INSERT failed: {result.stderr[:200]}")
    return False


def try_fix_infra_credentials(plan: dict) -> bool:
    """Ensure {feature}.username/.password exist in the environment+country
    properties file. Returns True only if the property was genuinely missing
    and just got written — "already present" means this genuinely isn't why
    the test is failing, so the caller should NOT re-run the test expecting
    it to be fixed and should fall through to the normal CODE_ERROR path.

    03_generate.py already writes this once, right after generating a new web
    module — this is a defensive re-check for cases that path doesn't cover:
    an existing module (03 only writes for brand-new modules), a session run
    before this existed, or a properties file edited/reverted since generation.
    """
    feature = plan.get("feature_name", "")
    if not feature:
        log("Credential auto-repair: no feature_name in plan")
        return False
    status = write_credential_property(
        AUTOMATION_FRAMEWORK_DIR, feature.lower(), plan.get("demo_credentials", {}), log=log
    )
    if status == "no credentials to write":
        log("Credential auto-repair: no demo_credentials in plan")
    return status == "written"


def resolve_test_method(test_class: str, test_method: str, files_written: list) -> str:
    """Confirm the method we are about to run exists; correct it if it does not.

    Step 03 records the method name, but that record can be stale — a resume runs
    step 04 against an 03-generate.json written before the class was regenerated,
    and re-running step 04 alone never revisits it at all. Handing a name that no
    longer exists to `mvn -Dtest=Class#method` runs ZERO tests and reports BUILD
    SUCCESS, so the mismatch is invisible unless it is checked here.

    Reads the class from disk, because disk is what maven will run.
    """
    if not test_class:
        return test_method

    path = next((AUTOMATION_FRAMEWORK_DIR / f for f in (files_written or [])
                 if Path(f).stem == test_class and (AUTOMATION_FRAMEWORK_DIR / f).exists()), None)
    if path is None:
        matches = list((AUTOMATION_FRAMEWORK_DIR / "src" / "test").rglob(f"{test_class}.java"))
        path = matches[0] if matches else None
    if path is None:
        log(f"Test method precheck: {test_class}.java not found on disk — running "
            f"{test_method!r} as recorded")
        return test_method

    try:
        declared = test_methods_in(path.read_text())
    except OSError as e:
        log(f"Test method precheck: cannot read {path.name} ({e}) — running as recorded")
        return test_method

    if not declared:
        log(f"WARNING: {path.name} declares no @Test method at all — nothing can run")
        return test_method
    if test_method in declared:
        return test_method

    corrected = declared[0]
    log(f"Test method precheck: {test_method!r} is not declared in {path.name} "
        f"(it has {declared}) — running {corrected!r} instead")
    return corrected


def load_run_target(gen_data: dict) -> tuple:
    """What step 04 will actually run: (test_class, test_method, files_written).

    Reconciles step 03's record against the code on disk in one place, so the
    reconciliation cannot be skipped by a caller — the reason this is a function
    and not two lines inside main() is that "we forgot to check" is precisely the
    failure it exists to prevent.
    """
    files_written = gen_data.get("files_written", [])
    test_class = gen_data.get("test_class", "")
    test_method = resolve_test_method(test_class, gen_data.get("test_method", ""),
                                      files_written)
    return test_class, test_method, files_written


def ensure_credentials(plan: dict) -> None:
    """Make sure the login properties exist BEFORE the first test run.

    run.sh syncs the framework repo with `git checkout -f <branch>`, which discards
    every uncommitted change — including the credential properties step 03 wrote,
    which ship deliberately never commits because they are secrets. They are
    therefore per-run state that the *next* run wipes.

    try_fix_infra_credentials() below already repairs this, but only after a maven
    cycle has failed AND classify_failure() matched a credential signature. A run
    that resumes at step 04, or whose step 03 came from TESTING_MODE cache, starts
    with no properties at all: getRunTimeProperty returns null, the login form is
    filled with nothing, and the failure looks like a broken locator on whatever
    page the test lands on. Writing them up front costs nothing and removes a whole
    class of misdiagnosis.
    """
    feature = (plan.get("feature_name") or "").lower()
    if not feature:
        log("Credential precheck: no feature_name in plan — skipping")
        return
    creds = plan.get("demo_credentials") or {}
    status = write_credential_property(AUTOMATION_FRAMEWORK_DIR, feature, creds, log=log)
    key = f"{feature}.username"
    if status == "no credentials to write":
        # Not necessarily wrong — an API-only flow or a CSV-backed module has none.
        log(f"Credential precheck: plan carries no demo_credentials; the test must "
            f"not depend on {key}")
    else:
        log(f"Credential precheck: {key} / {feature}.password — {status}")


def ensure_url_properties(plan: dict, gen_data: dict) -> None:
    """Make sure the URL properties the generated code reads exist BEFORE the run.

    Same reason as ensure_credentials(): run.sh syncs the framework repo with
    `git checkout -f`, so a run that resumes at step 04 starts with none of what
    step 03 wrote. A missing URL property is quieter than a missing credential —
    getRunTimeProperty returns null, navigation goes nowhere, and the failure
    looks like a page object whose locators stopped matching.
    """
    urls = gen_data.get("url_properties") or url_properties.collect_urls(plan)
    if not urls:
        return
    feature = (plan.get("feature_name") or "").lower()
    status = url_properties.write_url_properties(
        AUTOMATION_FRAMEWORK_DIR, urls, feature, log=log)
    log(f"URL property precheck: {len(urls)} key(s) — {status}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    gen_data = json.loads((AUDIT_DIR / "03-generate.json").read_text())
    test_class, test_method, files_written = load_run_target(gen_data)
    plan_data     = json.loads((AUDIT_DIR / "01-parse.json").read_text())
    api_val_path  = AUDIT_DIR / "02-validate-api.json"
    api_validation = json.loads(api_val_path.read_text()) if api_val_path.exists() else {}

    if not test_class:
        log("No test class found in generate output — skipping test run")
        _write_gate("skipped")
        _write_result({"skipped": True, "reason": "no test class"}, [], FIX_ATTEMPT)
        return

    if not AUTOMATION_FRAMEWORK_DIR.exists():
        log(f"ERROR: Automation framework repo not found: {AUTOMATION_FRAMEWORK_DIR}")
        _write_gate("skipped")
        _write_result({"skipped": True, "reason": f"Automation framework repo not found at {AUTOMATION_FRAMEWORK_DIR}"}, [], FIX_ATTEMPT)
        return

    # ── FIX_ATTEMPT == 0 — initial run, no fix ────────────────────────────────
    if FIX_ATTEMPT == 0:
        ensure_credentials(plan_data)
        ensure_url_properties(plan_data, gen_data)
        log(f"Initial test run: {test_class}#{test_method}")
        run_started_at = time.time()
        passed, test_output = run_maven_test(test_class, test_method)
        failure_ctx = {}
        if not passed:
            failure_ctx = build_failure_context(test_class, test_method, test_output, run_started_at)
            log("Initial test FAILED — re-running once (no code change) to rule out "
                "flakiness before spending a fix attempt")
            run_started_at = time.time()
            passed_retry, test_output_retry = run_maven_test(test_class, test_method)
            if passed_retry:
                log("Re-run PASSED — treating the first failure as a flake, no fix needed")
                passed = True
                test_output = test_output_retry
                failure_ctx = {}
            else:
                log("Re-run also FAILED — reproducible, fix attempt 1 will apply a Claude fix")
                test_output = test_output_retry
                failure_ctx = build_failure_context(test_class, test_method, test_output, run_started_at)
        else:
            log("Initial test PASSED")
        _write_gate("true" if passed else "false")
        _write_result({
            "attempt": 0,
            "test_class": test_class,
            "test_method": test_method,
            "passed": passed,
            "test_output": test_output,
            "fixes_applied": [],
            **failure_ctx,
        }, files_written, 0)
        return

    # ── FIX_ATTEMPT >= 1 — classify previous failure → fix → run ─────────────
    # Load the failure output (and structured failure context) written by the
    # previous attempt — this is the failure THIS attempt is being asked to fix.
    prev_output = ""
    prev_failure_location = ""
    prev_root_cause = ""
    prev_run_started_at = 0.0
    prev_fix_path = AUDIT_DIR / "04-run-and-fix.json"
    if prev_fix_path.exists():
        try:
            prev = json.loads(prev_fix_path.read_text())
            prev_output = prev.get("test_output", "")[-3000:]
            prev_failure_location = prev.get("failure_location", "")
            prev_failure_message  = prev.get("failure_message", "")
            prev_screenshot       = prev.get("screenshot_path", "")
            prev_summary_lines    = prev.get("summary_lines", [])
            prev_root_cause       = prev.get("root_cause", "")
            prev_run_started_at   = float(prev.get("run_started_at") or 0)
            log(f"Fix attempt {FIX_ATTEMPT}/{MAX_ATTEMPTS} — loaded previous failure ({len(prev_output)} chars)"
                + (f", location={prev_failure_location}" if prev_failure_location else ""))
        except Exception:
            prev_failure_message, prev_screenshot, prev_summary_lines = "", "", []
    else:
        prev_failure_message, prev_screenshot, prev_summary_lines = "", "", []

    # Read the DOM, failure context and trace the previous attempt's run left on
    # disk. These describe the exact failure this attempt is being asked to fix,
    # and step 04 ignored all three until now.
    evidence = gather_runtime_evidence(test_method, newer_than=prev_run_started_at)

    failure_class = classify_failure(prev_output, plan_data.get("api_base_url", ""))
    log(f"Failure classified as: {failure_class}")

    if failure_class == "INFRA_DB":
        log("Detected DB infrastructure error — attempting auto-repair")
        if try_fix_infra_db():
            log("DB auto-configured — running test")
            passed, test_output = run_maven_test(test_class, test_method)
            if passed:
                log("Test PASSED after DB auto-repair")
                _write_gate("true")
                _write_result({
                    "attempt": FIX_ATTEMPT,
                    "test_class": test_class,
                    "test_method": test_method,
                    "passed": True,
                    "test_output": test_output,
                    "fixes_applied": ["auto:db_config"],
                    "infra_repair": "INFRA_DB",
                }, files_written, FIX_ATTEMPT)
                return
            failure_class = classify_failure(test_output, plan_data.get("api_base_url", ""))

    if failure_class == "INFRA_USER":
        log("Detected user pool error — attempting demo user auto-insert")
        if try_fix_infra_user(plan_data):
            log("Demo user inserted/reset — running test")
            passed, test_output = run_maven_test(test_class, test_method)
            if passed:
                log("Test PASSED after user auto-repair")
                _write_gate("true")
                _write_result({
                    "attempt": FIX_ATTEMPT,
                    "test_class": test_class,
                    "test_method": test_method,
                    "passed": True,
                    "test_output": test_output,
                    "fixes_applied": ["auto:user_insert"],
                    "infra_repair": "INFRA_USER",
                }, files_written, FIX_ATTEMPT)
                return
            failure_class = classify_failure(test_output, plan_data.get("api_base_url", ""))

    if failure_class == "INFRA_CREDENTIALS":
        log("Detected a null-credential error signature — checking the demo-credential property")
        if try_fix_infra_credentials(plan_data):
            log("Credential property written — running test")
            passed, test_output = run_maven_test(test_class, test_method)
            if passed:
                log("Test PASSED after credential auto-repair")
                _write_gate("true")
                _write_result({
                    "attempt": FIX_ATTEMPT,
                    "test_class": test_class,
                    "test_method": test_method,
                    "passed": True,
                    "test_output": test_output,
                    "fixes_applied": ["auto:credential_property"],
                    "infra_repair": "INFRA_CREDENTIALS",
                }, files_written, FIX_ATTEMPT)
                return
            failure_class = classify_failure(test_output, plan_data.get("api_base_url", ""))
        else:
            # The property was already correct (or there were no demo_credentials
            # to write at all) — this signature match wasn't actually a missing-
            # credential issue after all. Unlike INFRA_DB/INFRA_USER, this is NOT
            # "unresolvable infra" — fall through to the normal CODE_ERROR path
            # rather than skipping the Claude fix loop for what's likely a real bug.
            log("Credential property was already correct — not a credentials issue; "
                "treating as a normal code failure")
            failure_class = "CODE_ERROR"

    api_auth_prevalidated_ok = api_validation.get("auth", {}).get("status") == "ok"
    api_auth_code_bug_hint = False  # surfaced to the Claude fix prompt below
    if failure_class == "INFRA_API_AUTH":
        if api_auth_prevalidated_ok:
            # Step 02 already proved these exact credentials work via a direct
            # HTTP call before codegen — a 401/403 now, with nothing else
            # changed, points at the GENERATED code (wrong header name/prefix,
            # token not attached, etc.), not the credentials or the API itself.
            # Fall through to the normal CODE_ERROR/Claude-fix path; the extra
            # context injected into the prompt below tells Claude as much.
            log("Detected an API 401/403, but step 02 pre-validated this exact "
                "auth recipe as working — treating as a code bug, not an infra issue")
            failure_class = "CODE_ERROR"
            api_auth_code_bug_hint = True
        else:
            log(f"Detected an API 401/403 — and step 02's auth pre-check did not "
                f"confirm it working either ({api_validation.get('auth', {}).get('status', 'not run')}: "
                f"{api_validation.get('auth', {}).get('detail', 'n/a')}) — not auto-resolvable")

    if failure_class in ("INFRA_API_AUTH", "INFRA_API_CONNECTION"):
        reason = (f"API auth not resolvable: {api_validation.get('auth', {}).get('detail', 'see test output')}"
                  if failure_class == "INFRA_API_AUTH" else
                  f"API at {plan_data.get('api_base_url', '?')} is unreachable — check network/VPN access")
        log(f"Infrastructure error not auto-resolvable ({failure_class}) — skipping Claude fix loop")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Infrastructure error: {failure_class} — {reason}",
            "test_output": prev_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    if failure_class == "INFRA_BUILD":
        log(f"ERROR: Maven project not found — check WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")
        log(f"Expected pom.xml at: {AUTOMATION_FRAMEWORK_DIR}")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Maven project not found at {AUTOMATION_FRAMEWORK_DIR} — ensure WORKSPACE_DIR and GITHUB_REPO_AUTOMATION point to a valid Maven project",
            "test_output": prev_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    if failure_class in ("INFRA_DB", "INFRA_USER"):
        log(f"Infrastructure error not auto-resolvable ({failure_class}) — skipping Claude fix loop")
        _write_gate("skipped")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "skipped": True,
            "reason": f"Infrastructure error: {failure_class} — manual setup required",
            "test_output": prev_output,
            "fixes_applied": [],
        }, files_written, FIX_ATTEMPT)
        return

    # CODE_ERROR — call Claude for a fix, apply it, then run the test
    log(f"Fix attempt {FIX_ATTEMPT}/{MAX_ATTEMPTS}: calling Claude for a fix...")
    generated_files = read_generated_files(files_written)
    # Read Jarvis/CLAUDE.md — single source of truth for framework conventions.
    fw_claude_md_path = AUTOMATION_FRAMEWORK_DIR / "CLAUDE.md"
    claude_md = fw_claude_md_path.read_text() if fw_claude_md_path.exists() else ""
    if not claude_md:
        log(f"WARNING: {fw_claude_md_path} not found — check FRAMEWORK_DIR, or "
            "WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")

    files_context = "\n".join(
        f"\n--- {path} ---\n{content}\n" for path, content in generated_files.items()
    )

    # Structured evidence section — precise failureLocation from the framework's
    # own JsonTestReporter (built "for AI agents to read", never consulted
    # before) plus the curated summary this file already computed but only
    # used to print to the console, not to inform the fix itself.
    structured_section = ""
    if prev_failure_location or prev_summary_lines:
        structured_section = "\n<structured_failure_report>\n"
        if prev_failure_location:
            structured_section += f"Failure location: {prev_failure_location}\n"
            if files_written and not any(prev_failure_location.startswith(Path(f).name)
                                         for f in files_written):
                structured_section += (
                    "NOTE: this location is NOT one of the files listed in "
                    "<generated_files> below — it may be a bug in shared framework "
                    "code you cannot see or edit here. If so, say so plainly in "
                    "root_cause instead of inventing a workaround in a file you can "
                    "edit; a workaround around a framework bug tends to make the "
                    "next failure harder to diagnose, not fix this one.\n"
                )
        if prev_failure_message:
            structured_section += f"Failure message: {prev_failure_message}\n"
        if prev_screenshot:
            structured_section += f"Screenshot (browser-driven web test only): {prev_screenshot}\n"
        if prev_summary_lines:
            structured_section += "Curated Maven/TestNG summary:\n" + "\n".join(
                f"  {ln}" for ln in prev_summary_lines
            ) + "\n"
        structured_section += "</structured_failure_report>\n"

    # Runtime evidence — what the browser actually saw. Ordered deliberately:
    # what the test did, then the page it was on, then the framework's own
    # verdict on that page.
    structured_section += (evidence["trace_section"]
                           + evidence["dom_section"]
                           + evidence["context_section"])

    failed_selector = ""
    try:
        if evidence.get("trace_path"):
            failed = failing_action(read_actions(Path(evidence["trace_path"])))
            failed_selector = (failed or {}).get("selector", "")
    except Exception:
        pass
    # Pass the maven tail as the stack trace, not failure_location. The engine
    # derives which page object the test believed it was on by matching
    # "SomePage.java" in a trace; a one-line "File.java:NN" (or the empty string
    # this used to be) gives it nothing to reason from, which is why every run
    # came back INSUFFICIENT_EVIDENCE.
    structured_section += advisory_diagnosis(
        test_class, test_method, evidence["dom_snapshot_path"], failed_selector,
        prev_failure_message, prev_output or prev_failure_location)

    if api_auth_code_bug_hint:
        api_auth = plan_data.get("api_auth", {})
        structured_section += (
            "\n<api_auth_note>\n"
            f"This test is failing with a 401/403, but step 02 (Validate API) already "
            f"confirmed THIS EXACT auth recipe works via a direct HTTP call: "
            f"{api_validation.get('auth', {}).get('detail', '')}\n"
            f"api_auth from the plan: {json.dumps(api_auth)}\n"
            "Since the credentials and endpoint are already proven to work outside the "
            "generated code, look for a bug in how the generated Helper/ApiHelper code "
            "builds and attaches the auth header (wrong header name, missing 'Bearer ' "
            "prefix, token not actually set before the call, wrong request going out) "
            "rather than treating this as a credentials or environment problem.\n"
            "</api_auth_note>\n"
        )

    retry_section = ""
    if FIX_ATTEMPT > 1:
        stuck_note = ""
        if prev_root_cause:
            stuck_note = (
                f"\nThe PREVIOUS fix attempt's stated root cause was:\n  {prev_root_cause}\n"
                "That attempt's fix did not resolve the test (see the failure above, "
                "captured AFTER that fix was applied and the test re-run) — so either "
                "that diagnosis was wrong, or the fix for it was incomplete. Do not "
                "repeat the same diagnosis unless you have a specific reason the fix "
                "for it was incomplete rather than misdiagnosed.\n"
            )
        retry_section = f"""
## ⚠️ RETRY — Fix attempt {FIX_ATTEMPT}
Previous fix did not resolve the test. Previous failure:
```
{prev_output}
```
{stuck_note}
Try a DIFFERENT approach — do NOT repeat what was tried before.
"""

    # Name the real properties file and the keys already in it, so "use a property"
    # is an instruction the model can follow rather than one it has to invent.
    props_file_name = properties_file.properties_path(AUTOMATION_FRAMEWORK_DIR).name
    known_url_keys  = sorted(gen_data.get("url_properties") or {})
    url_keys_hint   = (f" — already defined: {', '.join(known_url_keys)}"
                       if known_url_keys else "")

    prompt = f"""You are a Java test automation debugging agent for the Jarvis framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<generated_files>
{files_context}
</generated_files>
{structured_section}
<test_failure>
```
{prev_output}
```
</test_failure>
{retry_section}

The test failed. Analyze the failure and return a JSON object with your diagnosis and
fixed file contents. Only include files that need to change.

Common failure causes:
- Import statements missing or wrong package names
- Locator not found — fix the selector or add a fallback
- Method not found — check the framework API (use BasePage/Element/WaitHelper wrappers)
- Compilation error — fix the Java syntax
- User not allocated — check allocateUser() call matches feature enum
- Auth not set — ensure setAuthToken(token) is called on the helper, or doLogin() for web tests

CRITICAL: Preserve ALL existing JavaDoc comments, inline comments, and annotations exactly as written.
Only change the minimum code required to fix the failure. Do NOT remove, shorten, or reword any comments.

CRITICAL: Never introduce a literal "http://" or "https://" URL — not in a test, a page object,
a helper, or a `static final` constant. A fix that adds one is REJECTED outright and the attempt
is wasted. URLs live in parameters/{props_file_name} and are read back with
config.getRunTimeProperty("<feature>.<page>.url"){url_keys_hint}. If the URL you need has no
property yet, use a key named that way anyway — the missing value is a clearer failure than a
URL welded into Java.

Return ONLY a JSON object of this exact shape:
{{
  "root_cause": "one or two sentences: what actually broke and why, not just what error appeared",
  "confidence": "high | medium | low",
  "edits": [
    {{
      "file": "src/main/java/automation/modules/{plan_data.get('feature_name', 'feature')}/web/SomePage.java",
      "old_string": "the exact text to replace, with enough surrounding context to be UNIQUE in the file",
      "new_string": "the replacement text"
    }}
  ]
}}

Return TARGETED EDITS, not whole files. Each "old_string" must appear EXACTLY ONCE in
its file — include a line or two of surrounding context if the snippet alone would be
ambiguous. An edit whose old_string is missing or matches twice is rejected outright,
because guessing which occurrence was meant is how an automated fix corrupts a file.
Keep edits minimal: change the lines that are wrong, nothing else. A diff larger than
{FIX_MAX_DIFF_LINES} lines is rejected as a whole-file regeneration.

Only if a file is too badly broken to patch (it does not compile at all, or the change
is structural), fall back to whole-file replacement instead:
  "files": {{ "<path>": "...COMPLETE file content..." }}

If this is a framework-level issue you cannot fix from the files you can see, return
"edits": [] and explain that clearly in root_cause rather than guessing at a workaround.
Output ONLY valid JSON.
"""

    fix_response = call_claude(prompt)
    fix_map = extract_json(fix_response)
    root_cause, confidence, files_map, edits_map = extract_fix_response(fix_map)

    fixes_applied = []
    fix_contents: dict = {}
    fix_rejections: list = []
    if files_map or edits_map:
        fixes_applied, fix_contents, fix_rejections = apply_fix(files_map, edits_map)
        if fixes_applied:
            log(f"Applied fixes to {len(fixes_applied)} file(s) — running test")
    elif root_cause:
        log(f"Claude diagnosed the failure but proposed no file changes: {root_cause}")
        log("  → Likely a framework-level issue outside the generated files — running "
            "test anyway in case it was already resolved, but expect this to still fail")
    else:
        log("WARNING: Claude did not return a valid fix map — running test without fix")
    if root_cause:
        log(f"Root cause ({confidence or 'unknown confidence'}): {root_cause}")

    # Nothing changed on disk, so the test would fail exactly as it just did.
    # Re-running it costs a full maven cycle to learn nothing and consumes the
    # attempt that could have carried a real fix — the wasted-attempt bug.
    nothing_applied = bool((files_map or edits_map) and not fixes_applied)
    if nothing_applied:
        log(f"No fix was applied — every proposed change was rejected "
            f"({len(fix_rejections)} file(s)). Skipping the test re-run: the code on "
            f"disk is unchanged, so the result would be identical.")
        for entry in fix_rejections:
            log(f"  - {entry['file']}: {entry['reason']}")
        _write_gate("false")
        _write_result({
            "attempt": FIX_ATTEMPT,
            "test_class": test_class,
            "test_method": test_method,
            "passed": False,
            "test_output": prev_output,
            "fixes_applied": [],
            "fix_rejections": fix_rejections,
            "fix_response_length": len(fix_response),
            "root_cause": root_cause,
            "confidence": confidence,
            "skipped_rerun": True,
        }, files_written, FIX_ATTEMPT)
        return

    # Run the test with the fix applied
    run_started_at = time.time()
    passed, test_output = run_maven_test(test_class, test_method)
    failure_ctx = {}
    if not passed:
        failure_ctx = build_failure_context(test_class, test_method, test_output, run_started_at)

    stuck_on_same_failure = bool(
        not passed and fixes_applied and prev_failure_location
        and failure_ctx.get("failure_location") == prev_failure_location
    )

    if passed:
        log(f"Test PASSED after fix attempt {FIX_ATTEMPT}")
        _write_gate("true")
    elif stuck_on_same_failure:
        log(f"Test still FAILED after fix attempt {FIX_ATTEMPT} — at the EXACT SAME location "
            f"as before the fix ({prev_failure_location}). The applied fix had no effect on "
            f"the actual failure point — stopping the fix loop rather than burning the "
            f"remaining attempts on a diagnosis that isn't converging.")
        # Distinct gate value from "skipped" — this test genuinely ran and genuinely
        # failed (unlike a real infra skip, where it never got a fair shot), so it
        # must NOT be treated as APPROVED/"not run" downstream in 05_ship.py the way
        # "skipped" is. run.sh stops the retry loop on "stuck" exactly like "skipped".
        _write_gate("stuck")
    else:
        log(f"Test still FAILED after fix attempt {FIX_ATTEMPT}")
        _write_gate("false")

    result_data = {
        "attempt": FIX_ATTEMPT,
        "test_class": test_class,
        "test_method": test_method,
        "passed": passed,
        "test_output": test_output,
        "fixes_applied": fixes_applied,
        "fix_rejections": fix_rejections,
        "fix_response_length": len(fix_response),
        "root_cause": root_cause,
        "confidence": confidence,
        **({"stuck": True, "reason": "stuck on identical failure across fix attempts — "
            "see root_cause history in the per-attempt audit files"} if stuck_on_same_failure else {}),
        **failure_ctx,
    }

    _write_result(result_data, files_written, FIX_ATTEMPT)

    # Per-attempt audit file — includes fix file contents for per-step git commits in ship step
    attempt_data = {**result_data, "fix_file_contents": fix_contents}
    (AUDIT_DIR / f"04-run-and-fix-attempt-{FIX_ATTEMPT}.json").write_text(
        json.dumps(attempt_data, indent=2)
    )


def _write_gate(value: str) -> None:
    (AUDIT_DIR / ".fix-passed").write_text(value)


def _write_result(data: dict, files_written: list, attempt: int) -> None:
    (AUDIT_DIR / "04-run-and-fix.json").write_text(json.dumps(data, indent=2))

    passed = data.get("passed", False)
    skipped = data.get("skipped", False)
    stuck = data.get("stuck", False)
    fixes = data.get("fixes_applied", [])

    result_label = "PASSED" if passed else "SKIPPED" if skipped else "STUCK" if stuck else "FAILED"
    lines = [
        "# Run & Fix Results",
        "",
        f"Attempt:  {attempt}",
        f"Result:   {result_label}",
    ]
    if data.get("reason"):
        lines.append(f"Reason:   {data['reason']}")
    if not skipped:
        lines += [
            f"Class:    {data.get('test_class')}",
            f"Method:   {data.get('test_method')}",
        ]
    if data.get("failure_location"):
        lines.append(f"Failure:  {data['failure_location']}")
    if data.get("screenshot_path"):
        lines.append(f"Screenshot: {data['screenshot_path']}")
    if data.get("root_cause"):
        lines += ["", "## Root Cause", f"({data.get('confidence', 'unknown')} confidence) {data['root_cause']}"]
    if fixes:
        lines += ["", "## Files Fixed", ] + [f"- `{f}`" for f in fixes]
    (AUDIT_DIR / "04-run-and-fix.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
