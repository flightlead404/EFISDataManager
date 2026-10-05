# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Main menu bar application for EFIS Data Manager.

Uses rumps to create a persistent macOS menu bar item with status display,
settings access, and background currency checking.
"""

import json
import logging
import os
import subprocess
import sys
import threading

import rumps

from efis_data_manager import config
from efis_data_manager.config import load_config, save_config
from efis_data_manager.mount_swap import msdos_mount, MountSwapError
from efis_data_manager.notification_history import (
    NotificationHistory,
    format_entry_line,
)

logger = logging.getLogger(__name__)

# Persistent image-state menu item (image-based-drive-write Task 9, Req 10).
# rumps keys a MenuItem by its ORIGINAL title, so the item is created under this
# constant key and is ALWAYS looked up by it; only the displayed ``.title`` is
# toggled between "current" and "new image ready". The badge glyph shown on the
# menu-bar title when a fresh image is pending is distinct from the existing
# status glyphs (↻ active, ● connected-idle, ⚠ alert) — see ``_update_title``.
IMAGE_STATE_CURRENT_TITLE = "USB image current"
IMAGE_READY_BADGE_GLYPH = "\u2b07"  # ⬇ (download/box glyph = fresh image pending)

# Configure logging to file (works regardless of how app is launched)
_log_dir = os.path.expanduser("~/EFIS/DataManagerLogs")
os.makedirs(_log_dir, exist_ok=True)


class RecentErrorHandler(logging.Handler):
    """Logging handler that retains the most recent error/warning records
    in an in-memory ring buffer for display in the menu bar UI."""

    def __init__(self, capacity: int = 10):
        super().__init__(level=logging.WARNING)
        from collections import deque
        self.records = deque(maxlen=capacity)

    def emit(self, record):
        try:
            ts = __import__("datetime").datetime.fromtimestamp(
                record.created
            ).strftime("%Y-%m-%d %H:%M:%S")
            self.records.append(
                f"[{ts}] {record.levelname}: {record.getMessage()}"
            )
        except Exception:
            pass

    def get_recent(self) -> list[str]:
        return list(self.records)


# Shared handler instance so the app can read recent errors
_recent_error_handler = RecentErrorHandler(capacity=10)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_log_dir, "efis_data_manager.log")),
        logging.StreamHandler(),
        _recent_error_handler,
    ],
)


def _install_exception_hooks():
    """Log uncaught exceptions (main thread and worker threads) to the log file.

    Previously an uncaught exception on the main thread killed the app silently
    with no traceback. These hooks ensure the full traceback is always logged.
    """
    def _log_uncaught(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "Uncaught exception (main thread):",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _log_uncaught

    # Thread exceptions (Python 3.8+)
    def _log_thread_exc(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            f"Uncaught exception (thread {args.thread.name if args.thread else '?'}):",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _log_thread_exc


_install_exception_hooks()


class EFISDataManagerApp(rumps.App):
    """Menu bar application for GRT HXr ground support automation."""

    def __init__(self):
        super().__init__(
            name="EFIS Data Manager",
            icon=_menubar_icon_path(),
            template=False,  # color icon (not a monochrome template)
            quit_button=None,
        )
        self.config = load_config()
        # Notification history (persistence enabled by default). Load prior
        # entries from disk so recent notices survive a restart (Req 6).
        self._history = self._load_history()
        self._charts_running = False
        self._nav_running = False
        self._software_running = False
        # Guards a composer refresh so overlapping chart/nav new-data events do
        # not launch concurrent composes (image-based-drive-write Task 8).
        self._compose_running = False
        # Persistent "new image ready" state (image-based-drive-write Task 9,
        # Req 10). `_image_ready` is True from the moment a fresh image is
        # promoted (`_refresh_composed_image`) until the user acts on it; it
        # drives both the persistent menu item title and the menu-bar badge.
        # `_image_ready_label` holds the human-readable "what's new" summary
        # (nav cycle + chart/plates cycle) shown in the menu item.
        self._image_ready = False
        self._image_ready_label = None
        self._charts_first_tick_skipped = False
        self._nav_first_tick_skipped = False
        self._pending_chart_downloads = None
        self._usb_monitor = None
        self._dashboard_process = None
        # Sleep/wake handling (task 11). The active MountWatchdog for an
        # in-progress drive update is held here so a will-sleep notification
        # can mark it at-risk and stop the running rsync safely (Req 7.4).
        self._active_watchdog = None
        self._sleeping = False
        self._sleep_observer = None
        # Operation-in-progress guard (prepare-drive-mount-swap bugfix). Holds
        # the mount keys of drives currently being prepared/adopted so the
        # mount-detect handler (_on_efis_drive_mounted) does not launch a
        # concurrent archive+auto-sync that would race prepare/adopt's own
        # populate write. Lock-protected because the poller fires on a separate
        # thread from the prepare/adopt worker thread.
        self._provisioning_drives = set()
        self._provisioning_lock = threading.Lock()

        self.menu = [
            "Status: Idle",
            "Drive: Not connected",
            "Eject Drive",
            "Verify Drive",
            None,
            "Alerts (0)",
            None,
            "Archive: " + self._short_path(self.config["archive_path"]),
            "USB Image: " + self._short_path(self.config["usb_image_path"]),
            # Persistent image-state line (image-based-drive-write Task 9,
            # Req 10.1): always present. Its TITLE toggles between the "current"
            # text and "New image ready (...)", and its callback is wired only
            # in the ready state. Unlike "Download Charts" (inserted/removed),
            # this item is never added/deleted — only its title + callback flip.
            IMAGE_STATE_CURRENT_TITLE,
            None,
            "Check Charts Now",
            "Check Nav DB Now",
            "Check EFIS/AHRS Software",
            None,
            "Analysis Dashboard...",
            None,
            "Settings...",
            "Seattle Avionics Login...",
            "Prepare Drive...",
            None,
            "Diagnostics...",
            "Recent Notifications...",
            "Recent Errors...",
            "About",
            "Quit",
        ]

        self.menu["Status: Idle"].set_callback(None)
        self.menu["Drive: Not connected"].set_callback(None)
        self.menu["Alerts (0)"].set_callback(None)
        self.menu["Archive: " + self._short_path(self.config["archive_path"])].set_callback(None)
        self.menu["USB Image: " + self._short_path(self.config["usb_image_path"])].set_callback(None)
        # The image-state line starts in the "current" (non-actionable) state:
        # no callback until a fresh image makes it "ready" (Req 10.1, 10.4).
        self.menu[IMAGE_STATE_CURRENT_TITLE].set_callback(None)

        # Sanity-check configured paths at startup (logs warnings)
        self._check_paths()

        # Start USB monitoring
        self._start_usb_monitor()

        # Register system sleep/wake observers (task 11)
        self._register_sleep_wake_observers()

        # Initial state: disable eject + verify if no drive
        if not getattr(self, '_drive_connected', False):
            self.menu["Eject Drive"].set_callback(None)
            self.menu["Verify Drive"].set_callback(None)

    def _check_paths(self):
        """Validate configured paths at startup, logging any concerns.

        Catches issues like a path pointing at a removed CloudStorage/Dropbox
        location, or a non-writable archive directory.
        """
        suspect_substrings = ("CloudStorage", "Dropbox")
        for key in ("archive_path", "usb_image_path"):
            path = self.config.get(key, "")
            if not path:
                logger.warning(f"Config '{key}' is empty.")
                continue
            if any(s in path for s in suspect_substrings):
                logger.warning(
                    f"Config '{key}' points at a cloud-storage path "
                    f"({path}) — this may be stale."
                )
            # Ensure it exists / is creatable
            try:
                os.makedirs(path, exist_ok=True)
                if not os.access(path, os.W_OK):
                    logger.warning(f"Config '{key}' path is not writable: {path}")
            except OSError as e:
                logger.warning(f"Config '{key}' path unusable ({path}): {e}")

    def _short_path(self, path_str: str) -> str:
        from pathlib import Path
        home = str(Path.home())
        if path_str.startswith(home):
            return "~" + path_str[len(home):]
        return path_str

    # ------------------------------------------------------------------
    # Notifications + history (notifications-history feature)
    # ------------------------------------------------------------------

    def _notify(self, title, message):
        """Single path for all notifications.

        Posts the OS notification exactly as before, then records it in the
        history and persists (best-effort). Recording/persisting MUST NOT
        prevent the notification from posting, so the rumps.notification call
        happens first and the record/save is wrapped so any failure there is
        logged but never propagates (Req 1.1, 1.2).
        """
        rumps.notification("EFIS Data Manager", title, message)
        try:
            self._history.add(title, message)
            self._save_history()
        except Exception as e:  # best-effort; never break posting
            logger.warning(f"Failed to record notification history: {e}")

    def _history_path(self):
        return config.APP_SUPPORT_DIR / "notification_history.json"

    def _load_history(self) -> NotificationHistory:
        """Load persisted notification history from disk (Req 6.2, 6.3).

        A missing file or invalid JSON yields an empty history without error.
        """
        config.ensure_dirs()
        path = self._history_path()
        try:
            with open(path, "r") as f:
                loaded = json.load(f)
            return NotificationHistory.from_serializable(loaded)
        except (json.JSONDecodeError, OSError):
            return NotificationHistory.from_serializable([])

    def _save_history(self):
        """Persist the history (<= 50 items) to disk (Req 6.1, 6.4).

        Wrapped in try/except; a failed persist logs a warning and never
        raises so the notification path is never interrupted.
        """
        try:
            config.ensure_dirs()
            with open(self._history_path(), "w") as f:
                json.dump(self._history.to_serializable(), f, indent=2)
        except OSError as e:
            logger.warning(f"Failed to save notification history: {e}")

    # ------------------------------------------------------------------
    # USB Monitor
    # ------------------------------------------------------------------

    def _start_usb_monitor(self):
        """Initialize and start the USB volume monitor."""
        from efis_data_manager.usb_monitor import USBMonitor
        self._usb_monitor = USBMonitor(
            on_efis_mount=self._on_efis_drive_mounted,
            on_efis_unmount=self._on_efis_drive_ejected,
            on_adoption_candidate=self._on_adoption_candidate,
        )
        self._usb_monitor.start()

    def _on_adoption_candidate(self, mount_point: str):
        """A non-managed, adoptable drive appeared — hint only (Req 10.5).

        This fires for a volume that looks like a previously-used GRT chart
        drive (from the Windows tool or an older app version) but carries no
        identity file. It deliberately does NOT auto-archive or auto-sync — a
        drive is only acted on once it is managed (identity present, Req 10.4).
        We just log it and post a non-intrusive notification so the user knows
        why nothing happened and how to adopt it.
        """
        name = os.path.basename(mount_point.rstrip("/")) or mount_point
        logger.info(
            f"Unmanaged drive detected at {mount_point} — no automatic action; "
            f"use Prepare Drive to set it up."
        )
        try:
            self._notify("Unmanaged drive detected", f"{name}: use Prepare Drive to set it up.")
        except Exception as e:
            # Notifications are best-effort; never let a failed toast disrupt
            # monitoring.
            logger.debug(f"Adoption-candidate notification failed: {e}")

    # ------------------------------------------------------------------
    # Sleep / wake handling (task 11)
    # ------------------------------------------------------------------

    def _register_sleep_wake_observers(self):
        """Observe system sleep/wake on the shared NSWorkspace (Req 7.4-7.6).

        Sleep/wake notifications are delivered on ``NSWorkspace``'s own
        notification center, NOT the default one. The observer must be an
        Objective-C object, so we register a small :class:`SleepWakeObserver`
        (an ``NSObject`` subclass) whose selectors delegate back to this app.

        On will-sleep we stop any in-progress sync safely (leaving the
        interrupted marker); on wake we resume via verify+repair. See
        design.md "Sleep / wake": sleep is a "stop then resume" special case of
        the interruption path, relying on the idempotent job rather than trying
        to keep USB I/O alive across sleep.
        """
        try:
            from AppKit import (
                NSWorkspace,
                NSWorkspaceWillSleepNotification,
                NSWorkspaceDidWakeNotification,
            )

            self._sleep_observer = SleepWakeObserver.alloc().initWithApp_(self)
            center = NSWorkspace.sharedWorkspace().notificationCenter()
            center.addObserver_selector_name_object_(
                self._sleep_observer,
                "receiveSleepNotification:",
                NSWorkspaceWillSleepNotification,
                None,
            )
            center.addObserver_selector_name_object_(
                self._sleep_observer,
                "receiveWakeNotification:",
                NSWorkspaceDidWakeNotification,
                None,
            )
            logger.info("Registered NSWorkspace sleep/wake observers.")
        except Exception as e:
            # Sleep/wake handling is a resilience feature; if the observers
            # cannot be registered the core sync still works (mount-presence
            # watchdog and next-mount verify+repair still cover interruptions).
            logger.warning(f"Could not register sleep/wake observers: {e}")

    def _on_will_sleep(self):
        """System is about to sleep — stop the current sync safely (Req 7.4).

        Set the sleeping flag and, if a drive update is in progress, mark its
        MountWatchdog at-risk. The running rsync then aborts on its next
        predicate poll, the commit marker is NOT written, and the durable
        interrupted-sync marker is left in place so the family is retried on
        wake or next mount. We do NOT try to keep rsync alive across sleep.
        """
        logger.info("System will sleep.")
        self._sleeping = True
        watchdog = self._active_watchdog
        if watchdog is not None:
            logger.warning(
                "System sleeping during a drive sync — stopping the current "
                "job safely; interrupted marker left for verify+repair on wake."
            )
            try:
                watchdog.mark_at_risk("system sleeping")
            except Exception as e:
                logger.warning(f"Failed to mark active sync at-risk on sleep: {e}")

    def _on_did_wake(self):
        """System woke — resume any interrupted sync (Req 7.5, 7.6).

        If a drive is still mounted and has pending families (an interrupted
        marker from the sleep-aborted sync, or any prior interruption), trigger
        verify+repair on a background thread. The job is idempotent/resumable,
        so re-running converges the drive to the correct final state.
        """
        logger.info("System did wake.")
        self._sleeping = False
        from efis_data_manager.usb_monitor import is_managed_drive
        from efis_data_manager.drive_updater import pending_families, resolve_drive_id

        try:
            mount_point = None
            for name in os.listdir("/Volumes"):
                path = os.path.join("/Volumes", name)
                if os.path.isdir(path) and is_managed_drive(path):
                    mount_point = path
                    break
            if mount_point is None:
                logger.info("Wake: no managed drive mounted; nothing to resume.")
                return
            # Sync-state is keyed by the drive's durable id, not the mount path.
            # Resolve it first; on an unresolved id (fail-safe) treat as no
            # pending families (Req 10.7) — the real gating is in
            # _run_drive_update, which repeats this resolution.
            drive_id = resolve_drive_id(mount_point)
            pending = pending_families(drive_id) if drive_id is not None else []
            if not pending:
                logger.info(
                    f"Wake: {mount_point} has no pending families; nothing to "
                    "resume."
                )
                return
            logger.info(
                f"Wake: resuming interrupted sync on {mount_point} "
                "(verify+repair)."
            )
            threading.Thread(
                target=self._run_drive_update, args=(mount_point,), daemon=True
            ).start()
        except Exception as e:
            logger.warning(f"Wake resume check failed: {e}")

    # ------------------------------------------------------------------
    # Operation-in-progress guard (prepare-drive-mount-swap bugfix)
    # ------------------------------------------------------------------
    def _mark_provisioning(self, key: str):
        """Register ``key`` as a drive currently being prepared/adopted.

        Called before the format/identity/populate span begins so the
        mount-detect handler suppresses a concurrent auto-sync for the drive.
        """
        if not key:
            return
        with self._provisioning_lock:
            self._provisioning_drives.add(key)

    def _unmark_provisioning(self, key: str):
        """Unregister ``key`` once prepare/adopt has finished (success or fail)."""
        if not key:
            return
        with self._provisioning_lock:
            self._provisioning_drives.discard(key)

    def _is_provisioning(self, mount_point: str) -> bool:
        """Return whether ``mount_point`` is currently being prepared/adopted.

        Matches on the full mount-point string AND its basename, so a drive
        registered under its ``/Volumes/<label>`` mount path is detected
        regardless of whether the caller passes the full path or a bare label.
        """
        if not mount_point:
            return False
        base = os.path.basename(os.path.normpath(mount_point))
        with self._provisioning_lock:
            if not self._provisioning_drives:
                return False
            # Match if the query (full path or its basename) equals any stored
            # key by full value OR by basename, so a drive registered under its
            # /Volumes/<label> mount path is found whether the caller passes the
            # full path or a bare label, and vice versa.
            for key in self._provisioning_drives:
                if mount_point == key or base == key:
                    return True
                key_base = os.path.basename(os.path.normpath(key))
                if mount_point == key_base or base == key_base:
                    return True
            return False

    def _on_efis_drive_mounted(self, mount_point: str):
        """Called when an EFIS drive is detected."""
        logger.info(f"EFIS drive mounted: {mount_point}")
        # Serialize against explicit prepare/adopt (prepare-drive-mount-swap
        # bugfix): if a prepare/adopt owns this drive, its own populate is
        # already writing through the mount swap. Launching archive+auto-sync
        # here would race it (auto-sync's swap unmounts the volume out from
        # under prepare). Skip and let the provisioning flow own the drive.
        if self._is_provisioning(mount_point):
            logger.info(
                f"prepare/adopt in progress for {mount_point}; "
                "skipping auto-archive/sync"
            )
            return
        self._set_drive_status(f"Connected: {mount_point}")
        self._set_status("EFIS drive detected")
        self._notify("EFIS Drive Detected", f"Drive mounted at {mount_point}. Starting archive...")

        # An interrupted prior sync is detected durably by drive_updater's
        # sync-state (pending_families) and handled inside _run_drive_update:
        # if this drive has pending families, that method runs verify+repair
        # before declaring the drive current (Req 6.3, 3.5). No local
        # .sync_in_progress file is managed here anymore.
        # sync-state is keyed by the drive's durable id (not the mount path),
        # so resolve the id before consulting pending_families. This is purely
        # informational logging; on an unresolved id (fail-safe) treat as no
        # pending families (Req 10.7). The real gating happens in
        # _run_drive_update.
        from efis_data_manager.drive_updater import (
            pending_families,
            resolve_drive_id,
        )
        drive_id = resolve_drive_id(mount_point)
        if drive_id is not None and pending_families(drive_id):
            logger.info(
                "Previous sync was interrupted for this drive — verify+repair "
                "will run after archive."
            )

        # Start archive in background thread
        threading.Thread(target=self._run_archive, args=(mount_point,), daemon=True).start()

    def _on_efis_drive_ejected(self, mount_point: str):
        """Called when an EFIS drive is ejected."""
        logger.info(f"EFIS drive ejected: {mount_point}")
        self._set_drive_status("Not connected")
        self._set_status("Idle")
        self._notify("EFIS Drive Ejected", "Drive removed safely.")

    def eject_drive(self, _):
        """Eject the currently mounted EFIS drive.

        Discovery + guards run here (fast); the actual ``diskutil eject`` runs
        on a background thread (``_run_eject``) because macOS can take a minute+
        to flush buffered writes after a large sync, and a synchronous eject on
        the main thread freezes the whole menu bar until it returns.
        """
        if not getattr(self, '_drive_connected', False):
            self._notify("No Drive", "No EFIS drive is connected.")
            return

        from efis_data_manager.usb_monitor import is_managed_drive
        mount_point = None
        # Find the currently connected managed drive (identity-only detection).
        for name in os.listdir("/Volumes"):
            path = os.path.join("/Volumes", name)
            if os.path.isdir(path) and is_managed_drive(path):
                mount_point = path
                break

        if not mount_point:
            self._notify("No Drive", "No EFIS drive found to eject.")
            return

        # Guard against double-clicks kicking off two ejects.
        if getattr(self, "_ejecting", False):
            return
        self._ejecting = True
        self._set_status("Ejecting...")
        threading.Thread(
            target=self._run_eject, args=(mount_point,), daemon=True
        ).start()

    def _run_eject(self, mount_point: str):
        """Eject ``mount_point`` on a background thread (slow flush + eject).

        Ejects the whole physical device (parent of the mounted partition) when
        it can be resolved. The USB monitor's unmount callback normally clears
        the drive status once macOS reports the volume gone; we set a
        "Safe to remove" status on success as immediate feedback.
        """
        import subprocess
        try:
            # Get the disk identifier first, then eject the whole physical device
            info_result = subprocess.run(
                ["diskutil", "info", "-plist", mount_point],
                capture_output=True, timeout=10
            )
            parent_disk = ""
            if info_result.returncode == 0:
                import plistlib
                try:
                    info = plistlib.loads(info_result.stdout)
                    # Get parent whole disk (e.g. disk4 from disk4s1)
                    parent_disk = info.get("ParentWholeDisk", "")
                except Exception:
                    parent_disk = ""

            target = f"/dev/{parent_disk}" if parent_disk else mount_point
            result = subprocess.run(
                ["diskutil", "eject", target],
                capture_output=True, text=True
            )

            if result.returncode == 0:
                logger.info(f"Ejected {mount_point}")
                self._set_status("Safe to remove")
                self._notify("EFIS Drive Ejected", "Safe to remove the drive.")
            else:
                self._set_status("Idle")
                self._notify("Eject Failed", result.stderr.strip()[:100] or "Unknown error")
        except Exception as e:
            logger.error(f"Eject failed for {mount_point}: {e}")
            self._set_status("Idle")
            self._notify("Eject Failed", str(e)[:100])
        finally:
            self._ejecting = False

    def verify_drive_menu(self, _):
        """User clicked "Verify Drive": run an on-demand exhaustive verify.

        Verify-only (``deep=False``, no repair) per Req 6.4 — repair happens via
        the interrupted-sync path in ``_run_drive_update``. Finds the connected
        EFIS mount (same discovery as ``eject_drive``), then runs
        ``verify_drive`` on a background thread since a full count+size walk can
        be slow, reporting per-family results via notification/status.
        """
        from efis_data_manager.usb_monitor import is_managed_drive
        mount_point = None
        # Find the currently connected managed drive (identity-only detection).
        for name in os.listdir("/Volumes"):
            path = os.path.join("/Volumes", name)
            if os.path.isdir(path) and is_managed_drive(path):
                mount_point = path
                break

        if not mount_point:
            self._notify("No Drive", "No EFIS drive is connected.")
            return

        threading.Thread(
            target=self._run_verify_drive, args=(mount_point,), daemon=True
        ).start()

    def _run_verify_drive(self, mount_point: str):
        """Run an exhaustive (count+size) verify of the drive and report results.

        Verify-only: no auto-repair here (Req 6.4). On a clean result the status
        returns to "Drive current"; if any family shows discrepancies they are
        reported per-family (via ``_format_verify_summary``) and the status
        names that the drive needs an update.
        """
        from efis_data_manager.drive_updater import (
            verify_drive,
            _format_verify_summary,
        )

        def on_progress(msg):
            self._set_status(msg)

        try:
            self._set_status("Verifying drive...")
            result = verify_drive(
                mount_point, deep=False, progress_callback=on_progress
            )
            summary = _format_verify_summary(result["families"])

            if result["clean"]:
                logger.info(f"Verify Drive: clean ({summary}).")
                self._notify("Drive Verified", f"Drive is complete: {summary}")
                self._set_status("Drive current")
            else:
                # Discrepancies found — report them; repair happens via the
                # interrupted-sync path, not this verify-only action (Req 6.4).
                logger.warning(f"Verify Drive found discrepancies: {summary}")
                self._notify("Drive Verify: Discrepancies", f"{summary}\nRe-connect or re-run update to repair.")
                self._set_status("Drive needs update")
        except Exception as e:
            logger.error(f"Verify Drive failed: {e}")
            self._notify("Verify Drive Failed", str(e)[:100])
            self._set_status("Verify failed")

    def _run_archive(self, mount_point: str):
        """Run the archive process on a mounted EFIS drive, then update."""
        from efis_data_manager.archiver import archive_efis_drive

        self._set_status("Archiving EFIS data...")

        def on_progress(msg):
            self._set_status(msg)

        try:
            results = archive_efis_drive(mount_point, progress_callback=on_progress)

            # Build summary
            total_moved = results["fdl_moved"] + results["demo_moved"] + results["snap_moved"]
            total_copied = results["logbook_copied"] + results["settings_copied"]
            parts = []
            if results["fdl_moved"]:
                parts.append(f"{results['fdl_moved']} FDL")
            if results["demo_moved"]:
                parts.append(f"{results['demo_moved']} demo")
            if results["snap_moved"]:
                parts.append(f"{results['snap_moved']} snap")
            if results["logbook_copied"]:
                parts.append(f"{results['logbook_copied']} logbook")
            if results["settings_copied"]:
                parts.append(f"{results['settings_copied']} settings")

            if total_moved + total_copied == 0 and not results["errors"]:
                msg = "No new files to archive."
            else:
                msg = f"Archived: {', '.join(parts)}."
                if results["skipped"]:
                    msg += f" ({results['skipped']} already archived)"
                if results["cleaned"]:
                    msg += f" Cleaned: {', '.join(results['cleaned'])}."

            if results["errors"]:
                self._notify("Archive Complete (with errors)", f"{msg}\n{len(results['errors'])} error(s).")
                self._set_status("Archive errors")
            else:
                self._notify("Archive Complete", msg)

            # Data is now imported into the analysis DB. Signal that flight
            # data is ready to analyze — independent of the (slow) chart sync.
            self._refresh_alerts()
            if results.get("fdl_imported"):
                self._notify("Flight Data Ready", f"Imported {results['fdl_imported']} operation(s). "
                    "Open the dashboard to analyze — chart sync continues in background.")

            # Kick off the drive update (long chart sync) on its OWN thread so
            # the archive thread finishes cleanly and analysis isn't blocked.
            threading.Thread(
                target=self._run_drive_update, args=(mount_point,), daemon=True
            ).start()

        except Exception as e:
            logger.error(f"Archive failed: {e}")
            self._notify("Archive Failed", str(e)[:100])
            self._set_status("Archive failed")

    def _run_drive_update(self, mount_point: str):
        """Check drive currency and sync updates if needed.

        Runs after the archive step on the update thread. Sequence:

          1. If a prior sync was interrupted for this drive (``pending_families``
             names it), run an exhaustive verify+repair on those families FIRST,
             so we never declare the drive current on a partial state
             (Req 6.3, 3.5). The durable sync-state is owned by drive_updater's
             sync-state helpers — no local ``.sync_in_progress`` file here.
          2. Quick currency check. If current AND nothing pending -> idle.
          3. Sync only the stale families, then derive a single terminal status
             from the per-family jobs via ``_terminal_status_from_jobs`` — no
             sticky error strings on success (Req 8.4). Every error surfaced in
             status is logged >= WARNING (the job driver already logs job errors;
             any status string we build here is logged too — Req 8.1/8.3).

        Watchdog ownership (tasks 7 & 11, hardened): each drive operation
        (verify+repair, then the update) runs under its OWN fresh
        :class:`MountWatchdog`, registered on ``self._active_watchdog`` for its
        duration via :meth:`_with_active_watchdog`. This is deliberate: a
        watchdog latch is permanent, so reusing ONE watchdog across the whole
        method meant a transient mount blip during the first operation poisoned
        every later operation (observed as an instant false-abort on re-mount).
        A fresh watchdog per operation reflects CURRENT mount state; the
        debounced removal detection (WATCHDOG_SETTLE_POLLS) tolerates a settling
        volume. Sleep still works: ``_on_will_sleep`` marks whichever watchdog
        is active at-risk, stopping the running rsync safely and leaving the
        interrupted marker (Req 7.2-7.6).

        Mount-swap wiring (chart-sync-stall-fix task 5): the identity/currency
        READS above run on the normal FSKit mount BEFORE any swap. Only if write
        work is actually needed (interrupted families to repair, or stale
        families to update) does the write window run inside
        ``with msdos_mount(mount_point, poller=self._usb_monitor) as
        work_mount:`` — the engine then runs against the private in-kernel
        msdosfs ``work_mount`` (which does not stall), and each write-window
        watchdog monitors ``work_mount`` (NOT the swapped-out FSKit mount). If
        nothing needs writing, the swap is never entered (idempotent no-op,
        Req 5.4). If the swap cannot be established, :class:`MountSwapError` is
        caught: the sync is reported incomplete, interrupted state is left
        intact, and the drive is never declared current (Req 7).
        """
        # update_drive / verify_drive / _terminal_status_from_jobs moved to the
        # write branch (`_write_via_mount_swap`, Task 10); only the currency /
        # sync-state reads are needed here now.
        from efis_data_manager.drive_updater import (
            check_drive_currency,
            pending_families,
            resolve_drive_id,
        )

        def on_progress(msg):
            self._set_status(msg)

        try:
            # --- 1. READ-only identity / currency reads (on the FSKit mount). --
            # These are cheap reads and, per design ("Where the mount-swap layer
            # lives" / "Preserving correctness"), run against the NORMAL FSKit
            # /Volumes/ mount BEFORE any swap. sync-state is keyed by the drive's
            # durable id, not the mount path. Resolve the id once; on an
            # unresolved id (fail-safe None — a diskutil hiccup or a
            # non-EFIS/unreadable volume) apply NO interrupted-sync state (never
            # another drive's) and fall through to the normal quick check
            # (Req 10.7). On-drive markers/payload remain the sole source of
            # truth for currency.
            drive_id = resolve_drive_id(mount_point)
            pending = pending_families(drive_id) if drive_id is not None else []
            if pending:
                logger.warning(
                    f"Previous sync was interrupted for {mount_point} "
                    f"({', '.join(pending)}); running verify+repair before "
                    "declaring current."
                )

            self._set_status("Checking drive currency...")
            currency = check_drive_currency(mount_point)
            stale = [
                name
                for name, detail in currency["families"].items()
                if not detail["current"]
            ]

            # --- 2. Idempotent no-op path (Req 5.4 / 6.3). --------------------
            # If nothing was interrupted AND nothing is stale, there is NO write
            # work to do. Report current and return WITHOUT entering the swap —
            # the swap only wraps the write window.
            if not pending and currency["is_current"]:
                logger.info("Drive is up to date, no sync needed.")
                self._set_status("Drive current")
                self._notify("Drive Current", "EFIS drive is up to date.")
                return

            # --- 3. Write work is needed: perform the chart/nav write. --------
            # The CURRENCY decision above (steps 1-2) is unchanged — image-write
            # replaces only HOW a needed write happens, not WHETHER (Req 12.1,
            # 12.3). `_perform_write` dispatches to the image-write path when
            # enabled (Req 11.1), otherwise to the msdos_mount swap (Req 11.2),
            # and falls back to the swap if an image-write attempt fails and the
            # fallback is enabled (Req 11.3). It owns all status/notify/watchdog
            # /MountSwapError handling for the write window.
            self._perform_write(
                mount_point, pending, stale, currency, on_progress
            )

        except Exception as e:
            logger.error(f"Drive update failed: {e}")
            # The durable sync-state is left intact by the job driver on failure,
            # so an interrupted family is retried on the next mount.
            self._notify("Drive Update Failed", str(e)[:100])
            self._set_status("Update failed")

    # ------------------------------------------------------------------
    # Chart/nav write dispatch (image-based-drive-write Task 10, Req 11/12)
    # ------------------------------------------------------------------
    #
    # The write mechanism is factored out of `_run_drive_update` so the currency
    # decision (steps 1-2) stays byte-for-byte unchanged and only the write
    # itself is pluggable. `_perform_write` chooses the branch; the two branches
    # encapsulate "do the chart/nav write and report a terminal status":
    #
    #   * `_write_via_image`      — compose-current-image guard->dd->verify path,
    #                               used when `image_write_enabled` is True.
    #   * `_write_via_mount_swap` — the EXISTING msdos_mount + update_drive /
    #                               verify_drive block, moved VERBATIM so the
    #                               proven path is preserved exactly.

    def _perform_write(self, mount_point, pending, stale, currency, on_progress):
        """Perform the needed chart/nav write, dispatching by config (Req 11).

        Called from `_run_drive_update` once the currency decision has
        established that a write is actually needed (interrupted families to
        repair, or stale families to update). This method does NOT re-decide
        whether a write is needed — that is preserved in steps 1-2 (Req 12.1,
        12.3). It only selects HOW the write happens:

          * `image_write_enabled` True  -> `_write_via_image` (Req 11.1): the
            composed-image guard->dd->verify path; the msdos_mount swap is NOT
            used for the write.
          * `image_write_enabled` False -> `_write_via_mount_swap` (Req 11.2):
            the fallback flag selects the proven swap write path.
          * image-write attempt FAILS (returns False) AND the fallback is
            enabled -> fall back to `_write_via_mount_swap` (Req 11.3). The
            fallback is "enabled" exactly when the mount-swap path is retained
            (it always is, Req 11.4); if image-write fails and fallback were
            ever disabled, we leave interrupted state intact and never declare
            current (handled inside `_write_via_image`).

        DEFAULT is the safe existing path: `image_write_enabled` defaults to
        False (see config.DEFAULT_CONFIG) until the Task 6 safety checkpoint and
        Task 12 hardware harness bless the image-write path.
        """
        image_write_enabled = self.config.get(
            "image_write_enabled",
            config.DEFAULT_CONFIG["image_write_enabled"],
        )
        if not image_write_enabled:
            # Fallback flag selects the proven swap path (Req 11.2).
            self._write_via_mount_swap(
                mount_point, pending, stale, currency, on_progress
            )
            return

        # Image-write enabled: use the composed-image path (Req 11.1).
        ok = self._write_via_image(
            mount_point, pending, stale, currency, on_progress
        )
        if not ok:
            # The image-write attempt failed (WriteResult.status != "complete"
            # or an exception). The mount-swap path is always retained (Req
            # 11.4), so the fallback is enabled: fall back to the swap write
            # (Req 11.3). `_write_via_image` has already left interrupted state
            # intact and declared nothing current, so re-running the swap path
            # converges the drive via the proven mechanism.
            logger.warning(
                "Image-write failed for %s; falling back to the msdos_mount "
                "swap write path (Req 11.3).",
                mount_point,
            )
            self._set_status("Image write failed; using mount-swap fallback...")
            self._write_via_mount_swap(
                mount_point, pending, stale, currency, on_progress
            )

    def _resolve_write_target(self, mount_point):
        """Resolve the dd device node + guard identity/label for a mount.

        The image writer needs the drive's ``/dev/diskNsN`` partition-slice node
        (it maps to the raw node internally) plus the guard expectations — the
        OS-level ``VolumeUUID`` and ``VolumeName`` — all read from the mount via
        the existing diskutil-backed resolvers. No mount of the raw slice is
        performed; these are read from the live FSKit ``/Volumes/`` mount BEFORE
        any write, consistent with the identity/currency reads above.

        Returns a tuple ``(device_node, volume_uuid, volume_label)``. A None
        ``device_node`` means the node could not be resolved (a diskutil hiccup
        or a non-removable volume); the caller treats that as an image-write
        failure and falls back.
        """
        from efis_data_manager.mount_swap import resolve_device_node
        from efis_data_manager.drive_updater import _volume_uuid, _volume_name

        device_node = resolve_device_node(mount_point)
        volume_uuid = _volume_uuid(mount_point)
        volume_label = _volume_name(mount_point)
        return device_node, volume_uuid, volume_label

    def _write_via_image(self, mount_point, pending, stale, currency, on_progress):
        """Write the composed current image to the drive's raw slice (Req 11.1).

        The image-write path: take the ALREADY-composed current image
        (``config["composed_image_path"]``) and ``dd`` it to the drive's raw
        partition slice via :func:`image_writer.write`, relying on the raw-
        readback verify inside ``write()`` to confirm completion. No msdos_mount
        swap is used for this write.

        The whole image is the unit of work (Req 13.1): a single ``dd`` carries
        every family, so ``pending`` / ``stale`` do not sub-select here — any
        needed write re-lays the whole current image. The on-drive app identity
        rides in the verified image bytes (composed in by Task 1.3), so the
        guard matches the physical slice by OS-level ``VolumeUUID`` while the
        verifier confirms the exact bytes landed.

        Device node + guard expectations are resolved from the mount via
        diskutil (``_resolve_write_target``): ``device_node`` = ``/dev/diskNsN``,
        ``expected_identity_id`` = ``VolumeUUID``, ``expected_label`` =
        ``VolumeName``, ``expected_size_bytes`` = the configured composed-image
        size (the guard independently re-verifies size against the real slice).

        ``confirmed=True`` is passed because THIS flow IS the authorization: the
        auto-sync-on-insert path (archive -> `_run_drive_update`) is the user/
        flow saying "this inserted managed drive is the intended sync target".
        The device guard still independently re-verifies removable + FAT32 +
        size + label + UUID, so ``confirmed=True`` means "this is the intended
        target", NOT "bypass the guard".

        Returns True IFF the write completed and verified (``WriteResult.status
        == "complete"``). On any failure (resolve failure, guard refusal,
        ``dd``/verify failure, or an exception) it reports the write incomplete,
        leaves interrupted sync-state intact, declares nothing current, and
        returns False so `_perform_write` can fall back (Req 11.3, 13.3).
        """
        from efis_data_manager import image_writer

        image_path = self.config.get(
            "composed_image_path",
            config.DEFAULT_CONFIG["composed_image_path"],
        )
        expected_size_bytes = self.config.get(
            "composed_image_size_bytes",
            config.DEFAULT_CONFIG["composed_image_size_bytes"],
        )

        device_node, volume_uuid, volume_label = self._resolve_write_target(
            mount_point
        )
        if not device_node:
            logger.warning(
                "Image-write: could not resolve a device node for %s; "
                "treating as image-write failure.",
                mount_point,
            )
            return False

        affected = ", ".join(pending or stale) or "chart data"
        self._set_status("Writing image to drive...")
        self._notify(
            "Writing Drive Image",
            f"Writing composed image to {os.path.basename(device_node)} "
            f"({affected}).",
        )

        try:
            result = image_writer.write(
                image_path,
                device_node,
                expected_size_bytes=expected_size_bytes,
                expected_label=volume_label,
                expected_identity_id=volume_uuid,
                # The auto-sync-on-insert flow is the authorization; the guard
                # still independently re-verifies the target (see docstring).
                confirmed=True,
            )
        except Exception as e:
            # Any unexpected error from the write path is an image-write failure;
            # the local image/state are untouched (Req 13.2), nothing is declared
            # current, and the caller falls back to the swap path.
            logger.error(f"Image write raised for {mount_point}: {e}")
            self._notify(
                "Drive Image Write Failed",
                f"Image write errored for {affected}; trying fallback.",
            )
            self._set_status("Image write failed")
            return False

        if result.status == "complete":
            logger.info(
                "Image write to %s complete and verified (%d bytes).",
                device_node,
                result.bytes_written,
            )
            self._notify(
                "Drive Image Written",
                "Composed image written and verified. Drive is current.",
            )
            self._set_status("Drive current")
            return True

        # status in {"incomplete", "aborted"}: the drive is NOT current. Leave
        # interrupted state intact (we never clear sync-state here), surface the
        # safe next step, and signal failure so the caller can fall back
        # (Req 11.3, 13.3).
        logger.warning(
            "Image write to %s did not complete (status=%s): %s",
            device_node,
            result.status,
            result.error,
        )
        self._notify(
            "Drive Image Write Incomplete",
            f"Image write {result.status} for {affected}; "
            "the drive is not current.",
        )
        self._set_status("Drive image write incomplete")
        return False

    def _write_via_mount_swap(
        self, mount_point, pending, stale, currency, on_progress
    ):
        """The EXISTING msdos_mount swap write path (Req 11.2 fallback).

        Moved VERBATIM out of `_run_drive_update` step 3 so the proven path is
        preserved byte-for-byte: run the write window against the private
        in-kernel msdosfs ``work_mount`` for the whole span (Req 5.1, 5.2), honor
        an interrupted sync with verify+repair first, then sync only the stale
        families, deriving a single terminal status from the per-family jobs.
        All status/notify/watchdog/MountSwapError handling is identical to the
        original inline block.
        """
        from efis_data_manager.drive_updater import (
            update_drive,
            verify_drive,
            _terminal_status_from_jobs,
        )

        # The /Volumes/ poller is quiesced across the swap via the poller hook
        # (guard for a not-yet-initialized monitor).
        poller = getattr(self, "_usb_monitor", None)
        try:
            with msdos_mount(mount_point, poller=poller) as work_mount:
                # --- Honor an interrupted sync: verify+repair first. ----------
                # The watchdog must monitor work_mount, NOT the original
                # /Volumes/ mount_point: during the swap the FSKit mount is
                # gone, so watching it would instantly false-abort.
                if pending:
                    self._set_status("Verifying drive after interrupted sync...")
                    self._notify(
                        "Verifying Drive",
                        f"Resuming interrupted sync: {', '.join(pending)}.",
                    )
                    repair = self._with_active_watchdog(
                        work_mount,
                        lambda is_aborted: verify_drive(
                            work_mount, families=pending, repair=True,
                            progress_callback=on_progress, is_aborted=is_aborted,
                        ),
                    )
                    # verify_drive already logs its repair errors at >= WARNING.
                    if not repair["clean"]:
                        status = "Drive verify incomplete"
                        logger.warning(
                            f"Verify+repair left discrepancies on {mount_point}: "
                            f"{repair['families']}"
                        )
                        self._notify(
                            "Verify Incomplete",
                            "Some families still differ; will retry on next mount.",
                        )
                        self._set_status(status)
                        return

                # --- Sync only the stale families. ----------------------------
                if not stale:
                    # Interrupted families were repaired clean and nothing is
                    # stale: no update work remains. Report current.
                    logger.info("Drive is up to date after verify+repair.")
                    self._set_status("Drive current")
                    self._notify("Drive Current", "EFIS drive is up to date.")
                    return

                stale_summary = ", ".join(currency["stale_items"][:3])
                if len(currency["stale_items"]) > 3:
                    stale_summary += f" +{len(currency['stale_items']) - 3} more"
                self._set_status("Updating drive...")
                self._notify(
                    "Updating Drive",
                    f"{len(stale)} family(ies) to update: {stale_summary}",
                )

                # Again, watch work_mount (the live mount during the swap).
                results = self._with_active_watchdog(
                    work_mount,
                    lambda is_aborted: update_drive(
                        work_mount, families=stale, progress_callback=on_progress,
                        is_aborted=is_aborted,
                    ),
                )
        except MountSwapError as e:
            # The swap could not be established or failed partway; msdos_mount
            # has already driven teardown toward "FSKit restored" (or logged
            # loudly and left the device flushed). The write window did NOT
            # run: report the sync incomplete with safe next-step guidance,
            # leave the interrupted sync-state intact (the engine leaves it on
            # failure — we never clear it here), and NEVER declare current
            # (Req 7.1, 7.2, 7.4, 7.5).
            logger.error(f"Mount swap failed for {mount_point}: {e}")
            affected = ", ".join(pending or stale) or "chart data"
            self._notify(
                "Drive Sync Incomplete",
                f"Could not prepare drive for sync ({affected}); reinsert and retry.",
            )
            self._set_status("Drive sync incomplete")
            return

        jobs = results["jobs"]
        updated = sum(r.files_updated for r in jobs.values())
        status = _terminal_status_from_jobs(jobs, results["aborted"])

        if results["errors"] or results["aborted"]:
            # The job driver already logged each job error at >= WARNING;
            # log the terminal status too so it always has a matching record
            # in Recent Errors (Req 8.1/8.3).
            logger.warning(f"Drive update finished with issues: {status}")
            self._notify("Drive Update Complete (errors)", f"Updated {updated} item(s), "
                               f"{len(results['errors'])} error(s).")
            self._set_status(status)
        else:
            # Clean terminal state -> current, never a sticky error (Req 8.4).
            self._notify("Drive Update Complete", f"Updated {updated} item(s). Drive is current.")
            self._set_status(status)

    def _with_active_watchdog(self, mount_point: str, run):
        """Run ``run(is_aborted)`` under a fresh, registered MountWatchdog.

        ``mount_point`` is the path the watchdog monitors via
        ``os.path.ismount``. During a mount swap (see :meth:`_run_drive_update`)
        this MUST be the private ``work_mount`` the write window runs against,
        NOT the original FSKit ``/Volumes/`` mount: while swapped, the FSKit
        mount is gone, so watching it would latch an instant false-abort. Outside
        a swap it is the live ``/Volumes/`` mount as before.

        A new :class:`MountWatchdog` is started for this single operation and
        published on ``self._active_watchdog`` so ``_on_will_sleep`` can mark
        exactly the running operation at-risk. The watchdog is always stopped
        and de-registered when the operation returns, so a latch never leaks
        into the next operation (the bug that made a transient mount blip during
        verify poison the subsequent update). ``run`` receives the watchdog's
        ``is_aborted`` predicate.
        """
        from efis_data_manager.drive_updater import MountWatchdog

        watchdog = MountWatchdog(mount_point).start()
        self._active_watchdog = watchdog
        try:
            return run(watchdog.is_aborted)
        finally:
            try:
                watchdog.stop()
            except Exception:
                pass
            if self._active_watchdog is watchdog:
                self._active_watchdog = None

    # ------------------------------------------------------------------
    # Timers (skip first tick, dispatch to background threads)
    # ------------------------------------------------------------------

    @rumps.timer(43200)
    def _check_charts_timer(self, _):
        if not self._charts_first_tick_skipped:
            self._charts_first_tick_skipped = True
            logger.info("Skipping first chart timer tick (startup delay).")
            return
        if not self._charts_running:
            logger.info("Scheduled chart check triggered.")
            threading.Thread(target=self._run_chart_check_auto, daemon=True).start()

    @rumps.timer(86400)
    def _check_nav_db_timer(self, _):
        if not self._nav_first_tick_skipped:
            self._nav_first_tick_skipped = True
            logger.info("Skipping first nav DB timer tick (startup delay).")
            return
        if not self._nav_running:
            logger.info("Scheduled nav DB check triggered.")
            threading.Thread(target=self._run_nav_db_check, daemon=True).start()

    @rumps.timer(30)
    def _startup_check(self, timer):
        """One-time startup check — runs all currency checks 30s after launch."""
        timer.stop()  # Only run once
        logger.info("Startup check: running all currency checks...")
        # Refresh alerts on startup
        self._refresh_alerts()
        if not self._charts_running:
            # Startup is a deliberate "state of everything" moment, so report
            # charts-current explicitly (matches nav DB / software). The silent
            # 24h scheduled tick leaves notify_if_current at its False default.
            threading.Thread(
                target=self._run_chart_check_auto,
                kwargs={"notify_if_current": True},
                daemon=True,
            ).start()
        if not self._nav_running:
            threading.Thread(target=self._run_nav_db_check, daemon=True).start()
        if not self._software_running:
            threading.Thread(target=self._run_software_check, daemon=True).start()

    # ------------------------------------------------------------------
    # Manual triggers
    # ------------------------------------------------------------------

    @rumps.clicked("Check Charts Now")
    def check_charts_now(self, _):
        if self._charts_running:
            logger.info("Chart check requested but already running.")
            self._notify("Busy", "Chart check is already running.")
            return
        logger.info("Manual chart check triggered.")
        threading.Thread(target=self._run_chart_check_manual, daemon=True).start()

    @rumps.clicked("Check Nav DB Now")
    def check_nav_db_now(self, _):
        if self._nav_running:
            logger.info("Nav DB check requested but already running.")
            self._notify("Busy", "Nav DB check is already running.")
            return
        logger.info("Manual nav DB check triggered.")
        threading.Thread(target=self._run_nav_db_check, daemon=True).start()

    @rumps.clicked("Check EFIS/AHRS Software")
    def check_efis_software(self, _):
        if self._software_running:
            logger.info("Software check requested but already running.")
            self._notify("Busy", "Software check is already running.")
            return
        logger.info("Manual software check triggered.")
        threading.Thread(target=self._run_software_check, daemon=True).start()

    # ------------------------------------------------------------------
    # Chart check (manual — check only, then offer download via menu item)
    # ------------------------------------------------------------------

    def _run_chart_check_manual(self):
        from efis_data_manager.currency import check_chart_currency, PageLayoutChangedError

        self._charts_running = True
        self._set_status("Checking charts...")

        try:
            all_entries, new_entries = check_chart_currency()

            if not new_entries:
                self._notify("Charts Current", f"All {len(all_entries)} chart data sets are up to date.")
                self._set_status("Idle")
                return

            # Notify user, add "Download Charts" menu item
            names = ", ".join(e["description"] for e in new_entries)
            self._pending_chart_downloads = new_entries
            self._notify(f"{len(new_entries)} Chart Update(s) Available", f"{names}\n\nClick 'Download Charts' in the EFIS menu to start.")
            self._set_status(f"{len(new_entries)} chart update(s) available")

            # Add download menu item on main thread
            from AppKit import NSOperationQueue
            def _add_menu_item():
                if "Download Charts" not in self.menu:
                    self.menu.insert_after("Check Charts Now",
                                           rumps.MenuItem("Download Charts",
                                                          callback=self._on_download_charts))
            NSOperationQueue.mainQueue().addOperationWithBlock_(_add_menu_item)

        except PageLayoutChangedError:
            self._notify("Chart Check Failed - Page Changed", "Seattle Avionics page layout may have changed.")
            self._set_status("Alert: Chart page changed")
        except Exception as e:
            logger.error(f"Chart check failed: {e}")
            self._notify("Chart Check Failed", str(e)[:100])
            self._set_status("Chart check failed")
        finally:
            self._charts_running = False

    def _on_download_charts(self, _):
        """User clicked Download Charts menu item."""
        if not self._pending_chart_downloads:
            return
        if self._charts_running:
            self._notify("Busy", "A check is already running.")
            return
        entries = self._pending_chart_downloads
        self._pending_chart_downloads = None
        # Remove menu item on main thread
        from AppKit import NSOperationQueue
        def _remove_menu_item():
            if "Download Charts" in self.menu:
                del self.menu["Download Charts"]
        NSOperationQueue.mainQueue().addOperationWithBlock_(_remove_menu_item)
        threading.Thread(target=self._do_chart_download, args=(entries,), daemon=True).start()

    def _do_chart_download(self, entries):
        from efis_data_manager.currency import download_charts

        self._charts_running = True
        self._set_status("Downloading charts...")
        self._notify("Chart Download Started", f"Downloading {len(entries)} chart update(s)...")

        def on_progress(filename, pct):
            short_name = filename.split(".")[0] if "." in filename else filename
            self._set_status(f"Downloading {short_name}... {pct}%")

        try:
            results = download_charts(entries, progress_callback=on_progress)
            if results["errors"]:
                self._notify("Chart Download Complete (errors)", f"Downloaded {results['downloaded']}, {len(results['errors'])} error(s).")
                self._set_status("Chart errors")
            else:
                self._notify("Chart Download Complete", f"Downloaded and extracted {results['downloaded']} update(s).")
                self._set_status("Idle")
                # Manual chart download success -> rebuild the local USB image
                # (image-based-drive-write Req 9.1).
                if results["downloaded"] > 0:
                    self._trigger_composer_refresh("new charts")
        except Exception as e:
            logger.error(f"Chart download failed: {e}")
            self._notify("Chart Download Failed", str(e)[:100])
            self._set_status("Chart download failed")
        finally:
            self._charts_running = False

    # ------------------------------------------------------------------
    # Chart check (auto/scheduled — downloads without asking)
    # ------------------------------------------------------------------

    def _run_chart_check_auto(self, notify_if_current: bool = False):
        from efis_data_manager.currency import update_charts

        self._charts_running = True
        self._set_status("Checking charts...")

        try:
            results = update_charts()
            if results["errors"]:
                self._notify("Chart Check - Errors", f"Downloaded {results['downloaded']}, {len(results['errors'])} error(s).")
                self._set_status("Chart errors")
            elif results["downloaded"] > 0:
                self._notify("Charts Updated", f"Downloaded {results['downloaded']} chart update(s).")
                self._set_status("Idle")
                # New chart data landed -> rebuild the local USB image
                # (image-based-drive-write Req 9.1).
                self._trigger_composer_refresh("new charts")
            else:
                logger.info("Charts are current.")
                if notify_if_current:
                    self._notify("Charts Current", "All chart data sets are up to date.")
                self._set_status("Idle")
        except Exception as e:
            logger.error(f"Chart check failed: {e}")
            self._notify("Chart Check Failed", str(e)[:100])
            self._set_status("Chart check failed")
        finally:
            self._charts_running = False

    # ------------------------------------------------------------------
    # Nav DB check
    # ------------------------------------------------------------------

    def _run_nav_db_check(self):
        from efis_data_manager.currency import check_and_download_nav_db

        self._nav_running = True
        self._set_status("Checking nav DB...")

        try:
            result = check_and_download_nav_db()
            if result["status"] == "updated":
                self._notify("Nav DB Updated", result["message"])
                # New nav DB landed -> rebuild the local USB image
                # (image-based-drive-write Req 9.2).
                self._trigger_composer_refresh("new nav DB")
            elif result["status"] == "current":
                self._notify("Nav DB Current", result["message"])
            elif result["status"] == "blocked":
                # Transient: page didn't fully load / bot-protection challenge.
                # Soft-fail quietly (no alarm) — the next check will retry.
                logger.warning(f"Nav DB check skipped: {result['message']}")
            else:
                self._notify("Nav DB Check Failed", result["message"][:100])
            self._set_status("Idle" if result["status"] != "error" else "Nav DB check failed")
        except Exception as e:
            logger.error(f"Nav DB check failed: {e}")
            self._notify("Nav DB Check Failed", str(e)[:100])
            self._set_status("Nav DB check failed")
        finally:
            self._nav_running = False

    # ------------------------------------------------------------------
    # Event-driven composer refresh (image-based-drive-write Task 8)
    # ------------------------------------------------------------------
    #
    # The local FAT32 image is rebuilt when the EXISTING data-manager checks
    # report NEWLY DOWNLOADED data — the chart check (`_run_chart_check_auto`
    # / `_do_chart_download`) and the nav check (`_run_nav_db_check`). There is
    # NO separate polling timer (Req 9.3): the refresh is driven only off those
    # checks, so it is naturally bounded by the 12h chart / 24h nav cadence.

    def _trigger_composer_refresh(self, reason: str):
        """Launch a composer refresh on a daemon thread (never blocks main).

        Called from the chart/nav new-data points. Spawns the compose on its own
        background thread so the rumps main thread and the calling check thread
        are never blocked by the slow newfs_msdos + hdiutil round-trip. The
        running-flag guard inside `_refresh_composed_image` drops the request if
        a compose is already in flight, so overlapping new-data events coalesce.
        """
        threading.Thread(
            target=self._refresh_composed_image, args=(reason,), daemon=True
        ).start()

    def _refresh_composed_image(self, reason: str):
        """Recompose the local FAT32 image; keep the previous image on failure.

        Composes to a TEMP image path and atomically promotes it to the
        configured `composed_image_path` only on success, so a failed compose
        never clobbers the last-good image (Req 9.4). Guarded by
        `self._compose_running` so concurrent refreshes do not overlap (mirrors
        `_charts_running` / `_nav_running`). Runs on a background thread.
        """
        # Lazy import — image_composer pulls in hdiutil/newfs tooling and is
        # only needed here, matching app.py's lazy-import convention.
        from efis_data_manager import image_composer

        if self._compose_running:
            logger.info("Composer refresh (%s) skipped: already running.", reason)
            return
        self._compose_running = True
        self._set_status("Composing USB image...")

        image_path = self.config.get(
            "composed_image_path",
            config.DEFAULT_CONFIG["composed_image_path"],
        )
        byte_size = self.config.get(
            "composed_image_size_bytes",
            config.DEFAULT_CONFIG["composed_image_size_bytes"],
        )
        usb_image_path = self.config.get("usb_image_path")

        # Compose to a sibling temp path, promote on success only. A failed
        # compose leaves `image_path` (the last-good image) untouched.
        tmp_path = f"{image_path}.new"
        try:
            image_composer.compose(
                tmp_path,
                byte_size,
                usb_image_path=usb_image_path,
            )
            # Atomic promote: os.replace swaps the new image into place in one
            # rename, so the current-image path is never a partial file.
            os.replace(tmp_path, image_path)
            logger.info("Composer refresh (%s) complete: %s", reason, image_path)
            self._notify("USB Image Ready", f"New USB image composed ({reason}).")
            self._set_status("Idle")
            # A fresh image is now the current one: flip the persistent menu
            # item to "new image ready (...)" and badge the menu-bar title
            # (image-based-drive-write Task 9, Req 10.2, 10.3). This is the
            # single point a composed image becomes user-visible as "pending".
            self._mark_image_ready(reason)
        except Exception as e:
            logger.error("Composer refresh (%s) failed: %s", reason, e)
            # Discard the partial temp image; the previous good image is kept.
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            self._notify("USB Image Compose Failed", str(e)[:100])
            self._set_status("Idle")
        finally:
            self._compose_running = False

    # ------------------------------------------------------------------
    # Persistent "new image ready" notification + menu-bar badge
    # (image-based-drive-write Task 9, Req 10.1-10.5)
    # ------------------------------------------------------------------
    #
    # Mirrors the existing "Download Charts" dynamic-menu idiom — all menu and
    # title mutation is marshalled onto the main thread via NSOperationQueue —
    # but the image-state line is PERSISTENT: it is created once in `__init__`
    # under the constant key `IMAGE_STATE_CURRENT_TITLE` and is never
    # inserted/removed. Only its displayed `.title` and its callback flip
    # between the "current" and "ready" states.

    def _describe_image_cycles(self, reason: str) -> str:
        """Build the "what's new" summary naming the nav + chart (plates) cycle.

        Sources are the data manager's OWN current cycle metadata (no drive
        access): the nav valid date lives in the GRT version metadata
        (`currency._load_grt_metadata()['nav_db_valid_date']`) and the chart
        cycle lives in `currency.load_cycle_metadata()` keyed by download URL,
        where the Plates entry's `valid_dates` is the authoritative chart/plates
        cycle. Returns a compact label like "nav 8/6 + plates 2610".

        This names what's new at the granularity readily available from the
        compose inputs. If a precise cycle string cannot be read (metadata
        missing/partial), it degrades gracefully to naming the trigger reason
        (e.g. "new nav DB"), so the user still sees *that* something is new.

        TODO(image-based-drive-write): enrich with the exact plates cycle number
        once the composer threads the precise per-family cycle through its
        ComposeResult, rather than re-deriving it from currency metadata here.
        """
        from efis_data_manager import currency

        nav_label = None
        plates_label = None
        try:
            grt_meta = currency._load_grt_metadata()
            nav_valid = grt_meta.get("nav_db_valid_date")
            if nav_valid:
                # Trim the year for a compact badge-line label (e.g. "8/6").
                nav_label = str(nav_valid).rsplit("/", 1)[0]
        except Exception as e:  # metadata is best-effort for labelling only
            logger.debug("Could not read nav cycle label: %s", e)

        try:
            cycle_meta = currency.load_cycle_metadata()
            # Prefer the Plates entry's valid_dates as the chart/plates cycle;
            # fall back to any entry's valid_dates if no Plates entry is present.
            plates_valid = None
            any_valid = None
            for entry in cycle_meta.values():
                desc = str(entry.get("description", "")).lower()
                valid = entry.get("valid_dates")
                if valid:
                    any_valid = any_valid or valid
                    if "plate" in desc or "approach" in desc:
                        plates_valid = valid
                        break
            chart_valid = plates_valid or any_valid
            if chart_valid:
                plates_label = str(chart_valid)
        except Exception as e:
            logger.debug("Could not read chart cycle label: %s", e)

        parts = []
        if nav_label:
            parts.append(f"nav {nav_label}")
        if plates_label:
            parts.append(f"plates {plates_label}")

        if parts:
            return " + ".join(parts)
        # Graceful fallback: name the trigger reason so the user still knows
        # *something* new landed even when cycle labels are unavailable.
        return reason

    def _mark_image_ready(self, reason: str):
        """Flip the persistent menu item + title badge into the "ready" state.

        Sets `_image_ready` True, computes the "what's new" label, then (on the
        main thread, like "Download Charts") retitles the image-state menu item
        to "New image ready (<label>)", wires its callback to the actionable
        prepare stub (Req 10.4), and refreshes the title so the ⬇ badge shows
        (Req 10.2, 10.3). Called from `_refresh_composed_image` on a successful
        promote.
        """
        from AppKit import NSOperationQueue

        label = self._describe_image_cycles(reason)
        self._image_ready = True
        self._image_ready_label = label
        ready_title = f"New image ready ({label})"

        def _do_update():
            item = self._image_state_item()
            if item is not None:
                item.title = ready_title
                # Actionable: clicking begins preparing a drive (Req 10.4).
                item.set_callback(self._on_prepare_image_drive)
            # Re-badge the menu-bar glyph to reflect the pending image.
            current_status = self.menu["Status: Idle"].title.replace("Status: ", "")
            self._update_title(current_status)

        NSOperationQueue.mainQueue().addOperationWithBlock_(_do_update)

    def _clear_image_ready(self):
        """Return the persistent menu item + title badge to "current" (Req 10.5).

        Clears `_image_ready`, retitles the image-state line back to
        `IMAGE_STATE_CURRENT_TITLE`, drops its callback (non-actionable in the
        current state), and refreshes the title so the ⬇ badge is removed.
        Marshalled onto the main thread like every other menu/title mutation.
        """
        from AppKit import NSOperationQueue

        self._image_ready = False
        self._image_ready_label = None

        def _do_update():
            item = self._image_state_item()
            if item is not None:
                item.title = IMAGE_STATE_CURRENT_TITLE
                item.set_callback(None)
            current_status = self.menu["Status: Idle"].title.replace("Status: ", "")
            self._update_title(current_status)

        NSOperationQueue.mainQueue().addOperationWithBlock_(_do_update)

    def _image_state_item(self):
        """Return the persistent image-state MenuItem, found by its stable key.

        The item is created under `IMAGE_STATE_CURRENT_TITLE` and keyed by that
        original title in rumps; once its `.title` changes to the "ready" text
        it can no longer be re-indexed by the new title. So — like the Alerts
        line — we locate it by scanning menu values and matching either the
        constant key title or the "New image ready" prefix.
        """
        try:
            return self.menu[IMAGE_STATE_CURRENT_TITLE]
        except KeyError:
            pass
        for item in self.menu.values():
            title = getattr(item, "title", None)
            if isinstance(title, str) and (
                title == IMAGE_STATE_CURRENT_TITLE
                or title.startswith("New image ready")
            ):
                return item
        return None

    def _on_prepare_image_drive(self, _):
        """User clicked "New image ready" — begin preparing a drive (Req 10.4).

        Wired by Task 10: resolve the currently-connected managed drive (the
        same identity-only discovery `verify_drive_menu` / `eject_drive` use)
        and kick off the image write on a daemon thread via
        `_run_prepare_image_write`. The ready-state UI is RESET here immediately
        (Req 10.5) — acting on the new image returns the menu item and the
        menu-bar badge to "current" as soon as the user acts, independent of the
        (slow, background) write outcome.

        If no managed drive is connected, we post a hint and still reset the UI
        (the user acted on the item). The actual guard -> dd -> verify runs in
        `_write_via_image`, which the write-on-sync path (`_perform_write`) also
        uses, so prepare and auto-sync share one write implementation.
        """
        logger.info(
            "Prepare-image-drive requested (image ready: %s).",
            self._image_ready_label,
        )

        from efis_data_manager.usb_monitor import is_managed_drive

        mount_point = None
        for name in os.listdir("/Volumes"):
            path = os.path.join("/Volumes", name)
            if os.path.isdir(path) and is_managed_drive(path):
                mount_point = path
                break

        # Acting on the new image returns BOTH the menu item and the badge to
        # the current state (Req 10.5), regardless of whether a drive is present.
        self._clear_image_ready()

        if not mount_point:
            self._notify(
                "No Drive",
                "No EFIS drive is connected to prepare. Insert a drive and retry.",
            )
            return

        self._set_status("Preparing drive from image...")
        self._notify(
            "Preparing Drive",
            "Writing the composed image to the connected drive.",
        )
        threading.Thread(
            target=self._run_prepare_image_write,
            args=(mount_point,),
            daemon=True,
        ).start()

    def _run_prepare_image_write(self, mount_point: str):
        """Run the image write for an explicit "prepare drive" action (Req 10.4).

        Thin background wrapper around `_write_via_image`. Unlike the write-on-
        sync path, prepare is a direct image-write action, so there is no
        currency-driven family selection and no mount-swap fallback here — the
        user explicitly asked to write the composed image to this drive. All
        status/notify and the device guard live in `_write_via_image`; the
        empty ``pending`` / ``stale`` and a minimal ``currency`` shape only feed
        its progress/label text (the whole image is the unit of work, Req 13.1).
        """
        def on_progress(msg):
            self._set_status(msg)

        try:
            self._write_via_image(
                mount_point,
                pending=[],
                stale=[],
                currency={"stale_items": []},
                on_progress=on_progress,
            )
        except Exception as e:
            logger.error(f"Prepare image write failed for {mount_point}: {e}")
            self._notify("Prepare Drive Failed", str(e)[:100])
            self._set_status("Prepare failed")

    # ------------------------------------------------------------------
    # EFIS/AHRS software check
    # ------------------------------------------------------------------

    def _run_software_check(self):
        from efis_data_manager.currency import check_and_download_efis_software

        self._software_running = True
        self._set_status("Checking EFIS software...")

        try:
            result = check_and_download_efis_software()
            if result["status"] == "available":
                # New version(s) detected — user downloads manually (Sucuri blocks auto)
                names = ", ".join(result["updated_items"])
                self._notify("New EFIS Software Available", f"{names}. See grtavionics.com to download.")
                logger.info(f"Software available for manual download: {result['message']}")
                self._set_status(f"Software update available: {names}")
            elif result["status"] == "current":
                self._notify("Software Current", result["message"])
                self._set_status("Idle")
            elif result["status"] == "blocked":
                # Bot protection — soft failure, don't alarm the user
                logger.warning(f"Software check blocked: {result['message']}")
                self._set_status("Idle")
            else:
                self._notify("Software Check Failed", result["message"][:100])
                self._set_status("Software check failed")
        except Exception as e:
            logger.error(f"EFIS software check failed: {e}")
            self._notify("Software Check Failed", str(e)[:100])
            self._set_status("Software check failed")
        finally:
            self._software_running = False

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @rumps.clicked("Analysis Dashboard...")
    def open_dashboard(self, _):
        """Launch the analysis dashboard web server (if needed) and open it."""
        import webbrowser
        from efis_data_manager.config import load_config

        port = load_config().get("dashboard_port", 5050)
        url = f"http://localhost:{port}"

        # Start the dashboard server if not already running
        proc = self._dashboard_process
        if proc is None or proc.poll() is not None:
            try:
                self._dashboard_process = subprocess.Popen(
                    [sys.executable, "-m", "efis_data_manager.dashboard"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info(f"Started analysis dashboard on {url}")
                # Give the server a moment to bind the port before opening
                threading.Timer(1.5, lambda: webbrowser.open(url)).start()
            except Exception as e:
                logger.error(f"Failed to start dashboard: {e}")
                self._notify("Dashboard Failed", f"Could not start dashboard: {e}")
                return
        else:
            # Already running — just open the browser
            webbrowser.open(url)

    @rumps.clicked("Settings...")
    def open_settings(self, _):
        from efis_data_manager.settings_window import show_settings

        # If a settings window is already open, just bring it to the front.
        # Building a new delegate on top of a live one releases the previous
        # delegate mid-flight, which under PyObjC can tear down its Cocoa
        # objects unsafely. Reusing the open window avoids that entirely.
        existing = getattr(self, "_settings_delegate", None)
        if existing is not None and existing.is_open():
            existing.focus()
            return

        def on_save(new_config):
            self.config = new_config
            save_config(self.config)
            self._notify("Settings Saved", "Configuration updated successfully.")

        self._settings_delegate = show_settings(self.config, on_save)

    @rumps.clicked("Seattle Avionics Login...")
    def set_seattle_avionics_creds(self, _):
        current_email = ""
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s",
                 "EFISDataManager-SeattleAvionics", "-g"],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if '"acct"' in line:
                    current_email = line.split('"')[-2]
                    break
        except Exception:
            pass

        email_window = rumps.Window(
            message="Seattle Avionics account email:",
            title="Seattle Avionics Login",
            default_text=current_email,
            ok="Next", cancel="Cancel", dimensions=(300, 24),
        )
        email_response = email_window.run()
        if not email_response.clicked:
            return
        email = email_response.text.strip()
        if not email:
            return

        password_window = rumps.Window(
            message="Seattle Avionics password:",
            title="Seattle Avionics Login",
            default_text="",
            ok="Save to Keychain", cancel="Cancel", dimensions=(300, 24),
        )
        password_response = password_window.run()
        if not password_response.clicked:
            return
        password = password_response.text.strip()
        if not password:
            return

        try:
            subprocess.run(["security", "delete-generic-password",
                            "-s", "EFISDataManager-SeattleAvionics"],
                           capture_output=True)
            subprocess.run(["security", "add-generic-password",
                            "-s", "EFISDataManager-SeattleAvionics",
                            "-a", email, "-w", password],
                           capture_output=True, check=True)
            self._notify("Credentials Saved", "Seattle Avionics login stored in macOS Keychain.")
        except subprocess.CalledProcessError:
            rumps.alert(title="Error", message="Failed to save credentials to Keychain.")

    @rumps.clicked("Prepare Drive...")
    def prepare_drive(self, _):
        """Format and provision a USB drive for EFIS use."""
        # List available removable volumes
        volumes = []
        for name in os.listdir("/Volumes"):
            path = os.path.join("/Volumes", name)
            if os.path.isdir(path) and name not in ("Macintosh HD", "com.apple.TimeMachine.localsnapshots"):
                # Skip Time Machine and internal drives
                if not name.startswith("Backups of"):
                    volumes.append(name)

        if not volumes:
            rumps.alert(title="Prepare Drive",
                        message="No removable USB drives found.")
            return

        # Ask user to confirm which drive to prepare.
        vol_list = "\n".join(f"• {v}" for v in volumes)
        window = rumps.Window(
            message="Select the drive to prepare. Type its EXACT name from the "
            f"list below (this only chooses which drive — you'll set its EFIS "
            f"label in the next step):\n\n{vol_list}",
            title="Prepare Drive — Select Drive to Prepare",
            default_text="",
            ok="Next", cancel="Cancel", dimensions=(300, 24),
        )
        response = window.run()
        if not response.clicked:
            return
        chosen = response.text.strip()
        if chosen not in volumes:
            rumps.alert(title="Prepare Drive", message=f"'{chosen}' not found in available volumes.")
            return

        volume_path = f"/Volumes/{chosen}"

        # Inspect the drive and branch on its state. Detection is identity-only:
        #   - already managed (our identity file) -> Update (adopt) vs Start clean
        #   - adoption candidate (GRTCHARTS/ChartData, no identity) -> Adopt vs
        #     Start clean (previously-used GRT / Windows-tool drive)
        #   - blank / no chart data -> Start clean only
        from efis_data_manager.usb_monitor import (
            is_adoption_candidate,
            is_managed_drive,
        )

        if is_managed_drive(volume_path):
            # Already ours. "Update" is just the adopt path (write-identity is a
            # no-op when one exists) which runs an incremental update_drive.
            choice = rumps.alert(
                title="Prepare Drive",
                message=f"'{chosen}' is already an EFIS-managed drive.\n\n"
                "Update it (non-destructive, brings charts current) or start "
                "clean (erase and reformat)?",
                ok="Update", cancel="Cancel", other="Start clean",
            )
            # rumps.alert: ok=1, cancel=0, other=-1.
            if choice == 1:
                adopt = True
            elif choice == -1:
                adopt = False
            else:
                return
        elif is_adoption_candidate(volume_path):
            # A previously-used GRT chart drive (Windows tool or older app).
            choice = rumps.alert(
                title="Prepare Drive",
                message=f"'{chosen}' already has chart data but isn't yet "
                "EFIS-managed.\n\nAdopt & update it (non-destructive — keeps "
                "existing charts, brings them current) or start clean (erase "
                "and reformat)?",
                ok="Adopt & update", cancel="Cancel", other="Start clean",
            )
            if choice == 1:
                adopt = True
            elif choice == -1:
                adopt = False
            else:
                return
        else:
            # Blank / no chart data: only Start clean applies.
            confirm = rumps.alert(
                title="Prepare Drive",
                message=f"'{chosen}' has no EFIS chart data.\n\n"
                "Start clean will ERASE the drive and format it as an EFIS "
                "drive.",
                ok="Start clean", cancel="Cancel",
            )
            if confirm != 1:
                return
            adopt = False

        # Flight-data safety (Req 10.11): applies to BOTH paths. If the drive
        # holds un-archived flight data / logbooks, offer to import (archive)
        # it before provisioning; Erase skips archiving; Cancel aborts.
        from efis_data_manager.drive_updater import has_unarchived_flight_data

        archive_first = False
        if has_unarchived_flight_data(volume_path):
            data_choice = rumps.alert(
                title="Unarchived Flight Data",
                message=f"'{chosen}' has flight data or logbooks that haven't "
                "been archived yet.\n\nImport them first (recommended), or "
                "erase without archiving?",
                ok="Import first", cancel="Cancel", other="Erase",
            )
            if data_choice == 1:
                archive_first = True
            elif data_choice == -1:
                archive_first = False
            else:
                return

        if adopt:
            # Non-destructive adopt/update path: no label prompt, no format.
            threading.Thread(
                target=self._do_adopt_drive,
                args=(volume_path,),
                kwargs={"archive_first": archive_first},
                daemon=True,
            ).start()
            return

        # Start-clean path: ask for a cosmetic label suffix. The drive is
        # labeled EFIS_<suffix> purely as a Finder convenience so rotating
        # drives can be named meaningfully; nothing keys on the label for
        # detection (that is identity-only). build_efis_label upcases, strips
        # FAT32-unsafe characters, and truncates to the 11-char FAT32 limit.
        from efis_data_manager.usb_monitor import build_efis_label

        # Pre-fill with the chosen drive's existing suffix (e.g. "EFIS_1" -> "1")
        # so re-using the same label is one click. build_efis_label also strips
        # a redundant EFIS_ prefix if the user leaves the full label in.
        _default_suffix = chosen[len("EFIS_"):] if chosen.upper().startswith("EFIS_") else ""
        label_window = rumps.Window(
            message="Choose a label for this EFIS drive. It will be labeled "
            "EFIS_<your text>\n(letters/numbers, upper-cased, max 11 chars "
            "total). Leave blank for just 'EFIS'.\nExamples: SPARE -> "
            "EFIS_SPARE, 2 -> EFIS_2.",
            title="Prepare Drive — Choose Label",
            default_text=_default_suffix,
            ok="Next", cancel="Cancel", dimensions=(300, 24),
        )
        label_response = label_window.run()
        if not label_response.clicked:
            return
        label = build_efis_label(label_response.text.strip())

        # Final confirmation (shows the resulting label).
        confirm = rumps.alert(
            title="CONFIRM FORMAT",
            message=f"This will ERASE ALL DATA on '{chosen}' and format it as an "
            f"EFIS drive labeled '{label}'.\n\nAre you sure?",
            ok="Erase and Format", cancel="Cancel",
        )
        if confirm != 1:
            return

        # Run in background.
        threading.Thread(
            target=self._do_prepare_drive,
            args=(volume_path, label),
            kwargs={"archive_first": archive_first},
            daemon=True,
        ).start()

    def _archive_before_provision(self, volume_path: str) -> bool:
        """Archive flight data off ``volume_path`` before provisioning.

        Runs the existing ``archive_efis_drive`` so un-archived flight data /
        logbooks are preserved before a Start-clean or Adopt overwrites them
        (Req 10.11). Returns True on success (or nothing to do), False if the
        archive raised — the caller aborts provisioning so data is never lost to
        a failed archive.
        """
        from efis_data_manager.archiver import archive_efis_drive

        self._set_status("Archiving flight data before preparing...")

        def on_progress(msg):
            self._set_status(msg)

        try:
            archive_efis_drive(volume_path, progress_callback=on_progress)
            return True
        except Exception as e:
            logger.error(f"Pre-provision archive failed: {e}")
            self._notify("Archive Failed", f"Could not archive flight data; aborting. {str(e)[:80]}")
            self._set_status("Prepare aborted (archive failed)")
            return False

    def _do_prepare_drive(
        self, volume_path: str, label: str = "EFIS", archive_first: bool = False
    ):
        """Background thread for the Start-clean (reformat + populate) path."""
        from efis_data_manager.drive_updater import prepare_drive

        # Preserve flight data first if the user chose Import. Abort on failure
        # rather than erasing un-archived data.
        if archive_first and not self._archive_before_provision(volume_path):
            return

        self._set_status("Preparing drive...")

        def on_progress(msg):
            self._set_status(msg)

        # Register the in-progress guard for the WHOLE prepare span so the
        # reformat+remount does not trip a concurrent auto-sync. The pre-format
        # mount (volume_path, e.g. /Volumes/NO NAME) differs from the
        # post-format mount the poller will see (/Volumes/<label>), so register
        # both (belt-and-suspenders).
        post_format_mount = os.path.join("/Volumes", label)
        self._mark_provisioning(volume_path)
        self._mark_provisioning(post_format_mount)
        try:
            result = prepare_drive(
                volume_path,
                label=label,
                progress_callback=on_progress,
                poller=self._usb_monitor,
            )
            if result["success"]:
                self._notify("Drive Prepared", result["message"])
                self._set_status("Idle")
                # See _do_adopt_drive: reconcile the connected-status UI against
                # the now-mounted managed drive (the swap resume() did not fire
                # on_efis_mount).
                self._reconcile_drive_status()
            else:
                self._notify("Prepare Drive Failed", result["message"][:100])
                self._set_status("Prepare failed")
        except Exception as e:
            logger.error(f"Prepare drive failed: {e}")
            self._notify("Prepare Drive Failed", str(e)[:100])
            self._set_status("Prepare failed")
        finally:
            self._unmark_provisioning(volume_path)
            self._unmark_provisioning(post_format_mount)

    def _do_adopt_drive(self, volume_path: str, archive_first: bool = False):
        """Background thread for the Adopt & update (non-destructive) path.

        Writes the identity file and runs an incremental ``update_drive`` without
        formatting — the migration path for a previously-used GRT / Windows-tool
        drive (Req 10.10).
        """
        from efis_data_manager.drive_updater import adopt_drive

        # Preserve flight data first if the user chose Import. Adopt can
        # overwrite settings backups, so abort on archive failure.
        if archive_first and not self._archive_before_provision(volume_path):
            return

        self._set_status("Adopting drive...")

        def on_progress(msg):
            self._set_status(msg)

        # Adopt writes identity in place (no reformat), so the drive keeps its
        # current mount path; register it so the identity-write-triggered
        # managed remount does not launch a concurrent auto-sync.
        self._mark_provisioning(volume_path)
        try:
            result = adopt_drive(
                volume_path,
                progress_callback=on_progress,
                poller=self._usb_monitor,
            )
            if result["success"]:
                self._notify("Drive Adopted", result["message"])
                self._set_status("Idle")
                # The managed mount was restored by the swap teardown without an
                # on_efis_mount callback (and the mount handler is gated while
                # provisioning), so the connected-status UI was never set.
                # Reconcile against what is actually mounted.
                self._reconcile_drive_status()
            else:
                self._notify("Adopt Drive Failed", result["message"][:100])
                self._set_status("Adopt failed")
        except Exception as e:
            logger.error(f"Adopt drive failed: {e}")
            self._notify("Adopt Drive Failed", str(e)[:100])
            self._set_status("Adopt failed")
        finally:
            self._unmark_provisioning(volume_path)

    @rumps.clicked("Quit")
    def quit_app(self, _):
        response = rumps.alert(
            title="Quit EFIS Data Manager?",
            message="Background monitoring will stop until the app is restarted.",
            ok="Quit", cancel="Cancel",
        )
        if response == 1:
            logger.info("User quit the app via menu.")
            # Shut down the dashboard server if we started it
            if self._dashboard_process and self._dashboard_process.poll() is None:
                try:
                    self._dashboard_process.terminate()
                except Exception:
                    pass
            rumps.quit_application()

    @rumps.clicked("Diagnostics...")
    def show_diagnostics(self, _):
        """Show a self-check panel: versions, paths, DB, browser, disk, launchd."""
        from efis_data_manager import __version__, MENUBAR_VERSION, DASHBOARD_VERSION
        import shutil

        lines = []
        lines.append(f"Project v{__version__}  (menu bar {MENUBAR_VERSION}, dashboard {DASHBOARD_VERSION})")
        lines.append("Copyright (C) 2026 Martin C. Walker. Licensed under AGPL-3.0-or-later")
        lines.append("Source: https://github.com/flightlead404/EFISDataManager")
        lines.append("This program comes with ABSOLUTELY NO WARRANTY.")
        lines.append("")

        # Config paths
        lines.append("Paths:")
        for key in ("archive_path", "usb_image_path"):
            path = self.config.get(key, "(unset)")
            exists = os.path.isdir(path) if path else False
            flag = "OK" if exists else "MISSING"
            if any(s in path for s in ("CloudStorage", "Dropbox")):
                flag = "SUSPECT (cloud path)"
            lines.append(f"  {key}: {path} [{flag}]")

        # Database
        try:
            from efis_data_manager.database import DB_PATH, get_db_connection
            if os.path.exists(str(DB_PATH)):
                size_mb = os.path.getsize(str(DB_PATH)) / (1024 * 1024)
                conn = get_db_connection()
                n_ops = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                n_flights = conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE has_flight=1"
                ).fetchone()[0]
                n_oil = conn.execute("SELECT COUNT(*) FROM oil_events").fetchone()[0]
                conn.close()
                lines.append("")
                lines.append(f"Database: {size_mb:.1f} MB")
                lines.append(f"  {n_ops} operations ({n_flights} flights), {n_oil} oil events")
            else:
                lines.append("")
                lines.append("Database: not yet created")
        except Exception as e:
            lines.append(f"Database: error ({e})")

        # Playwright browser
        try:
            from efis_data_manager.currency import _check_playwright_browser
            berr = _check_playwright_browser()
            lines.append("")
            lines.append(f"Playwright browser: {'OK' if not berr else 'MISSING'}")
        except Exception:
            lines.append("Playwright browser: unknown")

        # Disk space on archive volume
        try:
            usage = shutil.disk_usage(os.path.expanduser("~"))
            free_gb = usage.free / (1024 ** 3)
            lines.append("")
            lines.append(f"Free disk space: {free_gb:.1f} GB")
        except Exception:
            pass

        # launchd plist target
        try:
            plist = os.path.expanduser("~/Library/LaunchAgents/com.efisdatamanager.plist")
            if os.path.exists(plist):
                with open(plist) as f:
                    content = f.read()
                bad = "CloudStorage" in content or "Dropbox" in content
                lines.append("")
                lines.append(f"launchd plist: {'SUSPECT (cloud path)' if bad else 'OK'}")
        except Exception:
            pass

        rumps.alert(title="EFIS Data Manager — Diagnostics",
                    message="\n".join(lines), ok="OK")

    @rumps.clicked("Recent Notifications...")
    def show_recent_notifications(self, _):
        entries = self._history.get_recent()
        if not entries:
            rumps.alert(
                title="Recent Notifications",
                message="No notifications have been recorded this session.",
                ok="OK",
            )
            return
        # Already newest-first. Shown in a non-modal, resizable, scrollable
        # window (NOT rumps.alert) so a long history cannot grow a modal dialog
        # off-screen with no reachable close button.
        from efis_data_manager.text_list_window import show_text_list
        body = "\n\n".join(format_entry_line(e) for e in entries)
        show_text_list(f"Recent Notifications ({len(entries)})", body)

    @rumps.clicked("Recent Errors...")
    def show_recent_errors(self, _):
        errors = _recent_error_handler.get_recent()
        if not errors:
            rumps.alert(
                title="Recent Errors",
                message="No errors or warnings logged this session.",
                ok="OK",
            )
            return
        # Show most recent first, in the same non-modal scrollable window as
        # Recent Notifications (avoids the off-screen modal-alert bug).
        from efis_data_manager.text_list_window import show_text_list
        body = "\n\n".join(reversed(errors))
        show_text_list(f"Recent Errors ({len(errors)})", body)

    @rumps.clicked("About")
    def about(self, _):
        from efis_data_manager import MENUBAR_VERSION, __version__
        rumps.alert(
            title="EFIS Data Manager",
            message=(
                f"Menu Bar Tool v{MENUBAR_VERSION}\n"
                f"Project release v{__version__}\n\n"
                "GRT HXr EFIS Ground Support Automation\n"
                "USB detection, chart currency, archiving, and analysis."
            ),
            ok="OK",
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _set_status(self, status_text: str):
        """Update status — dispatches to main thread for UI safety."""
        from AppKit import NSOperationQueue

        def _do_update():
            self.menu["Status: Idle"].title = f"Status: {status_text}"
            self._update_title(status_text)

        NSOperationQueue.mainQueue().addOperationWithBlock_(_do_update)

    def _reconcile_drive_status(self):
        """Set the drive-connected UI to match what is ACTUALLY mounted.

        The ``_drive_connected`` flag (and the Eject/Verify enablement) is
        otherwise only edge-triggered by the auto mount/eject callbacks. Those
        callbacks are deliberately bypassed in two situations, which left the UI
        stale ("Drive: Not connected", Eject/Verify greyed out) even though a
        managed drive was mounted:
          * a Prepare/Adopt owns the drive, so ``_on_efis_drive_mounted``
            returns early at the ``_is_provisioning`` guard (never setting
            connected status); and
          * the mount-swap ``resume()`` folds the restored FSKit mount into the
            monitor's baseline WITHOUT firing ``on_efis_mount``.
        So after a provisioning operation completes we reconcile against ground
        truth: scan ``/Volumes/`` for a mounted managed drive and assert the UI.
        Idempotent and safe to call from a worker thread (``_set_drive_status``
        marshals the actual menu mutation onto the main queue).
        """
        from efis_data_manager.usb_monitor import find_mounted_managed_drive

        mount_point = find_mounted_managed_drive()
        if mount_point is not None:
            self._set_drive_status(f"Connected: {mount_point}")
        else:
            self._set_drive_status("Not connected")

    def _set_drive_status(self, drive_text: str):
        """Update drive connection status."""
        from AppKit import NSOperationQueue

        self._drive_connected = "Not connected" not in drive_text

        def _do_update():
            self.menu["Drive: Not connected"].title = f"Drive: {drive_text}"
            # Enable/disable eject + verify buttons (both need a drive)
            if self._drive_connected:
                self.menu["Eject Drive"].set_callback(self.eject_drive)
                self.menu["Verify Drive"].set_callback(self.verify_drive_menu)
            else:
                self.menu["Eject Drive"].set_callback(None)
                self.menu["Verify Drive"].set_callback(None)
            # Update title to reflect drive state
            current_status = self.menu["Status: Idle"].title.replace("Status: ", "")
            self._update_title(current_status)

        NSOperationQueue.mainQueue().addOperationWithBlock_(_do_update)

    def _refresh_alerts(self):
        """Refresh the Alerts menu item with current anomalies."""
        from AppKit import NSOperationQueue

        def _do_refresh():
            try:
                from efis_data_manager.analysis import detect_anomalies
                anomalies = detect_anomalies()

                # Update the alerts menu item title. rumps keys menu items by
                # their ORIGINAL title, so find the item by iterating values and
                # matching on the current title prefix — never re-index by the
                # new title (that key doesn't exist and raises KeyError).
                alert_title = f"Alerts ({len(anomalies)})"
                for item in self.menu.values():
                    title = getattr(item, "title", None)
                    if isinstance(title, str) and title.startswith("Alerts"):
                        item.title = alert_title
                        break

                # If there are new warnings, show a warning glyph beside the icon
                warnings = [a for a in anomalies if a.severity == "warning"]
                if warnings:
                    self.title = "\u26A0"  # ⚠ (icon carries the branding)

            except Exception as e:
                logger.error(f"Alert refresh failed: {e}")

        NSOperationQueue.mainQueue().addOperationWithBlock_(_do_refresh)

    def _update_title(self, status_text: str):
        """Set the menu bar title based on current state.

        The color PFD icon carries the branding; the title is a compact status
        glyph shown next to it. Glyph precedence (highest wins):

          1. ``↻`` active work (a check / download / compose is in progress)
          2. ``⬇`` fresh image pending (``_image_ready`` — Task 9, Req 10.3)
          3. ``●`` drive connected, idle
          4. ``""`` no drive, idle — icon only

        Rationale: active work is the most time-sensitive and transient, so it
        outranks the (persistent) image badge; the image badge in turn outranks
        the steady connected-idle dot because a pending fresh image is the thing
        the user most needs to notice. The ⚠ alert glyph is set independently by
        ``_refresh_alerts`` and is not part of this ordering.
        """
        drive_connected = getattr(self, '_drive_connected', False)
        image_ready = getattr(self, '_image_ready', False)

        if "..." in status_text or "Downloading" in status_text:
            # Active work — outranks everything (precedence 1).
            self.title = "\u21BB"  # ↻
        elif image_ready:
            # A freshly composed image is waiting to be prepared onto a drive
            # (precedence 2). Distinct from ↻ ● ⚠ so it reads as its own state.
            self.title = IMAGE_READY_BADGE_GLYPH  # ⬇
        elif drive_connected:
            # Drive connected, idle (precedence 3).
            self.title = "\u25CF"  # ● (filled circle = connected)
        else:
            # No drive, idle — icon only (precedence 4).
            self.title = ""


try:
    import objc
    from Foundation import NSObject

    class SleepWakeObserver(NSObject):
        """ObjC observer for NSWorkspace sleep/wake notifications (task 11).

        Sleep/wake notifications are delivered on ``NSWorkspace``'s own
        notification center and the observer must be an Objective-C object, so
        this thin ``NSObject`` subclass holds a weak-ish reference to the app
        and forwards each notification to a plain Python handler. PyObjC maps
        ``receiveSleepNotification:`` to ``receiveSleepNotification_`` (the
        selector takes the notification argument).
        """

        def initWithApp_(self, app):
            self = objc.super(SleepWakeObserver, self).init()
            if self is None:
                return None
            self._app = app
            return self

        def receiveSleepNotification_(self, _notification):
            try:
                self._app._on_will_sleep()
            except Exception as e:
                logger.warning(f"Sleep notification handler failed: {e}")

        def receiveWakeNotification_(self, _notification):
            try:
                self._app._on_did_wake()
            except Exception as e:
                logger.warning(f"Wake notification handler failed: {e}")

except Exception:  # pragma: no cover - PyObjC unavailable (e.g. headless CI)
    SleepWakeObserver = None


def _menubar_icon_path() -> str:
    """Absolute path to the menu bar icon PNG shipped in the package.

    The sibling 'menubar@2x.png' is picked up automatically by AppKit on
    Retina displays.
    """
    return os.path.join(os.path.dirname(__file__), "resources", "menubar.png")


def _acquire_single_instance_lock():
    """Ensure only one menu-bar instance runs at a time.

    Both the login item (launchd) and a manual double-click of the .app can
    try to start the app. We take an exclusive advisory lock on a file in the
    log directory; if another instance already holds it, we exit cleanly.

    Returns the open lock-file object (must be kept alive for the process
    lifetime), or None if another instance is already running.
    """
    import fcntl

    lock_path = os.path.join(_log_dir, "app.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def main():
    lock = _acquire_single_instance_lock()
    if lock is None:
        logger.info("Another EFIS Data Manager instance is already running; exiting.")
        return
    # Keep a reference so the lock is held for the life of the process.
    globals()["_INSTANCE_LOCK"] = lock

    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    ns_app = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    logger.info("EFIS Data Manager starting...")
    app = EFISDataManagerApp()
    logger.info("EFIS Data Manager ready.")
    app.run()


if __name__ == "__main__":
    main()
