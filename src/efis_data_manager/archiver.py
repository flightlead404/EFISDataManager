# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""EFIS data archiver — captures files from USB drive to local archive.

Handles:
- FDL CSV files (move with validation)
- DEMO LOG files (move with validation)
- SNAP PNG files (move with validation)
- Logbook CSV files (copy, don't delete)
- Settings .bak files (copy with date stamp)
- GRTCHARTS cleanup
- E:ChartData removal
"""

import hashlib
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from efis_data_manager.config import load_config

logger = logging.getLogger(__name__)


def sha256_file(filepath: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(256 * 1024):
            h.update(chunk)
    return h.hexdigest()


def archive_efis_drive(
    mount_point: str,
    progress_callback: Optional[Callable] = None,
    today: Optional[str] = None,
) -> dict:
    """Archive all EFIS data from a mounted USB drive.

    Runs sequentially: all reads/copies from USB first, then deletions.

    Args:
        mount_point: Path to the mounted EFIS volume (e.g. /Volumes/EFIS).
        progress_callback: Optional callable(message) for status updates.
        today: Optional ``YYYY-MM-DD`` date string to stamp into archived
            settings snapshots. Defaults to the current date. Tests inject this
            to drive Archive_Date ordering deterministically.

    Returns:
        Dict with results: {
            "fdl_moved": int, "demo_moved": int, "snap_moved": int,
            "logbook_copied": int, "settings_copied": int,
            "errors": list[str], "skipped": int
        }
    """
    config = load_config()
    archive_root = Path(config["archive_path"])
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    results = {
        "fdl_moved": 0, "demo_moved": 0, "snap_moved": 0,
        "logbook_copied": 0, "settings_copied": 0, "fdl_imported": 0,
        "errors": [], "skipped": 0, "cleaned": [],
    }

    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    _status("Scanning drive for EFIS data...")

    # --- Phase 1: Copy/move files FROM USB (reads) ---

    # --- Priority: FDL files first, imported immediately ---
    # Flight data is the time-sensitive payload — move and import it before
    # the bulky DEMO logs so the analysis DB is ready ASAP.

    # FDL CSV files
    fdl_dest = archive_root / "FDL" / today
    fdl_archived_paths = []
    for f in _find_files(mount_point, "GRT FDL*.csv"):
        result = _move_file(f, fdl_dest, mount_point, archive_root)
        if result == "moved":
            results["fdl_moved"] += 1
            # Track archived path for database import
            fdl_archived_paths.append(fdl_dest / os.path.basename(f))
        elif result == "skipped":
            results["skipped"] += 1
            # Already archived (matching size), but it may not have been
            # imported to the DB — queue it. import_fdl_file dedups by
            # (filename, start_time), so re-importing is a no-op if present.
            fdl_archived_paths.append(fdl_dest / os.path.basename(f))
        else:
            results["errors"].append(result)

    # Import FDL data into analysis DB immediately (before slow DEMO archive)
    if fdl_archived_paths:
        _status("Importing FDL data to database...")
        _import_fdl_to_database(fdl_archived_paths, results)

    # Logbook CSV files. The EFIS overwrites the logbook each save (same name,
    # possibly same size), so we can't dedup by name+size. Flow:
    #   1. Copy to archive with a date stamp (keeps each snapshot)
    #   2. Verify with SHA-256
    #   3. Import to DB (watermark ensures only new entries are processed)
    #   4. Delete from USB once import is verified
    logbook_dest = archive_root / "Logbook"
    for f in _find_files(mount_point, "Logbook*.csv"):
        name, ext = os.path.splitext(os.path.basename(f))
        dated_name = f"{name}-{today}{ext}"
        archived_path = logbook_dest / dated_name

        # Copy with verification
        logbook_dest.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, archived_path)
            if sha256_file(f) != sha256_file(str(archived_path)):
                archived_path.unlink(missing_ok=True)
                results["errors"].append(f"Logbook validation failed: {os.path.basename(f)}")
                continue
        except OSError as e:
            results["errors"].append(f"Logbook copy failed: {e}")
            continue

        results["logbook_copied"] += 1

        # Import to DB (watermark filters to new entries only)
        import_ok = _import_logbook_to_database([archived_path], results)

        # Delete from USB only after a verified copy + successful import
        if import_ok:
            try:
                os.remove(f)
                logger.info(f"Logbook imported and removed from USB: {os.path.basename(f)}")
            except OSError as e:
                results["errors"].append(f"Logbook delete failed (archived OK): {e}")

    # Settings families: content-aware, per-family archive-on-change (Req 3).
    # For each family (Settings/WP/Plan — State is NOT archived, Req 3.8) pick
    # the current slot on the drive, compare its Content_Hash against the
    # family's Most_Recent_Archive, and write a new dated snapshot only when the
    # content changed (or no archive exists). Source files are never modified or
    # deleted (Req 3.2).
    settings_dest = archive_root / "Settings"
    for family in _ARCHIVED_SETTINGS_FAMILIES:
        try:
            result = _archive_settings_family(
                mount_point, settings_dest, family, today
            )
        except Exception as e:  # fault tolerance (Req 10): never crash the run
            logger.info(f"Settings family {family} archive skipped: {e}")
            continue
        if result == "copied":
            results["settings_copied"] += 1
        elif result == "skipped":
            results["skipped"] += 1
        elif result == "none":
            pass  # family not present / unparseable — nothing to do
        else:
            results["errors"].append(result)

    # --- Bulk archival: DEMO and snapshot files (not time-sensitive) ---

    # DEMO LOG files
    demo_dest = archive_root / "Demo" / today
    for f in _find_files(mount_point, "DEMO-*.LOG"):
        result = _move_file(f, demo_dest, mount_point, archive_root)
        if result == "moved":
            results["demo_moved"] += 1
        elif result == "skipped":
            results["skipped"] += 1
        else:
            results["errors"].append(result)

    # Snapshot PNG files
    snap_dest = archive_root / "Snapshots" / today
    for f in _find_files(mount_point, "SNAP*.PNG"):
        result = _move_file(f, snap_dest, mount_point, archive_root)
        if result == "moved":
            results["snap_moved"] += 1
        elif result == "skipped":
            results["skipped"] += 1
        else:
            results["errors"].append(result)

    # --- Phase 2: Cleanup (writes/deletes on USB) ---

    # Clean GRTCHARTS/ contents
    grtcharts = os.path.join(mount_point, "GRTCHARTS")
    if os.path.isdir(grtcharts):
        for item in os.listdir(grtcharts):
            item_path = os.path.join(grtcharts, item)
            if item.startswith("."):
                continue
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    results["cleaned"].append(f"GRTCHARTS/{item}")
                else:
                    os.remove(item_path)
                    results["cleaned"].append(f"GRTCHARTS/{item}")
            except OSError as e:
                results["errors"].append(f"Cleanup GRTCHARTS/{item}: {e}")

    # Remove E:ChartData/ if present
    echartdata = os.path.join(mount_point, "ChartData", "E:ChartData")
    if os.path.isdir(echartdata):
        _status("Removing E:ChartData (stale Windows artifact)...")
        try:
            shutil.rmtree(echartdata)
            results["cleaned"].append("ChartData/E:ChartData")
        except OSError as e:
            results["errors"].append(f"Remove E:ChartData: {e}")

    _status("Archive complete.")
    return results


def _find_files(mount_point: str, pattern: str) -> list[str]:
    """Find files matching a glob pattern at the root of the mount point."""
    import fnmatch

    # Match case-insensitively: GRT writes .CSV/.LOG/.PNG (uppercase) but we
    # want patterns to match regardless of case. fnmatch's case behavior is
    # OS-dependent, so normalize both sides explicitly.
    pattern_lower = pattern.lower()
    found = []
    try:
        for name in os.listdir(mount_point):
            if fnmatch.fnmatchcase(name.lower(), pattern_lower):
                full_path = os.path.join(mount_point, name)
                if os.path.isfile(full_path):
                    found.append(full_path)
    except OSError as e:
        logger.error(f"Error scanning {mount_point}: {e}")
    return sorted(found)


def _move_file(src: str, dest_dir: Path, mount_point: str, archive_root: Path) -> str:
    """Move a file from USB to archive with SHA-256 validation.

    Returns:
        "moved" on success, "skipped" if already archived, or error string.
    """
    filename = os.path.basename(src)
    dest_path = dest_dir / filename

    # Check if already archived (same name + same size)
    if dest_path.exists() and dest_path.stat().st_size == os.path.getsize(src):
        return "skipped"

    # Copy to destination
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest_path)
    except OSError as e:
        return f"Copy failed {filename}: {e}"

    # Validate (SHA-256)
    try:
        src_hash = sha256_file(src)
        dest_hash = sha256_file(str(dest_path))
    except OSError as e:
        return f"Hash failed {filename}: {e}"

    if src_hash != dest_hash:
        # Validation failed — remove bad copy, don't delete source
        dest_path.unlink(missing_ok=True)
        return f"Validation failed {filename}: hash mismatch"

    # Validated — delete from USB
    try:
        os.remove(src)
        logger.info(f"Archived and removed: {filename}")
    except OSError as e:
        return f"Delete failed {filename} (archived OK): {e}"

    return "moved"


def _copy_file(src: str, dest_dir: Path) -> str:
    """Copy a file to archive without deleting from USB.

    Returns:
        "copied" on success, "skipped" if identical file exists, or error string.
    """
    filename = os.path.basename(src)
    dest_path = dest_dir / filename

    # Check if already archived (same name + same size)
    if dest_path.exists() and dest_path.stat().st_size == os.path.getsize(src):
        return "skipped"

    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest_path)
        logger.info(f"Copied: {filename}")
        return "copied"
    except OSError as e:
        return f"Copy failed {filename}: {e}"


# ---------------------------------------------------------------------------
# Settings families archive-on-change (Requirement 3)
# ---------------------------------------------------------------------------

# Families the Archiver keeps a change-history for. State is intentionally
# excluded — it is an ephemeral runtime snapshot with no configuration history
# value (Req 3.8).
_ARCHIVED_SETTINGS_FAMILIES = ["Settings", "WP", "Plan"]


def _current_settings_slot(mount_point: str, family: str):
    """Select the current slot of ``family`` on the drive (Req 3.1).

    Parses whichever of ``<family>.bak`` / ``<family>.dat`` are present and
    picks the current slot via ``Settings_Selector.select`` (higher UPDATE=,
    with the deterministic .dat/path tiebreak). Returns the chosen
    ``ParsedBackup`` (its ``path`` tells which file/extension to copy), or
    ``None`` when neither slot is present or neither parses.
    """
    from efis_data_manager.efis_settings import (
        Settings_Parser,
        Settings_Selector,
        SettingsParseError,
    )

    candidates = []
    for ext in (".bak", ".dat"):
        path = os.path.join(mount_point, f"{family}{ext}")
        if not os.path.isfile(path):
            continue
        try:
            candidates.append(Settings_Parser.parse(path))
        except SettingsParseError as e:
            logger.debug(f"{family}{ext} did not parse, ignoring: {e}")

    if not candidates:
        return None

    return Settings_Selector.select(candidates).current


def _most_recent_family_archive(settings_dest: Path, family: str):
    """Find the Most_Recent_Archive for ``family`` (Req 3.5).

    Scans existing ``<family>-YYYY-MM-DD[-N].<ext>`` archives, orders by
    Archive_Date (via ``parse_archive_date`` — never mtime or UPDATE=), and
    within the same date by the highest sequence suffix (un-suffixed == 1).
    Returns the parsed ``ParsedBackup`` of the most recent archive, or ``None``
    when no archive exists for the family (or none parses).
    """
    from efis_data_manager.efis_settings import (
        Settings_Parser,
        SettingsParseError,
        parse_archive_date,
    )

    if not settings_dest.is_dir():
        return None

    best = None  # (archive_date, sequence, path)
    for path in settings_dest.iterdir():
        if not path.is_file():
            continue
        name = path.name
        base, dot, ext = name.rpartition(".")
        if not dot:
            continue
        # Must be "<family>-..." for this family.
        prefix = f"{family}-"
        if not base.startswith(prefix):
            continue
        archive_date = parse_archive_date(name)
        if archive_date is None:
            continue  # no parseable datestamp — ignore for recency
        seq = _archive_sequence(base, prefix)
        key = (archive_date, seq)
        if best is None or key > best[0]:
            best = (key, str(path))

    if best is None:
        return None

    try:
        return Settings_Parser.parse(best[1])
    except SettingsParseError as e:
        logger.debug(f"Most-recent archive {best[1]} did not parse: {e}")
        return None


def _archive_sequence(base: str, prefix: str) -> int:
    """Return the same-date sequence number encoded in an archive base name.

    ``<family>-YYYY-MM-DD`` -> 1 (un-suffixed first same-date snapshot).
    ``<family>-YYYY-MM-DD-N`` -> N. Falls back to 1 when unparseable.
    """
    remainder = base[len(prefix):]  # e.g. "2026-09-12" or "2026-09-12-3"
    parts = remainder.split("-")
    # A bare date is Y, M, D (3 parts). A sequenced one is Y, M, D, N (4 parts).
    if len(parts) >= 4:
        try:
            return int(parts[3])
        except ValueError:
            return 1
    return 1


def _next_available_snapshot_name(
    settings_dest: Path, family: str, date_str: str, ext: str
) -> Path:
    """Return the next non-destructive same-date snapshot path (Req 3.7).

    First same-date snapshot is ``<family>-YYYY-MM-DD.<ext>``; if that exists a
    distinct same-date change gets ``<family>-YYYY-MM-DD-2.<ext>``, then -3, etc.
    Never returns a path that already exists.
    """
    primary = settings_dest / f"{family}-{date_str}{ext}"
    if not primary.exists():
        return primary
    seq = 2
    while True:
        candidate = settings_dest / f"{family}-{date_str}-{seq}{ext}"
        if not candidate.exists():
            return candidate
        seq += 1


def _archive_settings_family(
    mount_point: str, settings_dest: Path, family: str, date_str: str
) -> str:
    """Archive one settings family on change (Req 3.1/3.3/3.4/3.5/3.7).

    Returns:
        "copied" when a new snapshot was written, "skipped" when the content was
        unchanged vs. the Most_Recent_Archive, "none" when the family is not
        present / unparseable on the drive, or an error string on write failure.
    """
    from efis_data_manager.efis_settings import content_hash

    current = _current_settings_slot(mount_point, family)
    if current is None:
        return "none"

    _, ext = os.path.splitext(current.path)

    latest = _most_recent_family_archive(settings_dest, family)
    if latest is not None and content_hash(current) == content_hash(latest):
        # Unchanged vs. most recent archive — no new snapshot (Req 3.4).
        return "skipped"

    dest_path = _next_available_snapshot_name(settings_dest, family, date_str, ext)
    settings_dest.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(current.path, dest_path)
        logger.info(f"Archived settings snapshot: {dest_path.name}")
        return "copied"
    except OSError as e:
        return f"Settings archive failed {dest_path.name}: {e}"


def _import_fdl_to_database(fdl_paths: list[Path], results: dict):
    """Parse archived FDL files and import into the analysis database.

    Non-fatal: errors here don't affect the archive results, just logged.
    """
    from efis_data_manager.fdl_parser import parse_fdl_file
    from efis_data_manager.database import import_fdl_file

    imported = 0
    for path in fdl_paths:
        try:
            fdl = parse_fdl_file(str(path))
            op_id = import_fdl_file(fdl)
            if op_id is not None:
                imported += 1
                logger.info(f"Imported to DB: {path.name} (op_id={op_id}, "
                           f"flight={fdl.has_flight})")
        except Exception as e:
            logger.error(f"FDL database import failed for {path.name}: {e}")

    results["fdl_imported"] = imported
    if imported:
        logger.info(f"Imported {imported} FDL file(s) to analysis database.")


def _import_logbook_to_database(logbook_paths: list[Path], results: dict) -> bool:
    """Import logbook CSV files into the analysis database.

    Non-fatal: errors are logged but don't crash the pipeline.

    Returns:
        True if all imports completed without a hard error (safe to delete
        the source from USB), False otherwise.
    """
    from efis_data_manager.database import import_logbook_csv

    all_ok = True
    for path in logbook_paths:
        if not path.exists():
            all_ok = False
            continue
        try:
            result = import_logbook_csv(str(path))
            if result.get("errors"):
                all_ok = False
                logger.error(f"Logbook import had errors for {path.name}: {result['errors'][:3]}")
            else:
                logger.info(
                    f"Logbook DB import from {path.name}: "
                    f"{result.get('legs_read', 0)} new legs, "
                    f"{result.get('oil_events_created', 0)} oil events, "
                    f"{result.get('operations_enriched', 0)} operations enriched"
                )
        except Exception as e:
            all_ok = False
            logger.error(f"Logbook database import failed for {path.name}: {e}")

    return all_ok
