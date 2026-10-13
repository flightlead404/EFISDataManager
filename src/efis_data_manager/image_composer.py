# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local FAT32 image composition for image-based drive write.

This module composes the drive filesystem on the local SSD so the slow
per-file work happens once, locally, rather than per-drive over a mounted
FAT32 USB volume. The eventual ``dd``-to-raw-slice write, raw-readback verify,
and image-attach lifecycle live in sibling tasks; THIS module carries only the
headless, pure-ish populate step and its sidecar guards, which operate against
ordinary directory trees and need no real device or ``hdiutil``.

Sidecar-free populate — LOCKED DECISION (task 1.1)
--------------------------------------------------
The macOS mount layer synthesises an AppleDouble ``._*`` companion for most
files written to a FAT32 volume (measured ~+86% files on a ~101k-file drive).
That tax is the whole reason the populate method is pinned here. The locked
approach, in order:

1. **Primary copy — rsync with extended-attribute copying OFF and ``._*`` /
   ``.DS_Store`` excluded.** The source tree lives on the local SSD and carries
   no resource forks; running rsync WITHOUT extended-attribute copying stops
   rsync itself from synthesising AppleDouble companions, and the ``--exclude``
   patterns drop any ``._*`` / ``.DS_Store`` already present in the source.
   (Req 3.1)

   Platform note on the flag spelling. The task's locked method names
   ``rsync --no-xattrs``. That flag belongs to GNU/samba rsync (rsync 3.x). The
   stock ``/usr/bin/rsync`` on current macOS is **openrsync**, which has NO
   ``--no-xattrs`` option at all: it does not copy extended attributes /
   resource forks UNLESS explicitly asked with ``-E`` / ``--extended-attributes``.
   So "xattrs off" is openrsync's DEFAULT and is achieved simply by NOT passing
   ``-E``. :func:`_build_rsync_argv` therefore passes ``--no-xattrs`` only when
   the available rsync advertises it (a GNU/samba rsync earlier on ``$PATH``),
   and otherwise relies on openrsync's xattrs-off default. Either way the result
   is identical — rsync does not carry resource forks — and the strip + hard
   assert below make the guarantee independent of which rsync ran.
2. **Mandatory post-populate strip.** After the copy, every residual ``._*``
   and ``.DS_Store`` entry under the destination is removed. This is belt-and-
   braces against anything the copy or the filesystem layer left behind, and
   it is independently callable (:func:`strip_sidecars`) so later property
   tests can exercise it against trees deliberately salted with sidecars.
   (Req 3.2)
3. **Hard assert — zero sidecars remain.** The finished tree is scanned and the
   populate FAILS LOUDLY (:class:`SidecarAssertionError`) if a single ``._*``
   or ``.DS_Store`` survives, so a dirty image can never be marked ready. This
   check is also independently callable (:func:`assert_sidecar_free`). (Req 3.3)

Why ``COPYFILE_DISABLE`` is NOT relied on
-----------------------------------------
``COPYFILE_DISABLE=1`` suppresses AppleDouble creation only for the ``copyfile``
family (``cp``/``ditto``). It did NOT suppress the sidecars observed in the
current write path, because those are synthesised by the FAT32 *mount layer*
when files land on the volume — not by the copying tool. The strip + hard
assert above therefore do not trust any environment flag to have prevented
sidecars; they verify the finished tree directly.

Native tools only: ``rsync`` ships with macOS; no new third-party dependency is
introduced (Req 14.1).
"""

import datetime
import logging
import os
import plistlib
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from efis_data_manager import write_verifier

logger = logging.getLogger(__name__)

# Absolute path to the system rsync, pinned rather than resolved off $PATH so
# the populate always runs the known native binary.
RSYNC_BIN = "/usr/bin/rsync"

# AppleDouble companion files are named "._<original>"; Finder's per-directory
# metadata file is ".DS_Store". These are the two entry classes a sidecar-free
# image must never contain.
_APPLEDOUBLE_PREFIX = "._"
_DS_STORE_NAME = ".DS_Store"

# rsync exclude patterns matching the two sidecar classes. "._*" matches every
# AppleDouble companion; ".DS_Store" matches the Finder metadata file.
_SIDECAR_EXCLUDES = ("._*", _DS_STORE_NAME)

# The GNU/samba-rsync flag that disables extended-attribute (resource-fork)
# copying. openrsync (stock macOS /usr/bin/rsync) does not have it and does not
# copy xattrs by default, so this is passed only when the available rsync
# advertises it. See the module docstring's platform note.
_NO_XATTRS_FLAG = "--no-xattrs"

# Ceiling for the rsync copy so a wedged copy cannot hang a caller forever.
RSYNC_TIMEOUT_SECONDS = 3600.0


class PopulateError(RuntimeError):
    """Raised when the rsync populate copy fails.

    This signals the copy stage itself did not complete (rsync missing,
    non-zero exit, or timeout). It is distinct from
    :class:`SidecarAssertionError`, which signals the copy ran but left sidecars
    behind.
    """


class SidecarAssertionError(RuntimeError):
    """Raised when the finished tree still contains ``._*`` / ``.DS_Store``.

    The hard assert at the end of :func:`populate` raises this when any sidecar
    survives the copy + strip, so a dirty image is reported as a failed
    composition and never marked ready (Req 3.3).
    """


def _is_sidecar(name: str) -> bool:
    """True iff a single path-segment name is an AppleDouble or .DS_Store entry.

    Operates on a bare basename (no separators), matching an entry name as
    scanned by :func:`os.walk`. ``.DS_Store`` is matched exactly; any name
    starting with ``._`` is treated as an AppleDouble companion.
    """
    return name == _DS_STORE_NAME or name.startswith(_APPLEDOUBLE_PREFIX)


def find_sidecars(root: str) -> List[str]:
    """Return absolute paths of every ``._*`` / ``.DS_Store`` entry under ``root``.

    Walks the whole tree and reports both sidecar files and (defensively) any
    directory whose own name is a sidecar. The result is sorted for stable,
    deterministic output so callers and tests can rely on ordering. A
    non-existent ``root`` yields an empty list rather than raising.
    """
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            if _is_sidecar(name):
                found.append(os.path.join(dirpath, name))
        for name in dirnames:
            if _is_sidecar(name):
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found


def strip_sidecars(root: str) -> int:
    """Remove every ``._*`` / ``.DS_Store`` entry under ``root``; return the count.

    Independently callable (so task 1.5's property test can salt a tree and
    exercise it directly). Removes sidecar files and, defensively, any directory
    whose name is itself a sidecar (removed recursively). Idempotent: a tree
    with no sidecars yields 0 and is left untouched. Returns the number of
    entries removed.
    """
    import shutil

    removed = 0
    # Collect first, then delete, so we never mutate the tree mid-walk.
    for path in find_sidecars(root):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except FileNotFoundError:
            # A parent sidecar directory may have already taken a child with it.
            continue
    if removed:
        logger.debug("strip_sidecars: removed %d sidecar entr(ies) under %s", removed, root)
    return removed


def assert_sidecar_free(root: str) -> None:
    """Raise :class:`SidecarAssertionError` if any sidecar remains under ``root``.

    The hard, final guard: scans the finished tree and raises if a single
    ``._*`` or ``.DS_Store`` entry is present, listing the offenders. Returns
    None when the tree is clean. Independently callable so task 1.5's property
    test can assert directly against a tree.
    """
    offenders = find_sidecars(root)
    if offenders:
        preview = ", ".join(offenders[:10])
        suffix = "" if len(offenders) <= 10 else f" (+{len(offenders) - 10} more)"
        raise SidecarAssertionError(
            f"image population left {len(offenders)} sidecar entr(ies) under "
            f"{root!r}: {preview}{suffix}"
        )


def _rsync_supports_no_xattrs() -> bool:
    """True iff the pinned rsync advertises ``--no-xattrs`` in its help/usage.

    GNU/samba rsync (3.x) lists ``--no-xattrs``; openrsync does not. Best-effort
    and tolerant of failure: any error running rsync yields False (treat as "no
    flag", relying on openrsync's xattrs-off default). ``--help`` and the usage
    text are both scanned because openrsync prints usage to stderr with a
    non-zero exit.
    """
    try:
        result = subprocess.run(
            [RSYNC_BIN, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    blob = f"{result.stdout or ''}\n{result.stderr or ''}"
    return _NO_XATTRS_FLAG in blob


def _build_rsync_argv(
    src: str, dst: str, extra_excludes: Optional[List[str]] = None
) -> List[str]:
    """Build the rsync argv for a sidecar-free copy of ``src`` into ``dst``.

    Always archive-mode (``-a``) with the two sidecar excludes. ``extra_excludes``
    are additional ``--exclude`` patterns appended after the sidecar ones — used
    by :func:`compose` to hold the per-family commit markers OUT of the bulk
    ChartData copy so they can be written last (Req 4.1). Appends
    ``--no-xattrs`` only when the available rsync supports it (GNU/samba rsync);
    on openrsync, extended attributes are off by default and the flag is
    omitted. Either path yields a copy that carries no resource forks.
    """
    argv = [RSYNC_BIN, "-a"]
    if _rsync_supports_no_xattrs():
        argv.append(_NO_XATTRS_FLAG)
    for pattern in _SIDECAR_EXCLUDES:
        argv.extend(["--exclude", pattern])
    for pattern in extra_excludes or ():
        argv.extend(["--exclude", pattern])
    argv.extend([src, dst])
    return argv


def populate(
    source_dir: str, dest_dir: str, extra_excludes: Optional[List[str]] = None
) -> None:
    """Populate ``dest_dir`` from ``source_dir`` with a sidecar-free guarantee.

    Executes the locked sidecar-free method documented in the module docstring:

      1. rsync copy of ``source_dir`` into ``dest_dir`` with extended-attribute
         copying off and ``._*`` / ``.DS_Store`` excluded (Req 3.1). The exact
         rsync flags are built by :func:`_build_rsync_argv` (see the module
         docstring's platform note on ``--no-xattrs`` vs openrsync).
      2. Strip any residual ``._*`` / ``.DS_Store`` from ``dest_dir`` (Req 3.2).
      3. Hard-assert ``dest_dir`` contains zero sidecars, raising
         :class:`SidecarAssertionError` otherwise (Req 3.3).

    ``source_dir`` and ``dest_dir`` are ordinary directories; the mounted image
    root is one such directory in production, but this function needs no device
    and is unit-testable against temp dirs. A trailing slash is forced on the
    source so rsync copies the *contents* of ``source_dir`` into ``dest_dir``
    rather than nesting ``source_dir`` under it.

    ``extra_excludes`` are additional rsync ``--exclude`` patterns on top of the
    sidecar excludes — :func:`compose` passes the commit-marker relative paths so
    the bulk ChartData copy omits them, letting them be written last (Req 4.1).

    Raises :class:`PopulateError` if the rsync copy itself fails, or
    :class:`SidecarAssertionError` if sidecars survive the copy + strip.
    """
    if not os.path.isdir(source_dir):
        raise PopulateError(f"populate: source directory does not exist: {source_dir!r}")
    os.makedirs(dest_dir, exist_ok=True)

    # Force a trailing slash on the source so rsync copies the contents of
    # source_dir INTO dest_dir (src/ -> dst) rather than creating dst/src.
    src = os.path.join(source_dir, "")
    dst = os.path.join(dest_dir, "")

    argv = _build_rsync_argv(src, dst, extra_excludes)

    logger.debug("populate: %s", argv)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=RSYNC_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise PopulateError(f"populate: rsync not found at {RSYNC_BIN}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PopulateError(
            f"populate: rsync timed out after {RSYNC_TIMEOUT_SECONDS}s"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PopulateError(f"populate: rsync failed to run: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:500]
        raise PopulateError(
            f"populate: rsync exited {result.returncode}: "
            f"{stderr or 'no rsync output'}"
        )

    # Step 2: mandatory strip of any residual sidecars the copy/mount left.
    strip_sidecars(dest_dir)

    # Step 3: hard assert the finished tree is sidecar-free (raises if not).
    assert_sidecar_free(dest_dir)
    logger.info("populate: %s -> %s complete, sidecar-free", source_dir, dest_dir)


# ---------------------------------------------------------------------------
# Image lifecycle — create (sized to slice), attach for population, detach
# (task 1.2)
# ---------------------------------------------------------------------------
#
# The composer builds a FAT32 image FILE on the local SSD sized to the exact
# byte length of the target drive's partition slice, so the eventual
# ``dd``-to-raw-slice write (task 7) is a one-to-one sequential transfer that
# preserves the slice geometry the EFIS expects. Two native tools do the work,
# pinned to absolute paths (mirroring mount_swap's MOUNT_MSDOS_BIN / DISKUTIL_BIN
# convention) so no binary is ever resolved off $PATH and no third-party
# dependency is introduced (Req 14.1):
#
#   * ``/sbin/newfs_msdos`` lays down a FAT32 filesystem (Req 2.2).
#   * ``/usr/bin/hdiutil`` attaches the image file as a local block device so it
#     can be formatted and then mounted for population, and detaches it again.
#
# Why hdiutil at all. ``newfs_msdos`` cannot format a plain file: it issues a
# geometry ioctl and fails on a regular file with "Cannot get partition offset".
# So the image file is attached as a raw block device first (``hdiutil attach
# -nomount -imagekey diskimage-class=CRawDiskImage``), newfs_msdos is run against
# that device node, then it is detached. ``CRawDiskImage`` means the device is a
# flat 1:1 view of the file, so the backing file's byte size is preserved exactly
# across the whole cycle — the invariant task 1.6's property test asserts
# (``byte_size`` == requested slice size, Req 2.1).
#
# Population (task 1.3) copies into the image through a real mount: the image is
# re-attached WITH a mount at an explicit app-chosen mountpoint, :func:`populate`
# copies the source tree in, and the image is detached. :func:`attach_for_population`
# is the context manager that yields the mount root and ALWAYS detaches in a
# ``finally`` so an attached image never leaks even if population raises.

# Absolute paths to the native image tools, pinned like mount_swap pins its
# privileged binaries. newfs_msdos formats FAT32; hdiutil attaches/detaches the
# image file as a local device. No third-party dependency (Req 14.1).
NEWFS_MSDOS_BIN = "/sbin/newfs_msdos"
HDIUTIL_BIN = "/usr/bin/hdiutil"

# hdiutil imagekey selecting a flat, raw 1:1 view of the backing file (no
# sparse/compressed container), so the device is exactly the file's bytes and
# the file size is preserved across attach/format/detach.
_RAW_DISKIMAGE_IMAGEKEY = "diskimage-class=CRawDiskImage"

# FAT type for the image filesystem — FAT32 is what the EFIS reads (Req 2.2).
_FAT_TYPE = "32"

# Ceilings so a wedged native tool cannot hang a caller forever.
HDIUTIL_TIMEOUT_SECONDS = 120.0
NEWFS_TIMEOUT_SECONDS = 600.0


# ---------------------------------------------------------------------------
# FAT volume label from the currency cycles (design "FAT volume label" DECISION)
# ---------------------------------------------------------------------------
#
# The hardware-harness DECISION (design.md, 2026-10-09) drops the old EFIS_#
# scheme and makes the FAT volume label carry BOTH currency cycles:
# ``C<charts>P<plates>`` (e.g. ``C2609P2610``), because charts/nav run a 56-day
# cycle while plates run a 28-day cycle and so differ in general. Device-type /
# identity stays in EFIS_DRIVE_ID.json, NOT the label.
#
# IMPORTANT — what ``valid_dates`` actually contains. The cycle numbers are NOT
# a 4-digit token inside ``valid_dates``; the 4-digit YYCC cycle lives only in
# the download-URL KEY of ``currency.load_cycle_metadata()``. The ``valid_dates``
# value is a prose date RANGE, e.g.
#   "Thursday, September 3, 2026 through Wednesday, October 28, 2026".
# So :func:`extract_cycle` parses the START date of the range and derives the
# YYCC cycle from the fixed 28-day cycle anchor below (a bare 4-digit token is
# only used as a fallback when there is no prose date, since the prose's only
# 4-digit tokens are the years and must not be mistaken for a cycle).
#
# Cycle anchor. The Seattle Avionics 28-day cycles are continuous; the first
# cycle whose start date falls in 2026 begins 2026-01-22 (verified against the
# stored metadata: 2607=>Jul 9, 2608=>Aug 6, 2609=>Sep 3, 2610=>Oct 1, each 28
# days apart). From any cycle start date we compute YY (the start year mod 100)
# and CC (the 1-based ordinal of that cycle within its start year).
_CYCLE_ANCHOR = datetime.date(2026, 1, 22)  # 2026 cycle 1 (YYCC = 2601)
_CYCLE_DAYS = 28

# Default FAT label when neither cycle is known — a compose-date stamp so the
# volume is still identifiable and never the "NO NAME" newfs_msdos default.
_LABEL_FALLBACK_PREFIX = "EFIS"

# FAT32 volume-label rules: uppercase, A-Z / 0-9 only, at most 11 characters.
_LABEL_MAX_LEN = 11
_LABEL_ALLOWED_RE = re.compile(r"[^A-Z0-9]")

# Month names (full) as they appear in the stored ``valid_dates`` prose.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _cycle_for_start_date(start: datetime.date) -> str:
    """Return the YYCC cycle string for a cycle whose first day is ``start``.

    YY is the two-digit start year; CC is the 1-based ordinal of the cycle
    within its start year, counting the continuous 28-day cycles anchored at
    :data:`_CYCLE_ANCHOR`. The date is snapped to its enclosing cycle boundary
    first, so a mid-range date (or a one-off off-by-a-day source) still maps to
    the right cycle.
    """
    n = (start - _CYCLE_ANCHOR).days // _CYCLE_DAYS
    cycle_start = _CYCLE_ANCHOR + datetime.timedelta(days=_CYCLE_DAYS * n)
    year = cycle_start.year
    # Ordinal within the year: find the first cycle index landing in `year`.
    first_in_year = n
    while True:
        candidate = _CYCLE_ANCHOR + datetime.timedelta(
            days=_CYCLE_DAYS * (first_in_year - 1)
        )
        if candidate.year != year:
            break
        first_in_year -= 1
    ordinal = n - first_in_year + 1
    return f"{year % 100:02d}{ordinal:02d}"


def _parse_start_date(valid_dates: str) -> Optional[datetime.date]:
    """Parse the START date out of a ``valid_dates`` prose range, or None.

    Handles the stored shape "<Weekday>, <Month> <D>, <YYYY> through ...": finds
    the first "<Month> <day>, <year>" triple and returns it as a date. Returns
    None when no such triple is present (so the caller degrades gracefully).
    """
    match = re.search(
        r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", valid_dates
    )
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return datetime.date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def extract_cycle(valid_dates: str) -> Optional[str]:
    """Extract a 4-digit YYCC cycle number from a ``valid_dates`` string.

    Robust to what the field ACTUALLY contains (see the module note above). The
    stored value is a prose date RANGE whose only 4-digit tokens are YEARS
    ("... 2026 through ... 2026"), so a naive 4-digit grab would wrongly return
    the year. Order of attempts:

      1. Parse the START date of the prose range and derive the YYCC cycle from
         the fixed 28-day cycle anchor. This is the real stored shape.
      2. Otherwise, if the string carries a bare 4-digit token whose last two
         digits are a plausible cycle ordinal (01..13), use it directly —
         future-proofing in case the field ever carries a literal YYCC cycle.
         The ordinal guard stops a stray YEAR (ending in e.g. "26") in
         date-like prose from being mistaken for a cycle.
      3. If neither works, return None so the label build degrades gracefully.

    Returns the cycle as a 4-character string (e.g. ``"2610"``) or None.
    """
    if not valid_dates:
        return None
    # 1. Derive from the start date of the prose range (the real stored shape).
    start = _parse_start_date(valid_dates)
    if start is not None:
        return _cycle_for_start_date(start)
    # 2. A standalone 4-digit YYCC token. Guard against grabbing a YEAR out of
    #    date-like prose: a real cycle's last two digits are the 1-based cycle
    #    ordinal (01..13, at most ~13 cycles/year), whereas a year ends in digits
    #    like "26"/"27". Only accept a token whose CC part is a plausible cycle
    #    ordinal, so "... 2026 ..." prose never yields a bogus "2026" cycle.
    token = re.search(r"(?<!\d)(\d{4})(?!\d)", valid_dates)
    if token:
        candidate = token.group(1)
        cc = int(candidate[2:])
        if 1 <= cc <= 13:
            return candidate
    # 3. Unparseable.
    return None


def _sanitize_label(label: str) -> str:
    """Sanitize ``label`` to the FAT32 rules: uppercase, [A-Z0-9], <=11 chars.

    Uppercases, strips every character outside A-Z / 0-9 (spaces, punctuation,
    lowercase after upcasing is a no-op), then truncates to 11 characters. The
    hard strip keeps ``newfs_msdos -v`` from rejecting an invalid label.
    """
    upper = (label or "").upper()
    cleaned = _LABEL_ALLOWED_RE.sub("", upper)
    return cleaned[:_LABEL_MAX_LEN]


def build_volume_label(
    charts_cycle: Optional[str],
    plates_cycle: Optional[str],
    *,
    now: Optional[datetime.date] = None,
) -> str:
    """Build the sanitized FAT volume label from the two currency cycles.

    Format ``C<charts>P<plates>`` (e.g. ``C2609P2610``). Degradation rules:

      * Both cycles known -> ``C<charts>P<plates>``.
      * Only one known -> just that component (``C2609`` or ``P2610``).
      * Neither known -> a compose-date fallback ``EFIS<YYMMDD>`` so the volume
        is still identifiable and never the ``newfs_msdos`` "NO NAME" default.

    The result is always sanitized (uppercase, [A-Z0-9], <=11 chars) so it is a
    legal FAT32 label. ``now`` is injectable for deterministic tests.
    """
    parts = ""
    if charts_cycle:
        parts += f"C{charts_cycle}"
    if plates_cycle:
        parts += f"P{plates_cycle}"
    if not parts:
        today = now or datetime.date.today()
        parts = f"{_LABEL_FALLBACK_PREFIX}{today:%y%m%d}"
    return _sanitize_label(parts)


class ImageCreateError(RuntimeError):
    """Raised when creating or formatting the local FAT32 image fails.

    Covers the backing-file allocation, the ``hdiutil`` attach/detach, and the
    ``newfs_msdos`` format — any failure to produce a usable, correctly-sized
    FAT32 image. Distinct from :class:`PopulateError` (the copy stage) and
    :class:`SidecarAssertionError` (sidecars survived), consistent with the
    module's typed-error style.
    """


@dataclass
class ComposeResult:
    """Result of composing the local FAT32 image (design Data Models).

    Fields:
      * ``image_path``: absolute path to the composed image file on the SSD.
      * ``image_sha256``: streamed sha256 of the finished image, computed via
        :func:`write_verifier.hash_image` — the SAME function the verifier uses
        on the raw readback, so the compose-side and verify-side hashes are
        guaranteed identical (design Property 3).
      * ``byte_size``: the image file's size in bytes; equals the requested
        target slice size (Req 2.1 / task 1.6 property).
      * ``families_included``: the component families populated into the image
        (e.g. "charts", "nav", "identity", "marker"); filled in by the
        population step (task 1.3). Empty for a freshly-created, unpopulated
        image.
      * ``composed_at``: ISO-8601 timestamp of when the result was produced.
    """

    image_path: str
    image_sha256: str
    byte_size: int
    families_included: List[str] = field(default_factory=list)
    composed_at: str = ""


def _run_tool(argv: List[str], timeout: float, what: str) -> subprocess.CompletedProcess:
    """Run a native image tool, raising :class:`ImageCreateError` on any failure.

    Centralises the subprocess invocation + error mapping for ``hdiutil`` and
    ``newfs_msdos`` so each call site reads cleanly. ``what`` is a short label
    used in error messages (e.g. "hdiutil attach"). A missing binary, a timeout,
    a spawn failure, or a non-zero exit all raise ImageCreateError with the
    tool's stderr (trimmed) attached.
    """
    logger.debug("%s: %s", what, argv)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ImageCreateError(f"{what}: binary not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ImageCreateError(f"{what}: timed out after {timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageCreateError(f"{what}: failed to run: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:500]
        raise ImageCreateError(
            f"{what}: exited {result.returncode}: {stderr or 'no output'}"
        )
    return result


def _allocate_backing_file(image_path: str, byte_size: int) -> None:
    """Create ``image_path`` as a file of EXACTLY ``byte_size`` bytes.

    The file is the backing store for the FAT32 image; its length must equal the
    target slice size so the composed ``byte_size`` matches the slice (Req 2.1).
    Uses :func:`os.truncate` on a freshly-created file to allocate the exact size
    (sparse where the filesystem supports it) without writing ``byte_size`` bytes
    of zeros. Any existing file at the path is replaced.
    """
    if byte_size <= 0:
        raise ImageCreateError(
            f"create_image: byte_size must be positive, got {byte_size}"
        )
    parent = os.path.dirname(os.path.abspath(image_path))
    os.makedirs(parent, exist_ok=True)
    # Truncate-to-size on a fresh file: O(1), exact length, sparse-friendly.
    with open(image_path, "wb") as handle:
        handle.truncate(byte_size)
    actual = os.path.getsize(image_path)
    if actual != byte_size:
        raise ImageCreateError(
            f"create_image: backing file is {actual} bytes, expected {byte_size}"
        )


def _attach_raw(image_path: str, *, nomount: bool, mountpoint: Optional[str] = None) -> str:
    """Attach ``image_path`` as a raw local device; return its ``/dev`` node.

    Attaches with the flat ``CRawDiskImage`` view so the device is a 1:1 image of
    the backing file (size preserved). ``nomount=True`` attaches the block device
    without mounting any filesystem (used to format a not-yet-FAT32 image);
    ``nomount=False`` mounts the filesystem at ``mountpoint`` (used for
    population). The device node is parsed from hdiutil's ``-plist`` output
    rather than scraped from a tab-separated line, so a volume name containing
    spaces (e.g. the default "NO NAME") never corrupts the parse.
    """
    argv = [
        HDIUTIL_BIN,
        "attach",
        "-plist",
        "-imagekey",
        _RAW_DISKIMAGE_IMAGEKEY,
    ]
    if nomount:
        argv.append("-nomount")
    if mountpoint is not None:
        argv.extend(["-mountpoint", mountpoint])
    argv.append(image_path)

    result = _run_tool(argv, HDIUTIL_TIMEOUT_SECONDS, "hdiutil attach")
    try:
        parsed = plistlib.loads((result.stdout or "").encode("utf-8"))
        entities = parsed["system-entities"]
    except (plistlib.InvalidFileException, KeyError, ValueError) as exc:
        raise ImageCreateError(
            f"hdiutil attach: could not parse device node from output: {exc}"
        ) from exc

    # The whole-disk entry is the one WITHOUT a mount-point when nomount, or the
    # one that carries the mount-point when mounted; in all cases the first
    # dev-entry is the attached disk. Prefer the entry whose dev-entry has no
    # trailing slice (the whole-disk node) so detach cleans up everything.
    dev_entries = [e.get("dev-entry") for e in entities if e.get("dev-entry")]
    if not dev_entries:
        raise ImageCreateError("hdiutil attach: no dev-entry in output")
    # Shortest dev path is the whole-disk node (e.g. /dev/disk5 vs /dev/disk5s1).
    dev_node = min(dev_entries, key=len)
    return dev_node


def _detach(dev_node: str) -> None:
    """Detach an attached image device, tolerating an already-detached node.

    Best-effort: a non-zero exit is logged but not raised, because detach runs
    in cleanup paths where the device may already be gone and failing there would
    mask the original error. A ``force`` detach is used so a transiently-busy
    device is still released.
    """
    argv = [HDIUTIL_BIN, "detach", dev_node, "-force"]
    logger.debug("hdiutil detach: %s", argv)
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=HDIUTIL_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("hdiutil detach %s failed to run: %s", dev_node, exc)
        return
    if result.returncode != 0:
        logger.warning(
            "hdiutil detach %s exited %d: %s",
            dev_node,
            result.returncode,
            (result.stderr or "").strip()[:200],
        )


def create_image(
    image_path: str, byte_size: int, *, volume_label: Optional[str] = None
) -> None:
    """Create a FAT32 image file at ``image_path`` sized to ``byte_size`` bytes.

    Realises Req 2.1 (size the image to the target slice byte length) and Req 2.2
    (create the filesystem as FAT32 with ``/sbin/newfs_msdos``). Steps:

      1. Allocate the backing file at EXACTLY ``byte_size`` bytes
         (:func:`_allocate_backing_file`).
      2. Attach it as a raw, unmounted block device
         (``hdiutil attach -nomount`` with the ``CRawDiskImage`` view) — required
         because ``newfs_msdos`` cannot format a plain file.
      3. Lay down FAT32 on the attached device node (``newfs_msdos -F 32``),
         setting the volume label via ``-v`` when ``volume_label`` is given.
      4. Detach the device.

    ``volume_label`` sets the FAT volume label (the hardware-harness DECISION's
    ``C<charts>P<plates>`` currency label). It is sanitized hard to the FAT32
    rules (uppercase, A-Z/0-9, <=11 chars) before being passed to
    ``newfs_msdos -v`` so an invalid label can never make the format fail; a
    label that sanitizes to empty is treated as "no label" (newfs_msdos default,
    preserving the pre-label behavior). ``None`` keeps the current behavior.

    The backing file's size is preserved across the cycle (the raw image is a 1:1
    view), so ``os.path.getsize(image_path)`` equals ``byte_size`` afterwards. On
    any failure the attached device is detached in a ``finally`` so nothing leaks.
    Raises :class:`ImageCreateError` on any step failure.
    """
    _allocate_backing_file(image_path, byte_size)

    dev_node = _attach_raw(image_path, nomount=True)
    try:
        argv = [NEWFS_MSDOS_BIN, "-F", _FAT_TYPE]
        if volume_label is not None:
            safe_label = _sanitize_label(volume_label)
            if safe_label:
                argv.extend(["-v", safe_label])
        argv.append(dev_node)
        _run_tool(argv, NEWFS_TIMEOUT_SECONDS, "newfs_msdos")
    finally:
        _detach(dev_node)

    final = os.path.getsize(image_path)
    if final != byte_size:
        raise ImageCreateError(
            f"create_image: image is {final} bytes after format, expected "
            f"{byte_size} (slice-size invariant violated)"
        )
    logger.info(
        "create_image: FAT32 image at %s sized to %d bytes", image_path, byte_size
    )


@contextmanager
def attach_for_population(image_path: str, mountpoint: str) -> Iterator[str]:
    """Attach ``image_path`` mounted at ``mountpoint``; yield it; always detach.

    Context manager for the population phase (task 1.3): attaches the already-
    formatted FAT32 image WITH a mount at ``mountpoint`` and yields that mount
    root, into which :func:`populate` copies the source tree. On exit — normal or
    exceptional — the image is ALWAYS detached in a ``finally`` so an attached
    image never leaks even if population raises. ``mountpoint`` is created if it
    does not exist.

    Yields the mount root path (equal to ``mountpoint``).
    """
    os.makedirs(mountpoint, exist_ok=True)
    dev_node = _attach_raw(image_path, nomount=False, mountpoint=mountpoint)
    try:
        yield mountpoint
    finally:
        _detach(dev_node)


def finalize_result(
    image_path: str,
    *,
    families_included: Optional[List[str]] = None,
) -> ComposeResult:
    """Build a :class:`ComposeResult` for the finished image at ``image_path``.

    Hashes the image with :func:`write_verifier.hash_image` — the exact function
    the verifier applies to the raw readback — so the compose-side hash and the
    verify-side hash are computed identically and are directly comparable (design
    Property 3). ``byte_size`` is read from the file; ``composed_at`` is the
    current UTC time in ISO-8601. ``families_included`` records which component
    families the population step put in (default empty for an unpopulated image).
    """
    byte_size = os.path.getsize(image_path)
    image_sha256 = write_verifier.hash_image(image_path)
    composed_at = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
    )
    return ComposeResult(
        image_path=os.path.abspath(image_path),
        image_sha256=image_sha256,
        byte_size=byte_size,
        families_included=list(families_included or []),
        composed_at=composed_at,
    )


# ---------------------------------------------------------------------------
# Top-level compose orchestration (task 1.3)
# ---------------------------------------------------------------------------
#
# compose() ties the pieces above into one drive-independent build: create the
# FAT32 image sized to the slice, attach it LOCALLY (via hdiutil — a local image
# mount, NOT a USB drive), populate from CURRENT AUTHORITATIVE SOURCES only, and
# write the per-family commit markers LAST so a partial compose can never leave
# an image that looks complete (Req 4.1).
#
# Drive-independence (design Property 5 / Req 1.6): every byte compose reads
# comes from the local chart/nav source under the configured usb_image_path, and
# every byte it writes goes into the local hdiutil-attached image. It touches NO
# /dev/disk USB node and NO /Volumes USB mount, and the identity is supplied by
# the caller (a known target drive's identity dict, or None to mint a fresh one)
# — it is NEVER read off an old drive. compose therefore succeeds with no drive
# attached, which task 1.4's property test asserts by mocking device access to
# raise and checking compose still completes.
#
# Current-source manifest (design "COMPOSITION SOURCING"), all under
# ``usb_image_path`` — the data manager's own current local copy:
#   * ChartData  <- usb_image_path/ChartData        (via populate())
#   * NAV.DB / NAV-proc.DB  <- usb_image_path/<name> (the nav check downloads
#                                                      these here, not a drive)
#   * EFIS_DRIVE_ID.json  <- the passed-in identity dict, or newly minted
#   * commit markers  <- copied LAST from usb_image_path/ChartData/...
#
# The markers reuse drive_updater.SCANNED_MARKER / PLATES_MARKER (imported, not
# hardcoded) so the composer and the updater agree on the sole currency source
# of truth by construction.

# Family labels recorded in ComposeResult.families_included.
_FAMILY_CHARTS = "charts"
_FAMILY_NAV = "nav"
_FAMILY_IDENTITY = "identity"
_FAMILY_MARKER = "marker"

# Subdirectory of usb_image_path holding the chart tree.
_CHARTDATA_DIRNAME = "ChartData"


class ComposeError(RuntimeError):
    """Raised when the top-level compose orchestration cannot complete.

    Signals a problem assembling the image from current sources — a missing
    chart source, a nav/marker file that is not present at its authoritative
    path, or an identity-write failure. Distinct from the lower-level
    :class:`ImageCreateError` / :class:`PopulateError` / :class:`SidecarAssertionError`,
    which compose lets propagate from the steps they guard, consistent with the
    module's typed-error style.
    """


def _cycles_from_metadata() -> tuple:
    """Return ``(charts_cycle, plates_cycle)`` from the current cycle metadata.

    Reuses the SAME source the "new image ready" notification uses
    (:func:`currency.load_cycle_metadata`, keyed by download URL with
    ``description`` + ``valid_dates``), so the composer and the notification
    agree by construction. Classification by description:

      * charts/nav cycle (56-day) = the SEC / LO / Scanned-Charts entry's cycle;
      * plates cycle (28-day)     = the Approach-Plates entry's cycle.

    Each cycle is extracted via :func:`extract_cycle`. Either side degrades to
    None when its entry is absent or its ``valid_dates`` is unparseable, so the
    caller (:func:`build_volume_label`) can fall back. Best-effort: any failure
    reading the metadata yields ``(None, None)``.
    """
    from efis_data_manager import currency

    charts_cycle: Optional[str] = None
    plates_cycle: Optional[str] = None
    try:
        metadata = currency.load_cycle_metadata()
    except Exception as exc:  # metadata is best-effort for labelling only
        logger.debug("compose: could not load cycle metadata for label: %s", exc)
        return (None, None)

    for entry in metadata.values():
        desc = str(entry.get("description", "")).lower()
        valid = entry.get("valid_dates")
        if not valid:
            continue
        cycle = extract_cycle(valid)
        if cycle is None:
            continue
        if "plate" in desc or "approach" in desc:
            # Prefer the newest plates cycle seen (lexically largest YYCC).
            if plates_cycle is None or cycle > plates_cycle:
                plates_cycle = cycle
        elif "sectional" in desc or "low altitude" in desc or "scanned" in desc:
            if charts_cycle is None or cycle > charts_cycle:
                charts_cycle = cycle

    return (charts_cycle, plates_cycle)


def _copy_file_into_image(src_file: str, dst_file: str) -> None:
    """Copy one file into the image, creating parent dirs; no sidecars carried.

    Used for the standalone nav files and the commit markers, which are copied
    individually (not through the ChartData tree rsync) so the markers can be the
    LAST bytes written.

    We deliberately do NOT use ``shutil.copy2`` here. ``copy2`` runs
    ``copystat`` after copying, which on macOS calls ``chflags(dst, st_flags)``.
    A FAT32 image mount (the real EFIS card filesystem) does not support BSD
    file flags, so that ``chflags`` raises ``OSError: [Errno 22] Invalid
    argument`` and aborts the whole compose — even though the file content was
    written fine. The headless tests missed this because plain temp dirs DO
    support file flags.

    Content is the hard requirement, so we copy bytes with ``shutil.copyfile``
    (content only, no metadata; it does not create AppleDouble sidecars, and the
    source lives on the local SSD with no resource forks). mtime preservation is
    only nice-to-have, so we attempt it best-effort via ``copystat`` and swallow
    any ``OSError`` the destination filesystem raises for unsupported metadata.
    """
    import shutil

    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    # CONTENT is mandatory; copyfile never touches metadata so it can't trip
    # the FAT32 chflags/copystat failure.
    shutil.copyfile(src_file, dst_file)
    # mtime (and other stat) preservation is best-effort: a filesystem like
    # FAT32 that rejects chflags/copystat must not abort the copy.
    try:
        shutil.copystat(src_file, dst_file)
    except OSError:
        pass


def compose(
    image_path: str,
    byte_size: int,
    *,
    usb_image_path: Optional[str] = None,
    identity: Optional[dict] = None,
    mountpoint: Optional[str] = None,
) -> ComposeResult:
    """Compose the local FAT32 image from current sources; markers written last.

    Drive-independent build (design Property 5 / Req 1.6): reads only from the
    local chart/nav source under ``usb_image_path`` and writes only into the
    local hdiutil-attached image. Touches no ``/dev/disk`` USB node and no
    ``/Volumes`` USB mount, and succeeds with no drive attached.

    Steps, in order:

      1. Create the FAT32 image sized to ``byte_size`` (:func:`create_image`,
         Req 2.1/2.2).
      2. Attach it locally for population (:func:`attach_for_population`) and,
         into the mounted image root:
         a. Populate ``ChartData`` from ``usb_image_path/ChartData`` via
            :func:`populate`, EXCLUDING the two commit markers so they are not
            written in the bulk copy (Req 1.2).
         b. Copy ``NAV.DB`` / ``NAV-proc.DB`` from ``usb_image_path`` — the
            current nav source the data manager downloads, NOT any drive
            (Req 1.3). A missing nav file is skipped (nav is optional for a
            charts-only compose); ``nav`` is only reported when at least one
            landed.
         c. Write ``EFIS_DRIVE_ID.json`` using ``identity`` when given (the
            target drive's own identity), else a newly minted identity — never
            read from an old drive (Req 1.4/1.5). Uses
            ``drive_updater.read_identity`` / ``write_identity``.
         d. Write the commit markers LAST (``drive_updater.SCANNED_MARKER`` /
            ``PLATES_MARKER``), copied from ``usb_image_path`` so they are the
            final content in the image (Req 4.1). Only markers whose source
            exists are copied.
      3. Return a :class:`ComposeResult` (:func:`finalize_result`) with
         ``families_included`` reflecting what was populated (e.g.
         ``["charts", "nav", "identity", "marker"]``).

    Args:
        image_path: Where to write the composed FAT32 image on the local SSD.
        byte_size: Target partition-slice byte length to size the image to.
        usb_image_path: Root of the current chart/nav source. Defaults to
            ``config.load_config()["usb_image_path"]``.
        identity: The target drive's identity dict when known; ``None`` mints a
            fresh identity for a drive being prepared. NEVER sourced from a drive.
        mountpoint: Where to mount the image for population. Defaults to a
            temp directory, created and removed around the attach.

    Returns:
        ComposeResult for the finished image.

    Raises:
        ComposeError: the chart source directory is missing, or an identity
            write failed.
        ImageCreateError / PopulateError / SidecarAssertionError: propagated
            from the underlying steps.
    """
    # Deferred imports: drive_updater pulls in config / mount_swap and is only
    # needed by the orchestration, not by the headless populate/lifecycle layer.
    from efis_data_manager import drive_updater
    from efis_data_manager.config import load_config

    if usb_image_path is None:
        usb_image_path = load_config()["usb_image_path"]

    chart_source = os.path.join(usb_image_path, _CHARTDATA_DIRNAME)
    if not os.path.isdir(chart_source):
        raise ComposeError(
            f"compose: chart source not found at {chart_source!r} "
            f"(usb_image_path={usb_image_path!r})"
        )

    # The two commit markers, relative to the ChartData root AND to the image
    # root, reused from drive_updater so composer and updater agree by
    # construction. SCANNED_MARKER = ChartData/ScannedCharts.sqlite,
    # PLATES_MARKER = ChartData/Plates/Plates.sqlite.
    markers = (drive_updater.SCANNED_MARKER, drive_updater.PLATES_MARKER)
    # Exclude the markers from the ChartData bulk copy, expressed relative to the
    # ChartData directory (populate copies ChartData's CONTENTS, so strip the
    # leading "ChartData/" component).
    chart_rel_marker_excludes = [
        os.path.relpath(m, _CHARTDATA_DIRNAME) for m in markers
    ]

    _own_mountpoint = mountpoint is None
    if _own_mountpoint:
        import tempfile

        mountpoint = tempfile.mkdtemp(prefix="efis_compose_mnt_")

    # Compute the FAT volume label from the current currency cycles (the
    # hardware-harness DECISION: C<charts>P<plates>, both cycles because plates
    # 28-day and charts/nav 56-day differ). Sourced from the SAME currency
    # metadata the "new image ready" notification uses. This same label is also
    # recorded in the identity below, so the composed image's identity carries
    # the REAL volume label — not the temp staging-mountpoint basename.
    charts_cycle, plates_cycle = _cycles_from_metadata()
    volume_label = build_volume_label(charts_cycle, plates_cycle)
    logger.info(
        "compose: volume label %s (charts=%s plates=%s)",
        volume_label, charts_cycle, plates_cycle,
    )

    families: List[str] = []
    try:
        # Step 1: create the FAT32 image sized to the slice, labelled with the
        # currency cycles.
        create_image(image_path, byte_size, volume_label=volume_label)

        # Step 2: attach locally and populate from current sources.
        with attach_for_population(image_path, mountpoint) as root:
            # 2a. ChartData bulk copy, markers held back for the last step.
            dst_chartdata = os.path.join(root, _CHARTDATA_DIRNAME)
            populate(
                chart_source,
                dst_chartdata,
                extra_excludes=chart_rel_marker_excludes,
            )
            families.append(_FAMILY_CHARTS)

            # 2b. Nav files from the CURRENT nav source (never a drive).
            nav_landed = False
            for nav_name in drive_updater.NAV_FILES:
                nav_src = os.path.join(usb_image_path, nav_name)
                if os.path.isfile(nav_src):
                    _copy_file_into_image(nav_src, os.path.join(root, nav_name))
                    nav_landed = True
            if nav_landed:
                families.append(_FAMILY_NAV)

            # 2c. Identity: the passed-in dict, or a freshly minted one. Never
            # read from an old drive — write_identity targets the image root.
            # When minting, pass the REAL volume label so the identity records
            # the C<charts>P<plates> currency label, NOT the temp staging
            # mountpoint basename (the identity-label-leak fix: without this,
            # _new_identity derived the label from the mountpoint, which for a
            # compose-into-temp-dir was "efis_compose_mnt_...").
            if identity is not None:
                drive_updater.write_identity(root, identity)
            else:
                drive_updater.write_identity(
                    root, drive_updater._new_identity(root, label=volume_label)
                )
            if drive_updater.read_identity(root) is None:
                raise ComposeError(
                    "compose: identity file was not written into the image"
                )
            families.append(_FAMILY_IDENTITY)

            # 2d. Commit markers LAST (Req 4.1). Copied individually, after the
            # bulk ChartData + nav + identity, so a partial compose never yields
            # an image carrying a complete-looking marker.
            marker_landed = False
            for marker_rel in markers:
                marker_src = os.path.join(usb_image_path, marker_rel)
                if os.path.isfile(marker_src):
                    _copy_file_into_image(
                        marker_src, os.path.join(root, marker_rel)
                    )
                    marker_landed = True
            if marker_landed:
                families.append(_FAMILY_MARKER)

            # The individually-copied files (nav, identity, markers) land AFTER
            # populate()'s own strip, and the FAT32 mount layer synthesises an
            # AppleDouble "._*" companion for each as it is written. Strip them
            # here, then hard-assert the whole image is sidecar-free before
            # detach (Req 3.2/3.3) — the same guarantee populate() makes for the
            # bulk ChartData copy, now extended over the last-written content.
            strip_sidecars(root)
            assert_sidecar_free(root)
    finally:
        if _own_mountpoint:
            try:
                os.rmdir(mountpoint)
            except OSError:
                pass

    logger.info(
        "compose: %s built from %s, families=%s", image_path, usb_image_path, families
    )
    return finalize_result(image_path, families_included=families)
