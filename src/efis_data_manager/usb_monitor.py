# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""USB volume mount/unmount detection for macOS.

Polls /Volumes/ for changes since DiskArbitration's C API is difficult to
use reliably from Python. Simple, robust, and low overhead (~1 check/sec.
"""

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Detection is identity-only (no label matching). A drive is "ours / managed"
# IFF it carries our identity file at the volume root with the expected kind
# discriminator (Req 10.2). The volume label plays NO role in detection.
#
# These constants mirror drive_updater.IDENTITY_FILENAME / IDENTITY_KIND. They
# are duplicated here deliberately so usb_monitor stays a leaf module: it reads
# the identity file itself (json + a local kind check) rather than importing
# drive_updater, which lazily imports usb_monitor inside resolve_drive_id — a
# top-level import both ways would be a cycle.
IDENTITY_FILENAME = "EFIS_DRIVE_ID.json"
IDENTITY_KIND = "efis-chart-drive"


def is_managed_drive(mount_point: str) -> bool:
    """Return True IFF the volume is one of ours (identity-only detection).

    A drive is managed if and only if it carries a valid identity file
    (``EFIS_DRIVE_ID.json``) at its volume root whose ``kind`` is
    ``"efis-chart-drive"`` (Req 10.2). The volume label is NOT consulted — the
    old ``EFIS``/``EFIS_N`` label regex is gone. A missing, unreadable, or
    wrong-kind identity file means the drive is not managed and triggers no
    auto-action; first contact with such a drive is an explicit Prepare Drive
    action (see :func:`is_adoption_candidate`).
    """
    path = os.path.join(mount_point, IDENTITY_FILENAME)
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and data.get("kind") == IDENTITY_KIND


def is_adoption_candidate(mount_point: str) -> bool:
    """Return True if the drive looks like a previously-used GRT chart drive.

    A candidate has a ``GRTCHARTS/`` folder OR a ``ChartData/`` folder at its
    root but NO valid identity file — i.e. a drive from the Windows tool or an
    older version of this app that could be adopted rather than reformatted.

    This is used ONLY by the Prepare Drive flow to offer adopt-vs-clean; it is
    NOT used for auto-detection. A managed drive (valid identity present) is
    never an adoption candidate.
    """
    if is_managed_drive(mount_point):
        return False
    if os.path.isdir(os.path.join(mount_point, "GRTCHARTS")):
        return True
    if os.path.isdir(os.path.join(mount_point, "ChartData")):
        return True
    return False


# FAT32 volume labels are limited: up to 11 characters, and diskutil upcases
# them. The Prepare Drive flow always produces an "EFIS_<suffix>" label from the
# user's free-text suffix.
EFIS_LABEL_PREFIX = "EFIS_"
FAT32_LABEL_MAXLEN = 11


def build_efis_label(user_suffix: str) -> str:
    """Build a FAT32-safe EFIS volume label from a user suffix.

    The label is purely COSMETIC — a Finder convenience for telling rotating
    drives apart. Nothing keys on it for detection or auto-action; a drive is
    managed only by its identity file (see :func:`is_managed_drive`). The
    suffix is upcased and stripped of characters FAT32 labels cannot carry
    (kept: A-Z, 0-9, ``_`` and ``-``), then the whole label is truncated to the
    11-character FAT32 limit. An empty/whitespace suffix yields the bare
    ``EFIS`` prefix trimmed of its trailing underscore, so the caller never
    produces a dangling "EFIS_" with nothing after it.

    Examples:
        build_efis_label("spare") -> "EFIS_SPARE"
        build_efis_label("n1")    -> "EFIS_N1"
        build_efis_label("")      -> "EFIS"
        build_efis_label("my drive 2") -> "EFIS_MYDRIV" (truncated to 11)
    """
    import re as _re

    cleaned = _re.sub(r"[^A-Za-z0-9_-]", "", (user_suffix or "")).upper()
    # Strip a leading EFIS_/EFIS the user may have typed (e.g. they entered the
    # full current label "EFIS_1" rather than just the suffix "1"). Without this
    # we would double-prefix to "EFIS_EFIS_1". Strip repeatedly so "EFIS_EFIS_2"
    # also collapses to "2".
    while cleaned.startswith(EFIS_LABEL_PREFIX):
        cleaned = cleaned[len(EFIS_LABEL_PREFIX):]
    if cleaned == "EFIS":
        cleaned = ""
    if not cleaned:
        # No usable suffix -> the bare label (no trailing "_").
        return "EFIS"
    label = f"{EFIS_LABEL_PREFIX}{cleaned}"
    return label[:FAT32_LABEL_MAXLEN]


VOLUMES_DIR = "/Volumes"

# Volume names under /Volumes that are never removable EFIS/GRT drives and must
# never be scanned for identity or adoption-candidate markers. This mirrors the
# exclusions the Prepare Drive menu uses (see app.prepare_drive) so the two
# stay in agreement: the boot volume, Time Machine local snapshots, and Time
# Machine backup volumes ("Backups of <host>").
_EXCLUDED_VOLUME_NAMES = frozenset({
    "Macintosh HD",
    "com.apple.TimeMachine.localsnapshots",
})
_EXCLUDED_VOLUME_PREFIXES = ("Backups of",)


def _is_scannable_volume(name: str) -> bool:
    """Return True if a /Volumes entry may be a removable EFIS/GRT drive.

    Filters out the system disk and Time Machine volumes using the same
    exclusions the Prepare Drive menu applies, so classification never misfires
    on the boot volume or a backup snapshot.
    """
    if name in _EXCLUDED_VOLUME_NAMES:
        return False
    for prefix in _EXCLUDED_VOLUME_PREFIXES:
        if name.startswith(prefix):
            return False
    return True


def classify_volume(mount_point: str) -> str:
    """Classify a mounted volume for the USB monitor's auto-action gating.

    Returns one of:
        ``"managed"``   — carries a valid identity file; auto-action (archive +
                          sync) is allowed (Req 10.4).
        ``"candidate"`` — no identity but looks like a previously-used GRT chart
                          drive (GRTCHARTS/ or ChartData/); NO automatic action,
                          only an adopt hint (Req 10.5).
        ``"ignore"``    — anything else (blank stick, unrelated volume): no
                          managed auto-action and no hint.

    Pure and OSError-tolerant: it only reads the volume via the identity/adoption
    helpers, which already swallow filesystem errors. The caller is responsible
    for excluding non-removable system volumes (see :func:`_is_scannable_volume`).
    """
    if is_managed_drive(mount_point):
        return "managed"
    if is_adoption_candidate(mount_point):
        return "candidate"
    return "ignore"


def _scan_volumes() -> tuple[set[str], set[str]]:
    """Scan /Volumes/ once, returning (managed_mounts, candidate_mounts).

    A single directory walk classifies every scannable volume so the monitor
    can track managed mounts and adoption candidates from one pass. Non-removable
    system volumes are skipped up front. Fully OSError-tolerant.
    """
    managed: set[str] = set()
    candidates: set[str] = set()
    try:
        names = os.listdir(VOLUMES_DIR)
    except OSError:
        return managed, candidates
    for name in names:
        if not _is_scannable_volume(name):
            continue
        mount_point = os.path.join(VOLUMES_DIR, name)
        try:
            if not os.path.isdir(mount_point):
                continue
        except OSError:
            continue
        kind = classify_volume(mount_point)
        if kind == "managed":
            managed.add(mount_point)
        elif kind == "candidate":
            candidates.add(mount_point)
    return managed, candidates


def _get_efis_volumes() -> set[str]:
    """Scan /Volumes/ and return set of mount points that are managed drives."""
    managed, _ = _scan_volumes()
    return managed


class USBMonitor:
    """Monitors for EFIS drive mount/unmount by polling /Volumes/."""

    def __init__(self, on_efis_mount: Callable[[str], None],
                 on_efis_unmount: Callable[[str], None],
                 on_adoption_candidate: Optional[Callable[[str], None]] = None):
        """
        Args:
            on_efis_mount: Called with mount_point when a MANAGED EFIS drive
                (valid identity file) is mounted. This is the ONLY mount event
                that triggers auto-action (archive + sync); unmanaged drives
                never fire it (Req 10.4).
            on_efis_unmount: Called with mount_point when a managed EFIS drive
                is unmounted.
            on_adoption_candidate: Optional. Called with mount_point when a
                NON-managed volume that looks like a previously-used GRT chart
                drive (GRTCHARTS/ or ChartData/ but no identity) newly appears.
                Purely a hint so the user knows to run Prepare Drive to adopt it
                (Req 10.5); it triggers NO automatic archive or sync. Fires once
                per appearance (tracked like managed mounts), never for managed
                drives or blank/unrelated volumes. Default None (no hint).
        """
        self.on_efis_mount = on_efis_mount
        self.on_efis_unmount = on_efis_unmount
        self.on_adoption_candidate = on_adoption_candidate
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._known_efis_mounts: set[str] = set()
        self._known_candidates: set[str] = set()
        # Quiesce switch for the mount-swap window. While paused the poll loop
        # takes no action on /Volumes/ churn (fires NO mount/unmount/candidate
        # callbacks), so the transient FSKit unmount + mount_msdos remount +
        # FSKit restore performed by mount_swap does not race the poller or
        # trigger a spurious auto-sync / redundant archive. The flag and the
        # known-mount baselines are shared between the caller's thread (which
        # calls pause()/resume()) and the poll thread, so a lock guards them.
        self._lock = threading.Lock()
        self._paused = False

    def start(self):
        """Start monitoring for USB events in a background thread."""
        if self._running:
            return
        self._running = True

        # Check what's already mounted
        with self._lock:
            self._known_efis_mounts, self._known_candidates = _scan_volumes()
            known_mounts = set(self._known_efis_mounts)
            known_candidates = set(self._known_candidates)
        for mount_point in known_mounts:
            logger.info(f"EFIS drive already mounted: {mount_point}")
            self.on_efis_mount(mount_point)
        for mount_point in known_candidates:
            logger.info(f"Adoption-candidate drive already mounted: {mount_point}")
            self._notify_candidate(mount_point)

        # Start polling thread
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="USBMonitor")
        self._thread.start()
        logger.info("USB monitor started (polling /Volumes/ every 2s).")

    def stop(self):
        """Stop monitoring."""
        self._running = False
        logger.info("USB monitor stopped.")

    def pause(self):
        """Quiesce the poller so it ignores /Volumes/ churn.

        Called by the mount-swap layer BEFORE it unmounts the FSKit volume.
        While paused the poll loop scans nothing and fires NO mount, unmount,
        or adoption-candidate callbacks, so the transient disappearance of the
        volume (FSKit unmount) and its later reappearance (FSKit restore) do
        not fire ``on_efis_unmount`` / ``on_efis_mount`` and do not trigger a
        redundant archive or spurious auto-sync. Idempotent.
        """
        with self._lock:
            self._paused = True
        logger.info("USB monitor paused (mount-swap window).")

    def resume(self):
        """Re-enable the poller, rebaselining current /Volumes/ as existing.

        Called by the mount-swap layer AFTER it has restored the FSKit mount.
        It re-reads /Volumes/ and adopts whatever is currently mounted as the
        EXISTING baseline: the restored managed mount is folded into
        ``_known_efis_mounts`` (and candidates into ``_known_candidates``) so
        the next poll sees NO new mount event for it — no spurious auto-sync,
        no redundant archive. Idempotent.
        """
        with self._lock:
            self._known_efis_mounts, self._known_candidates = _scan_volumes()
            self._paused = False
        logger.info("USB monitor resumed (rebaselined /Volumes/ as existing).")

    def _notify_candidate(self, mount_point: str):
        """Fire the optional adoption-candidate hint, if wired.

        Never raises into the poll loop: a misbehaving handler must not stop
        monitoring.
        """
        if self.on_adoption_candidate is None:
            return
        try:
            self.on_adoption_candidate(mount_point)
        except Exception as e:
            logger.error(f"on_adoption_candidate handler error: {e}")

    def _poll_loop(self):
        """Poll /Volumes/ for changes every 2 seconds."""
        while self._running:
            time.sleep(2)
            try:
                # Quiesce gate: while a mount swap is in progress, take no
                # action on /Volumes/ churn. Snapshot the flag and baselines
                # under the lock so pause()/resume() on another thread cannot
                # tear them mid-iteration. When paused, do not even scan — the
                # transient unmount/remount must produce no callbacks and the
                # baseline is owned by resume() (which rebaselines on exit).
                with self._lock:
                    if self._paused:
                        continue
                    known_mounts = set(self._known_efis_mounts)
                    known_candidates = set(self._known_candidates)

                current, candidates = _scan_volumes()

                # Detect new managed mounts -> auto-action (Req 10.4).
                new_mounts = current - known_mounts
                for mount_point in new_mounts:
                    logger.info(f"EFIS drive mounted: {mount_point}")
                    self.on_efis_mount(mount_point)

                # Detect ejections of managed drives.
                ejected = known_mounts - current
                for mount_point in ejected:
                    logger.info(f"EFIS drive ejected: {mount_point}")
                    self.on_efis_unmount(mount_point)

                # Detect newly-appeared adoption candidates -> hint only, no
                # automatic action (Req 10.5). Fire once per appearance; a
                # candidate that disappears and reappears fires again because
                # it drops out of _known_candidates while absent.
                new_candidates = candidates - known_candidates
                for mount_point in new_candidates:
                    logger.info(
                        f"Unmanaged adoption-candidate drive detected: "
                        f"{mount_point} (no automatic action; use Prepare "
                        f"Drive to adopt)."
                    )
                    self._notify_candidate(mount_point)

                # Commit the new baseline, but only if a concurrent resume()
                # has not paused us again since the scan started. If we became
                # paused mid-iteration, resume() owns the baseline and this
                # scan's result (taken across the swap window) must be
                # discarded to avoid re-introducing the transient churn.
                with self._lock:
                    if not self._paused:
                        self._known_efis_mounts = current
                        self._known_candidates = candidates

            except Exception as e:
                logger.error(f"USB monitor poll error: {e}")
