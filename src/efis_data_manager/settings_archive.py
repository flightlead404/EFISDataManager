# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-device settings archive location and history (Settings Management).

Pure path/scan helpers that partition a family's archived snapshots by the
owning device's Link_ID, so one display's settings history never mixes with
another's. All functions here are read-only: they resolve paths and scan
directories but never create, move, or modify archived files.

Recency is always Archive_Date then same-date sequence suffix — never file
mtime or UPDATE_Value (Req 1.7, 2.1). This reuses the existing
``efis_settings.parse_archive_date`` and the archiver's sequence parsing
(``archiver._archive_sequence``) so the ordering matches the writer.
"""

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from efis_data_manager.archiver import (
    _ARCHIVED_SETTINGS_FAMILIES,
    _archive_sequence,
    _next_available_snapshot_name,
)
from efis_data_manager.efis_settings import (
    Settings_Parser,
    extract_source_key,
    parse_archive_date,
)

logger = logging.getLogger(__name__)

# Sentinel written directly under ``Settings/`` after a complete migration pass.
# Its presence makes ``migrate_flat_archives`` a no-op on subsequent runs
# (Req 11.6).
_MIGRATION_SENTINEL = ".per_device_migrated"


@dataclass
class ArchivedSnapshot:
    """One archived settings snapshot in a device's history.

    Attributes:
        path: Full path to the archived snapshot file.
        family: Settings family ("Settings", "WP", "Plan").
        link_id: Owning device's Link_ID, or ``None`` for the unresolved
            location.
        archive_date: The ``YYYY-MM-DD`` Archive_Date stamped into the filename.
        sequence: The same-date sequence suffix (``-N``); 1 for an un-suffixed
            first same-date snapshot.
    """

    path: Path
    family: str
    link_id: Optional[str]
    archive_date: date
    sequence: int


def device_settings_dir(archive_root: Path, link_id: Optional[str]) -> Path:
    """Return the per-device settings archive directory for ``link_id``.

    ``archive_root/Settings/link{N}`` for a resolved Link_ID, or
    ``archive_root/Settings/unresolved`` when ``link_id`` is ``None`` or empty
    (the content-underivable location, Req 1.10 / glossary).

    This is a pure path computation; it does not create the directory.
    """
    archive_root = Path(archive_root)
    settings_root = archive_root / "Settings"
    if link_id is None or str(link_id).strip() == "":
        return settings_root / "unresolved"
    return settings_root / f"link{str(link_id).strip()}"


def family_history(
    archive_root: Path, link_id: Optional[str], family: str
) -> list[ArchivedSnapshot]:
    """Return a device-and-family's archived history, newest to oldest.

    Scans ``device_settings_dir(archive_root, link_id)`` for snapshots named
    ``<family>-YYYY-MM-DD[-N].<ext>`` and returns them ordered from newest to
    oldest by (Archive_Date, sequence) — never by mtime or UPDATE_Value
    (Req 1.7, 2.1). A missing device directory yields an empty list without
    error (Req 2.3). Files that carry no parseable Archive_Date are ignored.

    Read-only: the directory is scanned but never modified (Req 2.4).
    """
    device_dir = device_settings_dir(archive_root, link_id)
    if not device_dir.is_dir():
        return []

    prefix = f"{family}-"
    snapshots: list[ArchivedSnapshot] = []
    for path in device_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        base, dot, _ext = name.rpartition(".")
        if not dot:
            continue  # no extension — not a settings snapshot
        if not base.startswith(prefix):
            continue  # a different family
        archive_date = parse_archive_date(name)
        if archive_date is None:
            continue  # no parseable datestamp — not orderable, ignore
        sequence = _archive_sequence(base, prefix)
        snapshots.append(
            ArchivedSnapshot(
                path=path,
                family=family,
                link_id=link_id if (link_id is not None and str(link_id).strip()) else None,
                archive_date=archive_date,
                sequence=sequence,
            )
        )

    # Newest first: descending by (Archive_Date, sequence).
    snapshots.sort(key=lambda s: (s.archive_date, s.sequence), reverse=True)
    return snapshots


def latest_version(
    archive_root: Path, link_id: Optional[str], family: str
) -> Optional[ArchivedSnapshot]:
    """Return the device-and-family's latest archived snapshot, or ``None``.

    The most recent snapshot by (Archive_Date, sequence), i.e. the first item
    of ``family_history`` (Req 2.1, used by Req 6 populate and Req 7
    divergence). Returns ``None`` when the device-and-family has no archived
    snapshot.
    """
    history = family_history(archive_root, link_id, family)
    return history[0] if history else None


@dataclass
class MigrationResult:
    """Outcome of a one-time flat-archive adoption pass (Req 11).

    Attributes:
        adopted: Count of flat snapshots moved under a resolved ``link{N}`` dir.
        unresolved: Count of flat snapshots moved under ``unresolved/`` because
            their owning Link_ID could not be derived from their content.
        collisions: Count of adopted snapshots that had to take a
            sequence-suffixed name because their original name already existed
            at the destination (Req 11.5).
        errors: Human-readable messages for per-file failures; the offending
            file is left in place (fault tolerance, resumable).
    """

    adopted: int = 0
    unresolved: int = 0
    collisions: int = 0
    errors: list[str] = field(default_factory=list)


def _flat_snapshot_family(base: str) -> Optional[str]:
    """Return the settings family a flat archive base name belongs to, or None.

    ``base`` is the filename without its extension, e.g. ``Settings-2026-09-12``
    or ``WP-2026-09-12-2``. Matches only the known archived families so unrelated
    files directly under ``Settings/`` are left untouched.
    """
    for family in _ARCHIVED_SETTINGS_FAMILIES:
        if base.startswith(f"{family}-"):
            return family
    return None


def migrate_flat_archives(archive_root: Path) -> MigrationResult:
    """Adopt pre-existing flat settings snapshots into the per-device layout.

    A one-time, idempotent migration (Req 11): every snapshot directly under
    ``archive_root/Settings/`` (not already in a ``link*/`` or ``unresolved/``
    subdir) is parsed read-only, its owning device's Link_ID is derived from its
    OWN content via ``extract_source_key`` (Req 11.2), and the file is
    ``shutil.move``d into ``device_settings_dir(archive_root, link_id)`` —
    ``unresolved/`` when the Link_ID is underivable (Req 11.3), never discarded
    and never assigned to an arbitrary device.

    The move preserves bytes and the file's Archive_Date/sequence by keeping its
    original name when free; on a same-name collision at the destination it
    takes the next non-destructive sequence-suffixed name via
    ``_next_available_snapshot_name`` (Req 11.4, 11.5), overwriting nothing.

    Per-file failures (parse errors, move errors) are logged and the file is
    left in place, so a partial run is safe to resume. After a complete pass a
    sentinel (``Settings/.per_device_migrated``) is written; a subsequent call
    short-circuits to a no-op (Req 11.6).
    """
    archive_root = Path(archive_root)
    settings_root = archive_root / "Settings"
    result = MigrationResult()

    sentinel = settings_root / _MIGRATION_SENTINEL
    if sentinel.exists():
        # Migration already completed — no-op (Req 11.6).
        return result

    if not settings_root.is_dir():
        # Nothing archived yet; still drop the sentinel so we never rescan.
        _write_sentinel(sentinel)
        return result

    for path in sorted(settings_root.iterdir()):
        if not path.is_file():
            continue  # link*/ and unresolved/ subdirs are already per-device
        name = path.name
        if name == _MIGRATION_SENTINEL:
            continue
        base, dot, ext_no_dot = name.rpartition(".")
        if not dot:
            continue  # no extension — not a settings snapshot
        family = _flat_snapshot_family(base)
        if family is None:
            continue  # unrelated file directly under Settings/
        archive_date = parse_archive_date(name)
        if archive_date is None:
            continue  # no parseable datestamp — not an archiver snapshot

        try:
            _adopt_one(archive_root, path, family, base, result)
        except Exception as exc:  # noqa: BLE001 — fault tolerance, resumable
            msg = f"migration: failed to adopt {name}: {exc}"
            logger.warning(msg)
            result.errors.append(msg)
            # File left in place; a later run retries it.

    # A complete pass — mark done so reruns are no-ops (Req 11.6).
    _write_sentinel(sentinel)
    return result


def _adopt_one(
    archive_root: Path,
    path: Path,
    family: str,
    base: str,
    result: MigrationResult,
) -> None:
    """Move one flat snapshot into its content-derived per-device location.

    Derives the owner from the snapshot's own parsed content; chooses a
    non-destructive destination name; moves bytes verbatim. Updates ``result``
    counters in place. Raises on parse/move failure so the caller can log and
    leave the file in place.
    """
    parsed = Settings_Parser.parse(str(path))  # read-only
    identity = extract_source_key(parsed)
    link_id = identity.link_id  # None when underivable (Req 11.3)

    dest_dir = device_settings_dir(archive_root, link_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    ext = path.suffix  # e.g. ".dat" / ".bak"
    # Prefer the original name so Archive_Date + sequence suffix are preserved
    # verbatim (Req 11.4). Only on a genuine collision do we fall back to the
    # next available sequence-suffixed name (Req 11.5), overwriting nothing.
    dest = dest_dir / path.name
    collided = False
    if dest.exists():
        collided = True
        date_str = _date_str_from_base(base, family)
        dest = _next_available_snapshot_name(dest_dir, family, date_str, ext)

    shutil.move(str(path), str(dest))

    if collided:
        result.collisions += 1
    if link_id:
        result.adopted += 1
    else:
        result.unresolved += 1


def _date_str_from_base(base: str, family: str) -> str:
    """Return the ``YYYY-MM-DD`` datestamp from an archive base name.

    ``_next_available_snapshot_name`` needs the date string (not the sequence)
    to build same-date candidates. ``<family>-YYYY-MM-DD[-N]`` -> ``YYYY-MM-DD``.
    """
    remainder = base[len(family) + 1:]  # strip "<family>-"
    parts = remainder.split("-")
    return "-".join(parts[:3])


def _write_sentinel(sentinel: Path) -> None:
    """Create the migration sentinel, best-effort (never raises)."""
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch(exist_ok=True)
    except OSError as exc:
        logger.warning(f"migration: could not write sentinel {sentinel}: {exc}")
