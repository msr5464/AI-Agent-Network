# Auto-Fix: Test Selection and Handoff

This document explains how `qa-auto-analyse` selects which failures to queue for `qa-auto-fix`, and how the handoff file works.

---

## Selection Criteria

`qa-auto-analyse` automatically filters failures using three hard criteria. A test is queued for autofix only when **all three** are true:

| Criterion | Required value | Where set |
|-----------|---------------|-----------|
| Classification | `AUTOMATION_ISSUE` | Claude's classification in step 03 |
| Confidence | `HIGH` | Claude's confidence score in step 03 |
| Root cause category | `ELEMENT_NOT_FOUND` | Rule engine + Claude in step 03 |

Tests that are `PRODUCT_BUG`, `UNKNOWN`, `MEDIUM`/`LOW` confidence, `TIMEOUT`, or `ASSERTION_FAILURE` are reported in the HTML report but are **not** queued.

This is intentional: the autofix agent only has a good chance of success on locator issues where we are highly confident the automation code is wrong, not the product.

---

## What a Queued Test Looks Like

Each queued test in the handoff file (`agents/qa-auto-fix/queue/<build_tag>.json`) contains:

```json
{
  "test_name": "Automation.Access.login.web.customer.TestLoginFlows.testLogin",
  "classification": "AUTOMATION_ISSUE",
  "confidence": "HIGH",
  "root_cause_category": "ELEMENT_NOT_FOUND",
  "root_cause": "NoSuchElementException on #submit-btn",
  "failure_signature": "NoSuchElementException: #submit-btn",
  "recommended_action": "Update locator",
  "error_type": "NoSuchElementException",
  "error_message": "Unable to locate element: {\"method\":\"css selector\",\"selector\":\"#submit-btn\"}",
  "stack_trace": "...",
  "execution_log": "...",
  "class_name": "Automation.Access.login.web.customer.TestLoginFlows",
  "method_name": "testLogin"
}
```

The fix agent uses `error_message`, `stack_trace`, `execution_log`, `class_name`, and `method_name` to locate the right file and generate the fix.

---

## When Is the Handoff Written?

`05_ship.py` (the final step of `qa-auto-analyse`) writes the handoff **only when**:
1. The review verdict is `APPROVED` (not `NEEDS-HUMAN`)
2. At least one failure passes all three selection criteria

If the build had only product bugs, or all automation issues were LOW/MEDIUM confidence, no handoff file is written. The HTML report is still generated and sent to Slack.

---

## Adjusting Selection Criteria

The filter is applied in `agents/qa-auto-analyse/actions/05_ship.py`:

```python
eligible = [
    c for c in classifications
    if c.get("classification") == "AUTOMATION_ISSUE"
    and c.get("confidence") == "HIGH"
    and c.get("root_cause_category") == "ELEMENT_NOT_FOUND"
]
```

To include `TIMEOUT` issues, add it to the category filter. To include `MEDIUM` confidence, adjust the confidence check. Changes here affect what goes into the handoff — the fix agent processes whatever it receives.

---

## Manually Creating or Editing a Handoff

If you want to run `qa-auto-fix` on a specific set of tests, you can create a handoff file manually:

```bash
cat > agents/qa-auto-fix/queue/MyBuild-541.json << 'EOF'
{
  "build_tag": "MyBuild-541",
  "created_at": "2026-03-29T10:00:00Z",
  "source_session": "manual",
  "source_audit_dir": "",
  "automation_issues": [
    {
      "test_name": "Automation.Foo.TestBar.testSomething",
      "classification": "AUTOMATION_ISSUE",
      "confidence": "HIGH",
      "root_cause_category": "ELEMENT_NOT_FOUND",
      "root_cause": "NoSuchElementException on .my-button",
      "error_type": "NoSuchElementException",
      "error_message": "Unable to locate element: .my-button",
      "stack_trace": "",
      "execution_log": "",
      "class_name": "Automation.Foo.TestBar",
      "method_name": "testSomething"
    }
  ]
}
EOF
```

Then run:
```bash
./scripts/run-autofix.sh agents/qa-auto-fix/queue/MyBuild-541.json
```

---

## Queue State

| Location | Meaning |
|----------|---------|
| `agents/qa-auto-fix/queue/*.json` | Pending — not yet processed |
| `agents/qa-auto-fix/queue/processed/*.json` | Done — moved after successful run |

A handoff file stays in `queue/processed/` even if the fixes failed — check the audit trail for results.
