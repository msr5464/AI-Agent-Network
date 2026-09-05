#!/usr/bin/env python3
"""
Step 04 — Review
Independent adversarial review of classifications by a separate Claude session.
Multi-round debate: reviewer challenges, classifier rebuts, reviewer decides.
Outputs: audit/<session>/.verdict (APPROVED or NEEDS-HUMAN) + 04-review-r{N}.md

Uses Claude CLI — second independent call. No shared context with classify step.
No DB queries. No code changes.
"""

import os, sys, json, re
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # agent dir → lib.*

from shared.log import log as _log
def log(msg): _log("review", msg)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
MAX_ROUNDS = int(os.environ.get("TRIAGING_MAX_REVIEW_ROUNDS", "2"))

TRIAGING_REVIEWER_MODEL = os.environ.get("TRIAGING_REVIEWER_MODEL", "claude-sonnet-4-6")
TRIAGING_REVIEWER_EFFORT = os.environ.get("TRIAGING_REVIEWER_EFFORT", "medium")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(filename):
    path = AUDIT_DIR / filename
    if not path.exists():
        log(f"ERROR: {filename} not found")
        sys.exit(1)
    return json.loads(path.read_text())


def read_file(path: Path) -> str:
    return path.read_text() if path.exists() else ""


from shared.claude import call_claude as _call_claude
def call_claude(prompt: str) -> str:
    output = _call_claude(prompt, TRIAGING_REVIEWER_MODEL, str(REPO_ROOT))
    if not output:
        log("Claude CLI returned empty response")
    return output


def extract_verdict(text: str) -> str:
    """Extract APPROVED or NEEDS-HUMAN from reviewer response."""
    text_upper = text.upper()
    if "APPROVED" in text_upper and "NEEDS-HUMAN" not in text_upper:
        return "APPROVED"
    if "NEEDS-HUMAN" in text_upper or "NEEDS_HUMAN" in text_upper:
        return "NEEDS-HUMAN"
    if "NEEDS HUMAN" in text_upper:
        return "NEEDS-HUMAN"
    # If ambiguous, default to safe escalation
    return "NEEDS-HUMAN"


def build_reviewer_prompt(classify_data: dict, round_num: int) -> str:
    """Build the reviewer prompt for a given round."""
    build_tag = classify_data.get("build_tag", "unknown")
    summary = classify_data.get("summary", {})
    category_breakdown = classify_data.get("category_breakdown", {})
    classifications = classify_data.get("classifications", [])

    # Sample classifications for review (max 30 to fit context)
    sample = classifications[:30]
    sample_text = json.dumps(sample, indent=2)

    # Some of these were measured rather than inferred: the diagnosis engine read
    # the captured DOM, the page object's own locator coverage and the network log.
    # A reviewer working from the error text alone cannot out-argue that, and
    # letting it try is how a correct verdict gets talked out of.
    measured = [c for c in sample if c.get("source") == "diagnosis"]
    measured_note = ""
    if measured:
        names = ", ".join(c["test_name"].split(".")[-1] for c in measured[:6])
        measured_note = f"""
## Measured, not inferred — {len(measured)} of these
{names}{" …" if len(measured) > 6 else ""}

These carry `"source": "diagnosis"`. They were decided by measurement, not by
reading the error message: how many of the page object's own locators were present
in the captured DOM, what the document request returned, whether the page was still
rendering. Challenge one only if you can point at evidence it contradicts — not
because the error text reads differently. Their `root_cause` lists what was measured.
"""

    # Include previous rounds if any
    prev_context = ""
    if round_num > 1:
        for r in range(1, round_num):
            challenge = read_file(AUDIT_DIR / f"04-review-r{r}.md")
            rebuttal = read_file(AUDIT_DIR / f"03-classifier-rebuttal-r{r}.md")
            if challenge:
                prev_context += f"\n## Round {r} — My Challenge\n{challenge[:2000]}\n"
            if rebuttal:
                prev_context += f"\n## Round {r} — Classifier Rebuttal\n{rebuttal[:2000]}\n"

    # Load static sections from prompt template (What to Check + Self-Resolving Checklist + Response Format)
    prompt_template = ""
    template_path = REPO_ROOT / "config" / "prompts" / "review.md"
    if template_path.exists():
        # Extract everything after the first --- separator (skip file header)
        raw = template_path.read_text()
        parts = raw.split("---\n", 1)
        prompt_template = parts[1] if len(parts) > 1 else raw

    return f"""You are a senior QA lead performing an independent review of AI-generated test failure classifications.
You were NOT part of the original classification — you have zero context bias.
{measured_note}

Your job: validate that PRODUCT_BUG vs AUTOMATION_ISSUE classifications are correct and actionable.
Focus on cases where the classification would mislead the engineering team.

## Build Tag: {build_tag}

## Classification Summary
{json.dumps(summary, indent=2)}

## Category Breakdown
{json.dumps(category_breakdown, indent=2)}

## Classifications to Review (sample of up to 30)
{sample_text}
{prev_context}

{prompt_template}
"""


def build_rebuttal_prompt(classify_data: dict, challenge: str) -> str:
    """Build the classifier's rebuttal prompt."""
    return f"""You are the original classifier defending your classifications to a reviewer.

## Reviewer's Challenge
{challenge[:3000]}

## Original Classification Data
Total failures: {classify_data.get('total_failures', 0)}
Summary: {json.dumps(classify_data.get('summary', {}), indent=2)}

## Instructions
1. Address each challenge point directly
2. Provide evidence from the error messages/stack traces for your reasoning
3. Concede any points where the reviewer is correct
4. Be honest — do not defend incorrect classifications just to win

Keep your rebuttal under 500 words. Be precise and factual.
"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    classify_data = load_json("03-classify.json")
    classifications = classify_data.get("classifications", [])

    if not classifications:
        log("No classifications to review — auto-approving empty result")
        (AUDIT_DIR / ".verdict").write_text("APPROVED")
        (AUDIT_DIR / "04-review-r1.md").write_text(
            "# Review\n\nNo classifications to review — auto-approved.\n\nVERDICT: APPROVED\n"
        )
        log("Verdict: APPROVED (no classifications)")
        return

    verdict = "NEEDS-HUMAN"  # Default to safe path

    for round_num in range(1, MAX_ROUNDS + 1):
        log(f"Review round {round_num}/{MAX_ROUNDS}...")

        # ── Reviewer challenge ─────────────────────────────────────────────────
        reviewer_prompt = build_reviewer_prompt(classify_data, round_num)
        challenge_response = call_claude(reviewer_prompt)

        if not challenge_response:
            log(f"Warning: empty reviewer response in round {round_num}")
            challenge_response = "No response from reviewer — defaulting to NEEDS-HUMAN"

        challenge_path = AUDIT_DIR / f"04-review-r{round_num}.md"
        challenge_path.write_text(
            f"# Review Round {round_num}\n\n{challenge_response}\n"
        )

        current_verdict = extract_verdict(challenge_response)
        log(f"Round {round_num} verdict: {current_verdict}")

        if current_verdict == "APPROVED":
            verdict = "APPROVED"
            break

        # ── If NEEDS-HUMAN and more rounds remain → classifier rebuts ─────────
        if round_num < MAX_ROUNDS:
            log(f"Reviewer challenged — generating classifier rebuttal...")
            rebuttal_prompt = build_rebuttal_prompt(classify_data, challenge_response)
            rebuttal_response = call_claude(rebuttal_prompt)

            rebuttal_path = AUDIT_DIR / f"03-classifier-rebuttal-r{round_num}.md"
            rebuttal_path.write_text(
                f"# Classifier Rebuttal — Round {round_num}\n\n{rebuttal_response}\n"
            )
            log(f"Wrote rebuttal for round {round_num}")

    # Final verdict — if we never got APPROVED, use the last round's verdict
    if verdict == "NEEDS-HUMAN":
        log("Final verdict: NEEDS-HUMAN (escalating to human review)")
    else:
        log("Final verdict: APPROVED")

    (AUDIT_DIR / ".verdict").write_text(verdict)

    # Write summary markdown
    summary_lines = [
        "# Review Summary",
        "",
        f"**Build Tag:** {classify_data.get('build_tag')}  ",
        f"**Rounds:** {min(round_num, MAX_ROUNDS)}  ",
        f"**Verdict:** {verdict}  ",
        f"**Total Classifications Reviewed:** {len(classifications)}",
        "",
        f"See 04-review-r1.md through 04-review-r{min(round_num, MAX_ROUNDS)}.md for full debate.",
        "",
    ]
    if verdict == "NEEDS-HUMAN":
        summary_lines += [
            "## Action Required",
            "",
            "The reviewer found issues with the classifications. A human QA lead should:",
            "1. Review `04-review-r*.md` for the specific concerns",
            "2. Manually review `03-classify.json` for the flagged tests",
            "3. Update classifications as needed before proceeding",
            "",
            "The ship step will escalate to Slack instead of creating a PR.",
        ]

    (AUDIT_DIR / "04-review-summary.md").write_text("\n".join(summary_lines) + "\n")
    log(f"Done — verdict={verdict}")


if __name__ == "__main__":
    main()
