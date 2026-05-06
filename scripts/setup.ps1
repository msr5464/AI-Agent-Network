[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$Requirements = "requirements.txt"
)

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-OrFail {
    param(
        [ScriptBlock]$Script,
        [string]$ErrorMessage
    )
    try {
        & $Script
        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
            throw "$ErrorMessage (exit code $LASTEXITCODE)"
        }
    } catch {
        throw $_
    }
}

Write-Step "Verifying Python executable ($PythonExe)"
$pythonCmd = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python was not found on the PATH. Install Python 3.9+ and try again."
}

if (-not (Test-Path $Requirements)) {
    throw "Requirements file '$Requirements' not found."
}

Write-Step "Checking Claude CLI"
$claudeCli = if ($env:CLAUDE_CLI_PATH) { $env:CLAUDE_CLI_PATH } else { "claude" }
$claudeFound = Get-Command $claudeCli -ErrorAction SilentlyContinue
if ($claudeFound) {
    Write-Host "  Claude CLI found: $($claudeFound.Source)" -ForegroundColor Green
} else {
    Write-Host "  Claude CLI not found — attempting install via npm..." -ForegroundColor Yellow
    $npmFound = Get-Command npm -ErrorAction SilentlyContinue
    if ($npmFound) {
        npm install -g @anthropic-ai/claude-code
        $claudeAfter = Get-Command claude -ErrorAction SilentlyContinue
        if ($claudeAfter) {
            Write-Host "  Claude CLI installed: $($claudeAfter.Source)" -ForegroundColor Green
        } else {
            Write-Warning "  Install appeared to succeed but 'claude' not on PATH. Restart your terminal or set CLAUDE_CLI_PATH in config\.env."
        }
    } else {
        Write-Warning "  npm not found — cannot auto-install Claude CLI."
        Write-Host "  Install Node.js from https://nodejs.org then re-run this script," -ForegroundColor Yellow
        Write-Host "  or install manually: npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
    }
}

Write-Step "Installing project dependencies"
Invoke-OrFail { & $PythonExe -m pip install -r $Requirements } "Failed to install dependencies"

$envPath = Join-Path "config" ".env"
$envExamplePath = Join-Path "config" ".env.example"
if (-not (Test-Path $envPath) -and (Test-Path $envExamplePath)) {
    Write-Step "Creating config\.env from template"
    Copy-Item $envExamplePath $envPath
} elseif (-not (Test-Path $envExamplePath)) {
    Write-Warning "config\.env.example is missing; skipping auto-copy."
} else {
    Write-Step "config\.env already exists (skipping copy)"
}

Write-Step "Setup complete. Next steps:"
Write-Host "  1. Edit config\.env — fill in DB credentials, Claude CLI path, GitHub token, Slack token." -ForegroundColor Yellow
Write-Host "  2. Analyse a build:  .\scripts\run-analyse.ps1" -ForegroundColor Yellow
Write-Host "  3. Fix and raise PR: .\scripts\run-autofix.ps1" -ForegroundColor Yellow
