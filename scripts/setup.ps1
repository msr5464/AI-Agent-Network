[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$VenvPath = "venv",
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

if (-not (Test-Path $VenvPath)) {
    Write-Step "Creating virtual environment at $VenvPath"
    Invoke-OrFail { & $PythonExe -m venv $VenvPath } "Failed to create virtual environment"
} else {
    Write-Step "Virtual environment already exists at $VenvPath (skipping creation)"
}

$venvPython = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Unable to locate $venvPython. Ensure the virtual environment was created successfully."
}

Write-Step "Upgrading pip"
Invoke-OrFail { & $venvPython -m pip install --upgrade pip } "Failed to upgrade pip"

if (-not (Test-Path $Requirements)) {
    throw "Requirements file '$Requirements' not found."
}

Write-Step "Installing project dependencies"
Invoke-OrFail { & $venvPython -m pip install -r $Requirements } "Failed to install dependencies"

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
Write-Host "  1. Update config\.env with your database and AI provider details." -ForegroundColor Yellow
Write-Host "  2. Place/verify your report folders under testdata/ (or adjust INPUT_DIR)." -ForegroundColor Yellow
Write-Host "  3. Run the agent via: powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1 --input-dir testdata/Regression-Growth-Tests-442 --output-dir reports" -ForegroundColor Yellow

