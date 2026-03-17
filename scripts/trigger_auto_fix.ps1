# Auto-fix only: skip report generation, enable auto-fix.
# Usage: .\scripts\trigger_auto_fix.ps1 -InputDir <report_dir> [-OutputDir ... -TableName ...] [-AutofixTests "Test1,Test2"] [-AutofixTestsFile path\to\file.txt]
# <report_dir> must point to the already generated report folder (same path you used for generate_report.ps1)
# -AutofixTests: Comma-separated list of test names to fix (optional)
# -AutofixTestsFile: Path to file with test names, one per line (optional)

param(
    [string]$InputDir = "testdata\ProdSanity-All-Tests-535",
    [string]$OutputDir = "reports",
    [string]$TableName = "",
    [string]$AutofixTests = "",
    [string]$AutofixTestsFile = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = 'SilentlyContinue'

# Get script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Set-Location $ProjectRoot

# Activate virtual environment
if (Test-Path "venv\Scripts\Activate.ps1") {
    & "venv\Scripts\Activate.ps1"
} else {
    Write-Host "❌ venv directory not found. Please create a virtual environment first." -ForegroundColor Red
    exit 1
}

# Check and install browser dependencies if needed
$seleniumInstalled = python -c "import selenium" 2>$null
$chromedriverInstalled = Get-Command chromedriver -ErrorAction SilentlyContinue

if (-not $seleniumInstalled -or -not $chromedriverInstalled) {
    Write-Host "📦 Installing browser dependencies (Selenium & ChromeDriver)..." -ForegroundColor Cyan
    & "$ScriptDir\install_browser_deps.ps1"
}

# Ensure auto-fix is on unless explicitly set otherwise
if (-not $env:AUTO_FIX_ENABLED) {
    $env:AUTO_FIX_ENABLED = "true"
}

# Build arguments
$args = @("--skip-report")
if ($InputDir) {
    $args += "--input-dir", $InputDir
}
if ($OutputDir) {
    $args += "--output-dir", $OutputDir
}
if ($TableName) {
    $args += "--table-name", $TableName
}
if ($AutofixTests) {
    $args += "--autofix-tests", $AutofixTests
}
if ($AutofixTestsFile) {
    $args += "--autofix-tests-file", $AutofixTestsFile
}

python src/main.py $args
