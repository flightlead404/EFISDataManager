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

# The user this app runs as (the menu-bar LaunchAgent runs as the login user,
# NOT root). Derive it the same way regardless of whether install.sh happens to
# be invoked directly or under sudo: prefer $SUDO_USER (set when run via sudo),
# otherwise the current login name. Never hardcode a username.
INSTALL_USER="${SUDO_USER:-$(id -un)}"

# Phase-1 privilege backend (chart-sync-stall-fix): narrow sudoers grant.
SUDOERS_FILE="/etc/sudoers.d/efis-data-manager"

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

# --- 6b. Mount-swap privilege grant (chart-sync-stall-fix, Phase 1) ---
#
# The chart-sync mount-swap must unmount the FSKit volume, remount it via
# /sbin/mount_msdos (the confirmed non-stalling in-kernel msdosfs path), and
# later restore the FSKit mount — all of which require root. Phase 1 grants
# that privilege with a narrow, promptless sudoers.d entry so the auto-on-insert
# menu-bar model keeps working without a password on every sync.
#
# INTERIM RISK — DOCUMENTED AND USER-ACCEPTED (retired in Phase 2):
#   /sbin/mount_msdos takes an arbitrary device node and an arbitrary
#   mountpoint, and sudoers cannot constrain those arguments. A NOPASSWD grant
#   for it therefore lets the install user mount a crafted FAT image as root at
#   an arbitrary path. This broad mount_msdos grant is an ACCEPTED Phase-1 risk.
#   Phase 2 replaces this sudoers file with a code-signed SMAppService helper
#   that validates arguments (removable FAT32 device; mountpoint under the app's
#   private dir) and removes this file (see the uninstall path below).
#
#   The diskutil subcommands are scoped to exactly `unmount` and `mount`; the
#   trailing pattern only matches the arguments of those two subcommands, not
#   other diskutil verbs.
step "Installing mount-swap privilege grant ($SUDOERS_FILE)"
info "This lets chart syncs remount the drive without a password each time."
info "You may be asked for your Mac password to write this system file."

# Build the sudoers content for THIS install user (never hardcoded). Pin exact
# absolute binary paths; avoid wildcards except where a subcommand's own
# arguments are unavoidably variable.
SUDOERS_CONTENT="# EFIS Data Manager — chart-sync mount-swap privilege grant (Phase 1, interim).
# Managed by install.sh; removed by the uninstall step below.
#
# INTERIM RISK (user-accepted, retired in Phase 2): the /sbin/mount_msdos grant
# is broad because sudoers cannot constrain its device/mountpoint arguments.
# Phase 2 replaces this with a signed SMAppService helper that validates args.
Cmnd_Alias EFIS_MOUNTSWAP = /sbin/mount_msdos, \\
                            /usr/sbin/diskutil unmount *, \\
                            /usr/sbin/diskutil mount *
$INSTALL_USER ALL=(root) NOPASSWD: EFIS_MOUNTSWAP"

# Validate in a temp file with visudo BEFORE touching /etc, then install
# atomically with the correct 0440 root:wheel perms. Never leave a broken
# sudoers.d file: if validation fails, we abort without writing to /etc.
SUDOERS_TMP="$(mktemp -t efis-data-manager-sudoers)"
printf '%s\n' "$SUDOERS_CONTENT" > "$SUDOERS_TMP"

if ! /usr/sbin/visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
    rm -f "$SUDOERS_TMP"
    fail "Generated sudoers entry failed visudo validation; not installing it."
fi

# Install behind the admin password (sudo). Set perms/owner on the temp file
# first so the file lands correctly, then move it into place. visudo -c on the
# final path is a belt-and-suspenders re-check; if it fails we remove the file
# so a broken entry can never persist.
sudo install -m 0440 -o root -g wheel "$SUDOERS_TMP" "$SUDOERS_FILE" \
    || { rm -f "$SUDOERS_TMP"; fail "Could not install $SUDOERS_FILE (need admin password)."; }
rm -f "$SUDOERS_TMP"

if ! sudo /usr/sbin/visudo -c -f "$SUDOERS_FILE" >/dev/null 2>&1; then
    sudo rm -f "$SUDOERS_FILE"
    fail "$SUDOERS_FILE failed post-install validation and was removed."
fi
info "Mount-swap privilege grant installed for user '$INSTALL_USER'."

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
    <key>CFBundleShortVersionString</key><string>1.5.1</string>
    <key>CFBundleVersion</key><string>1.5.1</string>
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
echo ""
echo "To remove everything later (including the mount-swap privilege grant at"
echo "$SUDOERS_FILE):  ./uninstall.sh"
