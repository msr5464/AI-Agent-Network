#!/usr/bin/env python3
"""
Step 05 — Ship
Check .verdict gate, generate HTML report, write handoff.json for test-healing-agent,
send Slack notification, record processed build tag.

No AI calls. No code changes. Read gate files, write outputs.

Handoff: if verdict=APPROVED and there are AUTOMATION_ISSUE HIGH ELEMENT_NOT_FOUND
failures, writes agents/test-healing-agent/queue/<build_tag>.json for the test-healing-agent agent
to pick up independently.
"""

import os, sys, json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

import warnings, urllib3
urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import logging
logging.basicConfig(level=logging.WARNING)

from shared.log import log as _log
from shared.dom_snapshot import find_snapshot, parse_header
from shared import diagnosis
from lib import artifacts
from shared.playwright_trace import read_actions, failing_action
from shared.slack import send_slack as _send_slack
def log(msg): _log("ship", msg)
def send_slack(channel: str, text: str) -> bool:
    return _send_slack(SLACK_BOT_TOKEN, channel, text)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
SESSION_ID = os.environ.get("SESSION_ID", AUDIT_DIR.name)

SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "")
SLACK_ALERT_CHANNEL  = os.environ.get("SLACK_ALERT_CHANNEL", "")

SKIP_FILE  = AGENT_DIR / "feedback" / "skip-buildtags.json"

# test-healing-agent queue — handoff destination
# Override with AUTOFIX_QUEUE_DIR env var if test-healing-agent lives elsewhere
_queue_dir_env = os.environ.get("AUTOFIX_QUEUE_DIR", "")
AUTOFIX_QUEUE_DIR = Path(_queue_dir_env) if _queue_dir_env else REPO_ROOT / "agents" / "test-healing-agent" / "queue"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(filename, required=True):
    path = AUDIT_DIR / filename
    if not path.exists():
        if required:
            log(f"ERROR: {filename} not found")
            sys.exit(1)
        return {}
    return json.loads(path.read_text())


def check_verdict() -> str:
    verdict_path = AUDIT_DIR / ".verdict"
    return verdict_path.read_text().strip() if verdict_path.exists() else "UNKNOWN"


def record_skip(build_tag: str, verdict: str):
    """Add this build tag to skip-buildtags.json so it isn't re-processed."""
    SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if SKIP_FILE.exists():
        try:
            entries = json.loads(SKIP_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            entries = []
    entries = [e for e in entries if e.get("build_tag") != build_tag]
    entries.append({
        "build_tag": build_tag,
        "processed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
    })
    SKIP_FILE.write_text(json.dumps(entries, indent=2))
    log(f"Recorded {build_tag} in skip-buildtags.json")




def attach_artifacts(issue: dict, report_dir: Path, method_name: str) -> None:
    """Preserve this failure's artefacts, unless collect already did.

    Collect attaches these now, so that classification can see them. Ship still
    runs the same code for handoffs built from an older collect output, and
    because the copies it makes are the ones that survive CI cleaning up the
    report directory.
    """
    if issue.get("dom_snapshot") and Path(issue["dom_snapshot"]).exists():
        return
    artifacts.attach_all(issue, report_dir, method_name, AUDIT_DIR, log)


def write_handoff(build_tag: str, classify_data: dict, collect_data: dict) -> str | None:
    """
    Write handoff.json to test-healing-agent queue if there are eligible candidates.
    Eligible = AUTOMATION_ISSUE + HIGH confidence + ELEMENT_NOT_FOUND.
    Returns the queue file path if written, else None.
    """
    all_classifications = classify_data.get("classifications", [])
    # Select on what the healing agent can actually act on, not on one category
    # name. ELEMENT_NOT_FOUND alone forwarded stale locators and nothing else, so
    # a page that was merely slow or covered — both fixable — stayed red forever
    # under TIMEOUT, while wrong-page failures came through wearing the label of
    # a locator problem.
    actionable = set(diagnosis.ACTIONS) | {"ELEMENT_NOT_FOUND"}
    eligible = [
        c for c in all_classifications
        if c.get("classification") == "AUTOMATION_ISSUE"
        and c.get("confidence") == "HIGH"
        and c.get("root_cause_category") in actionable
        and c.get("root_cause_category") not in diagnosis.STOP
    ]

    if not eligible:
        log("No AUTOMATION_ISSUE HIGH ELEMENT_NOT_FOUND failures — no handoff queued")
        return None

    # Build failure lookup from collect data (provides execution_log, stack_trace etc.)
    failure_lookup = {f["full_name"]: f for f in collect_data.get("failures", [])}

    report_dir = Path(collect_data.get("report_dir", "")) if collect_data.get("report_dir") else None

    # Merge classification + failure data into handoff issues
    automation_issues = []
    for c in eligible:
        test_name = c["test_name"]
        failure = failure_lookup.get(test_name, {})
        issue = {
            # Classification fields
            "test_name":          test_name,
            "classification":     c.get("classification", ""),
            "confidence":         c.get("confidence", ""),
            "root_cause_category": c.get("root_cause_category", ""),
            "root_cause":         c.get("root_cause", ""),
            "failure_signature":  c.get("failure_signature", ""),
            # Root-cause grouping decided at classification time. The healing
            # agent re-clusters using the page object it resolves, which is more
            # precise, but this is a useful prior when that resolution fails.
            "cause_group_key":    c.get("cause_group_key", ""),
            "cause_group_size":   c.get("cause_group_size", 1),
            "recommended_action": c.get("recommended_action", ""),
            # Failure detail fields (needed by 01_fix.py for CodeAnalyzer + prompt)
            "error_type":    failure.get("error_type", ""),
            "error_message": failure.get("error_message", ""),
            "stack_trace":   failure.get("stack_trace", ""),
            "execution_log": failure.get("execution_log", ""),
            "class_name":    failure.get("class_name", ""),
            "method_name":   failure.get("method_name", ""),
            "full_name":     failure.get("full_name", test_name),
            # Populated below when the framework captured a DOM on failure.
            "dom_snapshot":  "",
            "failure_url":   "",
            "trace_path":    "",
            "failed_selector": "",
        }
        method = failure.get("method_name") or test_name.split(".")[-1]
        attach_artifacts(issue, report_dir, method)
        automation_issues.append(issue)

    handoff = {
        "build_tag":       build_tag,
        "created_at":      datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_session":  SESSION_ID,
        "source_audit_dir": str(AUDIT_DIR),
        "automation_issues": automation_issues,
    }

    # Write to test-healing-agent queue
    AUTOFIX_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    safe_tag = build_tag.replace("/", "-")
    queue_file = AUTOFIX_QUEUE_DIR / f"{safe_tag}.json"
    queue_file.write_text(json.dumps(handoff, indent=2))
    log(f"Handoff queued: {queue_file} ({len(automation_issues)} issues)")
    return str(queue_file)


def generate_html_report(collect_data: dict, classify_data: dict) -> str | None:
    try:
        from lib.settings import Config
        from lib.parsers.models import TestResult, TestStatus, TestSummary
        from lib.agent.analyzer import FailureClassification
        from lib.reporters.report_generator import ReportGenerator
        from lib.reporters.category_rules import CategoryRuleEngine
        from lib.utils import FailureClassificationUtils, TestDataCache

        build_tag  = collect_data.get("build_tag", "unknown")
        report_dir = collect_data.get("report_dir", "")
        output_dir = os.environ.get("OUTPUT_DIR", Config.OUTPUT_DIR)

        # Reconstruct TestResult objects
        test_results = []
        for tr_dict in collect_data.get("test_results", []):
            status_str = tr_dict.get("status", "FAIL").upper()
            try:
                status = TestStatus(status_str)
            except ValueError:
                status = TestStatus.FAIL
            tr = TestResult(
                class_name=tr_dict.get("class_name", ""),
                method_name=tr_dict.get("method_name", ""),
                status=status,
                duration_seconds=float(tr_dict.get("duration_seconds") or 0.0),
                error_type=tr_dict.get("error_type"),
                error_message=tr_dict.get("error_message"),
                stack_trace=tr_dict.get("stack_trace"),
                platform=tr_dict.get("platform"),
                execution_log=tr_dict.get("execution_log"),
                description=tr_dict.get("description"),
                known_failure=tr_dict.get("known_failure"),
            )
            test_results.append(tr)

        # Reconstruct TestSummary
        s = collect_data.get("summary", {})
        summary = TestSummary(
            total=s.get("total", 0),
            passed=s.get("passed", 0),
            failed=s.get("failed", 0),
            skipped=s.get("skipped", 0),
            errors=s.get("errors", 0),
            duration_seconds=float(s.get("duration_seconds") or 0.0),
        )

        # Reconstruct FailureClassification objects
        classifications = []
        for c_dict in classify_data.get("classifications", []):
            fc = FailureClassification(
                test_name=c_dict["test_name"],
                classification=c_dict.get("classification", "UNKNOWN"),
                confidence=c_dict.get("confidence", "LOW"),
                root_cause=c_dict.get("root_cause", ""),
                recommended_action=c_dict.get("recommended_action", ""),
                root_cause_category=c_dict.get("root_cause_category", "OTHER"),
                failure_signature=c_dict.get("failure_signature", ""),
            )
            classifications.append(fc)

        deduplicated = FailureClassificationUtils.deduplicate(classifications)

        html_links = collect_data.get("html_links", {})
        test_data_cache = TestDataCache(test_results, html_links)
        rule_engine = CategoryRuleEngine()
        category_counts = {}
        category_failures = {}
        for fc in deduplicated:
            cat = rule_engine.classify(fc, test_data_cache)
            category_counts[cat] = category_counts.get(cat, 0) + 1
            category_failures.setdefault(cat, []).append(fc)

        # AI executive summary removed — classifications from 03_classify.py are sufficient
        ai_summary = None

        report_gen = ReportGenerator()
        html_content, _ = report_gen.generate_html_report(
            summary=summary,
            classifications=deduplicated,
            report_name=build_tag,
            ai_summary=ai_summary,
            recurring_failures=collect_data.get("flaky_tests", []),
            trend=collect_data.get("trend", "UNKNOWN"),
            report_dir=report_dir,
            test_results=test_results,
            test_html_links=html_links,
            environment=os.environ.get("ENVIRONMENT", ""),
            output_dir=output_dir,
        )

        safe_tag = "".join(c for c in build_tag if c.isalnum() or c in ("-", "_")).strip()
        html_path = Path(output_dir) / f"AI-Generated-Report_{safe_tag}.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        saved = report_gen.save_report(html_content, str(html_path))
        log(f"HTML report saved: {saved}")
        return saved

    except Exception as e:
        log(f"Warning: HTML report generation failed: {e}")
        import traceback
        traceback.print_exc()
        return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    collect_data  = load_json("02-collect.json")
    classify_data = load_json("03-classify.json")

    build_tag   = collect_data.get("build_tag", "unknown")
    verdict     = check_verdict()
    cls_summary = classify_data.get("summary", {})

    log(f"Build tag: {build_tag}")
    log(f"Verdict: {verdict}")
    log(f"Classification summary: {cls_summary}")

    report_path   = None
    handoff_path  = None
    slack_notified = False
    escalated     = False

    # ── Gate: review verdict ───────────────────────────────────────────────────
    if verdict == "NEEDS-HUMAN":
        log("Verdict is NEEDS-HUMAN — escalating to Slack (no handoff queued)")
        escalated = True

        alert_channel = SLACK_ALERT_CHANNEL or SLACK_NOTIFY_CHANNEL
        if alert_channel:
            summary = collect_data.get("summary", {})
            text = (
                f":warning: *QA Analysis — Human Review Needed*\n"
                f"Build: `{build_tag}`\n"
                f"Pass Rate: {summary.get('pass_rate', 0):.1f}% ({summary.get('failed', 0)} failures)\n"
                f"Product Bugs: {cls_summary.get('PRODUCT_BUG', 0)} | "
                f"Automation Issues: {cls_summary.get('AUTOMATION_ISSUE', 0)}\n"
                f"Reviewer challenged classifications — needs manual QA review.\n"
                f"Audit: `{AUDIT_DIR.name}`"
            )
            slack_notified = send_slack(alert_channel, text)

    # ── Generate HTML report (always) ─────────────────────────────────────────
    log("Generating HTML report...")
    report_path = generate_html_report(collect_data, classify_data)

    # ── Write handoff for test-healing-agent (only if APPROVED) ─────────────────────
    if not escalated:
        handoff_path = write_handoff(build_tag, classify_data, collect_data)

    # ── Slack notification ────────────────────────────────────────────────────
    if not escalated and SLACK_NOTIFY_CHANNEL:
        summary    = collect_data.get("summary", {})
        total      = summary.get("total", 0)
        failed     = summary.get("failed", 0)
        pass_rate  = summary.get("pass_rate", 0)
        trend      = collect_data.get("trend", "UNKNOWN")
        product_bugs = cls_summary.get("PRODUCT_BUG", 0)
        auto_issues  = cls_summary.get("AUTOMATION_ISSUE", 0)

        trend_icon = {"IMPROVING": ":chart_with_upwards_trend:",
                      "DECLINING": ":chart_with_downwards_trend:"}.get(trend, ":bar_chart:")

        lines = [
            f":white_check_mark: *QA Analysis Complete* — `{build_tag}`",
            f"Pass Rate: {pass_rate:.1f}% ({total - failed}/{total}) {trend_icon} {trend}",
            f"Product Bugs: {product_bugs} | Automation Issues: {auto_issues}",
        ]
        if handoff_path:
            n = len(classify_data.get("classifications", []))
            # Count eligible specifically
            eligible_count = sum(
                1 for c in classify_data.get("classifications", [])
                if c.get("classification") == "AUTOMATION_ISSUE"
                and c.get("confidence") == "HIGH"
                and c.get("root_cause_category") in actionable
                and c.get("root_cause_category") not in diagnosis.STOP
            )
            lines.append(f":wrench: {eligible_count} automation issue(s) queued for test-healing-agent")
        else:
            lines.append("No automation issues queued for autofix")
        if report_path:
            lines.append(f"Report: `{Path(report_path).name}`")

        slack_notified = send_slack(SLACK_NOTIFY_CHANNEL, "\n".join(lines))

    # ── Record processed build tag ────────────────────────────────────────────
    record_skip(build_tag, verdict)

    # ── Write JSON ─────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "timestamp":             ts,
        "build_tag":             build_tag,
        "verdict":               verdict,
        "escalated":             escalated,
        "report_path":           report_path,
        "handoff_queued":        handoff_path is not None,
        "handoff_path":          handoff_path,
        "slack_notified":        slack_notified,
        "summary":               collect_data.get("summary", {}),
        "classification_summary": cls_summary,
    }
    (AUDIT_DIR / "05-ship.json").write_text(json.dumps(result, indent=2))

    # ── Write Markdown ─────────────────────────────────────────────────────────
    summary_obj = collect_data.get("summary", {})
    md_lines = [
        "# Ship Results",
        "",
        f"**Build Tag:** {build_tag}  ",
        f"**Timestamp:** {ts}  ",
        f"**Verdict:** {verdict}  ",
        f"**Escalated:** {escalated}",
        "",
        "## Outcome",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Pass Rate | {summary_obj.get('pass_rate', 0):.1f}% |",
        f"| Total Tests | {summary_obj.get('total', 0)} |",
        f"| Failures | {summary_obj.get('failed', 0) + summary_obj.get('errors', 0)} |",
        f"| Product Bugs | {cls_summary.get('PRODUCT_BUG', 0)} |",
        f"| Automation Issues | {cls_summary.get('AUTOMATION_ISSUE', 0)} |",
        f"| HTML Report | {report_path or 'Not generated'} |",
        f"| Handoff Queued | {'Yes — ' + str(handoff_path) if handoff_path else 'No'} |",
        f"| Slack | {'Sent' if slack_notified else 'Skipped'} |",
    ]
    if escalated:
        md_lines += [
            "",
            "## Escalation",
            "",
            "Classifications were challenged by the reviewer — human review required.",
            f"Check `{AUDIT_DIR.name}/04-review-r*.md` for details.",
        ]
    if handoff_path:
        md_lines += [
            "",
            "## Handoff",
            "",
            f"Queued for test-healing-agent: `{handoff_path}`",
            "Run: `make run AGENT=test-healing-agent`",
        ]

    (AUDIT_DIR / "05-ship.md").write_text("\n".join(md_lines) + "\n")

    status = "ESCALATED" if escalated else ("HANDOFF_QUEUED" if handoff_path else "REPORT_ONLY")
    log(f"Done — status={status}")
    if report_path:
        log(f"Report: {report_path}")
    if handoff_path:
        log(f"Handoff: {handoff_path}")


if __name__ == "__main__":
    main()
