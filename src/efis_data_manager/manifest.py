"""SHA-256 content manifests for the EFIS chart-drive sync engine.

This module introduces a uniform content manifest of a chart family tree,
computed at download time on the fast SSD, which the sync engine diffs against a
manifest stored on the card to drive the transfer. Content hashing is the only
correct change signal for these charts: neither byte size (the retired
``rsync --size-only`` gap) nor file timestamp can be trusted.

Task 1.1 scope: the manifest data model (``ManifestEntry``, ``FamilyManifest``,
``CardManifest``) and the ``Manifest_Builder`` that walks a family tree and
hashes it. The store, diff, cycle gate, and sync wiring land in later tasks.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Optional

from . import drive_updater
from .config import APP_SUPPORT_DIR

logger = logging.getLogger(__name__)

# Current manifest schema version. Bump when the on-disk/on-card JSON shape
# changes in a way that is not backward compatible.
SCHEMA_VERSION = 1

# Kind tag stored in the card manifest so a stray reader can tell what the file
# is (mirrors the identity file's ``kind`` convention).
CARD_MANIFEST_KIND = "efis-chart-manifest"

# Local manifests live on the SSD under the app-support dir, one JSON per chart
# family (``scanned.json`` / ``plates.json``). nav has no manifest.
LOCAL_MANIFEST_DIR = APP_SUPPORT_DIR / "manifests"

# The single card manifest at the EFIS card volume root. The card is per-device,
# so there is exactly one of these per card. It coexists with
# ``EFIS_DRIVE_ID.json`` and is independent of it.
CARD_MANIFEST_NAME = "EFIS_MANIFEST.json"

# Read files in this many bytes at a time when hashing. Reuses the chunk size of
# ``drive_updater._file_checksum`` so the manifest hash matches the nav job's
# deep-checksum behavior exactly.
_HASH_CHUNK = drive_updater._CHECKSUM_CHUNK

# Basenames excluded from a family manifest so it matches exactly what rsync
# transfers: macOS metadata plus the per-family commit markers. The ``._*``
# AppleDouble prefix is handled separately by basename inspection.
_EXCLUDED_BASENAMES = frozenset(
    {
        ".DS_Store",
        os.path.basename(drive_updater.SCANNED_MARKER),
        os.path.basename(drive_updater.PLATES_MARKER),
    }
)


@dataclass(frozen=True)
class ManifestEntry:
    """One file in a family manifest.

    ``relpath`` is the family-root-relative POSIX path (the stable key),
    ``sha256`` is the lowercase hex content digest (the authoritative change
    signal), and ``size`` is a cheap cross-check that is never the sole signal.
    """

    relpath: str
    sha256: str
    size: int


@dataclass
class FamilyManifest:
    """The content manifest for one chart family.

    ``family`` is ``"scanned"`` (SEC + LO + optional HI) or ``"plates"``;
    ``cycle`` is the governing cycle number (charts cycle for scanned, plates
    cycle for plates). ``entries`` maps relpath -> :class:`ManifestEntry`.
    """

    family: str
    cycle: Optional[str]
    entries: Dict[str, ManifestEntry] = field(default_factory=dict)
    built: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class CardManifest:
    """The single ``EFIS_MANIFEST.json`` stored at the card volume root.

    Records the authoritative cycle numbers written to the card and, per
    family, the manifest of what content was last written. ``charts_cycle`` /
    ``plates_cycle`` at the top level are authoritative for the per-stream cycle
    gate; the volume label is only a human-facing mirror.
    """

    charts_cycle: Optional[str] = None
    plates_cycle: Optional[str] = None
    written: str = ""
    families: Dict[str, FamilyManifest] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    kind: str = CARD_MANIFEST_KIND


def _now_iso() -> str:
    """Local-time ISO-8601 timestamp with offset, for the ``built`` field."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _is_excluded(basename: str) -> bool:
    """True if a file should be left out of the manifest.

    Excludes macOS metadata (``.DS_Store``, AppleDouble ``._*``) and the
    per-family commit markers so the manifest matches exactly what rsync
    transfers.
    """
    if basename in _EXCLUDED_BASENAMES:
        return True
    if basename.startswith("._"):
        return True
    return False


def _hash_file(path: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``path``.

    Uses the same chunked read as ``drive_updater._file_checksum``. Unlike that
    helper this raises on an unreadable file rather than returning ``None``: a
    manifest must not silently record a file it could not hash.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class Manifest_Builder:
    """Walks a chart family tree on the SSD and computes its manifest."""

    @staticmethod
    def build(family: str, family_root: str, cycle: Optional[str]) -> FamilyManifest:
        """Build the :class:`FamilyManifest` for ``family`` rooted at
        ``family_root``.

        Walks ``family_root``, SHA-256-hashes each file, and keys entries by the
        family-root-relative POSIX path. macOS metadata (``.DS_Store``,
        ``._*``) and the per-family commit markers are excluded so the manifest
        matches exactly what rsync transfers.
        """
        entries: Dict[str, ManifestEntry] = {}
        for dirpath, _dirnames, filenames in os.walk(family_root):
            for name in filenames:
                if _is_excluded(name):
                    continue
                abspath = os.path.join(dirpath, name)
                # Skip anything that is not a regular file (e.g. a dangling
                # symlink); rsync would not transfer a broken link as content.
                if not os.path.isfile(abspath):
                    continue
                rel = os.path.relpath(abspath, family_root)
                # Normalize to POSIX separators so the key is stable across
                # machines and mountpoints.
                rel_posix = rel.replace(os.sep, "/")
                size = os.path.getsize(abspath)
                digest = _hash_file(abspath)
                entries[rel_posix] = ManifestEntry(
                    relpath=rel_posix, sha256=digest, size=size
                )
        return FamilyManifest(
            family=family,
            cycle=cycle,
            entries=entries,
            built=_now_iso(),
            schema_version=SCHEMA_VERSION,
        )


# --- Serialization ----------------------------------------------------------
#
# The JSON schema the cycle gate and diff depend on. Serialize/deserialize must
# round-trip: ``load(save(m)) == m`` for any FamilyManifest or CardManifest
# (Property 5, Req 5.3). A FamilyManifest's ``entries`` serialize as a map of
# relpath -> {sha256, size}; the relpath is already the key so it is not
# duplicated inside the value.


def _entries_to_json(entries: Dict[str, ManifestEntry]) -> dict:
    """Serialize a relpath -> ManifestEntry map to a JSON-ready dict.

    The relpath is the map key, so each value carries only ``sha256`` and
    ``size``; the key is re-attached as ``relpath`` on load.
    """
    return {
        relpath: {"sha256": entry.sha256, "size": entry.size}
        for relpath, entry in entries.items()
    }


def _entries_from_json(raw: dict) -> Dict[str, ManifestEntry]:
    """Rebuild a relpath -> ManifestEntry map from its JSON form."""
    entries: Dict[str, ManifestEntry] = {}
    for relpath, value in (raw or {}).items():
        entries[relpath] = ManifestEntry(
            relpath=relpath,
            sha256=value["sha256"],
            size=int(value["size"]),
        )
    return entries


def family_to_json(manifest: FamilyManifest) -> dict:
    """Serialize a :class:`FamilyManifest` to a JSON-ready dict."""
    return {
        "schema_version": manifest.schema_version,
        "family": manifest.family,
        "cycle": manifest.cycle,
        "built": manifest.built,
        "entries": _entries_to_json(manifest.entries),
    }


def family_from_json(raw: dict) -> FamilyManifest:
    """Rebuild a :class:`FamilyManifest` from its JSON form."""
    return FamilyManifest(
        family=raw["family"],
        cycle=raw.get("cycle"),
        entries=_entries_from_json(raw.get("entries", {})),
        built=raw.get("built", ""),
        schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
    )


def card_to_json(card: CardManifest) -> dict:
    """Serialize a :class:`CardManifest` to a JSON-ready dict."""
    return {
        "schema_version": card.schema_version,
        "kind": card.kind,
        "charts_cycle": card.charts_cycle,
        "plates_cycle": card.plates_cycle,
        "written": card.written,
        "families": {
            name: family_to_json(fam) for name, fam in card.families.items()
        },
    }


def card_from_json(raw: dict) -> CardManifest:
    """Rebuild a :class:`CardManifest` from its JSON form."""
    return CardManifest(
        charts_cycle=raw.get("charts_cycle"),
        plates_cycle=raw.get("plates_cycle"),
        written=raw.get("written", ""),
        families={
            name: family_from_json(fam)
            for name, fam in (raw.get("families") or {}).items()
        },
        schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
        kind=raw.get("kind", CARD_MANIFEST_KIND),
    )


class Manifest_Store:
    """Reads/writes manifests locally (SSD) and on the card.

    Local manifests are the source of truth for the newly downloaded cycle; the
    card manifest records what was last written to a given card. The card write
    is atomic (temp + fsync + os.replace + os.sync), mirroring
    ``drive_updater.write_identity``, so a card pulled mid-write never leaves a
    half-written manifest.
    """

    @staticmethod
    def _local_path(family: str):
        """Return the local JSON path for ``family`` (``scanned`` / ``plates``)."""
        return LOCAL_MANIFEST_DIR / f"{family}.json"

    @staticmethod
    def load_local(family: str) -> Optional[FamilyManifest]:
        """Load the local manifest for ``family``, or ``None`` if absent/corrupt.

        A missing, unreadable, or unparseable local manifest returns ``None`` so
        the caller falls back to a full write for that family (Req 6.3).
        """
        path = Manifest_Store._local_path(family)
        try:
            with open(path) as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        try:
            return family_from_json(raw)
        except (KeyError, TypeError, ValueError):
            logger.warning("Local manifest for %s is malformed; ignoring", family)
            return None

    @staticmethod
    def save_local(manifest: FamilyManifest) -> None:
        """Persist ``manifest`` to the local app-support manifests dir.

        Keyed by ``manifest.family`` (``scanned.json`` / ``plates.json``). The
        local store is on the fast SSD and not pull-exposed, so a plain write
        with a parent-dir ensure is sufficient (atomicity matters only for the
        card write).
        """
        LOCAL_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        path = Manifest_Store._local_path(manifest.family)
        with open(path, "w") as f:
            json.dump(family_to_json(manifest), f, indent=2)

    @staticmethod
    def _card_path(mount_point: str) -> str:
        """Return the card manifest path at the volume root of ``mount_point``."""
        return os.path.join(mount_point, CARD_MANIFEST_NAME)

    @staticmethod
    def load_card(mount_point: str) -> Optional[CardManifest]:
        """Load the single card manifest at ``mount_point``'s volume root.

        Returns ``None`` when the manifest is missing, corrupt, or unparseable,
        so the caller treats the card as having no manifest and performs a full,
        self-healing write (Req 6.1, 6.2). Never raises.
        """
        path = Manifest_Store._card_path(mount_point)
        try:
            with open(path) as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        try:
            return card_from_json(raw)
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Card manifest at %s is malformed; treating as absent", mount_point
            )
            return None

    @staticmethod
    def save_card(mount_point: str, card: CardManifest) -> None:
        """Atomically write the card manifest to ``mount_point``'s volume root.

        Writes to a temp file in the same directory, flushes + fsyncs it, then
        ``os.replace``s it over the destination so a reader never observes a
        partially written card manifest (Req 5.5). A best-effort ``os.sync()``
        follows for FAT/exFAT media where fsync alone may not push the rename to
        the device. Mirrors ``drive_updater.write_identity``.
        """
        path = Manifest_Store._card_path(mount_point)
        os.makedirs(mount_point, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".efis_manifest.", dir=mount_point)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(card_to_json(card), f, indent=2)
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
        # Best-effort filesystem-wide flush for FAT media where fsync alone may
        # not guarantee the rename/metadata is on the device. Never fatal.
        try:
            os.sync()
        except OSError:
            pass


# --- Diff -------------------------------------------------------------------
#
# The pure comparison that drives the transfer. It diffs the newly downloaded
# local manifest against the card's stored manifest for one family and produces
# the change set rsync consumes. It deliberately does NOT re-hash the card:
# the card manifest is written only after a verified sync, so it is trusted as
# the faithful record of what was last written (design Decision 4, Req 4.2).


@dataclass
class ManifestDiff:
    """The change set produced by diffing a local manifest against a card family.

    ``added`` are relpaths present locally but absent on the card; ``changed``
    are relpaths present on both whose SHA-256 differs; ``removed`` are relpaths
    present on the card but absent locally; ``unchanged`` is the count of files
    present on both with an identical SHA-256 (kept for logging only).
    """

    added: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    unchanged: int = 0

    @property
    def files_from(self) -> list:
        """Relpaths to transfer: ``added`` + ``changed`` (the rsync
        ``--files-from`` set). ``removed`` paths are pruned separately."""
        return self.added + self.changed


def diff_family(
    local: FamilyManifest, card_family: Dict[str, ManifestEntry]
) -> ManifestDiff:
    """Diff a local family manifest against the card's stored family entries.

    Compares by ``relpath -> sha256``. Byte size is a cheap first discriminator
    but the SHA-256 digest is authoritative, which closes the retired
    ``rsync --size-only`` gap (a same-size content change is still detected,
    Req 4.1). This does NOT re-hash the card: ``card_family`` is trusted as the
    faithful record of what was last written (Req 4.2).

    ``card_family`` is the ``entries`` dict (relpath -> :class:`ManifestEntry`)
    of the corresponding family in the card manifest; pass an empty dict for a
    first-sync / absent-manifest full write.
    """
    local_entries = local.entries
    card_entries = card_family or {}

    added: list = []
    changed: list = []
    unchanged = 0

    for relpath, local_entry in local_entries.items():
        card_entry = card_entries.get(relpath)
        if card_entry is None:
            # Present locally, absent on the card.
            added.append(relpath)
            continue
        # Size is a quick discriminator, but SHA-256 is the authoritative
        # signal: if the digests differ the file changed, regardless of size.
        if local_entry.sha256 != card_entry.sha256:
            changed.append(relpath)
        else:
            unchanged += 1

    removed = [relpath for relpath in card_entries if relpath not in local_entries]

    return ManifestDiff(
        added=added,
        changed=changed,
        removed=removed,
        unchanged=unchanged,
    )
