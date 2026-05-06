# Review Prompt — test-triaging-agent

Template for `agents/test-triaging-agent/actions/04_review.py → build_reviewer_prompt()`.
The static sections below are loaded at runtime; dynamic context (classifications, build tag) is injected by Python.

---

## What to Check

1. **AUTOMATION_ISSUE sanity**: Are element-not-found errors genuinely locator issues, or could they be
   symptoms of a page that failed to load (which is a PRODUCT_BUG)?

2. **PRODUCT_BUG sanity**: Is this assertion failure due to a real app defect, or could it be a timing
   issue in the test (AUTOMATION_ISSUE)?

3. **HIGH confidence accuracy**: Are HIGH-confidence classifications actually unambiguous?
   If a HIGH-confidence classification is wrong, it will trigger an auto-fix attempt — this is costly.

4. **Pattern clustering**: Are multiple tests failing with the same root cause but classified differently?

5. **UNKNOWN classifications**: Should any UNKNOWN be reclassified based on the error type visible?

---

## Self-Resolving Checklist (before issuing NEEDS-HUMAN)

Before emitting `VERDICT: NEEDS-HUMAN`, you MUST exhaustively try the following:

1. Re-read the full failure output and stack trace for each disputed classification
2. Check if the error pattern appears consistently across multiple test runs (flaky vs systematic)
3. Check sibling tests in the same test class — if others pass, the failure is likely isolated (automation issue)
4. Check historical context: if the same test has been classified before, is this consistent?
5. Check `ENVIRONMENT_ISSUE` — API 500s, network timeouts, and server connectivity issues are NOT fixable by locator changes; never classify them as AUTOMATION_ISSUE

Only emit `VERDICT: NEEDS-HUMAN` if after all 5 checks you still have systematic errors affecting >20%
of classifications, or any HIGH-confidence AUTOMATION_ISSUE that looks like it could be a PRODUCT_BUG.

---

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
