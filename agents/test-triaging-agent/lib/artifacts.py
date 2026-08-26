"""Preserve the artefacts a failure produced, and reference them in its record.

The framework writes a DOM snapshot, a Playwright trace and a structured failure
context when a test fails, under a report directory CI will delete. Copying them
into the audit session is what keeps a handoff meaningful hours later.

This lives in `lib/` rather than inside a step because two steps need it, and the
order matters: collect has to attach these *before* anything classifies the
failure. Classification used to run two steps earlier than attachment, which meant
the classifier decided what kind of failure it was while structurally unable to
see the evidence — and its verdict is what gates the entire downstream pipeline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from shared.dom_snapshot import find_snapshot, parse_header
from shared.playwright_trace import read_actions, failing_action
from shared import failure_context as _failure_context


def attach_dom_snapshot(issue: dict, report_dir: Path, method_name: str,
                        audit_dir: Path, log=print) -> None:
    """Copy the failure-time DOM into this session and reference it in the issue.

    The framework writes it under the report dir on failure. Copying it into the
    audit session means the handoff stays valid after CI cleans up the report,
    and test-healing-agent then never has to reach the page itself — the DOM it
    needs was already captured in the right session, at the right step, with the
    right test data.
    """
    if not report_dir or not method_name:
        return
    try:
        snapshot = find_snapshot(report_dir, method_name)
        if not snapshot:
            return
        text = snapshot.read_text(encoding="utf-8", errors="ignore")
        dom_dir = audit_dir / "dom"
        dom_dir.mkdir(parents=True, exist_ok=True)
        preserved = dom_dir / f"{method_name}.html"
        preserved.write_text(text, encoding="utf-8")

        issue["dom_snapshot"] = str(preserved)
        issue["failure_url"] = parse_header(text).get("url", "")
        log(f"  DOM snapshot attached for {method_name} "
            f"({len(text) // 1024}KB) → {preserved.name}")
    except Exception as e:
        # Never let a missing artefact block the handoff — the healing agent
        # falls back to live inspection or static inference.
        log(f"  Could not attach DOM snapshot for {method_name}: {e}")


def attach_trace(issue: dict, report_dir: Path, method_name: str,
                 audit_dir: Path, log=print) -> None:
    """Copy the Playwright trace for this test into the session, if one exists.

    The trace names the selector that actually failed at runtime, which beats
    inferring it from an error message, and it keeps the whole flow available in
    Trace Viewer for whoever reviews the PR.
    """
    if not report_dir or not method_name:
        return
    try:
        traces = [p for p in Path(report_dir).rglob(f"traces/{method_name}_*.zip")]
        if not traces:
            return
        trace = max(traces, key=lambda p: p.stat().st_mtime)
        trace_dir = audit_dir / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        preserved = trace_dir / f"{method_name}.zip"
        preserved.write_bytes(trace.read_bytes())

        issue["trace_path"] = str(preserved)
        failed = failing_action(read_actions(preserved))
        if failed and failed.get("selector"):
            issue["failed_selector"] = failed["selector"]
            log(f"  Trace attached for {method_name} — failing selector: {failed['selector']}")
        else:
            log(f"  Trace attached for {method_name}")
    except Exception as e:
        log(f"  Could not attach trace for {method_name}: {e}")



def attach_failure_context(issue: dict, report_dir: Path, method_name: str,
                           audit_dir: Path, log=print) -> None:
    """Copy the framework's structured failure context, when it wrote one.

    This is what carries the readiness, DOM-volatility, anchor-count and live
    page-object coverage signals. Older framework versions write none, which only
    means the diagnosis has fewer channels to work from.
    """
    if not report_dir or not method_name:
        return
    try:
        found = _failure_context.find(report_dir, method_name)
        if not found:
            return
        context_dir = audit_dir / "dom"
        context_dir.mkdir(parents=True, exist_ok=True)
        preserved = context_dir / f"{method_name}.context.json"
        preserved.write_text(found.read_text(encoding="utf-8", errors="ignore"),
                             encoding="utf-8")
        issue["failure_context"] = str(preserved)
        log(f"  Failure context attached for {method_name}")
    except Exception as e:
        log(f"  Could not attach failure context for {method_name}: {e}")


def attach_all(issue: dict, report_dir: Path, method_name: str, audit_dir: Path,
               log=print) -> None:
    """Attach every artefact this failure produced."""
    attach_dom_snapshot(issue, report_dir, method_name, audit_dir, log)
    attach_trace(issue, report_dir, method_name, audit_dir, log)
    attach_failure_context(issue, report_dir, method_name, audit_dir, log)
