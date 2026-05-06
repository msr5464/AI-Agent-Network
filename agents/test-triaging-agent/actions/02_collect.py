#!/usr/bin/env python3
"""
Step 02 — Collect
Load DB results, parse HTML logs, detect flaky tests, calculate trends,
merge into a unified context JSON for downstream steps.
Outputs: audit/<session>/02-collect.json + 02-collect.md

No AI calls. All deterministic.
"""

import os, sys, json, logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
def log(msg): _log("collect", msg)

# Suppress verbose library warnings
import warnings, urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

# Silence internal loggers — we use our own log()
logging.basicConfig(level=logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))

from lib.settings import Config

INPUT_DIR = os.environ.get("INPUT_DIR", Config.INPUT_DIR)
TABLE_NAME_OVERRIDE = os.environ.get("TABLE_NAME", "")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(filename):
    path = AUDIT_DIR / filename
    if not path.exists():
        log(f"ERROR: {filename} not found in audit dir")
        sys.exit(1)
    return json.loads(path.read_text())


def test_result_to_dict(tr) -> dict:
    """Serialize TestResult to JSON-safe dict."""
    return {
        "class_name": tr.class_name,
        "method_name": tr.method_name,
        "full_name": tr.full_name,
        "status": tr.status.value,
        "duration_seconds": tr.duration_seconds,
        "error_type": tr.error_type,
        "error_message": tr.error_message,
        "stack_trace": tr.stack_trace,
        "platform": tr.platform,
        "execution_log": tr.execution_log,
        "description": tr.description,
        "known_failure": tr.known_failure,
        "is_failure": tr.is_failure,
    }


def summary_to_dict(s) -> dict:
    return {
        "total": s.total,
        "passed": s.passed,
        "failed": s.failed,
        "skipped": s.skipped,
        "errors": s.errors,
        "duration_seconds": s.duration_seconds,
        "pass_rate": round(s.pass_rate, 2),
    }

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    scout = load_json("01-scout.json")
    build_tag = scout.get("selected_build_tag") or (AUDIT_DIR / ".selected-buildtag").read_text().strip()
    log(f"Build tag: {build_tag}")

    # Derive report name (build_tag IS the report name / folder name)
    report_name = build_tag
    table_name = TABLE_NAME_OVERRIDE or None

    # Resolve input directory: look for a subfolder matching build_tag, else use INPUT_DIR directly
    input_path = Path(INPUT_DIR)
    report_dir = input_path / build_tag
    if not report_dir.exists():
        # Try INPUT_DIR itself (build_tag matches the root folder name)
        if Path(INPUT_DIR).exists() and Path(INPUT_DIR).name == build_tag:
            report_dir = input_path
        else:
            # Fall back to INPUT_DIR root and scan for latest
            report_dir = input_path
    log(f"Input dir: {report_dir}")

    # ── 1. Query DB ────────────────────────────────────────────────────────────
    log("Querying database for test results...")
    from lib.agent.memory import AgentMemory
    memory = AgentMemory()

    try:
        db_results = memory.get_test_results_by_buildtag(report_name, build_tag, table_name=table_name)
    except Exception as e:
        log(f"ERROR querying DB: {e}")
        sys.exit(1)

    if not db_results:
        log(f"ERROR: No test results found in DB for buildTag: {build_tag}")
        sys.exit(1)

    log(f"Found {len(db_results)} test results in DB")

    # ── 2. Flaky detection ─────────────────────────────────────────────────────
    log("Detecting flaky tests...")
    all_test_names = [r.get("testcaseName", "") for r in db_results if r.get("testcaseName")]
    current_failure_names = [
        r.get("testcaseName", "") for r in db_results
        if r.get("testStatus", "").upper() in ["FAIL", "FAILED", "ERROR", "ERRORED"]
    ]

    try:
        recurring = memory.detect_recurring_failures(
            current_failure_names,
            days=Config.FLAKY_TESTS_LAST_RUNS,
            min_occurrences=Config.FLAKY_TESTS_MIN_FAILURES,
            report_name=report_name,
            all_test_names=all_test_names,
            table_name=table_name,
        )
    except Exception as e:
        log(f"Warning: flaky detection failed: {e}")
        recurring = []

    if recurring:
        log(f"Detected {len(recurring)} flaky tests")

    # ── 3. Trend analysis ──────────────────────────────────────────────────────
    log("Calculating trend analysis...")
    trends = {"trend": "UNKNOWN", "average_pass_rate": 0.0}
    try:
        trends = memory.get_trend_analysis(days=10, report_name=report_name, table_name=table_name)
    except Exception as e:
        log(f"Warning: trend analysis failed: {e}")

    trend_value = trends.get("trend", "UNKNOWN")
    avg_pass_rate = float(trends.get("average_pass_rate") or 0.0)
    log(f"Trend: {trend_value} (avg pass rate: {avg_pass_rate:.1f}%)")

    # ── 4. Parse HTML execution logs ───────────────────────────────────────────
    log("Parsing HTML execution logs...")
    execution_logs = {}
    html_links = {}
    durations = {}

    if report_dir.exists():
        try:
            from lib.parsers.data_builder import get_execution_logs_from_html, get_test_durations_from_html
            execution_logs, html_links = get_execution_logs_from_html(str(report_dir))
            durations = get_test_durations_from_html(str(report_dir))
            log(f"Extracted logs for {len(execution_logs)} tests from HTML")
        except Exception as e:
            log(f"Warning: HTML parsing failed: {e}")
    else:
        log(f"Warning: report_dir not found: {report_dir} — skipping HTML parse")

    # ── 5. Merge DB + HTML ─────────────────────────────────────────────────────
    log("Merging DB results with HTML logs...")
    try:
        from lib.parsers.data_builder import get_full_report_data_from_db
        data = get_full_report_data_from_db(
            str(report_dir), db_results, execution_logs, durations, html_links
        )
        summary = data["summary"]
        test_results = data["test_results"]
        merged_html_links = data.get("html_links", {})
    except Exception as e:
        log(f"ERROR merging data: {e}")
        sys.exit(1)

    failures = [r for r in test_results if r.is_failure]
    log(f"Total: {summary.total} | Pass rate: {summary.pass_rate:.1f}% | Failures: {len(failures)}")

    # ── 6. Filter flaky tests to current run ───────────────────────────────────
    if recurring and test_results:
        all_names = {t.full_name for t in test_results}
        patterns = set()
        for t in test_results:
            parts = t.full_name.split(".")
            if len(parts) >= 2:
                patterns.add(".".join(parts[-2:]))

        filtered_recurring = []
        for r in recurring:
            tn = r["test_name"]
            tparts = tn.split(".")
            tp = ".".join(tparts[-2:]) if len(tparts) >= 2 else tn
            if r.get("in_current_run") or tn in all_names or tp in patterns:
                filtered_recurring.append(r)

        recurring = filtered_recurring
        log(f"After filtering: {len(recurring)} flaky tests match current run")

    # ── Write JSON ─────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp": ts,
        "build_tag": build_tag,
        "report_name": report_name,
        "report_dir": str(report_dir),
        "summary": summary_to_dict(summary),
        "trend": trend_value,
        "average_pass_rate": avg_pass_rate,
        "flaky_tests": recurring,
        "html_links": merged_html_links,
        "test_results": [test_result_to_dict(r) for r in test_results],
        "failures": [test_result_to_dict(r) for r in failures],
        "failure_count": len(failures),
        "total_count": summary.total,
    }

    json_path = AUDIT_DIR / "02-collect.json"
    json_path.write_text(json.dumps(result, indent=2, default=str))
    log(f"Wrote 02-collect.json ({json_path.stat().st_size // 1024}KB)")

    # ── Write Markdown ─────────────────────────────────────────────────────────
    md_lines = [
        "# Collect Results",
        "",
        f"**Build Tag:** {build_tag}  ",
        f"**Timestamp:** {ts}  ",
        f"**Report Dir:** {report_dir}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        "|--------|-------|",
        f"| Total Tests | {summary.total} |",
        f"| Passed | {summary.passed} |",
        f"| Failed | {summary.failed} |",
        f"| Errors | {summary.errors} |",
        f"| Skipped | {summary.skipped} |",
        f"| Pass Rate | {summary.pass_rate:.1f}% |",
        f"| Trend | {trend_value} |",
        f"| Avg Pass Rate (10d) | {avg_pass_rate:.1f}% |",
        f"| Flaky Tests | {len(recurring)} |",
        "",
    ]

    if failures:
        md_lines += [
            "## Failures",
            "",
            "| Test | Status | Error Type |",
            "|------|--------|------------|",
        ]
        for f in failures[:30]:
            et = f.error_type or ""
            md_lines.append(f"| {f.full_name[:80]} | {f.status.value} | {et[:60]} |")
        if len(failures) > 30:
            md_lines.append(f"| ... and {len(failures) - 30} more | | |")

    if recurring:
        md_lines += [
            "",
            "## Flaky Tests",
            "",
        ]
        for r in recurring[:10]:
            md_lines.append(f"- {r['test_name']} ({r.get('failure_count', 0)} failures in {r.get('last_days', 10)} days)")

    (AUDIT_DIR / "02-collect.md").write_text("\n".join(md_lines) + "\n")
    log(f"Done — {len(failures)} failures ready for classification")


if __name__ == "__main__":
    main()
