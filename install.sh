#!/bin/bash
#
# EFIS Data Manager — installer for macOS
#
# Bootstraps prerequisites (Xcode Command Line Tools, Homebrew, Python 3.11+),
# sets up a Python virtual environment, installs dependencies and the Playwright
# browser, and installs a menu-bar app + login item that use THIS machine's home
# directory (no hardcoded paths).
#
# Designed to be run by non-technical users. It narrates each step and asks for
# confirmation only where macOS itself requires it (your password, or Apple's
# Command Line Tools dialog).
#
# Usage:
#   ./install.sh              (or double-click install.command)
#
set -euo pipefail

# --- Resolve project root (directory containing this script) ---
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
APP_DIR="/Applications/EFIS Data Manager.app"
PLIST="$HOME/Library/LaunchAgents/com.efisdatamanager.plist"
LOG_DIR="$HOME/EFIS/DataManagerLogs"

# Pretty output helpers
step()  { echo ""; echo "==> $*"; }
info()  { echo "    $*"; }
fail()  { echo ""; echo "ERROR: $*" >&2; exit 1; }

echo "=================================================="
echo "  EFIS Data Manager — Installer"
echo "=================================================="
echo "  Project folder: $PROJECT_DIR"
echo ""
echo "  This will set up everything the app needs. You may be asked for your"
echo "  Mac password, and (on a new Mac) to approve an Apple 'Command Line"
echo "  Tools' download. That's expected."
echo ""

# --- 1. Xcode Command Line Tools (provides git, compilers) ---
if ! xcode-select -p >/dev/null 2>&1; then
    step "Installing Apple Command Line Tools"
    info "A system dialog will appear — click 'Install' and accept the license."
    info "This can take several minutes."
    xcode-select --install 2>/dev/null || true
    # Wait for the tools to finish installing
    until xcode-select -p >/dev/null 2>&1; do
        sleep 5
    done
    info "Command Line Tools installed."
else
    step "Apple Command Line Tools already present"
fi

# --- 2. Homebrew (package manager, used to install Python) ---
BREW=""
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$candidate" ] && BREW="$candidate" && break
done
if [ -z "$BREW" ] && command -v brew >/dev/null 2>&1; then
    BREW="$(command -v brew)"
fi

if [ -z "$BREW" ]; then
    step "Installing Homebrew (macOS package manager)"
    info "You'll be asked for your Mac password. This is Homebrew's own installer."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
        || fail "Homebrew installation failed. See https://brew.sh for manual steps."
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [ -x "$candidate" ] && BREW="$candidate" && break
    done
    [ -n "$BREW" ] || fail "Homebrew installed but 'brew' was not found where expected."
else
    step "Homebrew already installed ($BREW)"
fi

# Make brew usable in this shell session
eval "$("$BREW" shellenv)"

# --- 3. Python 3.11+ ---
need_python=1
if command -v python3 >/dev/null 2>&1; then
    PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
    PYMAJ="${PYVER%%.*}"; PYMIN="${PYVER##*.}"
    if [ "$PYMAJ" -gt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -ge 11 ]; }; then
        need_python=0
        step "Python $PYVER already present"
    fi
fi
if [ "$need_python" -eq 1 ]; then
    step "Installing Python via Homebrew"
    "$BREW" install python || fail "Python installation failed."
    hash -r
    command -v python3 >/dev/null 2>&1 || fail "Python installed but 'python3' not found on PATH."
    PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    PYMAJ="${PYVER%%.*}"; PYMIN="${PYVER##*.}"
    if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
        fail "Python 3.11+ required, but $PYVER is active. Check your PATH."
    fi
    info "Python $PYVER ready."
fi

# --- 4. Create / refresh the virtual environment ---
if [ ! -d "$VENV" ]; then
    step "Creating Python virtual environment"
    python3 -m venv "$VENV"
fi
step "Installing Python dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# --- 5. Install the Playwright browser (needed for GRT nav DB checks) ---
step "Installing chart-checker browser (one-time, ~100 MB — may take a few minutes)"
"$VENV/bin/playwright" install chromium

# --- 6. Log directory ---
mkdir -p "$LOG_DIR"

# --- 7. Menu-bar .app bundle (login-safe, uses this project + venv) ---
step "Installing menu-bar app to $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# App icon (banked-PFD design)
if [ -f "$PROJECT_DIR/assets/EFISDataManager.icns" ]; then
    cp "$PROJECT_DIR/assets/EFISDataManager.icns" "$APP_DIR/Contents/Resources/EFISDataManager.icns"
fi

cat > "$APP_DIR/Contents/Info.plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>EFIS Data Manager</string>
    <key>CFBundleIdentifier</key><string>com.efisdatamanager.app</string>
    <key>CFBundleExecutable</key><string>launch</string>
    <key>CFBundleIconFile</key><string>EFISDataManager</string>
    <key>CFBundleShortVersionString</key><string>0.9.6</string>
    <key>CFBundleVersion</key><string>0.9.6</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLISTEOF

cat > "$APP_DIR/Contents/MacOS/launch" <<LAUNCHEOF
#!/bin/bash
cd "$PROJECT_DIR/src"
exec "$VENV/bin/python3" -m efis_data_manager.app
LAUNCHEOF
chmod +x "$APP_DIR/Contents/MacOS/launch"

# --- 8. launchd login item (auto-start after login) ---
step "Installing login item (launchd) to $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<LDEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.efisdatamanager.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>sleep 60 &amp;&amp; cd "$PROJECT_DIR/src" &amp;&amp; exec "$VENV/bin/python3" -m efis_data_manager.app</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/launchd_stdout.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/launchd_stderr.log</string>
</dict>
</plist>
LDEOF

# Load (or reload) the login item
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

# Register the bundle so Finder/Dock pick up the icon immediately
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP_DIR" 2>/dev/null || true

echo ""
echo "Install complete."
echo ""
echo "Next steps:"
echo "  1. Launch 'EFIS Data Manager' from /Applications (or it auto-starts at next login)."
echo "  2. In the menu-bar icon: Settings... to set archive & USB image folders."
echo "  3. Seattle Avionics Login... to store your chart-subscription credentials."
echo "  4. Configure number of cylinders and thresholds in the Analysis Dashboard settings."
echo ""
echo "To start the app now:  open \"$APP_DIR\""
