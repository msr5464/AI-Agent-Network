# Auto-Fix Guide: Running Self-Healing Locator Fixes

This guide explains how to run the auto-fix system to automatically fix test failures with `ELEMENT_NOT_FOUND` or `TIMEOUT` issues using browser-based locator discovery.

## Prerequisites

### Automatic Installation (Recommended)

The scripts will automatically install all dependencies when you run auto-fix. No manual installation needed!

**macOS/Linux:**
```bash
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
# Dependencies (Selenium & ChromeDriver) will be installed automatically if missing
```

**Windows:**
```powershell
.\scripts\trigger_auto_fix.ps1 -InputDir Regression-Frs-Tests-249
# Dependencies (Selenium & ChromeDriver) will be installed automatically if missing
```

### Manual Installation (Optional)

If you prefer to install dependencies manually:

**macOS/Linux:**
```bash
./scripts/install_browser_deps.sh
```

**Windows:**
```powershell
.\scripts\install_browser_deps.ps1
```

**What gets installed:**
- Selenium Python package (via pip)
- ChromeDriver (via package manager or manual download)
- Chrome browser detection (will warn if not found)

### Configure Environment Variables

Ensure your `config/.env` file has the following variables set:

```bash
# Enable auto-fix
AUTO_FIX_ENABLED=true

# Set to false to actually create PRs (true = dry run, no PRs created)
AUTO_FIX_DRY_RUN=false

# Maximum number of fixes per run
AUTO_FIX_MAX_FIXES_PER_RUN=5

# GitHub configuration (required for PR creation)
GITHUB_TOKEN=your_github_token_here
GITHUB_ORG=your_org_name
GITHUB_REPO_AUTOMATION=your_automation_repo_name
GITHUB_DEFAULT_BRANCH=main

# LLM configuration
OPENAI_API_KEY=your_openai_key_here
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-4o-mini

# Optional: Override environment detection
AUTO_FIX_ENV_OVERRIDE=qa-1  # Optional: force specific environment
```

## Running Auto-Fix

### Method 1: Using the Trigger Script (Recommended)

**macOS/Linux:**
```bash
# Run auto-fix on a specific report (dependencies auto-installed)
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249 --output-dir reports

# With custom table name
./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249 --table-name results_frs

# With environment override
AUTO_FIX_ENV_OVERRIDE=qa-1 ./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
```

**Windows:**
```powershell
# Run auto-fix on a specific report (dependencies auto-installed)
.\scripts\trigger_auto_fix.ps1 -InputDir Regression-Frs-Tests-249 -OutputDir reports

# With custom table name
.\scripts\trigger_auto_fix.ps1 -InputDir Regression-Frs-Tests-249 -TableName results_frs
```

### Method 2: Direct Python Command

**macOS/Linux:**
```bash
source venv/bin/activate

# Enable auto-fix and run
export AUTO_FIX_ENABLED=true
export AUTO_FIX_DRY_RUN=false

python3 src/main.py \
  --input-dir Regression-Frs-Tests-249 \
  --output-dir reports \
  --table-name results_frs \
  --skip-report
```

**Windows:**
```powershell
venv\Scripts\Activate.ps1

$env:AUTO_FIX_ENABLED = "true"
$env:AUTO_FIX_DRY_RUN = "false"

python src/main.py --input-dir Regression-Frs-Tests-249 --output-dir reports --table-name results_frs --skip-report
```

### Method 3: Dry Run (Test Without Creating PRs)

**macOS/Linux:**
```bash
# Set dry run mode
export AUTO_FIX_DRY_RUN=true
export AUTO_FIX_ENABLED=true

./scripts/trigger_auto_fix.sh --input-dir Regression-Frs-Tests-249
```

**Windows:**
```powershell
$env:AUTO_FIX_DRY_RUN = "true"
$env:AUTO_FIX_ENABLED = "true"

.\scripts\trigger_auto_fix.ps1 -InputDir Regression-Frs-Tests-249
```

## How It Works

### Step-by-Step Process

1. **Report Analysis**
   - The system reads the test report (from `--input-dir`)
   - Identifies failures classified as `AUTOMATION_ISSUE` with `HIGH` or `MEDIUM` confidence
   - Filters for `ELEMENT_NOT_FOUND` or `TIMEOUT` root cause categories

2. **Browser Inspection** (for ELEMENT_NOT_FOUND/TIMEOUT only)
   - Extracts page URL from execution logs
   - Extracts element name from root cause (e.g., "Block Reason PopUp Header")
   - Opens headless Chrome browser
   - Navigates to the page
   - Discovers elements matching the element name using multiple strategies:
     - Text matching (exact and partial)
     - ID, name, class attributes
     - Data attributes (`data-cy`, `data-testid`)
     - ARIA labels
   - Generates candidate locators with confidence scores

3. **Fix Generation**
   - LLM receives:
     - Original failing test code
     - Stack traces and file snippets
     - Page object code
     - **Discovered locators from browser**
   - LLM generates fix:
     - Updates test code if needed
     - Updates page object with new locator
     - Creates `additional_changes` for page object updates

4. **Verification**
   - Runs the test locally to verify the fix
   - If it passes, proceeds to PR creation
   - If it fails, retries with additional context (up to 2 attempts)

5. **PR Creation** (if not dry run)
   - Creates a new branch: `auto-fix/testMethodName`
   - Commits the changes
   - Pushes to GitHub
   - Creates a Pull Request with reviewers

## What Gets Fixed

The auto-fix system will automatically:

✅ **Update Page Object Locators**
   - When an element is not found, discovers new locators from the browser
   - Updates `@FindBy` annotations in page object files
   - Example: Changes `@FindBy(css = "old-selector")` to `@FindBy(css = "[data-cy='new-selector']")`

✅ **Fix Test Code**
   - Updates test methods to use correct element references
   - Fixes timing issues
   - Adjusts wait conditions

✅ **Update Related Files**
   - Can update helper classes if needed
   - Can update configuration if environment-specific

## Example Output

When running auto-fix, you'll see logs like:

```
🔧 Auto-fix: attempting up to 5 fixes (dry_run=false)
Processing auto-fix for: TestDashMobileDevices.testReasonPopUpOnBlockingDevice
🏃 Running test locally to capture fresh logs: Automation.Access.Frs.web.customer.TestDashMobileDevices.testReasonPopUpOnBlockingDevice
🔍 Discovering locators for element 'Block Reason PopUp Header' on page: https://qa-1-dash.your-app.example.com/people/...
Discovered 3 locator candidates from browser
Found 1 page object files for locators: ['src/main/java/Automation/Access/dash/web/DashPeopleDetailsPage.java']
Generating fix for: TestDashMobileDevices.testReasonPopUpOnBlockingDevice
✅ Verification passed for Automation.Access.Frs.web.customer.TestDashMobileDevices.testReasonPopUpOnBlockingDevice
Created branch: auto-fix/testReasonPopUpOnBlockingDevice
Created PR: https://github.com/your-org/your-repo/pull/123
🔧 Auto-fix completed: 1 succeeded, 0 skipped, 0 failed
```

## Troubleshooting

### Issue: "selenium not installed"
**Solution:** 
- Run `./scripts/install_browser_deps.sh` (macOS/Linux) or `.\scripts\install_browser_deps.ps1` (Windows)
- Or manually: `pip install selenium>=4.15.0`

### Issue: "ChromeDriver not found"
**Solution:** 
- Run `./scripts/install_browser_deps.sh` (macOS/Linux) or `.\scripts\install_browser_deps.ps1` (Windows)
- The script will automatically install ChromeDriver
- Or install manually:
  - macOS: `brew install chromedriver`
  - Linux: `sudo apt-get install chromium-chromedriver`
  - Windows: `choco install chromedriver`

### Issue: "No page URL found in execution logs"
**Solution:** Ensure the test execution logs contain "Page URL:- https://..." format. This is automatically logged by the test framework.

### Issue: "Failed to initialize browser inspector"
**Solution:** 
- Check ChromeDriver installation: `chromedriver --version`
- Ensure Chrome browser is installed
- Try running with visible browser (modify `browser_inspector.py` to set `headless=False` for debugging)

### Issue: "No auto-fixable classifications found"
**Solution:** 
- Ensure failures are classified as `AUTOMATION_ISSUE` (not `PRODUCT_BUG`)
- Check confidence level is `HIGH` or `MEDIUM`
- Verify root cause category is `ELEMENT_NOT_FOUND` or `TIMEOUT`

### Issue: "GitHub configuration is missing"
**Solution:** Set `GITHUB_TOKEN`, `GITHUB_ORG`, and `GITHUB_REPO_AUTOMATION` in `config/.env`

## Best Practices

1. **Start with Dry Run**: Always test with `AUTO_FIX_DRY_RUN=true` first to see what would be fixed
2. **Limit Fixes**: Use `AUTO_FIX_MAX_FIXES_PER_RUN=1` initially to test one fix at a time
3. **Review PRs**: Always review the generated PRs before merging
4. **Monitor Logs**: Check the logs to see which locators were discovered and why
5. **Verify Locators**: The discovered locators are suggestions - verify they're correct before merging

## Advanced Configuration

### Custom Browser Options

Edit `src/auto_fix/browser_inspector.py` to customize:
- Headless mode: `headless=True/False`
- Timeout: `timeout=10` (seconds)
- Browser window size
- Additional Chrome options

### Filtering Which Tests to Fix

The system automatically filters:
- Only `AUTOMATION_ISSUE` classifications
- Only `HIGH` or `MEDIUM` confidence
- Only `ELEMENT_NOT_FOUND` or `TIMEOUT` categories

To change this, modify `src/main.py` in the `_to_autofixable` function.

## Troubleshooting

### "Table 'thanos.results_&lt;name&gt;' doesn't exist"

If the agent fails with a message that the results table doesn't exist (e.g. `Table 'thanos.results_frs' doesn't exist`):

1. **Upload test results first.** The table is created and populated by your test run (e.g. the step that uploads results to the database). Run that step for the same build (e.g. `Regression-Frs-Tests-266`) before running the QA-AI-Agent.
2. **Check DB and table name.** Verify `DB_NAME` and `DB_HOST` in `config/.env`. If you pass `--table-name results_frs`, ensure that table exists in that database (same name your upload job uses).
3. **Table naming.** Tables are usually `results_<project>` in lowercase (e.g. `results_frs` for FRS). The agent will now show a clear error instead of a full traceback when the table is missing.

## Support

For issues or questions:
1. Check the logs in `logs/qa_ai_agent.log`
2. Review the generated PR to see what changes were made
3. Check browser inspector logs for locator discovery details
