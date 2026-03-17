# Installs Cursor CLI (agent) on Windows and ensures PATH is updated for the current user session.
$ProgressPreference = 'SilentlyContinue'

Write-Host "📦 Installing Cursor CLI (agent)..."
$installScript = "$env:TEMP\cursor-install.ps1"
Invoke-WebRequest -UseBasicParsing -Uri "https://cursor.com/install/windows" -OutFile $installScript
powershell -ExecutionPolicy Bypass -File $installScript

# Common install locations
$candidates = @(
  "$env:LOCALAPPDATA\Cursor\bin",
  "$env:USERPROFILE\.local\bin"
)

$added = $false
foreach ($dir in $candidates) {
  if (Test-Path (Join-Path $dir "agent.exe")) {
    if (-not ($env:PATH -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ -eq $dir })) {
      $env:PATH = "$dir;" + $env:PATH
      $added = $true
    }
    break
  }
}

# Persist PATH for the user if added
if ($added -and $dir) {
  $newPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
  if (-not ($newPath -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ -eq $dir })) {
    $newPath = "$dir;" + $newPath
    [System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "ℹ️  PATH updated for current session and persisted for the user: $dir"
  }
}

if (Get-Command agent -ErrorAction SilentlyContinue) {
  Write-Host "✅ Cursor CLI installed. agent path: $(Get-Command agent).Source"
} else {
  Write-Host "❌ agent not found on PATH. Check installer output and add install dir to PATH manually."
  exit 1
}
