#!/bin/bash
#
# EFIS Data Manager — installer for macOS
#
# Sets up a Python virtual environment, installs dependencies and the
# Playwright browser, and installs a menu-bar app + login item that use
# THIS machine's home directory (no hardcoded paths).
#
# Usage:
#   cd /path/to/EFISDataManager
#   ./install.sh
#
set -euo pipefail

# --- Resolve project root (directory containing this script) ---
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
APP_DIR="/Applications/EFIS Data Manager.app"
PLIST="$HOME/Library/LaunchAgents/com.efisdatamanager.plist"
LOG_DIR="$HOME/EFIS/DataManagerLogs"

echo "EFIS Data Manager installer"
echo "  Project: $PROJECT_DIR"
echo ""

# --- 1. Check Python 3.11+ ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.11+ first (e.g. 'brew install python')."
    exit 1
fi
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYMAJ="${PYVER%%.*}"; PYMIN="${PYVER##*.}"
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
    echo "ERROR: Python 3.11+ required, found $PYVER."
    exit 1
fi
echo "==> Python $PYVER OK"

# --- 2. Create / refresh the virtual environment ---
if [ ! -d "$VENV" ]; then
    echo "==> Creating virtual environment"
    python3 -m venv "$VENV"
fi
echo "==> Installing Python dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# --- 3. Install the Playwright browser (needed for GRT nav DB checks) ---
echo "==> Installing Playwright Chromium browser (one-time, ~100 MB)"
"$VENV/bin/playwright" install chromium

# --- 4. Log directory ---
mkdir -p "$LOG_DIR"

# --- 5. Menu-bar .app bundle (login-safe, uses this project + venv) ---
echo "==> Installing menu-bar app to $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"

cat > "$APP_DIR/Contents/Info.plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>EFIS Data Manager</string>
    <key>CFBundleIdentifier</key><string>com.efisdatamanager.app</string>
    <key>CFBundleExecutable</key><string>launch</string>
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

# --- 6. launchd login item (auto-start after login) ---
echo "==> Installing login item (launchd) to $PLIST"
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
