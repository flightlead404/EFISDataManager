# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Drive updater — syncs local USB image to a physical EFIS USB drive.

Uses rsync for efficient delta-copy of chart data (large, many files)
and direct file comparison for the small number of top-level files
(NAV.DB, software .dat files).
"""

import json
import logging
import os
import plistlib
import socket
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from efis_data_manager import __version__
from efis_data_manager.config import load_config

logger = logging.getLogger(__name__)

# Durable sync-state lives alongside the logs. The JSON file records which
# families are mid-sync for which mount, so an interrupted sync forces a
# verify+repair before the quick currency check is trusted again.
SYNC_STATE_DIR = os.path.expanduser("~/EFIS/DataManagerLogs")
SYNC_STATE_PATH = os.path.join(SYNC_STATE_DIR, ".sync_state.json")
# Legacy single-string marker from the old monolithic updater. Both this file
# and the v1 single-"mount" JSON object are DISCARDED (not migrated) on first
# read of the v2 code: neither can be reliably mapped to a drive id when the
# drive may be absent. A genuinely-interrupted drive is re-detected by its
# on-drive markers/payload on next mount and repaired.
LEGACY_SYNC_MARKER_PATH = os.path.join(SYNC_STATE_DIR, ".sync_in_progress")

# Current durable sync-state schema version (v2, id-keyed map — Req 10.5).
SYNC_STATE_SCHEMA_VERSION = 2


@dataclass
class JobResult:
    """Per-family result of a sync job."""

    name: str  # "scanned" | "plates" | "nav"
    status: str  # "current" | "updated" | "failed" | "aborted"
    files_updated: int = 0
    errors: list = field(default_factory=list)
    verified: bool = False


@dataclass
class SyncJob:
    """A self-describing sync job for a single product family."""

    name: str
    kind: str  # "tree" | "files"
    payload_root_local: str = ""  # for tree jobs
    payload_root_drive: str = ""
    excludes: list = field(default_factory=list)  # includes the marker filename for tree jobs
    marker_src: Optional[str] = None
    marker_dst: Optional[str] = None
    files: list = field(default_factory=list)  # for "files" jobs (nav)


# --- Sync-state helpers (v2: id-keyed map) ----------------------------------
#
# The durable sync-state is a map keyed by the drive's app-generated id (Req
# 10.5), so an arbitrary number of drives are tracked independently and a swap
# never mis-attributes one drive's interrupted-sync state to another (Property
# 6). Shape:
#
#   {
#     "schema_version": 2,
#     "drives": {
#       "<drive-id>": {
#         "families": ["scanned", "plates"],
#         "started": "<iso>",
#         "mount": "<last-seen mount, informational only>"
#       },
#       ...
#     }
#   }
#
# A family entry is removed as each job completes; a drive's entry is pruned
# once its families list is empty; the file is deleted when no drives remain.
#
# Legacy formats are DISCARDED (not migrated) on first read: the v1 single-
# "mount" object and the even older `.sync_in_progress` string cannot be
# reliably mapped to a drive id when the drive may be absent. A genuinely-
# interrupted drive is re-detected by its on-drive markers/payload on next
# mount and repaired.


def _now_iso() -> str:
    """Return the current local time as an ISO-8601 string with offset."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _remove_sync_state_file() -> None:
    """Delete the durable sync-state file, ignoring absence."""
    try:
        os.remove(SYNC_STATE_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _write_sync_state(state: dict) -> None:
    """Persist the v2 sync-state map, or remove the file when no drives remain.

    ``state`` is the full v2 dict. Any drive entry whose ``families`` list is
    empty is pruned first; if no drives remain the file is deleted entirely so
    "no interrupted sync" is represented by the absence of the file (matching
    ``read_sync_state`` returning None).
    """
    drives = state.get("drives") or {}
    # Prune empty drive entries so a completed drive leaves no residue.
    drives = {
        drive_id: entry
        for drive_id, entry in drives.items()
        if entry.get("families")
    }
    if not drives:
        _remove_sync_state_file()
        return
    state = {"schema_version": SYNC_STATE_SCHEMA_VERSION, "drives": drives}
    os.makedirs(SYNC_STATE_DIR, exist_ok=True)
    with open(SYNC_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _discard_legacy_markers() -> None:
    """Discard any legacy `.sync_in_progress` file (v2 does not migrate it)."""
    try:
        os.remove(LEGACY_SYNC_MARKER_PATH)
    except OSError:
        pass


def read_sync_state() -> Optional[dict]:
    """Return the current v2 sync-state dict, or None if no sync is in progress.

    Parses the v2 id-keyed map. Legacy formats are DISCARDED, not migrated:

      - the v1 single-``mount`` object (no ``drives`` map / wrong schema), and
      - the older ``.sync_in_progress`` string file,

    are both dropped on first read because neither can be reliably mapped to a
    drive id when the drive may be absent (Req 10.5/10.6). A corrupt or empty
    file is likewise treated as no state and removed. Any legacy
    ``.sync_in_progress`` file is removed regardless.
    """
    # The legacy string marker is never honoured under v2 — remove it if present.
    _discard_legacy_markers()

    if not os.path.exists(SYNC_STATE_PATH):
        return None

    try:
        with open(SYNC_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt state file — discard it.
        _remove_sync_state_file()
        return None

    # v2 shape check: must be a dict carrying a "drives" map. Anything else
    # (notably the v1 single-"mount" object) is a legacy/unknown format and is
    # discarded rather than migrated.
    if not isinstance(state, dict) or not isinstance(state.get("drives"), dict):
        _remove_sync_state_file()
        return None

    # Drop any empty drive entries; if nothing remains, report no state.
    drives = {
        drive_id: entry
        for drive_id, entry in state["drives"].items()
        if isinstance(entry, dict) and entry.get("families")
    }
    if not drives:
        _remove_sync_state_file()
        return None

    return {"schema_version": SYNC_STATE_SCHEMA_VERSION, "drives": drives}


def begin_family(drive_id: str, family: str, mount: Optional[str] = None) -> None:
    """Record a family as in-progress under ``drive_id``, BEFORE copying.

    Creates the drive's entry if absent and appends ``family`` (no duplicates).
    ``mount`` is stored as informational last-seen mount only; it never keys the
    state. Other drives' entries are left untouched (Property 6).
    """
    state = read_sync_state() or {
        "schema_version": SYNC_STATE_SCHEMA_VERSION,
        "drives": {},
    }
    drives = state.setdefault("drives", {})
    entry = drives.get(drive_id)
    if entry is None:
        entry = {"families": [], "started": _now_iso()}
        drives[drive_id] = entry
    if mount is not None:
        entry["mount"] = mount
    families = entry.setdefault("families", [])
    if family not in families:
        families.append(family)
    _write_sync_state(state)


def complete_family(drive_id: str, family: str) -> None:
    """Remove ``family`` from ``drive_id``'s entry.

    Prunes the drive's entry when its families list becomes empty, and deletes
    the state file entirely when no drives remain. A no-op when the drive has no
    recorded state. Other drives' entries are untouched (Property 6).
    """
    state = read_sync_state()
    if state is None:
        return
    drives = state.get("drives") or {}
    entry = drives.get(drive_id)
    if entry is None:
        return
    entry["families"] = [f for f in (entry.get("families") or []) if f != family]
    # _write_sync_state prunes the now-empty entry and removes the file if the
    # last drive completed.
    _write_sync_state(state)


def pending_families(drive_id: str) -> list:
    """Return families needing verify+repair for ``drive_id``.

    Empty when there is no interrupted sync recorded for that drive id.
    """
    state = read_sync_state()
    if state is None:
        return []
    entry = (state.get("drives") or {}).get(drive_id)
    if entry is None:
        return []
    return list(entry.get("families") or [])


# --- Drive identity (multi-drive rotation) ----------------------------------
#
# An app-owned identity file at the volume root gives each physical drive a
# stable id independent of its mount path (which macOS derives from the volume
# label and disambiguates with a history/timing-dependent numeric suffix).
# sync-state is keyed on this id (task 15) so a drive swap never mis-attributes
# one drive's interrupted-sync state to another (Req 10). The identity file is
# VISIBLE and lives at the volume root so it shares the chart data's lifecycle
# (wiping the charts wipes the identity, forcing re-adoption + resync). It never
# decides currency — the on-drive commit markers + payload remain the sole
# source of truth for "is this drive current".

IDENTITY_FILENAME = "EFIS_DRIVE_ID.json"
IDENTITY_KIND = "efis-chart-drive"
IDENTITY_SCHEMA_VERSION = 1


def _identity_path(mount_point: str) -> str:
    """Return the absolute path of the identity file at the volume root."""
    return os.path.join(mount_point, IDENTITY_FILENAME)


def _volume_uuid(mount_point: str) -> Optional[str]:
    """Return the OS VolumeUUID for a mount via ``diskutil info -plist``.

    Best-effort and tolerant of failure: any diskutil error, non-zero exit,
    unparseable plist, or missing key yields None rather than raising, so a
    diskutil hiccup never blocks identity resolution (Req 10.7).
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", mount_point],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        info = plistlib.loads(result.stdout)
    except Exception:
        return None
    return info.get("VolumeUUID") or None


def _volume_name(mount_point: str) -> Optional[str]:
    """Return the diskutil VolumeName for a mount, or None on any failure."""
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", mount_point],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        info = plistlib.loads(result.stdout)
    except Exception:
        return None
    return info.get("VolumeName") or None


def read_identity(mount_point: str) -> Optional[dict]:
    """Read and parse the identity file at the volume root.

    Returns the parsed dict, or None if the file is missing or unparseable
    (a corrupt/unreadable identity is treated as absent so a caller can adopt
    or fail safe). This never raises.
    """
    path = _identity_path(mount_point)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_identity(mount_point: str, data: dict) -> None:
    """Atomically write the identity file to the volume root.

    Writes to a temp file in the same directory, flushes + fsyncs it, then
    ``os.replace``s it over the destination so a reader never observes a
    partially-written identity (Req 10.8). A best-effort ``os.sync()`` follows
    for FAT/exFAT media.
    """
    path = _identity_path(mount_point)
    os.makedirs(mount_point, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".efis_drive_id.", dir=mount_point)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Clean up the temp file on any failure so a botched write leaves no
        # partial artifact behind.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    # Best-effort filesystem-wide flush for FAT media where fsync alone may not
    # guarantee the rename/metadata is on the device. Never fatal.
    try:
        os.sync()
    except OSError:
        pass


def update_identity_provenance(mount_point: str, **fields) -> None:
    """Merge provenance/telemetry ``fields`` into the identity file atomically.

    Reads the existing identity (if any), updates the given fields, and writes
    the result back atomically (temp + rename). The ``id`` and ``kind`` are
    never clobbered by this merge: an existing id/kind is preserved even if the
    caller passes conflicting values, so provenance updates around the
    commit-marker step (task 16) cannot rewrite a drive's identity. When no
    identity file exists this is a no-op (there is nothing to attach provenance
    to; callers resolve/adopt first).
    """
    existing = read_identity(mount_point)
    if existing is None:
        return
    merged = dict(existing)
    merged.update(fields)
    # Identity-defining fields are immutable across a provenance merge.
    merged["id"] = existing.get("id")
    merged["kind"] = existing.get("kind")
    write_identity(mount_point, merged)


def _current_data_cycle() -> Optional[str]:
    """Best-effort identifier for the data cycle the drive is being brought to.

    Prefers the GRT nav DB valid date recorded in the currency module's GRT
    metadata (``nav_db_valid_date``), which is the most reliable single "what
    cycle is this data" signal we track locally. Returns a compact string, or
    None when the metadata is unavailable/unreadable.

    Deliberately tolerant: any import or read error yields None rather than
    raising, so provenance never depends on — and is never broken by — the
    currency metadata being present (Req 10.8 provenance must never affect the
    sync path). This is a hint for a future "drive is N cycles behind" feature,
    not a currency decision.
    """
    try:
        from efis_data_manager import currency

        metadata = currency._load_grt_metadata()
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    valid_date = metadata.get("nav_db_valid_date")
    if not valid_date:
        return None
    return str(valid_date)


def _safe_update_provenance(mount_point: str, **fields) -> None:
    """Update identity provenance, swallowing (and logging) any error.

    Provenance is telemetry only: it must NEVER change currency behaviour and
    must NEVER raise into the sync path (Req 10.8). This wrapper guarantees
    that — any error from reading/merging/writing the identity file is caught
    and logged at WARNING, and the sync job proceeds unaffected.

    ``update_identity_provenance`` is already a no-op when the drive has no
    identity file (a plain temp dir, or a fail-safe mount-path key), so this is
    safe to call unconditionally; it simply does nothing when there is no
    identity to attach provenance to, and never creates one.
    """
    try:
        update_identity_provenance(mount_point, **fields)
    except Exception as e:  # pragma: no cover - defensive; must never propagate
        logger.warning(
            "Provenance update for %s failed (ignored): %s", mount_point, e
        )


def _merged_sync_families(mount_point: str, family: str) -> list:
    """Return existing ``last_sync_families`` plus ``family`` (deduped, ordered).

    Accumulates the families synced across a whole update run: each family job
    updates provenance independently, so we merge the current identity's
    recorded families with the one just completed rather than overwriting.
    Tolerant of a missing/unreadable identity (returns just ``[family]``).
    """
    existing = read_identity(mount_point) or {}
    families = list(existing.get("last_sync_families") or [])
    if family not in families:
        families.append(family)
    return families


def _bumped_sync_count(mount_point: str) -> int:
    """Return the identity's ``sync_count`` incremented by one.

    Semantics (documented, chosen for simplicity + correctness): ``sync_count``
    is incremented once per successful family sync. A full update that refreshes
    three stale families therefore advances the count by three. This counts
    "verified family syncs" rather than "user-initiated updates"; a later
    feature that wants the latter can derive it, but per-family counting needs
    no cross-job coordination and never double-counts a family that no-ops
    (an already-current family still completes cleanly and counts as a verified
    sync). Tolerant of a missing/unreadable identity (starts from 0).
    """
    existing = read_identity(mount_point) or {}
    try:
        current = int(existing.get("sync_count") or 0)
    except (TypeError, ValueError):
        current = 0
    return current + 1


def _new_identity(mount_point: str) -> dict:
    """Build a fresh identity dict for a drive at ``mount_point``.

    Captures the OS VolumeUUID and label as cross-check fields, an app-generated
    uuid4 as the durable ``id`` key, and initialises all provenance/telemetry
    fields (Req 10.1, 10.8). ``label`` prefers the mount's basename and falls
    back to the diskutil VolumeName.
    """
    label = os.path.basename(mount_point.rstrip(os.sep)) or _volume_name(mount_point)
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "kind": IDENTITY_KIND,
        "volume_uuid": _volume_uuid(mount_point),
        "label": label,
        "created": _now_iso(),
        "prepared_by": f"EFISDataManager {__version__} on {socket.gethostname()}",
        "last_sync_started": None,
        "last_sync_completed": None,
        "last_sync_result": None,
        "last_sync_families": [],
        "sync_count": 0,
        "data_cycle": None,
    }


def resolve_drive_id(mount_point: str) -> Optional[str]:
    """Resolve (or lazily adopt) the durable drive id for a mounted volume.

    Resolution order (design.md "Resolution + adoption"):
      1. Wait for the mount to be fully ready (mount-readiness race).
      2. If a valid identity file exists (parses with ``kind ==
         "efis-chart-drive"``), return its ``id``. Opportunistically capture the
         OS VolumeUUID and WARN if it differs from the stored ``volume_uuid``
         (a cloned drive or copied identity file) — the app-generated id still
         wins; the mismatch never fails resolution (Req 10.9).
      3. Else, if the volume is a recognized EFIS drive, ADOPT it: generate a
         uuid4, write a fresh identity file, and return the new id (Req 10.4).
      4. Else return None — not one of ours / unreadable — so callers fail safe
         and never apply another drive's state to this drive (Req 10.7).

    Returns:
        The drive's durable id string, or None when the drive cannot be
        resolved (never raises for a diskutil/read error).
    """
    # Import here to avoid a module-level import cycle (usb_monitor is a leaf).
    from efis_data_manager.usb_monitor import is_efis_drive

    # Step 1: mount-readiness race.
    if not wait_for_mount_ready(mount_point):
        logger.warning(
            "resolve_drive_id: mount %s not ready; failing safe (no id)",
            mount_point,
        )
        return None

    # Step 2: existing, valid identity file.
    identity = read_identity(mount_point)
    if identity is not None and identity.get("kind") == IDENTITY_KIND:
        stored_uuid = identity.get("volume_uuid")
        observed_uuid = _volume_uuid(mount_point)
        if (
            stored_uuid is not None
            and observed_uuid is not None
            and stored_uuid != observed_uuid
        ):
            logger.warning(
                "EFIS drive identity VolumeUUID mismatch at %s "
                "(stored=%s, observed=%s); using app id %s",
                mount_point,
                stored_uuid,
                observed_uuid,
                identity.get("id"),
            )
        drive_id = identity.get("id")
        if drive_id:
            return drive_id
        # Identity present but missing an id — fall through to adopt/None.

    # Step 3: recognized-but-unmarked drive -> lazy adoption.
    try:
        recognized = is_efis_drive(mount_point)
    except OSError:
        recognized = False
    if recognized:
        data = _new_identity(mount_point)
        try:
            write_identity(mount_point, data)
        except OSError as e:
            logger.warning(
                "resolve_drive_id: could not adopt %s (%s); failing safe",
                mount_point,
                e,
            )
            return None
        logger.info(
            "Adopted EFIS drive at %s with new id %s", mount_point, data["id"]
        )
        return data["id"]

    # Step 4: not ours / unreadable -> fail safe.
    logger.warning(
        "resolve_drive_id: %s is not a recognized EFIS drive and has no valid "
        "identity file; failing safe (no id)",
        mount_point,
    )
    return None


def _ensure_identity(mount_point: str) -> Optional[str]:
    """Ensure a freshly-prepared drive has a durable identity file (Req 10.3).

    Writes a fresh identity file at the volume root so a prepared drive carries
    a durable id from the very start, before the populate step runs. If an
    identity file already exists (e.g. a re-prepare), it is left in place and
    its id returned so the drive keeps its established identity.

    Best-effort by design: a failure to write the identity does NOT fail drive
    preparation. The drive is still fully usable — ``resolve_drive_id`` will
    lazily adopt it on the next operation. Any error is logged at WARNING and
    swallowed, returning None.

    Returns:
        The drive's durable id string, or None if the identity could not be
        written (in which case preparation should continue regardless).
    """
    existing = read_identity(mount_point)
    if existing is not None and existing.get("id"):
        return existing.get("id")
    data = _new_identity(mount_point)
    try:
        write_identity(mount_point, data)
    except Exception as e:  # pragma: no cover - defensive; must never fail prepare
        logger.warning(
            "Could not write identity file to %s (ignored; drive still usable, "
            "will be adopted lazily): %s",
            mount_point,
            e,
        )
        return None
    logger.info(
        "Wrote fresh identity to prepared drive at %s (id %s)",
        mount_point,
        data["id"],
    )
    return data["id"]


# Files/dirs in the local USB image that should be synced to the drive.
# Anything on the drive NOT in this list is left untouched (.bak, System Volume Information, etc.)
SYNC_ITEMS = [
    "ChartData",
    "GRTCHARTS",
    "NAV.DB",
    "NAV-proc.DB",
]

# Excludes applied to every tree (rsync) job. These filter macOS metadata and
# a stray legacy directory that must never be pushed to the drive.
COMMON_TREE_EXCLUDES = [
    ".DS_Store",
    "._*",
    "E:ChartData",
]

# macOS metadata patterns that must be actively PURGED from the drive, not just
# skipped on copy. rsync's default --delete protects excluded files, so these
# accumulated on the drive over time (AppleDouble "._*" sidecars especially).
# sync_payload feeds these to --delete-excluded so they are removed from the
# receiver, while every other exclude (structural: the sibling family's subtree,
# the legacy E:ChartData dir, the commit marker) is PROTECTED from deletion.
METADATA_EXCLUDES = [
    ".DS_Store",
    "._*",
]

# Per-family commit markers (relative to the mount / local image root). The
# marker is written last in a job and is always excluded from the payload copy.
SCANNED_MARKER = os.path.join("ChartData", "ScannedCharts.sqlite")
PLATES_MARKER = os.path.join("ChartData", "Plates", "Plates.sqlite")

# Standalone nav files handled by the checksum-based "files" job.
NAV_FILES = ["NAV.DB", "NAV-proc.DB"]


# --- Job definitions --------------------------------------------------------


def build_jobs(mount_point: str, families: Optional[list] = None) -> list:
    """Build the per-family SyncJob definitions for the requested families.

    Three families are defined (see design.md "Product families"):

      - scanned: payload ``ChartData/`` minus ``Plates/`` and minus the
        ``ScannedCharts.sqlite`` marker. Commit marker
        ``ChartData/ScannedCharts.sqlite``.
      - plates: payload ``ChartData/Plates/`` minus the ``Plates.sqlite``
        marker. Commit marker ``ChartData/Plates/Plates.sqlite``.
      - nav: standalone files ``NAV.DB`` and ``NAV-proc.DB``; no directory
        marker (each file is verified by checksum).

    Payload roots for tree jobs use a trailing separator so rsync treats them
    as "contents of" the directory. The scanned job excludes ``Plates/`` so the
    scanned and plates families own disjoint slices of the ``ChartData`` tree.

    Args:
        mount_point: Path to the mounted EFIS drive (e.g. ``/Volumes/EFIS``).
        families: Optional subset of ``["scanned", "plates", "nav"]``. When
            None (default), all three jobs are returned in a stable order.

    Returns:
        List of :class:`SyncJob` in the order scanned, plates, nav (filtered to
        the requested families).
    """
    config = load_config()
    local_root = config["usb_image_path"]

    requested = ["scanned", "plates", "nav"] if families is None else list(families)

    def _tree_root(*parts: str) -> str:
        # Trailing separator => rsync copies the directory contents.
        return os.path.join(*parts) + os.sep

    builders = {
        "scanned": lambda: SyncJob(
            name="scanned",
            kind="tree",
            payload_root_local=_tree_root(local_root, "ChartData"),
            payload_root_drive=_tree_root(mount_point, "ChartData"),
            # Exclude the scanned marker and the entire Plates/ subtree (owned
            # by the plates family), plus the common metadata excludes.
            excludes=list(COMMON_TREE_EXCLUDES) + ["Plates/", "ScannedCharts.sqlite"],
            marker_src=os.path.join(local_root, SCANNED_MARKER),
            marker_dst=os.path.join(mount_point, SCANNED_MARKER),
        ),
        "plates": lambda: SyncJob(
            name="plates",
            kind="tree",
            payload_root_local=_tree_root(local_root, "ChartData", "Plates"),
            payload_root_drive=_tree_root(mount_point, "ChartData", "Plates"),
            excludes=list(COMMON_TREE_EXCLUDES) + ["Plates.sqlite"],
            marker_src=os.path.join(local_root, PLATES_MARKER),
            marker_dst=os.path.join(mount_point, PLATES_MARKER),
        ),
        "nav": lambda: SyncJob(
            name="nav",
            kind="files",
            files=list(NAV_FILES),
        ),
    }

    return [builders[name]() for name in requested if name in builders]


# --- Tree-job payload sync --------------------------------------------------


def sync_payload(job: SyncJob, is_aborted: Optional[Callable[[], bool]] = None):
    """Sync a tree job's payload to the drive via rsync (size+mtime delta).

    Implements design.md "Per-job sequence (tree job)" step 3: an efficient
    delta copy using size + modification time (NOT a full-content checksum) so
    a routine sync converges quickly instead of running for an hour.

    rsync flags:
      - ``-r``               recurse into the tree.
      - ``--delete``         remove drive files no longer present locally so the
                             payload converges exactly to the source.
      - ``--size-only``      compare by size only; combined with...
      - ``--modify-window=2`` ...a 2-second mtime tolerance to absorb FAT/exFAT
                             timestamp granularity (Req 4.1).
      - one ``--exclude`` per entry in ``job.excludes`` (common metadata excludes
        plus the family's commit marker, which is written separately as the
        final atomic step).

    There is deliberately NO fixed wall-clock timeout: a large-but-healthy
    transfer must not be failed as an error (Req 4.2). Liveness/abort is handled
    out-of-band via the optional ``is_aborted`` predicate (wired to the mount
    watchdog in a later task); if it returns True the running rsync is
    terminated and the abort is recorded as an error.

    rsync failures (non-zero exit) are logged at ERROR and their stderr is
    captured (truncated) into the returned errors list so the Recent Errors
    panel mirrors the status line (Req 8.1).

    Args:
        job: A ``kind == "tree"`` :class:`SyncJob` with payload roots + excludes.
        is_aborted: Optional predicate polled while rsync runs; when it returns
            True the transfer is terminated and reported as aborted.

    Returns:
        Tuple ``(files_updated, errors)`` where ``files_updated`` is the count of
        files rsync reported transferring and ``errors`` is a list of strings
        (empty on success).
    """
    if job.kind != "tree":
        raise ValueError(f"sync_payload expects a tree job, got kind={job.kind!r}")

    errors: list = []

    # Two-tier exclude handling (see METADATA_EXCLUDES). rsync's default
    # --delete PROTECTS excluded files from deletion, which is why macOS "._*"
    # sidecars piled up on the drive. We split job.excludes into:
    #   - metadata patterns -> plain --exclude, and we add --delete-excluded so
    #     any that already exist on the drive are purged; and
    #   - structural patterns (the sibling family's subtree, the legacy
    #     E:ChartData dir, the commit marker) -> a PROTECT filter rule ("P ...")
    #     so --delete-excluded does NOT delete them from the receiver.
    # Filter rules are evaluated in order; protect rules must precede the
    # excludes so a structural path is shielded before the exclude/delete pass.
    metadata = set(METADATA_EXCLUDES)
    structural = [p for p in job.excludes if p not in metadata]
    metadata_used = [p for p in job.excludes if p in metadata]

    cmd = ["rsync", "-r", "--delete", "--delete-excluded",
           "--size-only", "--modify-window=2", "--out-format=%n"]
    # Protect structural excludes from --delete-excluded.
    for pattern in structural:
        cmd += ["--filter", f"P {pattern}"]
    # Exclude structural paths from the transfer (don't send them either).
    for pattern in structural:
        cmd += ["--exclude", pattern]
    # Purge metadata from the receiver and never send it.
    for pattern in metadata_used:
        cmd += ["--exclude", pattern]
    cmd += [job.payload_root_local, job.payload_root_drive]

    env = dict(os.environ)
    env["COPYFILE_DISABLE"] = "1"

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
    except OSError as e:
        msg = f"rsync {job.name} could not start: {e}"
        errors.append(msg)
        logger.error(msg)
        return 0, errors

    # Poll for completion so an out-of-band abort (mount removed / sleep) can
    # terminate the transfer. No wall-clock deadline is imposed.
    aborted = False
    while proc.poll() is None:
        if is_aborted is not None and is_aborted():
            aborted = True
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue

    stdout, stderr = proc.communicate()

    if aborted:
        msg = f"{job.name} sync aborted (drive removed or system sleeping)"
        errors.append(msg)
        logger.error(msg)
        return 0, errors

    # Count transferred files from --out-format lines. rsync emits one line per
    # transferred item; directory entries end in a separator and are ignored.
    files_updated = 0
    if stdout:
        for line in stdout.splitlines():
            name = line.strip()
            if name and not name.endswith("/"):
                files_updated += 1

    if proc.returncode != 0:
        detail = (stderr or "").strip()[:200]
        msg = f"rsync {job.name} failed (exit {proc.returncode}): {detail}"
        errors.append(msg)
        logger.error("rsync %s failed (exit %s): %s",
                     job.name, proc.returncode, (stderr or "")[:500])

    return files_updated, errors


# --- Payload verification (count + size) ------------------------------------
#
# verify_family walks the local and drive family trees (excluding the commit
# marker and any configured excludes), builds {relpath: size} maps for both
# sides, and compares to produce discrepancies. Count + size is the default
# depth (deep=False); deep=True adds a content hash comparison (opt-in).


class _ExcludeMatcher:
    """Matches a basename against rsync-style exclude patterns.

    ``verify_family`` must skip exactly what the rsync payload copy skips, so
    both the count+size comparison and the copy agree on the file set. That
    includes glob patterns like ``._*`` (macOS AppleDouble sidecars) — dropping
    them (as an earlier version did) made verify count tens of thousands of
    ``._*`` files as spurious "extra" on the drive while rsync correctly ignored
    them. Patterns are matched by basename via :func:`fnmatch.fnmatch`, which
    covers both literal names (``Plates``, ``ScannedCharts.sqlite``) and globs
    (``._*``). A trailing path separator is stripped so ``Plates/`` matches the
    ``Plates`` directory name.
    """

    def __init__(self, excludes=None):
        self._patterns = set()
        for pat in excludes or []:
            self.add(pat)

    def add(self, pattern: str) -> None:
        token = (pattern or "").rstrip("/\\")
        if token:
            self._patterns.add(token)

    def matches(self, name: str) -> bool:
        import fnmatch

        return any(fnmatch.fnmatch(name, pat) for pat in self._patterns)


def _iter_tree_relpaths(root: str, excludes: "_ExcludeMatcher"):
    """Yield (relpath, abspath) for every file under ``root``.

    ``excludes`` is an :class:`_ExcludeMatcher`. A directory whose name matches
    is pruned entirely (os.walk does not descend into it); a file whose name
    matches is skipped. Matching is by basename against rsync-style patterns
    (literals and globs), mirroring the rsync payload copy so verify and copy
    agree on the file set.
    """
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place so os.walk does not descend.
        dirnames[:] = [d for d in dirnames if not excludes.matches(d)]
        for fname in filenames:
            if excludes.matches(fname):
                continue
            abspath = os.path.join(dirpath, fname)
            relpath = os.path.relpath(abspath, root)
            yield relpath, abspath


def _normalize_excludes(excludes) -> "_ExcludeMatcher":
    """Build an :class:`_ExcludeMatcher` from rsync-style exclude patterns.

    Both literal names (``Plates/``, ``Plates.sqlite``, ``.DS_Store``,
    ``E:ChartData``) and glob patterns (``._*``) are honored so verify skips
    exactly what the rsync payload copy skips.
    """
    return _ExcludeMatcher(excludes)


def _file_hash(path: str, chunk: int = 1024 * 1024) -> str:
    """Return a hex SHA-256 digest of a file's contents (for deep verify)."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _build_size_map(root: str, exclude_names: set) -> dict:
    """Build a ``{relpath: size}`` map for a family tree rooted at ``root``."""
    size_map = {}
    for relpath, abspath in _iter_tree_relpaths(root, exclude_names):
        try:
            size_map[relpath] = os.path.getsize(abspath)
        except OSError:
            # Unreadable/vanished entry — treat as absent on this side.
            continue
    return size_map


def _compare_maps(
    local_map: dict,
    drive_map: dict,
    deep: bool = False,
    local_root: str = "",
    drive_root: str = "",
) -> dict:
    """Compare two ``{relpath: size}`` maps into a discrepancy dict.

    Returns sorted ``missing`` (local only), ``extra`` (drive only), and
    ``size_mismatch`` (present both sides, size differs). With ``deep=True``,
    same-size files are additionally content-hashed and any hash difference is
    surfaced as a ``size_mismatch`` (the "changed" bucket we expose).
    """
    local_keys = set(local_map)
    drive_keys = set(drive_map)

    missing = local_keys - drive_keys  # present locally, absent on drive
    extra = drive_keys - local_keys  # present on drive, absent locally
    size_mismatch = {
        rel for rel in (local_keys & drive_keys) if local_map[rel] != drive_map[rel]
    }

    if deep:
        for rel in local_keys & drive_keys:
            if rel in size_mismatch:
                continue
            try:
                if _file_hash(os.path.join(local_root, rel)) != _file_hash(
                    os.path.join(drive_root, rel)
                ):
                    size_mismatch.add(rel)
            except OSError:
                size_mismatch.add(rel)

    return {
        "missing": sorted(missing),
        "extra": sorted(extra),
        "size_mismatch": sorted(size_mismatch),
    }


def _verify_files_job(job: SyncJob, mount_point: str, deep: bool = False) -> dict:
    """Verify a ``files`` (nav) job: compare each listed file by size.

    Files absent on both sides are ignored (a family may legitimately lack an
    optional file such as NAV-proc.DB). Otherwise the same missing/extra/
    size_mismatch semantics apply, keyed by the file's name.
    """
    config = load_config()
    local_root = config["usb_image_path"]

    local_map = {}
    drive_map = {}
    for name in job.files:
        local_path = os.path.join(local_root, name)
        drive_path = os.path.join(mount_point, name)
        if os.path.isfile(local_path):
            try:
                local_map[name] = os.path.getsize(local_path)
            except OSError:
                pass
        if os.path.isfile(drive_path):
            try:
                drive_map[name] = os.path.getsize(drive_path)
            except OSError:
                pass

    return _compare_maps(
        local_map, drive_map, deep=deep, local_root=local_root, drive_root=mount_point
    )


def verify_family(job: SyncJob, mount_point: str, deep: bool = False) -> dict:
    """Verify a family's drive payload against the local image by count + size.

    Walks the local and drive family trees (excluding the commit marker and the
    job's configured excludes), builds ``{relpath: size}`` maps for both sides,
    and compares them. For ``tree`` jobs the roots are
    ``job.payload_root_local`` / ``job.payload_root_drive``; for the ``files``
    (nav) job each entry in ``job.files`` is compared directly against its
    counterpart under the local image root and the mount.

    Args:
        job: The :class:`SyncJob` describing the family to verify.
        mount_point: Path to the mounted EFIS drive (used by the nav job to
            resolve drive-side file paths; tree jobs already carry absolute
            drive roots).
        deep: When True, add content-hash comparison for files whose size
            matches, surfacing same-size content differences as size_mismatch
            entries. Default False = count + size only (Req 5.4).

    Returns:
        Dict of the shape::

            {"missing": [relpath, ...],       # present locally, absent on drive
             "extra": [relpath, ...],          # present on drive, absent locally
             "size_mismatch": [relpath, ...]}  # present both sides, size differs

        Lists are sorted for stable output.
    """
    if job.kind == "files":
        return _verify_files_job(job, mount_point, deep=deep)

    exclude_names = _normalize_excludes(job.excludes)
    # The commit marker is always excluded from the payload comparison. Its
    # basename is added defensively (tree jobs already list it in excludes).
    if job.marker_src:
        exclude_names.add(os.path.basename(job.marker_src))

    local_map = _build_size_map(job.payload_root_local, exclude_names)
    drive_map = _build_size_map(job.payload_root_drive, exclude_names)

    return _compare_maps(
        local_map,
        drive_map,
        deep=deep,
        local_root=job.payload_root_local,
        drive_root=job.payload_root_drive,
    )


# --- Commit-marker write + per-job driver -----------------------------------
#
# run_sync_job implements the design.md "Per-job sequence". The commit marker
# is the crux of the trust model: it is copied and flushed ONLY after the
# payload has been synced and verified with no discrepancies and no errors, so
# an interrupted or failed job never advertises the family as current.


def _copy_and_fsync(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` and flush the destination to persistent storage.

    Writes byte-for-byte, then ``os.fsync`` on the destination fd and a
    best-effort ``os.sync()`` so a FAT/exFAT drive has the data on media before
    the job is declared complete (Req 2.4). Parent directories are created.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            block = fsrc.read(1024 * 1024)
            if not block:
                break
            fdst.write(block)
        fdst.flush()
        os.fsync(fdst.fileno())
    # Best-effort filesystem-wide sync for FAT media where fsync alone may not
    # guarantee metadata is on the device. Never fatal.
    try:
        os.sync()
    except OSError:
        pass


# How long preflight waits for a freshly-attached volume to become fully
# mounted + writable before giving up. Right after macOS surfaces a volume in
# /Volumes the mount can briefly report not-a-mount or deny access while it is
# still settling; acting instantly caused spurious "Permission denied" /
# false-abort on re-mount. We poll for readiness up to this budget.
MOUNT_READY_TIMEOUT = 8.0
MOUNT_READY_POLL = 0.5


def wait_for_mount_ready(
    mount_point: str,
    timeout: float = MOUNT_READY_TIMEOUT,
    poll: float = MOUNT_READY_POLL,
    sleep=None,
) -> bool:
    """Wait until ``mount_point`` is a real, writable, listable mount.

    Returns True once the volume is fully attached (it is a mount point, is
    writable, and its root can be listed without error), or False if the budget
    elapses first. This debounces the mount-readiness race a re-attached drive
    exhibits: for the first fraction of a second the mount can exist in
    /Volumes yet deny access. ``sleep`` is injectable for tests.
    """
    import time as _time

    _sleep = sleep if sleep is not None else _time.sleep
    deadline = _time.monotonic() + max(0.0, timeout)
    while True:
        ready = False
        try:
            if (
                os.path.isdir(mount_point)
                and os.path.ismount(mount_point)
                and os.access(mount_point, os.W_OK)
            ):
                # A settling volume can pass the checks above yet still raise
                # on the first real access; confirm the root is listable.
                os.listdir(mount_point)
                ready = True
        except OSError:
            ready = False
        if ready:
            return True
        if _time.monotonic() >= deadline:
            return False
        _sleep(poll)


def _preflight(job: SyncJob, mount_point: str) -> Optional[str]:
    """Confirm the mount is present + writable and has room for the family.

    Returns an error string when preflight fails, or None when the job may
    proceed. Free space is compared against the local family size so an
    obviously-too-small drive fails fast before any partial copy (design.md
    "Per-job sequence" step 1; Error Handling "Insufficient free space").

    A freshly re-attached volume may still be settling, so we first wait
    briefly for it to become fully mounted + writable rather than failing on
    the first transient (the mount-readiness race).
    """
    if not wait_for_mount_ready(mount_point):
        if not os.path.isdir(mount_point) or not os.path.ismount(mount_point):
            return f"{job.name}: drive mount {mount_point} is not present"
        return f"{job.name}: drive mount {mount_point} is not writable"

    # Compute the local family size (payload + marker for tree jobs; the listed
    # files for nav). If we cannot stat free space, skip the space check rather
    # than block a healthy sync.
    try:
        needed = _local_family_size(job)
    except OSError:
        needed = 0

    if needed:
        try:
            st = os.statvfs(mount_point)
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = None
        if free is not None and free < needed:
            return (
                f"{job.name}: insufficient free space on drive "
                f"(need {needed} bytes, have {free})"
            )
    return None


def _local_family_size(job: SyncJob) -> int:
    """Total size in bytes of a job's local payload (+ marker for tree jobs)."""
    total = 0
    if job.kind == "tree":
        exclude_names = _normalize_excludes(job.excludes)
        if job.marker_src:
            exclude_names.add(os.path.basename(job.marker_src))
        for _rel, abspath in _iter_tree_relpaths(job.payload_root_local, exclude_names):
            try:
                total += os.path.getsize(abspath)
            except OSError:
                continue
        if job.marker_src and os.path.isfile(job.marker_src):
            total += os.path.getsize(job.marker_src)
    else:  # files (nav)
        config = load_config()
        local_root = config["usb_image_path"]
        for name in job.files:
            local_path = os.path.join(local_root, name)
            if os.path.isfile(local_path):
                try:
                    total += os.path.getsize(local_path)
                except OSError:
                    continue
    return total


def _has_discrepancies(disc: dict) -> bool:
    """True if a verify_family discrepancy dict reports any difference."""
    return bool(disc.get("missing") or disc.get("extra") or disc.get("size_mismatch"))


def _run_tree_job(job, mount_point, drive_id, sync_state, progress_callback, is_aborted):
    """Driver for a tree job (scanned / plates): sync -> verify -> marker.

    Implements design.md "Per-job sequence (tree job)" steps 2-7. The interrupted
    marker is recorded (begin_family) BEFORE any payload copy; the commit marker
    is copied + flushed ONLY when the payload verifies clean with no errors; and
    complete_family (clearing the interrupted marker) happens ONLY on that
    success. Any error/abort/mismatch leaves the interrupted marker in place.

    ``drive_id`` is the durable sync-state key (resolved once by
    :func:`run_sync_job`); ``mount_point`` is recorded as informational.
    """
    result = JobResult(name=job.name, status="failed")

    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    # Step 2: record the interrupted-sync marker BEFORE copying payload.
    sync_state.begin_family(drive_id, job.name, mount=mount_point)
    # Provenance (telemetry only): stamp job-begin. No-op without an identity
    # file, and wrapped so it never raises into the sync path (Req 10.8).
    _safe_update_provenance(mount_point, last_sync_started=_now_iso())

    # Step 3: payload sync (size+mtime delta, marker excluded).
    _status(f"Syncing {job.name}...")
    files_updated, errors = sync_payload(job, is_aborted=is_aborted)
    result.files_updated = files_updated
    if errors:
        result.errors.extend(errors)
        # sync_payload logs at ERROR already. Distinguish abort from failure so
        # the status line can say "removed during update" vs "failed".
        result.status = "aborted" if any("abort" in e.lower() for e in errors) else "failed"
        # Record the outcome in provenance without bumping sync_count or setting
        # a completion time (a partial sync must not advertise false progress).
        _safe_update_provenance(mount_point, last_sync_result=result.status)
        # Do NOT write the marker; do NOT complete_family. Interrupted marker
        # stays so the next quick check forces verify+repair.
        return result

    # An abort may be signalled with a clean rsync exit if it landed between
    # polls; honour the predicate explicitly before verifying/committing.
    if is_aborted is not None and is_aborted():
        msg = f"{job.name} sync aborted (drive removed or system sleeping)"
        result.errors.append(msg)
        logger.error(msg)
        result.status = "aborted"
        _safe_update_provenance(mount_point, last_sync_result="aborted")
        return result

    # Step 4: verify payload (count + size). Any mismatch => failed, no marker.
    disc = verify_family(job, mount_point, deep=False)
    if _has_discrepancies(disc):
        msg = (
            f"{job.name} verification failed: "
            f"{len(disc['missing'])} missing, {len(disc['extra'])} extra, "
            f"{len(disc['size_mismatch'])} size-mismatch"
        )
        result.errors.append(msg)
        logger.warning(msg)
        result.status = "failed"
        _safe_update_provenance(mount_point, last_sync_result="failed")
        return result

    # Step 5: write the commit marker (copy + fsync) — the atomic commit point.
    if job.marker_src and os.path.isfile(job.marker_src):
        try:
            _copy_and_fsync(job.marker_src, job.marker_dst)
        except OSError as e:
            msg = f"{job.name} failed writing commit marker: {e}"
            result.errors.append(msg)
            logger.error(msg)
            result.status = "failed"
            _safe_update_provenance(mount_point, last_sync_result="failed")
            return result

    # Step 6: clear the interrupted marker for this family (success only).
    sync_state.complete_family(drive_id, job.name)

    # Step 6b: record a clean, verified pass in provenance (around the commit
    # marker step, Req 10.8). sync_count is bumped once per successful family;
    # last_sync_families accumulates every family synced in this run.
    _safe_update_provenance(
        mount_point,
        last_sync_completed=_now_iso(),
        last_sync_result="clean",
        sync_count=_bumped_sync_count(mount_point),
        last_sync_families=_merged_sync_families(mount_point, job.name),
        data_cycle=_current_data_cycle(),
    )

    # Step 7: success.
    result.verified = True
    result.status = "updated" if files_updated else "current"
    _status(f"{job.name} sync complete.")
    return result


def _run_files_job(job, mount_point, drive_id, sync_state, progress_callback, is_aborted):
    """Driver for the nav (files) job: per-file checksum copy + verify.

    NAV files have no separate marker — the file IS the marker (Req 1.5). Each
    changed file is copied and flushed, then the family is verified by
    size/checksum. On any error the interrupted marker is left in place and
    complete_family is NOT called.

    ``drive_id`` is the durable sync-state key (resolved once by
    :func:`run_sync_job`); ``mount_point`` is recorded as informational.
    """
    result = JobResult(name=job.name, status="failed")

    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    config = load_config()
    local_root = config["usb_image_path"]

    sync_state.begin_family(drive_id, job.name, mount=mount_point)
    # Provenance (telemetry only): stamp job-begin. No-op without an identity
    # file, and wrapped so it never raises into the sync path (Req 10.8).
    _safe_update_provenance(mount_point, last_sync_started=_now_iso())
    _status(f"Syncing {job.name}...")

    files_updated = 0
    for name in job.files:
        if is_aborted is not None and is_aborted():
            msg = f"{job.name} sync aborted (drive removed or system sleeping)"
            result.errors.append(msg)
            logger.error(msg)
            result.status = "aborted"
            _safe_update_provenance(mount_point, last_sync_result="aborted")
            return result

        local_path = os.path.join(local_root, name)
        drive_path = os.path.join(mount_point, name)
        if not os.path.isfile(local_path):
            # Optional file (e.g. NAV-proc.DB) absent locally — nothing to sync.
            continue

        # Fast path: identical size + matching checksum => already current.
        needs_copy = True
        if os.path.isfile(drive_path):
            try:
                same_size = os.path.getsize(local_path) == os.path.getsize(drive_path)
            except OSError:
                same_size = False
            if same_size and _file_checksum(local_path) == _file_checksum(drive_path):
                needs_copy = False

        if needs_copy:
            try:
                _copy_and_fsync(local_path, drive_path)
                files_updated += 1
            except OSError as e:
                msg = f"{job.name} failed copying {name}: {e}"
                result.errors.append(msg)
                logger.error(msg)
                result.status = "failed"
                _safe_update_provenance(mount_point, last_sync_result="failed")
                return result

    result.files_updated = files_updated

    # Verify by checksum (deep) — the files are the marker, so content must match.
    disc = verify_family(job, mount_point, deep=True)
    if _has_discrepancies(disc):
        msg = (
            f"{job.name} verification failed: "
            f"{len(disc['missing'])} missing, {len(disc['extra'])} extra, "
            f"{len(disc['size_mismatch'])} changed"
        )
        result.errors.append(msg)
        logger.error(msg)
        result.status = "failed"
        _safe_update_provenance(mount_point, last_sync_result="failed")
        return result

    sync_state.complete_family(drive_id, job.name)
    # Record a clean, verified pass in provenance around the commit step
    # (Req 10.8). sync_count bumps once per successful family; last_sync_families
    # accumulates the families synced in this run.
    _safe_update_provenance(
        mount_point,
        last_sync_completed=_now_iso(),
        last_sync_result="clean",
        sync_count=_bumped_sync_count(mount_point),
        last_sync_families=_merged_sync_families(mount_point, job.name),
        data_cycle=_current_data_cycle(),
    )
    result.verified = True
    result.status = "updated" if files_updated else "current"
    _status(f"{job.name} sync complete.")
    return result


def run_sync_job(
    job: SyncJob,
    mount_point: str,
    sync_state=None,
    progress_callback: Optional[Callable] = None,
    is_aborted: Optional[Callable[[], bool]] = None,
) -> JobResult:
    """Run a single family's sync job end-to-end and return its result.

    Implements the commit-marker sequence from design.md "Per-job sequence".
    The overarching invariant (Correctness Property 1): the family's commit
    marker is written ONLY after the payload is synced and verified clean with
    no errors, and the interrupted-sync marker is cleared (complete_family) ONLY
    on that success. Any error, abort, or verification mismatch leaves the
    interrupted marker in place, does NOT write the commit marker, and does NOT
    complete the family — so the next quick currency check reports the family
    stale and forces verify+repair (Property 2). Because each job owns a disjoint
    slice, a failure in one family never touches another's marker (Property 4).

    Args:
        job: The :class:`SyncJob` to run (``kind`` "tree" or "files").
        mount_point: Path to the mounted EFIS drive.
        sync_state: Module (or object) providing ``begin_family`` /
            ``complete_family``. Defaults to this module so production callers
            use the durable JSON state; tests may pass a stand-in.
        progress_callback: Optional ``callable(message)`` for status updates.
        is_aborted: Optional predicate polled during the transfer; when it
            returns True the job aborts (leaving the interrupted marker).

    Returns:
        A :class:`JobResult` with ``status`` in
        ``{"current", "updated", "failed", "aborted"}``, ``files_updated``,
        ``errors`` (also logged at >= WARNING), and ``verified``.
    """
    import sys as _sys

    if sync_state is None:
        sync_state = _sys.modules[__name__]

    # Step 1: preflight — mount present + writable + enough free space.
    preflight_err = _preflight(job, mount_point)
    if preflight_err is not None:
        logger.error(preflight_err)
        return JobResult(name=job.name, status="failed", errors=[preflight_err])

    # Resolve the durable sync-state key ONCE for this job (task 15). The full
    # threading of resolve_drive_id through currency/app is task 16; here we keep
    # run_sync_job working end-to-end by resolving locally and, when the id
    # cannot be resolved (fail-safe None — e.g. a temp dir or a non-EFIS/
    # unreadable volume), falling back to the mount path as the key so a drive
    # with no identity still gets tracked (keyed by mount) and behaviour degrades
    # safely. The fallback is logged once at WARNING (Req 10.7).
    drive_id = resolve_drive_id(mount_point)
    if drive_id is None:
        drive_id = mount_point
        logger.warning(
            "run_sync_job: could not resolve a drive id for %s; keying sync-state "
            "by mount path as a fail-safe fallback",
            mount_point,
        )

    if job.kind == "tree":
        return _run_tree_job(
            job, mount_point, drive_id, sync_state, progress_callback, is_aborted
        )
    if job.kind == "files":
        return _run_files_job(
            job, mount_point, drive_id, sync_state, progress_callback, is_aborted
        )

    msg = f"{job.name}: unknown job kind {job.kind!r}"
    logger.error(msg)
    return JobResult(name=job.name, status="failed", errors=[msg])


# --- Exhaustive verification + repair ---------------------------------------
#
# verify_drive aggregates verify_family across the requested families into a
# per-family discrepancy report. With repair=True it feeds any discrepancy back
# into the same idempotent sync job (run_sync_job) and re-verifies — "repair"
# is essentially "run the job again", because rsync --delete + size compare
# converges the drive tree to the local source. A successful, discrepancy-free
# repair clears the family's interrupted-sync record (run_sync_job already calls
# complete_family on success), so the quick currency check can trust the drive
# again (design.md "Exhaustive verification and repair"; Req 6.1, 6.2, 6.5).


def verify_drive(
    mount_point: str,
    families: Optional[list] = None,
    deep: bool = False,
    repair: bool = False,
    progress_callback: Optional[Callable] = None,
    is_aborted: Optional[Callable[[], bool]] = None,
    sync_state=None,
) -> dict:
    """Exhaustively verify (and optionally repair) the drive against the image.

    Builds the per-family :class:`SyncJob` set via :func:`build_jobs` and
    aggregates :func:`verify_family` results into a per-family discrepancy
    report. This is the on-demand "Verify Drive" mechanism (Req 6.1, 6.4) and
    the post-interruption verify+repair path (Req 6.3).

    Depth is count + size by default (``deep=False``); ``deep=True`` adds a
    content-hash comparison for same-size files (opt-in, Req 5.4 / 4.3).

    When ``repair=True``, any family that shows a discrepancy is re-run through
    :func:`run_sync_job` (idempotent: rsync ``--delete`` + size compare converges
    the tree), then re-verified. Because ``run_sync_job`` writes the commit
    marker and calls ``complete_family`` only on a clean, verified pass, a
    successful repair also clears that family's interrupted-sync record
    (Req 6.2, 6.5). Families that were already clean are left untouched.

    Args:
        mount_point: Path to the mounted EFIS drive.
        families: Optional subset of ``["scanned", "plates", "nav"]``; default
            (None) verifies all three.
        deep: Add content hashing for same-size files (opt-in deep verify).
        repair: When True, re-run any discrepant family and re-verify.
        progress_callback: Optional ``callable(message)`` for status updates
            (passed through to ``run_sync_job`` during repair).
        is_aborted: Optional abort predicate passed through to ``run_sync_job``
            during repair (wired to the mount watchdog by callers).
        sync_state: Optional sync-state module/object for ``run_sync_job``
            (defaults to this module). Only used when ``repair=True``.

    Returns:
        Dict shaped as::

            {
              "families": {
                name: {"missing": [...], "extra": [...], "size_mismatch": [...]}
              },
              "repaired": [name, ...],   # families re-run during repair
              "clean": bool,             # True iff no family has any discrepancy
                                         # (after repair, if requested)
              "errors": [str, ...],      # repair errors, also logged >= WARNING
            }
    """
    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    jobs = build_jobs(mount_point, families)
    jobs_by_name = {job.name: job for job in jobs}

    per_family: dict = {}
    for name, job in jobs_by_name.items():
        _status(f"Verifying {name}...")
        per_family[name] = verify_family(job, mount_point, deep=deep)

    repaired: list = []
    errors: list = []

    if repair:
        for name, disc in list(per_family.items()):
            if not _has_discrepancies(disc):
                continue
            _status(f"Repairing {name}...")
            repaired.append(name)
            # Re-run the same job: idempotent convergence re-copies missing /
            # changed files, deletes extras, re-verifies, and (on success)
            # writes the marker + clears the interrupted-sync record.
            result = run_sync_job(
                jobs_by_name[name],
                mount_point,
                sync_state=sync_state,
                progress_callback=progress_callback,
                is_aborted=is_aborted,
            )
            if result.errors:
                errors.extend(result.errors)
            # Re-verify to report the post-repair state authoritatively.
            per_family[name] = verify_family(jobs_by_name[name], mount_point, deep=deep)

    clean = not any(_has_discrepancies(disc) for disc in per_family.values())

    return {
        "families": per_family,
        "repaired": repaired,
        "clean": clean,
        "errors": errors,
    }


def _format_verify_summary(families: dict) -> str:
    """Build a concise per-family summary line from a ``verify_drive`` result.

    Pure function (no I/O, no rumps/AppKit) so it can be unit-tested headless
    and reused by ``app.py`` for the "Verify Drive" notification/status
    (Req 6.4). For each family it reports either ``"<name>: clean"`` or the
    non-zero discrepancy counts, e.g.::

        scanned: clean, plates: 3 missing, nav: 1 size mismatch

    Family order follows insertion order of the ``families`` dict (build_jobs
    order: scanned, plates, nav). A family with no discrepancies reads
    "clean"; otherwise only the non-zero categories are listed.

    Args:
        families: The ``"families"`` sub-dict of a ``verify_drive`` result:
            ``{name: {"missing": [...], "extra": [...], "size_mismatch": [...]}}``.

    Returns:
        A single comma-separated summary string. Empty input yields
        ``"no families"``.
    """
    if not families:
        return "no families"

    parts = []
    for name, disc in families.items():
        if not _has_discrepancies(disc):
            parts.append(f"{name}: clean")
            continue
        counts = []
        n_missing = len(disc.get("missing") or [])
        n_extra = len(disc.get("extra") or [])
        n_mismatch = len(disc.get("size_mismatch") or [])
        if n_missing:
            counts.append(f"{n_missing} missing")
        if n_extra:
            counts.append(f"{n_extra} extra")
        if n_mismatch:
            counts.append(f"{n_mismatch} size mismatch")
        parts.append(f"{name}: {', '.join(counts)}")
    return ", ".join(parts)


# --- Mount-presence watchdog ------------------------------------------------
#
# While a job runs, a background thread polls os.path.ismount(mount_point)
# (design.md "Mount-presence watchdog"). If the mount vanishes — the drive is
# ejected, a dock/hub is pulled — the watchdog latches an "aborted" condition
# and the is_aborted() predicate it exposes returns True, which sync_payload /
# run_sync_job already honour by terminating rsync and returning status
# "aborted" while leaving the interrupted-sync marker in place (Req 7.2, 7.3).
#
# The watchdog also honours an explicit at-risk flag: system sleep (task 11)
# sets it so an in-progress sync stops safely and leaves the interrupted marker
# (Req 7.4), treating sleep as a "stop then resume" special case of the same
# interruption path rather than trying to keep USB I/O alive across sleep.
#
# Design for testability: the poll interval and the ismount function are both
# injectable, and an internal Event lets a test flip the condition
# deterministically without racing the poll thread.

# Default seconds between mount-presence polls (design.md: "every ~2 s").
WATCHDOG_POLL_INTERVAL = 2.0

# A transient mount blip (the drive re-attaching, or a momentary ismount()
# false-negative right after mount) must NOT be mistaken for a real removal.
# The watchdog only latches "mount removed" after the mount has been absent for
# this many CONSECUTIVE polls, debouncing the mount-readiness race seen when a
# volume is still settling. A genuine removal persists across polls and latches
# after ~WATCHDOG_SETTLE_POLLS * poll_interval seconds. Sleep/at-risk still
# latches immediately (it is a decisive, out-of-band signal).
WATCHDOG_SETTLE_POLLS = 2


class MountWatchdog:
    """Polls mount presence and drives an ``is_aborted()`` abort predicate.

    Usage::

        wd = MountWatchdog(mount_point)
        wd.start()
        try:
            run_sync_job(job, mount_point, is_aborted=wd.is_aborted)
        finally:
            wd.stop()

    A background thread checks ``ismount(mount_point)`` every ``poll_interval``
    seconds. The moment the mount is gone the watchdog latches
    (:attr:`aborted` becomes True) and logs an ERROR naming the mount. Callers
    poll :meth:`is_aborted`, which is also True when the at-risk flag is set
    (system sleep). Once latched the condition stays latched until :meth:`stop`.

    Args:
        mount_point: The mount whose presence gates the running job.
        poll_interval: Seconds between polls. Injectable for tests.
        ismount: Callable ``(path) -> bool`` used to test mount presence.
            Defaults to :func:`os.path.ismount`; injectable for tests.
        job_name: Optional family name used only in the log/status message.
    """

    def __init__(
        self,
        mount_point: str,
        poll_interval: float = WATCHDOG_POLL_INTERVAL,
        ismount: Optional[Callable[[str], bool]] = None,
        job_name: Optional[str] = None,
        settle_polls: int = WATCHDOG_SETTLE_POLLS,
    ):
        self.mount_point = mount_point
        self.poll_interval = poll_interval
        self._ismount = ismount if ismount is not None else os.path.ismount
        self.job_name = job_name
        # Consecutive absent-polls required before latching a real removal.
        self.settle_polls = max(1, int(settle_polls))

        # Latched once the mount disappears; never un-latches while running.
        self._aborted = False
        # Reason string for the status line ("removed" vs "sleeping").
        self.abort_reason: Optional[str] = None
        # At-risk flag for the sleep case (task 11). When set, is_aborted() is
        # True even though the mount may still be present.
        self._at_risk = False

        import threading as _threading

        self._threading = _threading
        self._stop_event = _threading.Event()
        # Signalled the instant the abort condition latches so tests (and the
        # job) do not have to spin on the poll interval.
        self._abort_event = _threading.Event()
        self._thread: Optional[_threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "MountWatchdog":
        """Start the background poll thread (idempotent). Returns self."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop_event.clear()
        self._thread = self._threading.Thread(
            target=self._run, name="mount-watchdog", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the poll thread and wait briefly for it to exit."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(self.poll_interval, 1.0) + 1.0)
        self._thread = None

    # -- abort surface -------------------------------------------------------

    def is_aborted(self) -> bool:
        """Predicate passed to run_sync_job/sync_payload.

        True once the mount has vanished OR the at-risk (sleep) flag is set.
        """
        return self._aborted or self._at_risk

    @property
    def aborted(self) -> bool:
        """True once the watchdog has latched an abort condition."""
        return self._aborted or self._at_risk

    def wait_for_abort(self, timeout: Optional[float] = None) -> bool:
        """Block until the abort condition latches; return whether it did.

        Useful in tests to synchronise with the poll thread instead of sleeping.
        """
        return self._abort_event.wait(timeout=timeout)

    def mark_at_risk(self, reason: str = "system sleeping") -> None:
        """Flag the in-progress job as at-risk (system sleep — Req 7.4).

        The running rsync is stopped safely on the next predicate poll and the
        interrupted-sync marker is left in place so the job resumes on wake or
        next mount. Task 11 wires this to the NSWorkspace will-sleep observer.
        """
        self._at_risk = True
        if self.abort_reason is None:
            self.abort_reason = reason
        self._latch(reason)

    def clear_at_risk(self) -> None:
        """Clear the at-risk flag (e.g. on wake). Does not un-latch a mount loss."""
        self._at_risk = False

    # -- internals -----------------------------------------------------------

    def _latch(self, reason: str) -> None:
        """Latch the abort condition, log once at ERROR, and signal waiters."""
        if not self._aborted:
            self._aborted = True
            self.abort_reason = reason
            target = f" during {self.job_name} sync" if self.job_name else " during sync"
            logger.error("EFIS drive %s%s (%s)", self.mount_point, target, reason)
        self._abort_event.set()

    def _run(self) -> None:
        """Poll loop: latch on a persistent mount loss or an at-risk signal.

        A single ``ismount() == False`` no longer latches: a volume that is
        still settling (or a momentary false-negative right after re-mount) can
        report absent for one poll and be present the next. We only latch
        "mount removed" after ``settle_polls`` CONSECUTIVE absent polls, which
        debounces the mount-readiness race while still catching a genuine
        removal within a second or two. The at-risk (sleep) signal is decisive
        and latches immediately.
        """
        absent_streak = 0
        while not self._stop_event.is_set():
            if self._at_risk:
                self._latch(self.abort_reason or "system sleeping")
                return
            try:
                present = self._ismount(self.mount_point)
            except OSError:
                present = False
            if present:
                absent_streak = 0
            else:
                absent_streak += 1
                if absent_streak >= self.settle_polls:
                    self._latch("mount removed")
                    return
            # Sleep in a way that wakes immediately on stop() for a clean exit.
            self._stop_event.wait(self.poll_interval)


def run_sync_job_watched(
    job: SyncJob,
    mount_point: str,
    sync_state=None,
    progress_callback: Optional[Callable] = None,
    poll_interval: float = WATCHDOG_POLL_INTERVAL,
    ismount: Optional[Callable[[str], bool]] = None,
) -> JobResult:
    """Run a job with a live mount-presence watchdog wired to ``is_aborted``.

    Convenience wrapper that starts a :class:`MountWatchdog` for the duration of
    the job and passes its predicate into :func:`run_sync_job`, so a drive
    removal (or a sleep at-risk flag) aborts the transfer, leaves the
    interrupted marker, and returns status "aborted". The watchdog is always
    stopped, even on error. Returns the :class:`JobResult`.
    """
    watchdog = MountWatchdog(
        mount_point,
        poll_interval=poll_interval,
        ismount=ismount,
        job_name=job.name,
    )
    watchdog.start()
    try:
        return run_sync_job(
            job,
            mount_point,
            sync_state=sync_state,
            progress_callback=progress_callback,
            is_aborted=watchdog.is_aborted,
        )
    finally:
        watchdog.stop()


# Software files — match by prefix since local names may differ from USB names
SOFTWARE_MAPPING = {
    # local image name -> USB drive name
    # We sync any .dat files from local image to USB
}


# Modify-window (seconds) that absorbs FAT/exFAT 2-second timestamp
# granularity when comparing a drive marker against the local marker.
MARKER_MTIME_TOLERANCE = 2

# Top-level scanned-chart subdirectories used for the cheap structural
# non-emptiness sanity check (sectionals + IFR low).
SCANNED_STRUCTURE_DIRS = ["LO", "SEC"]

# Read files in this many bytes at a time when checksumming nav files.
_CHECKSUM_CHUNK = 1024 * 1024


def _file_checksum(path: str) -> Optional[str]:
    """Return the SHA-256 hex digest of a file, or None if it can't be read."""
    import hashlib

    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHECKSUM_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _dir_nonempty(path: str) -> bool:
    """True if `path` is a directory containing at least one entry."""
    try:
        with os.scandir(path) as it:
            return any(True for _ in it)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False


def _check_tree_family(local_root, drive_root, marker_rel, structure_dirs):
    """Quick currency test for a marker-based tree family (scanned/plates).

    A family is current when its drive commit marker exists, is newer-or-equal
    to the local marker (within the FAT/exFAT modify-window), matches the local
    marker size, and the family's required top-level structure directories
    exist and are non-empty on the drive.

    Returns:
        (current: bool, reason: str). ``current`` is None-safe: if the local
        marker is missing there is nothing to sync, which reads as current.
    """
    local_marker = os.path.join(local_root, marker_rel)
    drive_marker = os.path.join(drive_root, marker_rel)

    if not os.path.exists(local_marker):
        # No local marker => nothing to push for this family.
        return True, "no local marker (nothing to sync)"

    if not os.path.exists(drive_marker):
        return False, "drive marker missing"

    local_size = os.path.getsize(local_marker)
    drive_size = os.path.getsize(drive_marker)
    if local_size != drive_size:
        return False, f"drive marker size differs (local={local_size}, drive={drive_size})"

    local_mtime = os.path.getmtime(local_marker)
    drive_mtime = os.path.getmtime(drive_marker)
    if drive_mtime + MARKER_MTIME_TOLERANCE < local_mtime:
        return False, "drive marker older"

    # Cheap structural sanity: required subtrees must exist and be non-empty.
    drive_family_root = os.path.dirname(drive_marker)
    for sub in structure_dirs:
        if not _dir_nonempty(os.path.join(drive_family_root, sub)):
            return False, f"{sub}/ missing or empty on drive"

    return True, "marker matches"


def _check_nav_family(local_root, drive_root):
    """Quick currency test for the nav family (per-file checksum).

    NAV.DB must match by checksum; NAV-proc.DB is checked only if it exists
    locally. Missing local files are treated as nothing-to-sync.

    Returns:
        (current: bool, reason: str).
    """
    checked_any = False
    for name in NAV_FILES:
        local_path = os.path.join(local_root, name)
        drive_path = os.path.join(drive_root, name)

        if not os.path.exists(local_path):
            # Nothing to sync for this file (NAV-proc.DB may be absent).
            continue
        checked_any = True

        if not os.path.exists(drive_path):
            return False, f"{name} missing from drive"

        # Fast path: a size difference is a guaranteed mismatch, no need to hash.
        if os.path.getsize(local_path) != os.path.getsize(drive_path):
            return False, f"{name} size differs"

        if _file_checksum(local_path) != _file_checksum(drive_path):
            return False, f"{name} checksum mismatch"

    if not checked_any:
        return True, "no nav files to sync"
    return True, "checksum matches"


def check_drive_currency(mount_point: str) -> dict:
    """Quick, per-family currency check between the local image and the drive.

    Each product family is tested cheaply (see design.md "Currency check
    (quick)"):

      - scanned / plates: the drive commit marker must exist, be newer-or-equal
        to the local marker (within the FAT modify-window) and match its size,
        and the family's top-level structure must be present and non-empty.
      - nav: NAV.DB (and NAV-proc.DB if present locally) must match by checksum.

    A family named in the durable sync-state (``pending_families``) is forced
    not-current regardless of its marker, so an interrupted sync always triggers
    verify+repair before the drive is trusted again (Req 3.5).

    Returns:
        Dict shaped as::

            {
              "is_current": bool,
              "stale_items": [str],           # backward-compatible summary
              "message": str,
              "families": {name: {"current": bool, "reason": str}},
            }
    """
    config = load_config()
    usb_image_path = config["usb_image_path"]

    families: dict = {}

    # Per-family quick checks.
    families["scanned"] = dict(
        zip(
            ("current", "reason"),
            _check_tree_family(
                usb_image_path, mount_point, SCANNED_MARKER, SCANNED_STRUCTURE_DIRS
            ),
        )
    )
    families["plates"] = dict(
        zip(
            ("current", "reason"),
            _check_tree_family(
                usb_image_path,
                mount_point,
                PLATES_MARKER,
                # Plates/ itself must be non-empty; the marker lives inside it.
                ["."],
            ),
        )
    )
    families["nav"] = dict(
        zip(("current", "reason"), _check_nav_family(usb_image_path, mount_point))
    )

    # An interrupted sync forces its families not-current until verify+repair.
    # Resolve the drive id once (task 15). Task 16 completes the app-side wiring;
    # here we consult the id-keyed sync-state directly. When the id cannot be
    # resolved (fail-safe None), we apply NO interrupted-sync state to this drive
    # (never another drive's) and rely solely on the marker-based check above —
    # the on-drive markers/payload remain the sole source of truth for currency
    # (Req 10.7). The identity file never decides currency.
    drive_id = resolve_drive_id(mount_point)
    pending = pending_families(drive_id) if drive_id is not None else []
    for name in pending:
        if name in families:
            families[name] = {
                "current": False,
                "reason": "interrupted sync — verify+repair required",
            }

    stale_items = [
        f"{name} ({detail['reason']})"
        for name, detail in families.items()
        if not detail["current"]
    ]

    if stale_items:
        return {
            "is_current": False,
            "stale_items": stale_items,
            "message": f"{len(stale_items)} family(ies) need updating.",
            "families": families,
        }
    return {
        "is_current": True,
        "stale_items": [],
        "message": "Drive is up to date.",
        "families": families,
    }


def update_drive(
    mount_point: str,
    families: Optional[list] = None,
    progress_callback: Optional[Callable] = None,
    is_aborted: Optional[Callable[[], bool]] = None,
) -> dict:
    """Sync the requested product families to the physical EFIS drive.

    Builds the per-family :class:`SyncJob` set via :func:`build_jobs` and runs
    each through :func:`run_sync_job` under a live :class:`MountWatchdog`, so a
    drive removal (or a system-sleep at-risk flag) mid-update aborts the running
    transfer, leaves the interrupted-sync marker in place, and yields an
    "aborted" :class:`JobResult` (Req 7.2, 7.3). Each job owns a disjoint slice
    of the tree, so one family failing or aborting never invalidates another's
    already-written commit marker (Property 4 / Req 1.2).

    ``update_drive`` runs exactly the families it is given: pass an explicit
    subset to sync only those (the app's ``_run_drive_update`` does its own quick
    currency check first and passes the stale families), or leave ``families``
    None to run all three. It does not itself consult
    :func:`check_drive_currency` — an already-current family simply reports
    ``status == "current"`` from its idempotent no-op sync.

    Args:
        mount_point: Path to the mounted EFIS drive (e.g. ``/Volumes/EFIS``).
        families: Optional subset of ``["scanned", "plates", "nav"]`` to sync.
            None (default) runs all three.
        progress_callback: Optional ``callable(message)`` for status updates.
        is_aborted: Optional externally-owned abort predicate. When None
            (default) each job runs under its own :class:`MountWatchdog` via
            :func:`run_sync_job_watched`, preserving the standalone behaviour.
            When provided (the app owns a single :class:`MountWatchdog` for the
            whole update so a system-sleep at-risk flag can stop the running
            job — task 11), the predicate is injected into :func:`run_sync_job`
            directly and no per-job watchdog is created. Backward compatible:
            existing callers that omit it are unaffected.

    Returns:
        Dict shaped as::

            {
              "jobs": {name: JobResult, ...},  # one entry per family run
              "errors": [str, ...],            # concatenation of all job errors
              "aborted": bool,                 # True if any job aborted
            }

        ``errors`` is the concatenation of every job's ``errors`` (each already
        logged at >= WARNING by the job driver, so the Recent Errors panel
        mirrors the status line — Req 8.1/8.2). ``aborted`` is True when any job
        finished with status ``"aborted"``.
    """
    def _status(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    jobs = build_jobs(mount_point, families)

    results: dict = {"jobs": {}, "errors": [], "aborted": False}

    for job in jobs:
        if is_aborted is not None:
            # The caller owns a single watchdog for the whole update (task 11):
            # inject its predicate so a mid-job drive removal OR a system-sleep
            # at-risk flag aborts promptly and leaves the interrupted marker.
            result = run_sync_job(
                job,
                mount_point,
                progress_callback=progress_callback,
                is_aborted=is_aborted,
            )
        else:
            # Each job runs under its own watchdog so a mid-job drive removal
            # aborts promptly and leaves the interrupted marker for verify+repair.
            result = run_sync_job_watched(
                job,
                mount_point,
                progress_callback=progress_callback,
            )
        results["jobs"][job.name] = result
        if result.errors:
            results["errors"].extend(result.errors)
        if result.status == "aborted":
            results["aborted"] = True

    _status("Drive update complete.")
    return results


def _terminal_status_from_jobs(jobs: dict, aborted: bool) -> str:
    """Map an ``update_drive`` jobs dict + aborted flag to a status-line string.

    Pure function (no I/O, no rumps/AppKit) so it can be unit-tested headless
    and reused by ``app.py`` for a consistent terminal status. It encodes the
    Req 8.4 "no sticky error strings" rule: any all-clean terminal state returns
    the idle/current status, never a leftover error string.

    Precedence (worst-first), matching the design's status transitions:

      - aborted (any job aborted, or the ``aborted`` flag set) ->
        ``"Drive removed during update"`` (the mount-presence watchdog / sleep
        at-risk path — Req 7.3).
      - any failed family -> ``"<name> update failed"`` (or, for several,
        ``"<a>, <b> update failed"``) so the status names the family (Req 8.2).
      - otherwise (every job updated/current) -> ``"Drive current"`` (Req 8.4).

    Args:
        jobs: ``{name: JobResult}`` from ``update_drive`` (each JobResult has a
            ``.status`` in ``current|updated|failed|aborted``).
        aborted: The aggregate ``aborted`` flag from ``update_drive``.

    Returns:
        A terminal status string suitable for the menu-bar status line.
    """
    if aborted or any(r.status == "aborted" for r in jobs.values()):
        return "Drive removed during update"

    failed = [name for name, r in jobs.items() if r.status == "failed"]
    if failed:
        return f"{', '.join(failed)} update failed"

    return "Drive current"


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

    # Write a durable identity file so the freshly-prepared drive has a stable
    # id from the start (Req 10.3). Best-effort: a failure here must NOT fail
    # preparation — the drive is still usable and resolve_drive_id will adopt it
    # lazily. Doing it before populate means the subsequent update_drive/
    # run_sync_job resolves THIS id (resolve_drive_id reads the file just
    # written).
    _status("Writing drive identity...")
    _ensure_identity(mount_point)

    # Now run the normal per-family sync to populate it (Req 9.4). A freshly
    # formatted drive has no markers, so every family is stale and copied.
    _status("Populating drive with current data...")
    update_results = update_drive(mount_point, progress_callback=progress_callback)

    jobs = update_results["jobs"]
    # Per-family summary, e.g. "scanned: updated, plates: updated, nav: updated".
    per_family = ", ".join(
        f"{name}: {result.status}" for name, result in jobs.items()
    )

    if update_results["errors"] or update_results["aborted"]:
        failed = [
            name
            for name, result in jobs.items()
            if result.status in ("failed", "aborted")
        ]
        detail = f" ({per_family})" if per_family else ""
        return {
            "success": False,
            "message": (
                f"Drive formatted but population had errors on "
                f"{', '.join(failed) or 'unknown'}{detail}."
            ),
        }

    detail = f" ({per_family})" if per_family else ""
    return {
        "success": True,
        "message": f"Drive prepared and populated successfully{detail}.",
    }
