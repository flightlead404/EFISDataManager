"""USB volume mount/unmount detection for macOS.

Polls /Volumes/ for changes since DiskArbitration's C API is difficult to
use reliably from Python. Simple, robust, and low overhead (~1 check/sec.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


import re

# Matches "EFIS", "EFIS_1", "EFIS 2", etc. macOS appends a numeric suffix
# when a volume name was recently used, and the user rotates multiple drives.
_EFIS_NAME_RE = re.compile(r"^EFIS([ _-]?\d+)?$", re.IGNORECASE)


def is_efis_drive(mount_point: str) -> bool:
    """Check if a mounted volume is an EFIS drive.

    Criteria (any one is sufficient):
    - Volume name is "EFIS" or "EFIS_N" (rotating drives / macOS suffix)
    - GRTCHARTS/ directory exists at volume root
    """
    volume_name = os.path.basename(mount_point)
    if _EFIS_NAME_RE.match(volume_name):
        return True
    if os.path.isdir(os.path.join(mount_point, "GRTCHARTS")):
        return True
    return False


def _get_efis_volumes() -> set[str]:
    """Scan /Volumes/ and return set of mount points that are EFIS drives."""
    volumes_dir = "/Volumes"
    efis_mounts = set()
    try:
        for name in os.listdir(volumes_dir):
            mount_point = os.path.join(volumes_dir, name)
            if os.path.isdir(mount_point) and is_efis_drive(mount_point):
                efis_mounts.add(mount_point)
    except OSError:
        pass
    return efis_mounts


class USBMonitor:
    """Monitors for EFIS drive mount/unmount by polling /Volumes/."""

    def __init__(self, on_efis_mount: Callable[[str], None],
                 on_efis_unmount: Callable[[str], None]):
        """
        Args:
            on_efis_mount: Called with mount_point when an EFIS drive is mounted.
            on_efis_unmount: Called with mount_point when an EFIS drive is unmounted.
        """
        self.on_efis_mount = on_efis_mount
        self.on_efis_unmount = on_efis_unmount
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._known_efis_mounts: set[str] = set()

    def start(self):
        """Start monitoring for USB events in a background thread."""
        if self._running:
            return
        self._running = True

        # Check what's already mounted
        self._known_efis_mounts = _get_efis_volumes()
        for mount_point in self._known_efis_mounts:
            logger.info(f"EFIS drive already mounted: {mount_point}")
            self.on_efis_mount(mount_point)

        # Start polling thread
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="USBMonitor")
        self._thread.start()
        logger.info("USB monitor started (polling /Volumes/ every 2s).")

    def stop(self):
        """Stop monitoring."""
        self._running = False
        logger.info("USB monitor stopped.")

    def _poll_loop(self):
        """Poll /Volumes/ for changes every 2 seconds."""
        while self._running:
            time.sleep(2)
            try:
                current = _get_efis_volumes()

                # Detect new mounts
                new_mounts = current - self._known_efis_mounts
                for mount_point in new_mounts:
                    logger.info(f"EFIS drive mounted: {mount_point}")
                    self.on_efis_mount(mount_point)

                # Detect ejections
                ejected = self._known_efis_mounts - current
                for mount_point in ejected:
                    logger.info(f"EFIS drive ejected: {mount_point}")
                    self.on_efis_unmount(mount_point)

                self._known_efis_mounts = current

            except Exception as e:
                logger.error(f"USB monitor poll error: {e}")
