#!/usr/bin/env python3
"""
Step 04 — Review
Independent adversarial review of classifications by a separate Claude session.
Multi-round debate: reviewer challenges, classifier rebuts, reviewer decides.
Outputs: audit/<session>/.verdict (APPROVED or NEEDS-HUMAN) + 04-review-r{N}.md

Uses Claude CLI — second independent call. No shared context with classify step.
No DB queries. No code changes.
"""

import os, sys, json, subprocess, re
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from log_utils import log as _log
def log(msg): _log("review", msg)

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[3]))
MAX_ROUNDS = int(os.environ.get("MAX_REVIEW_ROUNDS", "2"))

CLAUDE_CLI = os.environ.get("CLAUDE_CLI_PATH", "claude")
REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "claude-sonnet-4-6")
REVIEWER_EFFORT = os.environ.get("REVIEWER_EFFORT", "medium")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(filename):
    path = AUDIT_DIR / filename
    if not path.exists():
        log(f"ERROR: {filename} not found")
        sys.exit(1)
    return json.loads(path.read_text())


def read_file(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def call_claude(prompt: str) -> str:
    result = subprocess.run(
        [CLAUDE_CLI, "-p", prompt, "--model", REVIEWER_MODEL],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        log(f"Claude CLI error (exit {result.returncode}): {result.stderr[:500]}")
        return ""
    return result.stdout


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

    return f"""You are a senior QA lead performing an independent review of AI-generated test failure classifications.
You were NOT part of the original classification — you have zero context bias.

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

## What to Check

1. **AUTOMATION_ISSUE sanity**: Are element-not-found errors genuinely locator issues, or could they be
   symptoms of a page that failed to load (which is a PRODUCT_BUG)?

2. **PRODUCT_BUG sanity**: Is this assertion failure due to a real app defect, or could it be a timing
   issue in the test (AUTOMATION_ISSUE)?

3. **HIGH confidence accuracy**: Are HIGH-confidence classifications actually unambiguous?
   If a HIGH-confidence classification is wrong, it will trigger an auto-fix attempt — this is costly.

4. **Pattern clustering**: Are multiple tests failing with the same root cause but classified differently?

5. **UNKNOWN classifications**: Should any UNKNOWN be reclassified based on the error type visible?

## Your Response Format

Start with a structured challenge section, then give your verdict:

### Challenge
List any classifications you disagree with or want to challenge, with reasoning:
- `<test_name>` — classified as X, should be Y because: <reason>

If you agree with all classifications, state "No challenges — classifications look correct."

### Verdict
End your response with EXACTLY one of these lines:
- `VERDICT: APPROVED` — if you are satisfied with the overall quality (minor disagreements ok)
- `VERDICT: NEEDS-HUMAN` — if you found significant systematic errors that would mislead engineering

Use NEEDS-HUMAN if: >20% of classifications seem wrong, or if any HIGH-confidence AUTOMATION_ISSUE
looks like it could be a PRODUCT_BUG.
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
