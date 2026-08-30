#!/bin/bash
#
# EFIS Data Manager — double-clickable installer.
#
# Double-click this file in Finder to install. It opens Terminal, runs the
# installer, and leaves the window open so you can read the result.
#
# (This is just a friendly wrapper around install.sh.)

# Resolve the folder this file lives in (Finder runs it from "/").
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$HERE"

# Remove the macOS "downloaded from the internet" quarantine flag on the
# installer files so they run without extra Gatekeeper prompts. Best-effort.
xattr -d com.apple.quarantine "$HERE/install.sh" 2>/dev/null || true
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true

echo ""
echo "Starting the EFIS Data Manager installer..."
echo ""

bash "$HERE/install.sh"
status=$?

echo ""
if [ "$status" -eq 0 ]; then
    echo "All done. You can close this window."
else
    echo "The installer stopped with an error (see the message above)."
    echo "You can close this window, fix the issue, and run it again."
fi
echo ""
# Keep the Terminal window open so the user can read the output.
read -r -p "Press Return to close..." _
