<#
.SYNOPSIS
    Run the qa-auto-analyse agent (Windows).

.DESCRIPTION
    Usage:
      .\scripts\run-analyse.ps1                                          # scout mode, dirs from .env
      .\scripts\run-analyse.ps1 -BuildTag ProdSanity-541                 # direct mode
      .\scripts\run-analyse.ps1 -BuildTag ProdSanity-541 -InputDir testdata -OutputDir reports
      $env:STOP_AFTER="classify"; .\scripts\run-analyse.ps1 -BuildTag ProdSanity-541
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
if ($InputDir)  { $env:INPUT_DIR   = $InputDir  }
if ($OutputDir) { $env:OUTPUT_DIR  = $OutputDir }

make run AGENT=qa-auto-analyse BUILD_TAG="$BuildTag"
exit $LASTEXITCODE
