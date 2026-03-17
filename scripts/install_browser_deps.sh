#!/usr/bin/env bash
# Installs Selenium and ChromeDriver for browser-based locator discovery on macOS/Linux
set -euo pipefail

echo "📦 Installing browser dependencies for auto-fix..."

# Check if virtual environment is activated
if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "⚠️  Virtual environment not detected. Activating venv..."
    if [ -d "venv" ]; then
        source venv/bin/activate
    else
        echo "❌ venv directory not found. Please create a virtual environment first."
        exit 1
    fi
fi

# Install Selenium
echo "📦 Installing Selenium..."
pip install -q selenium>=4.15.0
echo "✅ Selenium installed"

# Check if ChromeDriver is installed
if command -v chromedriver >/dev/null 2>&1; then
    CHROMEDRIVER_VERSION=$(chromedriver --version 2>/dev/null | head -1 | awk '{print $2}' || echo "unknown")
    echo "✅ ChromeDriver already installed: $CHROMEDRIVER_VERSION"
else
    echo "📦 Installing ChromeDriver..."
    
    # Detect OS
    OS="$(uname -s)"
    case "${OS}" in
        Linux*)
            # Try package manager first
            if command -v apt-get >/dev/null 2>&1; then
                echo "   Using apt-get (Ubuntu/Debian)..."
                sudo apt-get update -qq
                sudo apt-get install -y chromium-chromedriver || {
                    echo "   ⚠️  Package manager install failed, trying manual install..."
                    install_chromedriver_manual
                }
            elif command -v yum >/dev/null 2>&1; then
                echo "   Using yum (RHEL/CentOS)..."
                sudo yum install -y chromium-headless || install_chromedriver_manual
            else
                install_chromedriver_manual
            fi
            ;;
        Darwin*)
            # macOS
            if command -v brew >/dev/null 2>&1; then
                echo "   Using Homebrew..."
                brew install chromedriver || {
                    echo "   ⚠️  Homebrew install failed, trying manual install..."
                    install_chromedriver_manual
                }
            else
                echo "   ⚠️  Homebrew not found, trying manual install..."
                install_chromedriver_manual
            fi
            ;;
        *)
            echo "   ⚠️  Unsupported OS: ${OS}. Please install ChromeDriver manually."
            exit 1
            ;;
    esac
    
    # Verify installation
    if command -v chromedriver >/dev/null 2>&1; then
        CHROMEDRIVER_PATH=$(which chromedriver)
        CHROMEDRIVER_VERSION=$(chromedriver --version 2>/dev/null | head -1 | awk '{print $2}' || echo "unknown")
        
        # Remove quarantine attribute on macOS to prevent security popup
        if [ "$(uname -s)" = "Darwin" ] && [ -n "$CHROMEDRIVER_PATH" ]; then
            echo "   Removing macOS quarantine attribute..."
            xattr -d com.apple.quarantine "$CHROMEDRIVER_PATH" 2>/dev/null || true
            # Also try to allow it via spctl if needed
            spctl --add --label "ChromeDriver" "$CHROMEDRIVER_PATH" 2>/dev/null || true
        fi
        
        echo "✅ ChromeDriver installed: $CHROMEDRIVER_VERSION"
        if [ "$(uname -s)" = "Darwin" ]; then
            echo "   ℹ️  If you see a security popup, go to: System Settings > Privacy & Security > Allow"
        fi
    else
        echo "❌ ChromeDriver installation failed. Please install manually:"
        echo "   macOS: brew install chromedriver"
        echo "   Linux: sudo apt-get install chromium-chromedriver"
        exit 1
    fi
fi

# Check if Chrome browser is installed
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
    echo "✅ Chrome/Chromium browser found"
elif [ -d "/Applications/Google Chrome.app" ]; then
    echo "✅ Chrome browser found (macOS)"
else
    echo "⚠️  Chrome browser not found. Please install Chrome:"
    echo "   https://www.google.com/chrome/"
fi

echo ""
echo "✅ Browser dependencies installation complete!"
echo "   - Selenium: $(pip show selenium 2>/dev/null | grep Version | awk '{print $2}' || echo 'installed')"
echo "   - ChromeDriver: $(chromedriver --version 2>/dev/null | head -1 | awk '{print $2}' || echo 'installed')"

install_chromedriver_manual() {
    echo "   📥 Downloading ChromeDriver manually..."
    
    # Get Chrome version
    if command -v google-chrome >/dev/null 2>&1; then
        CHROME_VERSION=$(google-chrome --version 2>/dev/null | awk '{print $3}' | cut -d. -f1 || echo "latest")
    elif command -v chromium >/dev/null 2>&1; then
        CHROME_VERSION=$(chromium --version 2>/dev/null | awk '{print $2}' | cut -d. -f1 || echo "latest")
    elif [ -d "/Applications/Google Chrome.app" ]; then
        CHROME_VERSION=$(/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --version 2>/dev/null | awk '{print $3}' | cut -d. -f1 || echo "latest")
    else
        CHROME_VERSION="latest"
    fi
    
    # Determine platform
    ARCH="$(uname -m)"
    case "${ARCH}" in
        x86_64) PLATFORM="linux64" ;;
        arm64|aarch64) PLATFORM="linux64" ;;  # ChromeDriver doesn't have ARM64, use x64
        *) PLATFORM="linux64" ;;
    esac
    
    if [ "$(uname -s)" = "Darwin" ]; then
        if [ "${ARCH}" = "arm64" ]; then
            PLATFORM="mac-arm64"
        else
            PLATFORM="mac-x64"
        fi
    fi
    
    # Download ChromeDriver
    if [ "${CHROME_VERSION}" = "latest" ]; then
        CD_VERSION=$(curl -sS https://chromedriver.storage.googleapis.com/LATEST_RELEASE 2>/dev/null || echo "114.0.5735.90")
    else
        CD_VERSION="${CHROME_VERSION}.0.0.0"
    fi
    
    CD_URL="https://chromedriver.storage.googleapis.com/${CD_VERSION}/chromedriver_${PLATFORM}.zip"
    TEMP_DIR=$(mktemp -d)
    CD_ZIP="${TEMP_DIR}/chromedriver.zip"
    
    echo "   Downloading from: ${CD_URL}"
    curl -fsSL "${CD_URL}" -o "${CD_ZIP}" || {
        echo "   ⚠️  Failed to download from ChromeDriver storage, trying alternative..."
        # Try alternative URL (newer ChromeDriver versions)
        CD_URL="https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/${CHROME_VERSION}/linux64/chromedriver-linux64.zip"
        curl -fsSL "${CD_URL}" -o "${CD_ZIP}" || {
            echo "   ❌ Failed to download ChromeDriver. Please install manually."
            rm -rf "${TEMP_DIR}"
            return 1
        }
    }
    
    # Extract and install
    unzip -q "${CD_ZIP}" -d "${TEMP_DIR}"
    sudo mv "${TEMP_DIR}/chromedriver"*"/chromedriver" /usr/local/bin/chromedriver 2>/dev/null || \
    sudo mv "${TEMP_DIR}/chromedriver" /usr/local/bin/chromedriver 2>/dev/null || {
        # Try user local bin
        mkdir -p "${HOME}/.local/bin"
        mv "${TEMP_DIR}/chromedriver"*"/chromedriver" "${HOME}/.local/bin/chromedriver" 2>/dev/null || \
        mv "${TEMP_DIR}/chromedriver" "${HOME}/.local/bin/chromedriver" 2>/dev/null || {
            echo "   ❌ Failed to install ChromeDriver. Please install manually."
            rm -rf "${TEMP_DIR}"
            return 1
        }
        export PATH="${HOME}/.local/bin:${PATH}"
        echo "   ℹ️  Installed to ${HOME}/.local/bin. Add to PATH if needed."
    }
    
    sudo chmod +x /usr/local/bin/chromedriver 2>/dev/null || chmod +x "${HOME}/.local/bin/chromedriver" 2>/dev/null
    rm -rf "${TEMP_DIR}"
    
    echo "   ✅ ChromeDriver installed manually"
}
