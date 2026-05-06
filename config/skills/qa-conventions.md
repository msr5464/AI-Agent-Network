# QA Agent Network — Classification Conventions

This file is loaded as a system prompt by test-triaging-agent classify and review steps.
It provides definitive classification rules so Claude's classification decisions are consistent.

---

## Classification Schema

Every test failure must be classified as one of:
- `PRODUCT_BUG` — the application is broken
- `AUTOMATION_ISSUE` — the test code is broken
- `UNKNOWN` — genuinely ambiguous; requires human review

---

## PRODUCT_BUG Indicators

A failure is a PRODUCT_BUG when:
- Assertion fails because the **application returned wrong data** (wrong value, wrong status, wrong count)
- API returns unexpected HTTP status codes (e.g., 4xx with wrong semantics, unexpected 5xx from app logic)
- OTP / authentication failures — always PRODUCT_BUG + ASSERTION_FAILURE category
- Feature not working correctly (buttons don't respond, workflows break)
- Data mismatch between what the app shows and what the test expects

**Key signal**: The test code is doing the right thing, but the app's behavior is wrong.

---

## AUTOMATION_ISSUE Indicators

A failure is an AUTOMATION_ISSUE when:
- `NoSuchElementException` — locator doesn't match any element in the DOM
- `ElementClickInterceptedException` — element is overlapped or not interactable
- `ElementNotInteractableException` — element exists but can't be interacted with
- `TimeoutException` — element or page took too long to appear
- Page load timeout: "PageName NOT loaded even after X seconds"
- `NullPointerException` in **test code** (not in application code)
- WebDriver session issues, Selenium framework errors
- `StaleElementReferenceException` — DOM changed between locating and using an element
- CSS/XPath locators that no longer match the current DOM structure

**Key signal**: The application itself is working, but the test code can't interact with it.

---

## Confidence Levels

| Level | Meaning | When to use |
|---|---|---|
| `HIGH` | Unambiguous — clear exception type with specific selector | NoSuchElementException with exact locator, OTP failure, assertion with exact expected vs actual |
| `MEDIUM` | Likely correct but could be either | TimeoutException (could be slow app or bad locator), assertion failure without clear context |
| `LOW` | Ambiguous — needs human review | Mixed signals, missing stack trace, environment noise |

**Important**: HIGH confidence AUTOMATION_ISSUE triggers auto-fix. Be conservative — only use HIGH
when you are certain the fix is a locator/code change, not a product defect.

---

## Root Cause Categories

| Category | Description | Error Examples |
|---|---|---|
| `ELEMENT_NOT_FOUND` | Locator doesn't match DOM | `NoSuchElementException`, `ElementNotFound` |
| `TIMEOUT` | Wait exceeded | `TimeoutException`, "NOT loaded after X seconds" |
| `ASSERTION_FAILURE` | Expected vs actual mismatch | `AssertionError`, `ComparisonFailure` |
| `ENVIRONMENT_ISSUE` | Infrastructure/API connectivity | `ConnectionRefused`, API 500, DB timeout |
| `CODE_ISSUE` | NPE or logic error in test code | `NullPointerException` in test stack frame |
| `OTHER` | Doesn't fit above categories | — |

---

## Handoff Criteria (what gets queued for auto-fix)

Only failures meeting ALL THREE criteria are queued for test-healing-agent:
1. `classification = AUTOMATION_ISSUE`
2. `confidence = HIGH`
3. `root_cause_category = ELEMENT_NOT_FOUND`

**Never queue:**
- PRODUCT_BUG (regardless of confidence)
- AUTOMATION_ISSUE + ENVIRONMENT_ISSUE (infra problem, not fixable by code change)
- Anything with confidence < HIGH

---

## Escalation Threshold

A batch of classifications should be escalated to `VERDICT: NEEDS-HUMAN` only when:
- More than 20% of classifications are disputed by the reviewer
- Any HIGH-confidence AUTOMATION_ISSUE appears to actually be a PRODUCT_BUG
- ENVIRONMENT_ISSUE failures are being classified as AUTOMATION_ISSUE (common mistake)

Minor disagreements (a few LOW or MEDIUM confidence reclassifications) do NOT warrant NEEDS-HUMAN.
