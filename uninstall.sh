#!/bin/bash
#
# EFIS Data Manager — uninstaller for macOS
#
# Removes what install.sh put in place: the menu-bar .app bundle, the launchd
# login item, and the Phase-1 mount-swap privilege grant
# (/etc/sudoers.d/efis-data-manager). It intentionally LEAVES your project
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

# --- 3. Remove the Phase-1 mount-swap privilege grant ---
#
# This is the /etc/sudoers.d/efis-data-manager file that install.sh wrote to
# allow promptless mount_msdos / diskutil unmount|mount during chart syncs.
# Removing it revokes that NOPASSWD grant. (Phase 2 will remove this file as
# part of migrating to the signed SMAppService helper; this uninstall path also
# covers manual removal.)
step "Removing mount-swap privilege grant ($SUDOERS_FILE)"
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
