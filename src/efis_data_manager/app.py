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


class EFISDataManagerApp(rumps.App):
    """Menu bar application for GRT HXr ground support automation."""

    def __init__(self):
        super().__init__(
            name="EFIS Data Manager",
            title="EFIS",
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

        self.menu = [
            "Status: Idle",
            "Drive: Not connected",
            "Eject Drive",
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
            "Recent Errors...",
            "About",
            "Quit",
        ]

        self.menu["Status: Idle"].set_callback(None)
        self.menu["Drive: Not connected"].set_callback(None)
        self.menu["Alerts (0)"].set_callback(None)
        self.menu["Archive: " + self._short_path(self.config["archive_path"])].set_callback(None)
        self.menu["USB Image: " + self._short_path(self.config["usb_image_path"])].set_callback(None)

        # Start USB monitoring
        self._start_usb_monitor()

        # Initial state: disable eject if no drive
        if not getattr(self, '_drive_connected', False):
            self.menu["Eject Drive"].set_callback(None)

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

    def _on_efis_drive_mounted(self, mount_point: str):
        """Called when an EFIS drive is detected."""
        logger.info(f"EFIS drive mounted: {mount_point}")
        self._set_drive_status(f"Connected: {mount_point}")
        self._set_status("EFIS drive detected")
        rumps.notification("EFIS Data Manager", "EFIS Drive Detected",
                           f"Drive mounted at {mount_point}. Starting archive...")

        # Check if a previous sync was interrupted for this mount point
        sync_state_file = os.path.expanduser("~/EFIS/DataManagerLogs/.sync_in_progress")
        if os.path.exists(sync_state_file):
            try:
                with open(sync_state_file) as f:
                    prev_mount = f.read().strip()
                if prev_mount == mount_point:
                    logger.info("Previous sync was interrupted — re-triggering update after archive.")
            except OSError:
                pass

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
        """Check drive currency and sync updates if needed."""
        from efis_data_manager.drive_updater import check_drive_currency, update_drive

        self._set_status("Checking drive currency...")
        sync_state_file = os.path.expanduser("~/EFIS/DataManagerLogs/.sync_in_progress")

        try:
            currency = check_drive_currency(mount_point)

            if currency["is_current"]:
                logger.info("Drive is up to date, no sync needed.")
                self._set_status("Idle")
                rumps.notification("EFIS Data Manager", "Drive Current",
                                   "EFIS drive is up to date.")
                return

            # Drive needs updating
            stale_summary = ", ".join(currency["stale_items"][:3])
            if len(currency["stale_items"]) > 3:
                stale_summary += f" +{len(currency['stale_items']) - 3} more"
            self._set_status("Updating drive...")
            rumps.notification("EFIS Data Manager", "Updating Drive",
                               f"{len(currency['stale_items'])} item(s) to update: {stale_summary}")

            # Write sync-in-progress state file
            os.makedirs(os.path.dirname(sync_state_file), exist_ok=True)
            with open(sync_state_file, "w") as f:
                f.write(mount_point)

            def on_progress(msg):
                self._set_status(msg)

            results = update_drive(mount_point, progress_callback=on_progress)

            # Sync complete — remove state file
            if os.path.exists(sync_state_file):
                os.remove(sync_state_file)

            if results["errors"]:
                rumps.notification("EFIS Data Manager", "Drive Update Complete (errors)",
                                   f"Updated {results['files_updated']} item(s), "
                                   f"{len(results['errors'])} error(s).")
                self._set_status("Update errors")
            else:
                rumps.notification("EFIS Data Manager", "Drive Update Complete",
                                   f"Updated {results['files_updated']} item(s). Drive is current.")
                self._set_status("Idle")

        except Exception as e:
            logger.error(f"Drive update failed: {e}")
            # Don't remove sync state file on failure — will retry on next mount
            rumps.notification("EFIS Data Manager", "Drive Update Failed", str(e)[:100])
            self._set_status("Update failed")

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
            # Shut down the dashboard server if we started it
            if self._dashboard_process and self._dashboard_process.poll() is None:
                try:
                    self._dashboard_process.terminate()
                except Exception:
                    pass
            rumps.quit_application()

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
            # Enable/disable eject button
            if self._drive_connected:
                self.menu["Eject Drive"].set_callback(self.eject_drive)
            else:
                self.menu["Eject Drive"].set_callback(None)
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

                # If there are new warnings, reflect in the menu bar title
                warnings = [a for a in anomalies if a.severity == "warning"]
                if warnings:
                    self.title = "\u26A0 EFIS"  # ⚠ EFIS

            except Exception as e:
                logger.error(f"Alert refresh failed: {e}")

        NSOperationQueue.mainQueue().addOperationWithBlock_(_do_refresh)

    def _update_title(self, status_text: str):
        """Set the menu bar title based on current state."""
        drive_connected = getattr(self, '_drive_connected', False)

        if "..." in status_text or "Downloading" in status_text:
            # Active work
            self.title = "\u21BB EFIS"  # ↻ EFIS
        elif drive_connected:
            # Drive connected, idle
            self.title = "\u25CF EFIS"  # ● EFIS (filled circle = connected)
        else:
            # No drive, idle
            self.title = "EFIS"


def main():
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    ns_app = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    logger.info("EFIS Data Manager starting...")
    app = EFISDataManagerApp()
    logger.info("EFIS Data Manager ready.")
    app.run()


if __name__ == "__main__":
    main()
