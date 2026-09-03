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

import logging
import os
import subprocess
import sys
import threading

import rumps

from efis_data_manager.config import load_config, save_config

logger = logging.getLogger(__name__)

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
        self._charts_running = False
        self._nav_running = False
        self._software_running = False
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
            "Recent Errors...",
            "About",
            "Quit",
        ]

        self.menu["Status: Idle"].set_callback(None)
        self.menu["Drive: Not connected"].set_callback(None)
        self.menu["Alerts (0)"].set_callback(None)
        self.menu["Archive: " + self._short_path(self.config["archive_path"])].set_callback(None)
        self.menu["USB Image: " + self._short_path(self.config["usb_image_path"])].set_callback(None)

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
    # USB Monitor
    # ------------------------------------------------------------------

    def _start_usb_monitor(self):
        """Initialize and start the USB volume monitor."""
        from efis_data_manager.usb_monitor import USBMonitor
        self._usb_monitor = USBMonitor(
            on_efis_mount=self._on_efis_drive_mounted,
            on_efis_unmount=self._on_efis_drive_ejected,
        )
        self._usb_monitor.start()

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
        from efis_data_manager.usb_monitor import is_efis_drive
        from efis_data_manager.drive_updater import pending_families, resolve_drive_id

        try:
            mount_point = None
            for name in os.listdir("/Volumes"):
                path = os.path.join("/Volumes", name)
                if os.path.isdir(path) and is_efis_drive(path):
                    mount_point = path
                    break
            if mount_point is None:
                logger.info("Wake: no EFIS drive mounted; nothing to resume.")
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

    def _on_efis_drive_mounted(self, mount_point: str):
        """Called when an EFIS drive is detected."""
        logger.info(f"EFIS drive mounted: {mount_point}")
        self._set_drive_status(f"Connected: {mount_point}")
        self._set_status("EFIS drive detected")
        rumps.notification("EFIS Data Manager", "EFIS Drive Detected",
                           f"Drive mounted at {mount_point}. Starting archive...")

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
        rumps.notification("EFIS Data Manager", "EFIS Drive Ejected",
                           "Drive removed safely.")

    def eject_drive(self, _):
        """Eject the currently mounted EFIS drive."""
        if not getattr(self, '_drive_connected', False):
            rumps.notification("EFIS Data Manager", "No Drive", "No EFIS drive is connected.")
            return

        import subprocess
        from efis_data_manager.usb_monitor import is_efis_drive
        mount_point = None
        # Find current EFIS mount (handles EFIS, EFIS_1, EFIS_2, ... via is_efis_drive)
        for name in os.listdir("/Volumes"):
            path = os.path.join("/Volumes", name)
            if os.path.isdir(path) and is_efis_drive(path):
                mount_point = path
                break

        if not mount_point:
            rumps.notification("EFIS Data Manager", "No Drive", "No EFIS drive found to eject.")
            return

        # Get the disk identifier first, then eject the whole physical device
        info_result = subprocess.run(
            ["diskutil", "info", "-plist", mount_point],
            capture_output=True, timeout=10
        )
        if info_result.returncode == 0:
            import plistlib
            info = plistlib.loads(info_result.stdout)
            # Get parent whole disk (e.g. disk4 from disk4s1)
            parent_disk = info.get("ParentWholeDisk", "")
            if parent_disk:
                result = subprocess.run(
                    ["diskutil", "eject", f"/dev/{parent_disk}"],
                    capture_output=True, text=True
                )
            else:
                result = subprocess.run(
                    ["diskutil", "eject", mount_point],
                    capture_output=True, text=True
                )
        else:
            result = subprocess.run(
                ["diskutil", "eject", mount_point],
                capture_output=True, text=True
            )
        if result.returncode == 0:
            logger.info(f"Ejected {mount_point}")
        else:
            rumps.notification("EFIS Data Manager", "Eject Failed",
                               result.stderr.strip()[:100] or "Unknown error")

    def verify_drive_menu(self, _):
        """User clicked "Verify Drive": run an on-demand exhaustive verify.

        Verify-only (``deep=False``, no repair) per Req 6.4 — repair happens via
        the interrupted-sync path in ``_run_drive_update``. Finds the connected
        EFIS mount (same discovery as ``eject_drive``), then runs
        ``verify_drive`` on a background thread since a full count+size walk can
        be slow, reporting per-family results via notification/status.
        """
        from efis_data_manager.usb_monitor import is_efis_drive
        mount_point = None
        # Find current EFIS mount (handles EFIS, EFIS_1, EFIS_2, ... via is_efis_drive)
        for name in os.listdir("/Volumes"):
            path = os.path.join("/Volumes", name)
            if os.path.isdir(path) and is_efis_drive(path):
                mount_point = path
                break

        if not mount_point:
            rumps.notification("EFIS Data Manager", "No Drive",
                               "No EFIS drive is connected.")
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
                rumps.notification("EFIS Data Manager", "Drive Verified",
                                   f"Drive is complete: {summary}")
                self._set_status("Drive current")
            else:
                # Discrepancies found — report them; repair happens via the
                # interrupted-sync path, not this verify-only action (Req 6.4).
                logger.warning(f"Verify Drive found discrepancies: {summary}")
                rumps.notification("EFIS Data Manager", "Drive Verify: Discrepancies",
                                   f"{summary}\nRe-connect or re-run update to repair.")
                self._set_status("Drive needs update")
        except Exception as e:
            logger.error(f"Verify Drive failed: {e}")
            rumps.notification("EFIS Data Manager", "Verify Drive Failed", str(e)[:100])
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
                rumps.notification("EFIS Data Manager", "Archive Complete (with errors)",
                                   f"{msg}\n{len(results['errors'])} error(s).")
                self._set_status("Archive errors")
            else:
                rumps.notification("EFIS Data Manager", "Archive Complete", msg)

            # Data is now imported into the analysis DB. Signal that flight
            # data is ready to analyze — independent of the (slow) chart sync.
            self._refresh_alerts()
            if results.get("fdl_imported"):
                rumps.notification(
                    "EFIS Data Manager", "Flight Data Ready",
                    f"Imported {results['fdl_imported']} operation(s). "
                    "Open the dashboard to analyze — chart sync continues in background."
                )

            # Kick off the drive update (long chart sync) on its OWN thread so
            # the archive thread finishes cleanly and analysis isn't blocked.
            threading.Thread(
                target=self._run_drive_update, args=(mount_point,), daemon=True
            ).start()

        except Exception as e:
            logger.error(f"Archive failed: {e}")
            rumps.notification("EFIS Data Manager", "Archive Failed", str(e)[:100])
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
        """
        from efis_data_manager.drive_updater import (
            check_drive_currency,
            pending_families,
            resolve_drive_id,
            update_drive,
            verify_drive,
            _terminal_status_from_jobs,
        )

        def on_progress(msg):
            self._set_status(msg)

        try:
            # --- 1. Honor an interrupted sync: verify+repair before trusting. --
            # sync-state is keyed by the drive's durable id, not the mount path.
            # Resolve the id once; on an unresolved id (fail-safe None — e.g. a
            # diskutil hiccup or a non-EFIS/unreadable volume) apply NO
            # interrupted-sync state (never another drive's) and fall straight
            # through to the normal quick-check/update below (Req 10.7). The
            # on-drive markers/payload remain the sole source of truth for
            # currency.
            drive_id = resolve_drive_id(mount_point)
            pending = pending_families(drive_id) if drive_id is not None else []
            if pending:
                logger.warning(
                    f"Previous sync was interrupted for {mount_point} "
                    f"({', '.join(pending)}); running verify+repair before "
                    "declaring current."
                )
                self._set_status("Verifying drive after interrupted sync...")
                rumps.notification("EFIS Data Manager", "Verifying Drive",
                                   f"Resuming interrupted sync: {', '.join(pending)}.")
                repair = self._with_active_watchdog(
                    mount_point,
                    lambda is_aborted: verify_drive(
                        mount_point, families=pending, repair=True,
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
                    rumps.notification("EFIS Data Manager", "Verify Incomplete",
                                       "Some families still differ; will retry "
                                       "on next mount.")
                    self._set_status(status)
                    return

            # --- 2. Quick currency check. -------------------------------------
            self._set_status("Checking drive currency...")
            currency = check_drive_currency(mount_point)

            if currency["is_current"]:
                logger.info("Drive is up to date, no sync needed.")
                self._set_status("Drive current")
                rumps.notification("EFIS Data Manager", "Drive Current",
                                   "EFIS drive is up to date.")
                return

            # --- 3. Sync only the stale families. -----------------------------
            stale = [
                name
                for name, detail in currency["families"].items()
                if not detail["current"]
            ]
            stale_summary = ", ".join(currency["stale_items"][:3])
            if len(currency["stale_items"]) > 3:
                stale_summary += f" +{len(currency['stale_items']) - 3} more"
            self._set_status("Updating drive...")
            rumps.notification("EFIS Data Manager", "Updating Drive",
                               f"{len(stale)} family(ies) to update: {stale_summary}")

            results = self._with_active_watchdog(
                mount_point,
                lambda is_aborted: update_drive(
                    mount_point, families=stale, progress_callback=on_progress,
                    is_aborted=is_aborted,
                ),
            )

            jobs = results["jobs"]
            updated = sum(r.files_updated for r in jobs.values())
            status = _terminal_status_from_jobs(jobs, results["aborted"])

            if results["errors"] or results["aborted"]:
                # The job driver already logged each job error at >= WARNING;
                # log the terminal status too so it always has a matching record
                # in Recent Errors (Req 8.1/8.3).
                logger.warning(f"Drive update finished with issues: {status}")
                rumps.notification("EFIS Data Manager", "Drive Update Complete (errors)",
                                   f"Updated {updated} item(s), "
                                   f"{len(results['errors'])} error(s).")
                self._set_status(status)
            else:
                # Clean terminal state -> current, never a sticky error (Req 8.4).
                rumps.notification("EFIS Data Manager", "Drive Update Complete",
                                   f"Updated {updated} item(s). Drive is current.")
                self._set_status(status)

        except Exception as e:
            logger.error(f"Drive update failed: {e}")
            # The durable sync-state is left intact by the job driver on failure,
            # so an interrupted family is retried on the next mount.
            rumps.notification("EFIS Data Manager", "Drive Update Failed", str(e)[:100])
            self._set_status("Update failed")

    def _with_active_watchdog(self, mount_point: str, run):
        """Run ``run(is_aborted)`` under a fresh, registered MountWatchdog.

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
            threading.Thread(target=self._run_chart_check_auto, daemon=True).start()
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
            rumps.notification("EFIS Data Manager", "Busy", "Chart check is already running.")
            return
        logger.info("Manual chart check triggered.")
        threading.Thread(target=self._run_chart_check_manual, daemon=True).start()

    @rumps.clicked("Check Nav DB Now")
    def check_nav_db_now(self, _):
        if self._nav_running:
            logger.info("Nav DB check requested but already running.")
            rumps.notification("EFIS Data Manager", "Busy", "Nav DB check is already running.")
            return
        logger.info("Manual nav DB check triggered.")
        threading.Thread(target=self._run_nav_db_check, daemon=True).start()

    @rumps.clicked("Check EFIS/AHRS Software")
    def check_efis_software(self, _):
        if self._software_running:
            logger.info("Software check requested but already running.")
            rumps.notification("EFIS Data Manager", "Busy", "Software check is already running.")
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
                rumps.notification("EFIS Data Manager", "Charts Current",
                                   f"All {len(all_entries)} chart data sets are up to date.")
                self._set_status("Idle")
                return

            # Notify user, add "Download Charts" menu item
            names = ", ".join(e["description"] for e in new_entries)
            self._pending_chart_downloads = new_entries
            rumps.notification("EFIS Data Manager",
                               f"{len(new_entries)} Chart Update(s) Available",
                               f"{names}\n\nClick 'Download Charts' in the EFIS menu to start.")
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
            rumps.notification("EFIS Data Manager", "Chart Check Failed - Page Changed",
                               "Seattle Avionics page layout may have changed.")
            self._set_status("Alert: Chart page changed")
        except Exception as e:
            logger.error(f"Chart check failed: {e}")
            rumps.notification("EFIS Data Manager", "Chart Check Failed", str(e)[:100])
            self._set_status("Chart check failed")
        finally:
            self._charts_running = False

    def _on_download_charts(self, _):
        """User clicked Download Charts menu item."""
        if not self._pending_chart_downloads:
            return
        if self._charts_running:
            rumps.notification("EFIS Data Manager", "Busy", "A check is already running.")
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
        rumps.notification("EFIS Data Manager", "Chart Download Started",
                           f"Downloading {len(entries)} chart update(s)...")

        def on_progress(filename, pct):
            short_name = filename.split(".")[0] if "." in filename else filename
            self._set_status(f"Downloading {short_name}... {pct}%")

        try:
            results = download_charts(entries, progress_callback=on_progress)
            if results["errors"]:
                rumps.notification("EFIS Data Manager", "Chart Download Complete (errors)",
                                   f"Downloaded {results['downloaded']}, {len(results['errors'])} error(s).")
                self._set_status("Chart errors")
            else:
                rumps.notification("EFIS Data Manager", "Chart Download Complete",
                                   f"Downloaded and extracted {results['downloaded']} update(s).")
                self._set_status("Idle")
        except Exception as e:
            logger.error(f"Chart download failed: {e}")
            rumps.notification("EFIS Data Manager", "Chart Download Failed", str(e)[:100])
            self._set_status("Chart download failed")
        finally:
            self._charts_running = False

    # ------------------------------------------------------------------
    # Chart check (auto/scheduled — downloads without asking)
    # ------------------------------------------------------------------

    def _run_chart_check_auto(self):
        from efis_data_manager.currency import update_charts

        self._charts_running = True
        self._set_status("Checking charts...")

        try:
            results = update_charts()
            if results["errors"]:
                rumps.notification("EFIS Data Manager", "Chart Check - Errors",
                                   f"Downloaded {results['downloaded']}, {len(results['errors'])} error(s).")
                self._set_status("Chart errors")
            elif results["downloaded"] > 0:
                rumps.notification("EFIS Data Manager", "Charts Updated",
                                   f"Downloaded {results['downloaded']} chart update(s).")
                self._set_status("Idle")
            else:
                logger.info("Charts are current.")
                self._set_status("Idle")
        except Exception as e:
            logger.error(f"Chart check failed: {e}")
            rumps.notification("EFIS Data Manager", "Chart Check Failed", str(e)[:100])
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
                rumps.notification("EFIS Data Manager", "Nav DB Updated", result["message"])
            elif result["status"] == "current":
                rumps.notification("EFIS Data Manager", "Nav DB Current", result["message"])
            else:
                rumps.notification("EFIS Data Manager", "Nav DB Check Failed",
                                   result["message"][:100])
            self._set_status("Idle" if result["status"] != "error" else "Nav DB check failed")
        except Exception as e:
            logger.error(f"Nav DB check failed: {e}")
            rumps.notification("EFIS Data Manager", "Nav DB Check Failed", str(e)[:100])
            self._set_status("Nav DB check failed")
        finally:
            self._nav_running = False

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
                rumps.notification("EFIS Data Manager", "New EFIS Software Available",
                                   f"{names}. See grtavionics.com to download.")
                logger.info(f"Software available for manual download: {result['message']}")
                self._set_status(f"Software update available: {names}")
            elif result["status"] == "current":
                rumps.notification("EFIS Data Manager", "Software Current",
                                   result["message"])
                self._set_status("Idle")
            elif result["status"] == "blocked":
                # Bot protection — soft failure, don't alarm the user
                logger.warning(f"Software check blocked: {result['message']}")
                self._set_status("Idle")
            else:
                rumps.notification("EFIS Data Manager", "Software Check Failed",
                                   result["message"][:100])
                self._set_status("Software check failed")
        except Exception as e:
            logger.error(f"EFIS software check failed: {e}")
            rumps.notification("EFIS Data Manager", "Software Check Failed", str(e)[:100])
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
                rumps.notification("EFIS Data Manager", "Dashboard Failed",
                                   f"Could not start dashboard: {e}")
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
            rumps.notification("EFIS Data Manager", "Settings Saved",
                               "Configuration updated successfully.")

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
            rumps.notification("EFIS Data Manager", "Credentials Saved",
                               "Seattle Avionics login stored in macOS Keychain.")
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

        # Ask user to confirm which drive to format
        vol_list = "\n".join(f"• {v}" for v in volumes)
        window = rumps.Window(
            message=f"Available volumes:\n{vol_list}\n\n"
            "Type the EXACT volume name to format as an EFIS drive.\n"
            "WARNING: ALL DATA WILL BE ERASED.",
            title="Prepare Drive — Select Volume",
            default_text="",
            ok="Format", cancel="Cancel", dimensions=(300, 24),
        )
        response = window.run()
        if not response.clicked:
            return
        chosen = response.text.strip()
        if chosen not in volumes:
            rumps.alert(title="Prepare Drive", message=f"'{chosen}' not found in available volumes.")
            return

        # Final confirmation
        confirm = rumps.alert(
            title="CONFIRM FORMAT",
            message=f"This will ERASE ALL DATA on '{chosen}' and format it as an EFIS drive.\n\n"
            "Are you sure?",
            ok="Erase and Format", cancel="Cancel",
        )
        if confirm != 1:
            return

        # Run in background
        volume_path = f"/Volumes/{chosen}"
        threading.Thread(target=self._do_prepare_drive, args=(volume_path,), daemon=True).start()

    def _do_prepare_drive(self, volume_path: str):
        """Background thread for Prepare Drive."""
        from efis_data_manager.drive_updater import prepare_drive

        self._set_status("Preparing drive...")

        def on_progress(msg):
            self._set_status(msg)

        try:
            result = prepare_drive(volume_path, progress_callback=on_progress)
            if result["success"]:
                rumps.notification("EFIS Data Manager", "Drive Prepared", result["message"])
                self._set_status("Idle")
            else:
                rumps.notification("EFIS Data Manager", "Prepare Drive Failed", result["message"][:100])
                self._set_status("Prepare failed")
        except Exception as e:
            logger.error(f"Prepare drive failed: {e}")
            rumps.notification("EFIS Data Manager", "Prepare Drive Failed", str(e)[:100])
            self._set_status("Prepare failed")

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
        # Show most recent first
        body = "\n\n".join(reversed(errors))
        rumps.alert(
            title=f"Recent Errors ({len(errors)})",
            message=body,
            ok="OK",
        )

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
        """Set the menu bar title based on current state."""
        drive_connected = getattr(self, '_drive_connected', False)

        # The color PFD icon carries the branding; the title is now just a
        # compact status glyph shown next to it (empty when idle/disconnected).
        if "..." in status_text or "Downloading" in status_text:
            # Active work
            self.title = "\u21BB"  # ↻
        elif drive_connected:
            # Drive connected, idle
            self.title = "\u25CF"  # ● (filled circle = connected)
        else:
            # No drive, idle — icon only
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
