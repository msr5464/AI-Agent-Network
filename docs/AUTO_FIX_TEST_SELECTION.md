# Auto-Fix Test Selection and Session Tracking

This guide explains how to select specific test cases for auto-fix and how the system tracks tests that pass locally.

## Test Case Selection

You can now select specific test cases to auto-fix instead of processing all auto-fixable failures.

### Method 1: Comma-Separated List

**macOS/Linux:**
```bash
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests "TestDashMobileDevices.testReasonPopUpOnBlockingDevice,TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername"
```

**Windows:**
```powershell
.\scripts\trigger_auto_fix.ps1 `
  -InputDir Regression-Frs-Tests-249 `
  -AutofixTests "TestDashMobileDevices.testReasonPopUpOnBlockingDevice,TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername"
```

### Method 2: Auto-Generated File (Automatic - Recommended)

When you generate a report, it automatically creates a file with all auto-fixable test names:

```
reports/autofix_tests_Regression-Frs-Tests-249.txt
```

This file contains all test names that are:
- Classified as `AUTOMATION_ISSUE` (not `PRODUCT_BUG`)
- Have `HIGH` or `MEDIUM` confidence

**The auto-fix script automatically detects and uses this file!** No need to specify it manually.

**macOS/Linux:**
```bash
# Step 1: Generate the report (this creates the autofix tests file)
./scripts/generate_report.sh --input-dir Regression-Frs-Tests-249

# Step 2: Run auto-fix (automatically uses the generated file)
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
# ✅ No need to specify --autofix-tests-file - it's auto-detected!
```

**Windows:**
```powershell
# Step 1: Generate the report (this creates the autofix tests file)
.\scripts\generate_report.ps1 -InputDir Regression-Frs-Tests-249

# Step 2: Run auto-fix (automatically uses the generated file)
.\scripts\trigger_auto_fix.ps1 -InputDir Regression-Frs-Tests-249
# ✅ No need to specify -AutofixTestsFile - it's auto-detected!
```

**Note:** You can still manually specify a different file if needed:
```bash
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests-file custom_tests.txt  # Override auto-detection
```

### Method 3: From Custom File

You can also create your own file `tests_to_fix.txt`:
```
TestDashMobileDevices.testReasonPopUpOnBlockingDevice
TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername
TestPushNotificationFlows.testAccountBlockingAfterConsecutiveLoginRequestDenials
```

**macOS/Linux:**
```bash
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests-file tests_to_fix.txt
```

**Windows:**
```powershell
.\scripts\trigger_auto_fix.ps1 `
  -InputDir Regression-Frs-Tests-249 `
  -AutofixTestsFile tests_to_fix.txt
```

### Test Name Formats

The system supports multiple test name formats:
- Full qualified name: `Automation.Access.Frs.web.customer.TestDashMobileDevices.testReasonPopUpOnBlockingDevice`
- Class.method: `TestDashMobileDevices.testReasonPopUpOnBlockingDevice`
- Method only: `testReasonPopUpOnBlockingDevice` (matches any test with this method name)

The system will match tests flexibly, so you can use any format that uniquely identifies the test.

## Session Tracking (Passed Tests)

The system automatically tracks tests that **pass locally** during auto-fix and skips them in subsequent runs for the same report.

### How It Works

1. **First Run**: When you run auto-fix, the system:
   - Runs each test locally to capture fresh logs
   - If a test **passes locally**, it's marked as "passed" and skipped from fix generation
   - The passed test is saved to a session file: `.autofix_session_{report_name}.json`

2. **Subsequent Runs**: When you run auto-fix again on the same report:
   - The system loads the session file
   - Tests that passed in previous runs are automatically skipped
   - Only tests that failed locally are processed

### Session File Location

Session files are stored in the output directory:
```
reports/.autofix_session_Regression-Frs-Tests-249.json
```

### Session File Format

```json
{
  "passed_tests": [
    "TestDashMobileDevices.testReasonPopUpOnBlockingDevice",
    "TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername"
  ],
  "last_updated": "2026-01-25T20:30:00.123456"
}
```

### Clearing Session Data

To reset the session and process all tests again:

**macOS/Linux:**
```bash
rm reports/.autofix_session_Regression-Frs-Tests-249.json
```

**Windows:**
```powershell
Remove-Item reports\.autofix_session_Regression-Frs-Tests-249.json
```

## Auto-Generated Tests File

When you generate a report, a file is automatically created containing all auto-fixable test names:

**File Location:**
```
reports/autofix_tests_{report_name}.txt
```

**File Format:**
```
# Auto-fixable test names (AUTOMATION_ISSUE with HIGH/MEDIUM confidence)
# Generated from report: Regression-Frs-Tests-249
# Total tests: 4
# Use with: --autofix-tests-file <this-file>
#
TestDashMobileDevices.testReasonPopUpOnBlockingDevice
TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername
TestPushNotificationFlows.testAccountBlockingAfterConsecutiveLoginRequestDenials
TestPasswordChangeFlows.testResetPasswordGoThroughCaptcha
```

**Benefits:**
- ✅ No manual file creation needed
- ✅ Always up-to-date with current failures
- ✅ Only includes auto-fixable tests (excludes product bugs)
- ✅ Ready to use immediately after report generation

## Example Workflow

### Scenario: Fix specific tests (override auto-detection)

If you want to fix only specific tests instead of all auto-fixable ones:

1. **Generate report:**
```bash
./scripts/generate_report.sh --input-dir Regression-Frs-Tests-249
```

2. **Run auto-fix with specific tests (overrides auto-detection):**
```bash
# Option A: Comma-separated list
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests "TestDashMobileDevices.testReasonPopUpOnBlockingDevice,TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername"

# Option B: Custom file
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests-file my_custom_tests.txt
```

### Scenario: Fix all auto-fixable tests (Simplest)

1. **Generate report (creates autofix tests file automatically):**
```bash
./scripts/generate_report.sh --input-dir Regression-Frs-Tests-249
```

2. **Run auto-fix (automatically uses the generated file):**
```bash
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
# ✅ Automatically uses reports/autofix_tests_Regression-Frs-Tests-249.txt
```

### Scenario: Fix specific tests and track progress

1. **First run - select specific tests:**
```bash
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests "TestDashMobileDevices.testReasonPopUpOnBlockingDevice,TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername"
```

Output:
```
🔧 Auto-fix: attempting up to 5 fixes
📋 Loaded 0 passed tests from session (will be skipped)
Found 2 auto-fixable failures
🏃 Running test locally: TestDashMobileDevices.testReasonPopUpOnBlockingDevice
✅ Test passed locally! Skipping fix generation
🏃 Running test locally: TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername
Test failed locally as expected. Capturing logs.
...
🔧 Auto-fix completed: 1 succeeded, 1 skipped, 0 failed
```

2. **Second run - same tests (one already passed):**
```bash
./scripts/trigger_auto_fix.sh \
  --input-dir Regression-Frs-Tests-249 \
  --autofix-tests "TestDashMobileDevices.testReasonPopUpOnBlockingDevice,TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername"
```

Output:
```
🔧 Auto-fix: attempting up to 5 fixes
📋 Loaded 1 passed tests from session (will be skipped)
⏭️  Skipping 1 tests that passed locally in previous runs
Found 1 auto-fixable failures (1 filtered out)
🏃 Running test locally: TestLoginFlows.testLoginWith3IncorrectPasswordAttemptsWithDifferentUsername
...
```

### Scenario: Process all tests, track passed ones

1. **First run - all auto-fixable tests:**
```bash
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
```

2. **Second run - only failed tests are processed:**
```bash
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
# Tests that passed in first run are automatically skipped
```

## Benefits

✅ **Selective Fixing**: Fix only the tests you want, not all failures
✅ **Progress Tracking**: Don't waste time re-processing tests that already pass
✅ **Incremental Work**: Run auto-fix multiple times, only new failures are processed
✅ **Session Persistence**: Session data persists across runs for the same report

## Notes

- Session files are **per report** - each report has its own session file
- Tests are tracked by their full test name (as classified)
- If you delete the session file, all tests will be processed again
- Tests that pass locally are skipped even if they were in the original failure list
- Session tracking works with both selected tests and all auto-fixable tests
