<#
.SYNOPSIS
    Run the qa-auto-fix agent (Windows).

.DESCRIPTION
    Usage:
      .\scripts\run-autofix.ps1                                          # queue mode: picks oldest
      .\scripts\run-autofix.ps1 -BuildTag ProdSanity-541                 # direct: by build tag
      .\scripts\run-autofix.ps1 -HandoffFile C:\path\to\handoff.json     # direct: by file path
      $env:AUTO_PUSH="false"; .\scripts\run-autofix.ps1                  # dry-run (no PR)
#>

[CmdletBinding()]
param(
    [string]$BuildTag    = "",
    [string]$HandoffFile = ""
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

if ($HandoffFile) {
    # Resolve to absolute path
    $HandoffFile = (Resolve-Path $HandoffFile).Path
    $env:HANDOFF_FILE = $HandoffFile
    # Extract build tag from file if not supplied
    if (-not $BuildTag) {
        $BuildTag = python -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" $HandoffFile
    }
}

if ($BuildTag) { $env:BUILD_TAG = $BuildTag }

make run AGENT=qa-auto-fix BUILD_TAG="$BuildTag"
exit $LASTEXITCODE
