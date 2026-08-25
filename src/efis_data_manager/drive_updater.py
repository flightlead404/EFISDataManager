"""Drive updater — syncs local USB image to a physical EFIS USB drive.

Uses rsync for efficient delta-copy of chart data (large, many files)
and direct file comparison for the small number of top-level files
(NAV.DB, software .dat files).
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from efis_data_manager.config import load_config

logger = logging.getLogger(__name__)

# Files/dirs in the local USB image that should be synced to the drive.
# Anything on the drive NOT in this list is left untouched (.bak, System Volume Information, etc.)
SYNC_ITEMS = [
    "ChartData",
    "GRTCHARTS",
    "NAV.DB",
    "NAV-proc.DB",
]

# Software files — match by prefix since local names may differ from USB names
SOFTWARE_MAPPING = {
    # local image name -> USB drive name
    # We sync any .dat files from local image to USB
}


def check_drive_currency(mount_point: str) -> dict:
    """Quick currency check — compare key files between USB and local image.

    Returns:
        Dict with: {"is_current": bool, "stale_items": list[str], "message": str}
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]

    stale_items = []

    # ChartData: compare ScannedCharts.sqlite mtime (local vs USB)
    local_sqlite = os.path.join(usb_image_path, "ChartData", "ScannedCharts.sqlite")
    usb_sqlite = os.path.join(mount_point, "ChartData", "ScannedCharts.sqlite")
    if os.path.exists(local_sqlite) and os.path.exists(usb_sqlite):
        local_mtime = os.path.getmtime(local_sqlite)
        usb_mtime = os.path.getmtime(usb_sqlite)
        if local_mtime > usb_mtime:
            stale_items.append("ChartData (local is newer)")
    elif os.path.exists(local_sqlite) and not os.path.exists(usb_sqlite):
        stale_items.append("ChartData (missing from drive)")

    for item in SYNC_ITEMS:
        # ChartData handled above via ScannedCharts.sqlite mtime
        if item == "ChartData":
            continue

        local_path = os.path.join(usb_image_path, item)
        usb_path = os.path.join(mount_point, item)

        if not os.path.exists(local_path):
            continue  # Nothing to sync for this item

        if not os.path.exists(usb_path):
            stale_items.append(f"{item} (missing from drive)")
            continue

        if os.path.isfile(local_path):
            # Compare size — different size = definitely stale
            local_size = os.path.getsize(local_path)
            usb_size = os.path.getsize(usb_path)
            if local_size != usb_size:
                stale_items.append(f"{item} (size differs: local={local_size}, usb={usb_size})")

    # Check for .dat software files
    for name in os.listdir(usb_image_path):
        if name.endswith(".dat"):
            local_path = os.path.join(usb_image_path, name)
            usb_path = os.path.join(mount_point, name)
            if not os.path.exists(usb_path):
                stale_items.append(f"{name} (missing from drive)")
            elif os.path.getsize(local_path) != os.path.getsize(usb_path):
                stale_items.append(f"{name} (size differs)")

    if stale_items:
        return {
            "is_current": False,
            "stale_items": stale_items,
            "message": f"{len(stale_items)} item(s) need updating.",
        }
    else:
        return {
            "is_current": True,
            "stale_items": [],
            "message": "Drive is up to date.",
        }


def update_drive(mount_point: str, progress_callback: Optional[Callable] = None) -> dict:
    """Sync local USB image to the physical EFIS drive.

    Uses rsync for ChartData (large directory tree) and direct copy for
    individual files (NAV.DB, software .dat).

    Args:
        mount_point: Path to mounted EFIS drive (e.g. /Volumes/EFIS).
        progress_callback: Optional callable(message) for status updates.

    Returns:
        Dict with: {"files_updated": int, "errors": list[str]}
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]

    results = {"files_updated": 0, "errors": []}

    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    # --- Sync ChartData via rsync (efficient delta) ---
    local_chartdata = os.path.join(usb_image_path, "ChartData")
    usb_chartdata = os.path.join(mount_point, "ChartData")

    if os.path.isdir(local_chartdata):
        _status("Syncing ChartData...")
        try:
            # rsync with checksum, delete extra files on destination,
            # trailing slash on source means "contents of"
            cmd = [
                "rsync", "-rc", "--delete",
                "--exclude", ".DS_Store",
                "--exclude", "E:ChartData",
                f"{local_chartdata}/",
                f"{usb_chartdata}/",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode == 0:
                # Count updated files from rsync output (verbose would give this,
                # but we use -c without -v for speed; just mark as updated)
                results["files_updated"] += 1  # At least ChartData was synced
                _status("ChartData sync complete.")
            else:
                err = proc.stderr.strip()[:200]
                results["errors"].append(f"rsync ChartData failed: {err}")
                logger.error(f"rsync failed: {proc.stderr[:500]}")
        except subprocess.TimeoutExpired:
            results["errors"].append("ChartData sync timed out (1 hour)")
        except Exception as e:
            results["errors"].append(f"ChartData sync error: {e}")

    # --- Sync GRTCHARTS directory (just ensure it exists, should be empty) ---
    usb_grtcharts = os.path.join(mount_point, "GRTCHARTS")
    if not os.path.isdir(usb_grtcharts):
        try:
            os.makedirs(usb_grtcharts)
            results["files_updated"] += 1
        except OSError as e:
            results["errors"].append(f"Create GRTCHARTS: {e}")

    # --- Sync individual files (NAV.DB, software .dat) ---
    for name in os.listdir(usb_image_path):
        local_path = os.path.join(usb_image_path, name)

        # Only sync files (not directories — ChartData/GRTCHARTS handled above)
        if not os.path.isfile(local_path):
            continue

        # Skip files we don't want to push to the drive
        if name.startswith("."):
            continue

        usb_path = os.path.join(mount_point, name)

        # Check if update needed (size comparison)
        needs_update = False
        if not os.path.exists(usb_path):
            needs_update = True
        elif os.path.getsize(local_path) != os.path.getsize(usb_path):
            needs_update = True

        if needs_update:
            _status(f"Copying {name}...")
            try:
                # Copy with shutil for reliability on FAT32
                import shutil
                shutil.copy2(local_path, usb_path)
                results["files_updated"] += 1
                logger.info(f"Updated on drive: {name}")
            except OSError as e:
                results["errors"].append(f"Copy {name}: {e}")

    _status("Drive update complete.")
    return results


def prepare_drive(volume_path: str, progress_callback: Optional[Callable] = None) -> dict:
    """Format a USB drive for EFIS use and populate with current data.

    WARNING: Destructive — erases all data on the target volume.

    Args:
        volume_path: Path to the mounted volume to format (e.g. /Volumes/UNTITLED).
        progress_callback: Optional callable(message) for status updates.

    Returns:
        Dict with: {"success": bool, "message": str}
    """
    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    # Find the disk identifier for this volume
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", volume_path],
            capture_output=True, timeout=10
        )
        if result.returncode != 0:
            return {"success": False, "message": f"Cannot identify disk for {volume_path}"}

        import plistlib
        info = plistlib.loads(result.stdout)
        disk_id = info.get("DeviceIdentifier", "")
        if not disk_id:
            return {"success": False, "message": "Could not determine disk identifier."}

    except Exception as e:
        return {"success": False, "message": f"Failed to get disk info: {e}"}

    # Format as FAT32 with label "EFIS"
    _status(f"Formatting {disk_id} as FAT32 (EFIS)...")
    try:
        result = subprocess.run(
            ["diskutil", "eraseDisk", "FAT32", "EFIS", "MBRFormat", f"/dev/{disk_id}"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return {"success": False, "message": f"Format failed: {result.stderr.strip()[:200]}"}
    except Exception as e:
        return {"success": False, "message": f"Format error: {e}"}

    # Wait for volume to remount
    _status("Waiting for drive to remount...")
    import time
    mount_point = None
    for _ in range(20):
        time.sleep(1)
        if os.path.isdir("/Volumes/EFIS"):
            mount_point = "/Volumes/EFIS"
            break

    if not mount_point:
        return {"success": False, "message": "Drive did not remount after format."}

    # Create GRTCHARTS flag directory
    _status("Creating GRTCHARTS directory...")
    try:
        os.makedirs(os.path.join(mount_point, "GRTCHARTS"), exist_ok=True)
    except OSError as e:
        return {"success": False, "message": f"Failed to create GRTCHARTS: {e}"}

    # Now run the normal update to populate it
    _status("Populating drive with current data...")
    update_results = update_drive(mount_point, progress_callback=progress_callback)

    if update_results["errors"]:
        return {
            "success": False,
            "message": f"Drive formatted but population had errors: {update_results['errors']}",
        }

    return {"success": True, "message": "Drive prepared and populated successfully."}
