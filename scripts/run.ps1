<#
.SYNOPSIS
    Unified run script for the AI QA Agent (Windows).

.DESCRIPTION
    Usage examples:
      .\scripts\run.ps1
        Runs with default input directory (testdata/Regression-Growth-Tests-442)
        and output directory (reports).

      .\scripts\run.ps1 --input-dir testdata/Regression-Smoke-Tests-420 --output-dir custom-reports
        Runs against a custom input directory with a custom output directory.

      .\scripts\run.ps1 --table-name results_custom_project
        Runs with explicit database table name, overriding auto-detection.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"

# Set UTF-8 encoding for console output to support emoji characters
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$defaultArgs = @("--input-dir", "testdata/Regression-Growth-Tests-442", "--output-dir", "reports")
if (-not $Arguments -or $Arguments.Count -eq 0) {
    $Arguments = $defaultArgs
}

$venvPython = Join-Path "venv" "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Could not find $venvPython. Run scripts\windows\setup.ps1 first to create the virtual environment."
}

Write-Host "Using Python: $venvPython" -ForegroundColor Cyan
Write-Host "Running src/main.py $Arguments --skip-autofix" -ForegroundColor Cyan

& $venvPython "src/main.py" $Arguments --skip-autofix
exit $LASTEXITCODE

