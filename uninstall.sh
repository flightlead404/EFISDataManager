#!/bin/bash
#
# EFIS Data Manager — uninstaller for macOS
#
# Removes what install.sh put in place: the menu-bar .app bundle, the launchd
# login item, and the privilege grant (/etc/sudoers.d/efis-data-manager) —
# BOTH the Phase-1 mount-swap grant and the image-based-drive-write raw dd
# write/read grant, which share that single file. It intentionally LEAVES your project
# folder, your virtualenv, your logs, and your chart data untouched — delete
# those by hand if you want a full wipe.
#
# Usage:
#   ./uninstall.sh
#
set -euo pipefail

APP_DIR="/Applications/EFIS Data Manager.app"
PLIST="$HOME/Library/LaunchAgents/com.efisdatamanager.plist"
SUDOERS_FILE="/etc/sudoers.d/efis-data-manager"

step()  { echo ""; echo "==> $*"; }
info()  { echo "    $*"; }

echo "=================================================="
echo "  EFIS Data Manager — Uninstaller"
echo "=================================================="
echo ""

# --- 1. Stop and remove the launchd login item ---
step "Removing login item (launchd)"
if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    info "Removed $PLIST"
else
    info "No login item found (already removed)."
fi

# --- 2. Remove the menu-bar .app bundle ---
step "Removing menu-bar app"
if [ -d "$APP_DIR" ]; then
    rm -rf "$APP_DIR"
    info "Removed $APP_DIR"
else
    info "No app bundle found (already removed)."
fi

# --- 3. Remove the privilege grant (mount-swap + raw image dd) ---
#
# This is the /etc/sudoers.d/efis-data-manager file that install.sh wrote. It
# holds TWO NOPASSWD aliases that share this one file:
#   - EFIS_MOUNTSWAP: promptless mount_msdos / diskutil unmount|mount during
#     chart syncs (Phase-1 mount-swap).
#   - EFIS_RAWIMAGE: /bin/dd write/read against a /dev/rdisk*s* raw slice for the
#     image-based-drive-write path.
# Removing the file revokes BOTH grants in one step. (The signed-helper
# migration — roadmap "sudo retirement" — will also remove this file; this
# uninstall path additionally covers manual removal.)
step "Removing privilege grant ($SUDOERS_FILE)"
if [ -e "$SUDOERS_FILE" ]; then
    info "You may be asked for your Mac password to remove this system file."
    sudo rm -f "$SUDOERS_FILE"
    info "Removed $SUDOERS_FILE"
else
    info "No privilege grant found (already removed)."
fi

echo ""
echo "Uninstall complete."
echo ""
echo "Left in place (delete by hand if you want a full wipe):"
echo "  - This project folder and its virtualenv"
echo "  - Your logs in ~/EFIS/DataManagerLogs"
echo "  - Your chart data and drive settings"
echo ""
