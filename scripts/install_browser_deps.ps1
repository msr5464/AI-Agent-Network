# Installs Selenium and ChromeDriver for browser-based locator discovery on Windows
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'

Write-Host "📦 Installing browser dependencies for auto-fix..." -ForegroundColor Cyan

# Check if virtual environment is activated
if (-not $env:VIRTUAL_ENV) {
    Write-Host "⚠️  Virtual environment not detected. Activating venv..." -ForegroundColor Yellow
    if (Test-Path "venv\Scripts\Activate.ps1") {
        & "venv\Scripts\Activate.ps1"
    } else {
        Write-Host "❌ venv directory not found. Please create a virtual environment first." -ForegroundColor Red
        exit 1
    }
}

# Install Selenium
Write-Host "📦 Installing Selenium..." -ForegroundColor Cyan
pip install -q selenium>=4.15.0
Write-Host "✅ Selenium installed" -ForegroundColor Green

# Check if ChromeDriver is installed
$chromedriverPath = Get-Command chromedriver -ErrorAction SilentlyContinue
if ($chromedriverPath) {
    $version = & chromedriver --version 2>$null | Select-Object -First 1
    Write-Host "✅ ChromeDriver already installed: $version" -ForegroundColor Green
} else {
    Write-Host "📦 Installing ChromeDriver..." -ForegroundColor Cyan
    
    # Try using Chocolatey if available
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Host "   Using Chocolatey..." -ForegroundColor Gray
        try {
            choco install chromedriver -y
            Write-Host "   ✅ ChromeDriver installed via Chocolatey" -ForegroundColor Green
        } catch {
            Write-Host "   ⚠️  Chocolatey install failed, trying manual install..." -ForegroundColor Yellow
            Install-ChromeDriverManual
        }
    } else {
        Write-Host "   Chocolatey not found, trying manual install..." -ForegroundColor Gray
        Install-ChromeDriverManual
    }
    
    # Verify installation
    $chromedriverPath = Get-Command chromedriver -ErrorAction SilentlyContinue
    if ($chromedriverPath) {
        $version = & chromedriver --version 2>$null | Select-Object -First 1
        Write-Host "✅ ChromeDriver installed: $version" -ForegroundColor Green
    } else {
        Write-Host "❌ ChromeDriver installation failed. Please install manually:" -ForegroundColor Red
        Write-Host "   choco install chromedriver" -ForegroundColor Yellow
        Write-Host "   Or download from: https://chromedriver.chromium.org/downloads" -ForegroundColor Yellow
        exit 1
    }
}

# Check if Chrome browser is installed
$chromePaths = @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe"
)

$chromeFound = $false
foreach ($path in $chromePaths) {
    if (Test-Path $path) {
        Write-Host "✅ Chrome browser found" -ForegroundColor Green
        $chromeFound = $true
        break
    }
}

if (-not $chromeFound) {
    Write-Host "⚠️  Chrome browser not found. Please install Chrome:" -ForegroundColor Yellow
    Write-Host "   https://www.google.com/chrome/" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "✅ Browser dependencies installation complete!" -ForegroundColor Green
$seleniumVersion = pip show selenium 2>$null | Select-String "Version" | ForEach-Object { $_.Line.Split()[1] }
Write-Host "   - Selenium: $seleniumVersion" -ForegroundColor Gray
$cdVersion = & chromedriver --version 2>$null | Select-Object -First 1
Write-Host "   - ChromeDriver: $cdVersion" -ForegroundColor Gray

function Install-ChromeDriverManual {
    Write-Host "   📥 Downloading ChromeDriver manually..." -ForegroundColor Gray
    
    # Get Chrome version
    $chromeVersion = $null
    foreach ($path in $chromePaths) {
        if (Test-Path $path) {
            $chromeVersion = (Get-Item $path).VersionInfo.FileVersion
            break
        }
    }
    
    if (-not $chromeVersion) {
        $chromeVersion = "latest"
    } else {
        $chromeVersion = $chromeVersion.Split('.')[0]
    }
    
    # Determine platform (Windows is always x64 for ChromeDriver)
    $platform = "win64"
    
    # Download ChromeDriver
    $tempDir = Join-Path $env:TEMP "chromedriver-install"
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    
    if ($chromeVersion -eq "latest") {
        try {
            $latestVersion = (Invoke-WebRequest -UseBasicParsing -Uri "https://chromedriver.storage.googleapis.com/LATEST_RELEASE").Content.Trim()
        } catch {
            $latestVersion = "114.0.5735.90"
        }
    } else {
        $latestVersion = "$chromeVersion.0.0.0"
    }
    
    $cdUrl = "https://chromedriver.storage.googleapis.com/$latestVersion/chromedriver_$platform.zip"
    $cdZip = Join-Path $tempDir "chromedriver.zip"
    
    Write-Host "   Downloading from: $cdUrl" -ForegroundColor Gray
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $cdUrl -OutFile $cdZip
    } catch {
        Write-Host "   ⚠️  Failed to download from ChromeDriver storage, trying alternative..." -ForegroundColor Yellow
        # Try alternative URL
        $cdUrl = "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/$chromeVersion/win64/chromedriver-win64.zip"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $cdUrl -OutFile $cdZip
        } catch {
            Write-Host "   ❌ Failed to download ChromeDriver. Please install manually." -ForegroundColor Red
            Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
            return
        }
    }
    
    # Extract
    Expand-Archive -Path $cdZip -DestinationPath $tempDir -Force
    
    # Find chromedriver.exe
    $chromedriverExe = Get-ChildItem -Path $tempDir -Filter "chromedriver.exe" -Recurse | Select-Object -First 1
    
    if ($chromedriverExe) {
        # Install to a location in PATH
        $installPaths = @(
            "${env:ProgramFiles}\chromedriver",
            "${env:ProgramFiles(x86)}\chromedriver",
            "${env:LOCALAPPDATA}\chromedriver",
            "$env:USERPROFILE\.local\bin"
        )
        
        $installed = $false
        foreach ($installPath in $installPaths) {
            try {
                New-Item -ItemType Directory -Force -Path $installPath | Out-Null
                Copy-Item $chromedriverExe.FullName -Destination (Join-Path $installPath "chromedriver.exe") -Force
                
                # Add to PATH if not already there
                $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
                if ($currentPath -notlike "*$installPath*") {
                    [Environment]::SetEnvironmentVariable("Path", "$currentPath;$installPath", "User")
                    $env:Path += ";$installPath"
                }
                
                Write-Host "   ✅ ChromeDriver installed to: $installPath" -ForegroundColor Green
                $installed = $true
                break
            } catch {
                continue
            }
        }
        
        if (-not $installed) {
            Write-Host "   ❌ Failed to install ChromeDriver. Please install manually." -ForegroundColor Red
        }
    } else {
        Write-Host "   ❌ chromedriver.exe not found in downloaded archive." -ForegroundColor Red
    }
    
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
}
