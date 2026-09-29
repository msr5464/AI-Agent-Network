<#
.SYNOPSIS
    Run the test-triaging-agent (Windows).

.DESCRIPTION
    Usage:
      .\scripts\run-triaging-agent.ps1                                          # scout mode, dirs from .env
      .\scripts\run-triaging-agent.ps1 -BuildTag ProdSanity-541                 # direct mode
      .\scripts\run-triaging-agent.ps1 -BuildTag ProdSanity-541 -InputDir testdata -OutputDir reports
      $env:STOP_AFTER="classify"; .\scripts\run-triaging-agent.ps1 -BuildTag ProdSanity-541
#>

[CmdletBinding()]
param(
    [string]$BuildTag  = "",
    [string]$InputDir  = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

if ($BuildTag)  { $env:BUILD_TAG   = $BuildTag  }
if ($InputDir)  { $env:TRIAGING_INPUT_DIR   = $InputDir  }
if ($OutputDir) { $env:TRIAGING_OUTPUT_DIR  = $OutputDir }

make run AGENT=test-triaging-agent BUILD_TAG="$BuildTag"
exit $LASTEXITCODE
