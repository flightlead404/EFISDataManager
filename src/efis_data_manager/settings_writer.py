# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Byte-preserving sole-slot settings writer (Requirements 5, 8).

Settings are flight-critical (Vne, redlines, engine limits, cylinder count), so
this writer is correctness-first. It NEVER fabricates, bumps, or recomputes the
GRT ``UPDATE=`` / ``CHECKSIZE=`` / ``CHECKSUM=`` bytes. It only ever copies an
archived file's own original, EFIS-written bytes verbatim onto a drive — the
"checksum wall" (R8): a settings file whose embedded CHECKSUM does not match its
contents is silently ignored by the EFIS, so the only safe write is the one that
moves original bytes untouched.

``sole_slot_write`` is the shared core used by both restoration (R5) and, later,
population (R6). Its ordering is the safety crux (R5.6):

    1. write the new slot (copy the archived bytes to ``<mount>/<family><ext>``),
    2. BYTE-VERIFY the written slot against the source (SHA-256), and
    3. only AFTER a passing verify, remove the OTHER ``.bak``/``.dat`` slot so
       the written file becomes the sole slot for that family.

A failure at any step leaves at least one valid, loadable slot on the drive and
never presents a half-written file as current: on a verify failure the new file
is unlinked and the pre-existing other slot is left intact, and the result
carries status ``"failed"``.

Restoration and population target the **Settings family only** (R5.9/R6.6);
``sole_slot_write`` accepts a ``family`` argument but ``restore_version`` fixes
it to ``"Settings"``.
"""

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The two settings-slot extensions a GRT family can occupy on a drive. The
# written slot takes the archived file's OWN extension; the other one is the
# slot removed after a verified write so exactly one slot remains.
_SLOT_EXTS = (".bak", ".dat")


@dataclass
class WriteResult:
    """Outcome of a sole-slot write (R5).

    Attributes:
        status: ``"written"`` on a verified sole-slot write; ``"failed"`` when
            the write could not be completed or verified (the other slot is left
            intact); ``"skipped-none"`` / ``"skipped-equal"`` are reserved for
            the population path (R6) and are not produced by this task's writer.
        written_path: Absolute path of the slot written on the drive, or None
            when nothing was written.
        removed_slots: Absolute paths of any other-extension slots removed to
            make the written file the sole slot. Empty when none were removed or
            when the write failed before the removal step.
        error: Human-readable failure reason when ``status == "failed"``, else
            None.
    """

    status: str
    written_path: Optional[str] = None
    removed_slots: list[str] = field(default_factory=list)
    error: Optional[str] = None


def _sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of a file, read in bounded blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(256 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sole_slot_write(
    archived_path, mount_point: str, family: str = "Settings"
) -> WriteResult:
    """Write an archived settings version as the SOLE slot of ``family`` (R5).

    Copies ``archived_path``'s original bytes verbatim to
    ``<mount_point>/<family><ext>`` where ``<ext>`` is the archived file's OWN
    extension (``.bak``/``.dat``), verifies the written slot is byte-identical to
    the source by SHA-256, and only after a passing verify removes the OTHER slot
    (``<family>{.dat|.bak}``) so the EFIS's native "Restore All Settings" loads
    the written version by default (R5.4).

    The bytes are copied with ``shutil.copy2`` and never parsed-then-reserialized,
    so ``UPDATE=`` / ``CHECKSIZE=`` / ``CHECKSUM=`` are physically untouched — the
    checksum wall is honored structurally (R5.3, R8.1, R8.3).

    Failure handling (R5.6): if the copy or the byte verify fails, the new file
    is unlinked and the pre-existing other slot is left intact, so the drive
    always retains a loadable slot; the result status is ``"failed"`` and the
    reason is surfaced in ``error``. The other slot is removed ONLY after a
    verified write, so a failed write never deletes the existing good slot.

    Args:
        archived_path: Path to the archived settings snapshot to write. Its
            extension determines the written slot's extension.
        mount_point: Path to the mounted drive the slot is written onto.
        family: Settings-format family name; the caller fixes this to
            ``"Settings"`` for restoration/population (R5.9/R6.6).

    Returns:
        A :class:`WriteResult`.
    """
    archived_path = Path(archived_path)
    source = str(archived_path)
    ext = archived_path.suffix.lower()

    # The written slot carries the archived file's own extension; the "other"
    # slot (to be removed only after a verified write) is the complementary one.
    if ext not in _SLOT_EXTS:
        return WriteResult(
            status="failed",
            error=(
                f"archived file {archived_path.name!r} has no recognized "
                f"settings-slot extension ({' / '.join(_SLOT_EXTS)})"
            ),
        )
    other_ext = _SLOT_EXTS[1] if ext == _SLOT_EXTS[0] else _SLOT_EXTS[0]

    target = os.path.join(mount_point, f"{family}{ext}")
    other_slot = os.path.join(mount_point, f"{family}{other_ext}")

    # --- Step 1: write the new slot (bytes verbatim; never reserialize). ---
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        # Do not leave a partial file behind; do NOT touch the other slot.
        _best_effort_unlink(target)
        logger.warning("sole_slot_write: copy failed for %s: %s", target, exc)
        return WriteResult(
            status="failed",
            error=f"copy failed: {exc}",
        )

    # --- Step 2: byte-verify the written slot against the source (R5.5). ---
    try:
        source_hash = _sha256_file(source)
        target_hash = _sha256_file(target)
    except OSError as exc:
        _best_effort_unlink(target)
        logger.warning("sole_slot_write: verify read failed for %s: %s", target, exc)
        return WriteResult(
            status="failed",
            error=f"verify read failed: {exc}",
        )

    if target_hash != source_hash:
        # Verify failed: unlink the new file, LEAVE the other slot intact (R5.6).
        _best_effort_unlink(target)
        logger.warning(
            "sole_slot_write: byte verify mismatch for %s "
            "(source sha256=%s, target sha256=%s); left other slot intact",
            target, source_hash, target_hash,
        )
        return WriteResult(
            status="failed",
            error="byte verify mismatch: written slot does not match source",
        )

    # --- Step 3: ONLY after a passing verify, remove the other slot (R5.4). ---
    removed: list[str] = []
    if os.path.isfile(other_slot):
        try:
            os.remove(other_slot)
            removed.append(other_slot)
        except OSError as exc:
            # The written slot is verified and present, so the drive carries a
            # valid current slot either way. Surface the failure but keep the
            # verified write — this is not a half-written state.
            logger.warning(
                "sole_slot_write: wrote and verified %s but could not remove "
                "other slot %s: %s", target, other_slot, exc,
            )
            return WriteResult(
                status="failed",
                written_path=target,
                removed_slots=removed,
                error=f"wrote and verified slot but failed to remove other slot: {exc}",
            )

    logger.info(
        "sole_slot_write: wrote verified sole slot %s (removed %s)",
        target, removed or "none",
    )
    return WriteResult(
        status="written",
        written_path=target,
        removed_slots=removed,
    )


def restore_version(archived_path, mount_point: str) -> WriteResult:
    """Restore a chosen archived version onto a bound drive (R5, Settings only).

    Thin wrapper over :func:`sole_slot_write` fixed to the Settings family
    (R5.9): WP and Plan are never restored. The archived file's original bytes
    are written verbatim and verified before the other slot is removed.
    """
    return sole_slot_write(archived_path, mount_point, "Settings")


def _drive_settings_hash(mount_point: str) -> Optional[str]:
    """Return the Content_Hash of the drive's current Settings slot, or None.

    Reuses the archiver's slot selection (``_current_settings_slot``) so the
    "current" slot on the drive is chosen exactly as archiving/divergence choose
    it (higher ``UPDATE=`` with the deterministic tiebreak), then hashes it with
    ``content_hash`` — the same order-independent, UPDATE/CHECKSIZE/CHECKSUM-
    excluding digest used everywhere else (R6.1/R6.2). Returns ``None`` when the
    drive carries no parseable Settings slot.
    """
    from efis_data_manager.archiver import _current_settings_slot
    from efis_data_manager.efis_settings import content_hash

    current = _current_settings_slot(mount_point, "Settings")
    if current is None:
        return None
    return content_hash(current)


def populate_latest(archive_root, link_id, mount_point: str) -> WriteResult:
    """Write the device's latest archived Settings onto a bound drive (R6).

    Looks up ``latest_version(archive_root, link_id, "Settings")`` — the most
    recent archived Settings snapshot for this Link_ID by (Archive_Date,
    sequence), never by mtime or ``UPDATE=``. Then:

    * no archived version for the device/family → ``WriteResult(
      status="skipped-none")`` (R6.4), nothing touched on the drive;
    * the drive's current Settings slot already carries content equal to the
      latest archived version (equal Content_Hash) → ``WriteResult(
      status="skipped-equal")`` (R6.1/R6.2), an idempotent no-op;
    * otherwise → :func:`sole_slot_write` of the latest archived file onto the
      drive (byte-verbatim, verify-then-sole-slot), returning its result.

    Settings only and scoped to the given Link_ID (R6.5/R6.6): WP and Plan are
    never populated, and only this device's history is consulted. Equality is a
    pure Content_Hash comparison via the shared ``content_hash`` digest, so an
    incidental ``UPDATE=`` churn never forces a rewrite.
    """
    from efis_data_manager.efis_settings import Settings_Parser, content_hash
    from efis_data_manager.settings_archive import latest_version

    latest = latest_version(archive_root, link_id, "Settings")
    if latest is None:
        return WriteResult(status="skipped-none")

    if _drive_settings_hash(mount_point) == content_hash(
        Settings_Parser.parse(str(latest.path))
    ):
        return WriteResult(status="skipped-equal")

    return sole_slot_write(latest.path, mount_point, "Settings")


def _best_effort_unlink(path: str) -> None:
    """Remove ``path`` if present, swallowing errors (cleanup after failure)."""
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Divergence predicate (Requirement 7)
# ---------------------------------------------------------------------------


@dataclass
class DivergenceResult:
    """Outcome of a drive-vs-archive divergence check (R7).

    Divergence is a pure Content_Hash mismatch between the drive's current
    Settings slot and the device's latest archived Settings version — never an
    older/newer verdict and never a function of file timestamps or ``UPDATE_Value``
    (R7.4, R7.5). The hash intentionally excludes the volatile ``UPDATE=`` /
    ``CHECKSIZE=`` / ``CHECKSUM=`` lines (see ``efis_settings.content_hash``), so a
    drive that merely re-saved identical settings with a bumped counter does not
    read as diverged.

    Attributes:
        diverged: ``True`` only when both a latest archived version and a current
            drive slot exist AND their Content_Hashes differ. Absent baseline or
            absent drive slot yields ``False`` (no warning, R7.2).
        latest_archive_date: Archive_Date of the latest archived version used for
            the comparison, or ``None`` when there was no baseline / no drive slot.
        link_id: The device Link_ID the check was scoped to, or ``None`` when the
            check short-circuited without a usable comparison.
        matched_archive_date: When diverged, the Archive_Date of the most recent
            OLDER archived Settings snapshot whose Content_Hash equals the
            card's; ``None`` if diverged with no match, or not diverged.
    """

    diverged: bool
    latest_archive_date: Optional[date] = None
    link_id: Optional[str] = None
    matched_archive_date: Optional[date] = None


def diverges(archive_root, link_id: Optional[str], mount_point: str) -> DivergenceResult:
    """Report whether a drive's Settings slot diverges from its latest archive (R7).

    Compares the Content_Hash of the drive's current Settings slot (selected by
    the archiver's :func:`archiver._current_settings_slot`) against the
    Content_Hash of the device's latest archived Settings version
    (:func:`settings_archive.latest_version`). The decision is a pure hash
    mismatch (R7.1/R7.4) and never consults file timestamps or ``UPDATE_Value``
    (R7.5).

    Short-circuits that are explicitly *not* divergence (R7.2):

        * no latest archived version for the device (no baseline), or
        * no current Settings slot on the drive (nothing to compare),

    both return ``DivergenceResult(diverged=False)``.

    Args:
        archive_root: Root of the archive tree; the device's history is read from
            ``<archive_root>/Settings/link{N}/``.
        link_id: The device's resolved Link_ID the check is scoped to; ``None``
            / empty reads the unresolved location.
        mount_point: Path to the mounted drive whose current Settings slot is
            compared against the archive.

    Returns:
        A :class:`DivergenceResult`.
    """
    # Imported lazily (mirrors archiver.py's own pattern) to keep the module's
    # import graph lean and avoid pulling the archiver/efis stack at import time.
    from efis_data_manager.archiver import _current_settings_slot
    from efis_data_manager.efis_settings import Settings_Parser, content_hash
    from efis_data_manager.settings_archive import family_history, latest_version

    latest = latest_version(archive_root, link_id, "Settings")
    if latest is None:
        # No archived baseline for this device — nothing to diverge from (R7.2).
        return DivergenceResult(diverged=False)

    drive = _current_settings_slot(mount_point, "Settings")
    if drive is None:
        # No loadable Settings slot on the drive — nothing to compare (R7.2).
        return DivergenceResult(diverged=False)

    archived = Settings_Parser.parse(str(latest.path))
    drive_hash = content_hash(drive)
    diverged = drive_hash != content_hash(archived)

    matched_archive_date: Optional[date] = None
    if diverged:
        # Look for the most recent OLDER snapshot whose Content_Hash equals the
        # card's (history is newest to oldest; skip the latest). Content_Hash
        # only — never UPDATE_Value or mtimes. Unparseable snapshots are skipped.
        for snap in family_history(archive_root, link_id, "Settings")[1:]:
            try:
                if content_hash(Settings_Parser.parse(str(snap.path))) == drive_hash:
                    matched_archive_date = snap.archive_date
                    break
            except Exception:
                logger.debug("Skipping unparseable snapshot %s", snap.path, exc_info=True)
                continue

    return DivergenceResult(
        diverged=diverged,
        latest_archive_date=latest.archive_date,
        link_id=link_id,
        matched_archive_date=matched_archive_date,
    )
