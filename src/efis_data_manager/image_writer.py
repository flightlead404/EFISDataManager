# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Image-based drive write: the destructive-write device guard.

This module is the highest-priority safety gate of the image-based-drive-write
feature. Before the Writer is ever allowed to issue a raw ``dd`` to a device
node, :func:`device_guard` must pass. It extends the mount-swap layer's existing
:func:`mount_swap.validate_device_node` (``/dev/diskNsN`` syntactic form +
removable + FAT32 via ``diskutil``) with the additional destructive-operation
checks the design calls for (design Property 4 — Destructive-write safety):

  * the target *slice* byte size equals the expected image size,
  * the target drive's volume label / identity equals the expected drive,
  * an explicit confirmation flag has been set for this specific target.

Any mismatch — or any check that cannot be verified (``diskutil`` unavailable) —
RAISES rather than returning, so the Writer never writes to an unverified or
mismatched node. The raised type is :class:`DeviceGuardError`, a subclass of
:class:`mount_swap.PrivilegeError`, so the existing refusal contract ("a refused
privileged operation raises ``PrivilegeError`` and nothing runs") holds for
callers that already catch ``PrivilegeError`` — while a caller that wants to
distinguish a destructive-guard refusal from a plain argv refusal still can.

The guard reuses — never duplicates — ``mount_swap.validate_device_node`` and
reads device facts from the same ``diskutil info -plist`` plumbing
(``mount_swap._device_info``), so it is headless-testable by mocking
``_device_info`` exactly like ``tests/test_mount_swap_privileged.py``. The guard
performs NO mount of the target drive (design Property 1): every fact it needs
(size, label, identity) comes from ``diskutil info`` on the device node, not
from reading files on a mounted volume.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from efis_data_manager import mount_swap, write_verifier
from efis_data_manager.mount_swap import DD_BIN, PrivilegeError

logger = logging.getLogger(__name__)

# dd block size for the raw write and raw readback. 1 MiB matches the verifier's
# HASH_BLOCK_SIZE and is a good sequential-transfer balance for an SSD source /
# USB raw destination. Passed as a string because dd operands are string tokens.
DD_BLOCK_SIZE = "1m"

# Ceiling for a single privileged dd call. A whole-image write/read of a ~30 GB
# slice over USB can take a while, so this is generous compared to the mount
# timeouts; it exists only so a wedged dd cannot hang the menu-bar tool forever.
DD_TIMEOUT_SECONDS = 3600.0


def _raw_slice_node(device_node: str) -> str:
    """Return the RAW slice node (/dev/rdiskNsN) for a /dev/diskNsN node.

    The device guard and the rest of the app address the slice by its non-raw
    node (/dev/diskNsN, the form diskutil understands). dd, however, must target
    the unbuffered RAW character device (/dev/rdiskNsN). This maps diskN -> rdiskN
    by inserting the leading ``r`` after ``/dev/``; an already-raw node is passed
    through unchanged so callers may hand in either form.
    """
    if device_node.startswith("/dev/rdisk"):
        return device_node
    if device_node.startswith("/dev/disk"):
        return "/dev/r" + device_node[len("/dev/") :]
    # Anything else is not a node this module should be turning into a dd target;
    # return as-is so the downstream run_privileged raw-slice guard refuses it.
    return device_node


def build_dd_write_argv(image_path: str, device_node: str) -> list:
    """Build the privileged ``/bin/dd`` argv that writes the image to the slice.

    Produces ``[/bin/dd, if=<image_path>, of=/dev/rdiskNsN, bs=1m]`` — the image
    file is the input, the drive's RAW partition slice is the output. The device
    node is mapped to its raw form via :func:`_raw_slice_node`; the actual
    raw-slice safety check happens in :func:`mount_swap.run_privileged`
    (``_validate_dd_argv``) before anything executes, so a non-raw or whole-disk
    target is refused there. This only constructs the argv — it does not run it.
    """
    raw_node = _raw_slice_node(device_node)
    return [DD_BIN, f"if={image_path}", f"of={raw_node}", f"bs={DD_BLOCK_SIZE}"]


def build_dd_read_argv(device_node: str, length_bytes: int) -> list:
    """Build the privileged ``/bin/dd`` argv that reads the slice region back.

    Produces ``[/bin/dd, if=/dev/rdiskNsN, bs=1m, count=<blocks>, iflag/...]``-
    style argv reading ``length_bytes`` from the drive's RAW partition slice to
    stdout (no ``of=`` — the bytes are captured from the privileged call's
    stdout and handed to the verifier). ``length_bytes`` is the composed image
    size so the readback covers exactly the written region. Only the argv is
    built here; the raw-slice guard in :func:`mount_swap.run_privileged` vets the
    ``if=`` target before execution.
    """
    if not isinstance(length_bytes, int) or length_bytes <= 0:
        raise ValueError(
            f"length_bytes must be a positive integer, got {length_bytes!r}"
        )
    raw_node = _raw_slice_node(device_node)
    # Read in whole DD_BLOCK_SIZE (1 MiB) blocks plus a trailing partial block so
    # the readback length equals the image length exactly regardless of whether
    # the image size is a multiple of the block size.
    block_bytes = 1024 * 1024  # keep in lockstep with DD_BLOCK_SIZE ("1m")
    full_blocks, remainder = divmod(length_bytes, block_bytes)
    argv = [DD_BIN, f"if={raw_node}"]
    if full_blocks:
        argv += [f"bs={DD_BLOCK_SIZE}", f"count={full_blocks}"]
    if remainder:
        # A second logical read for the tail; expressed as a separate 1-byte-bs
        # count so no bytes beyond the image region are read. When the image is a
        # clean multiple of the block size this branch is skipped.
        argv += [f"ibs=1", f"skip={full_blocks * block_bytes}", f"count={remainder}"]
    return argv


def dd_write_image(
    image_path: str, device_node: str, timeout: float = DD_TIMEOUT_SECONDS
) -> "mount_swap.subprocess.CompletedProcess":
    """Route a raw ``dd`` image write through the privileged choke point.

    Thin wrapper: builds the write argv (:func:`build_dd_write_argv`) and hands
    it to :func:`mount_swap.run_privileged`, the single audited privilege-
    injection point. Validation (raw-slice-only target) runs there BEFORE
    execution, so a bad target raises :class:`PrivilegeError` and nothing is
    written. The end-to-end guard -> write -> verify flow (Task 7) composes this
    with the device guard and the verifier; this is the write leg only.
    """
    argv = build_dd_write_argv(image_path, device_node)
    return mount_swap.run_privileged(argv, timeout=timeout)


def raw_read_source(
    device_node: str, length_bytes: int, timeout: float = DD_TIMEOUT_SECONDS
):
    """Return a zero-arg callable that raw-reads the slice region as ``bytes``.

    The verifier (:mod:`efis_data_manager.write_verifier`) accepts a bytes-
    returning callable as its readback source; this builds exactly that. Calling
    the returned closure runs ``/bin/dd if=/dev/rdiskNsN ...`` through
    :func:`mount_swap.run_privileged` and returns the ``length_bytes`` of device
    content read from the slice. Hashing is NOT done here — the verifier hashes
    the returned bytes against the composed image hash — so this helper only
    moves bytes off the raw device through the privileged choke point.

    The read is deferred into the closure so the verifier controls WHEN the
    (slow, privileged) readback happens, matching its injectable-source contract.
    """

    def _read() -> bytes:
        argv = build_dd_read_argv(device_node, length_bytes)
        # The readback is BINARY device content, so the privileged call is made
        # in binary mode (binary=True): run_privileged then captures stdout as
        # raw bytes rather than text, so arbitrary device bytes are not corrupted
        # by a text decode (Task 7, Decision A). The isinstance check below stays
        # as a defensive guard — on the happy path stdout is already bytes and it
        # no longer triggers.
        result = mount_swap.run_privileged(argv, timeout=timeout, binary=True)
        stdout = result.stdout
        if isinstance(stdout, (bytes, bytearray)):
            return bytes(stdout)
        raise TypeError(
            "raw readback requires binary stdout from run_privileged; got "
            f"{type(stdout).__name__}. The dd raw-read path is captured as bytes "
            "via run_privileged(..., binary=True); a text-mode stdout here would "
            "corrupt the readback bytes."
        )

    return _read


class DeviceGuardError(PrivilegeError):
    """Raised when the destructive-write device guard refuses a target node.

    A subclass of :class:`mount_swap.PrivilegeError` so the existing refusal
    contract is preserved: any caller that already treats a ``PrivilegeError``
    as "refused, nothing was written" handles a guard refusal correctly without
    change. The subclass exists only so a caller that wants to tell a
    destructive-guard refusal apart from a plain argv refusal can catch the more
    specific type.
    """


@dataclass
class DeviceGuardInfo:
    """The facts the destructive guard checked for a target device node.

    Mirrors the design's Data Models entry. Produced only for a node that has
    PASSED the full guard; every field reflects a verified, matching value:

      * ``device_node`` — the validated ``/dev/diskNsN`` partition-slice node.
      * ``is_removable`` — True; the node passed the removable check.
      * ``is_fat32`` — True; the node passed the FAT32 check.
      * ``slice_size_bytes`` — the raw partition-slice byte length that ``dd``
        will target (``diskutil`` ``Size``), confirmed equal to the expected
        image size.
      * ``volume_label`` — the drive's ``diskutil`` ``VolumeName``, confirmed
        equal to the expected label (when an expected label was supplied).
      * ``identity_id`` — the drive's OS-level ``VolumeUUID`` read from
        ``diskutil`` (NOT the on-drive ``EFIS_DRIVE_ID.json``, which would
        require a mount), confirmed equal to the expected identity (when an
        expected identity was supplied).
    """

    device_node: str
    is_removable: bool
    is_fat32: bool
    slice_size_bytes: int
    volume_label: Optional[str]
    identity_id: Optional[str]


def _slice_size_bytes(info: dict) -> Optional[int]:
    """Return the raw partition-slice byte length from a diskutil info dict.

    The image is ``dd``'d to the raw slice node ``/dev/rdiskNsN`` and is sized
    to the byte length of that slice (Req 2.1), so the quantity the guard must
    match is the *slice* size — ``diskutil``'s ``Size`` key (equivalently
    ``IOKitSize``), the full media/slice byte length — NOT ``VolumeSize`` /
    ``TotalSize``, which report the mounted filesystem's usable capacity and are
    smaller than the slice. Returns None if neither key is a usable integer so
    an unverifiable size is refused rather than guessed.
    """
    for key in ("Size", "IOKitSize"):
        value = info.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def device_guard(
    device_node: str,
    expected_size_bytes: int,
    expected_label: Optional[str] = None,
    expected_identity_id: Optional[str] = None,
    confirmed: bool = False,
) -> DeviceGuardInfo:
    """Destructive-write guard for a raw ``dd`` target; raises on any failure.

    This is the gate Req 7 requires before the Writer issues a ``dd``. It:

      1. Reuses :func:`mount_swap.validate_device_node` for the base checks —
         ``/dev/diskNsN`` syntactic form, removable, and FAT32 via ``diskutil``
         (Req 7.2). That call already raises :class:`PrivilegeError` if the node
         is not a removable FAT32 slice or cannot be verified.
      2. Requires an explicit ``confirmed`` flag for this specific target
         (Req 7.5) — an unconfirmed call is refused before any device facts are
         even trusted.
      3. Re-reads the device's ``diskutil info`` and confirms the *slice* byte
         size equals ``expected_size_bytes`` (Req 7.3 / 2.4).
      4. Confirms the volume label equals ``expected_label`` and/or the
         OS-level identity (``VolumeUUID``) equals ``expected_identity_id``
         when those expectations are supplied (Req 7.4).

    Any mismatch OR any value that cannot be verified (``diskutil`` info
    unavailable, size key missing) RAISES :class:`DeviceGuardError` (a
    ``PrivilegeError``) and returns nothing (Req 7.6). On success it returns a
    :class:`DeviceGuardInfo` recording the verified facts.

    No mount of the target drive is performed (design Property 1): size, label,
    and identity all come from ``diskutil info`` on the device node.

    Args:
      device_node: the ``/dev/diskNsN`` partition-slice node to guard.
      expected_size_bytes: the composed image byte size the slice must equal.
      expected_label: the drive's expected ``VolumeName``; skipped if None.
      expected_identity_id: the drive's expected ``VolumeUUID``; skipped if None.
      confirmed: explicit user confirmation of THIS target; must be True.
    """
    # Step 1: reuse the existing base checks. Raises PrivilegeError (which the
    # Writer already treats as "refused, nothing written") on a bad/unverifiable
    # node. We do NOT duplicate the /dev/diskNsN + removable + FAT32 logic.
    mount_swap.validate_device_node(device_node)

    # Step 2: explicit confirmation of this specific target (Req 7.5). Checked
    # before trusting any device facts so an unconfirmed write is refused up
    # front regardless of whether the rest would match.
    if confirmed is not True:
        raise DeviceGuardError(
            f"refusing destructive write: target {device_node!r} has not been "
            "explicitly confirmed"
        )

    # Validate the expected size is itself usable; a non-positive expectation
    # can never be matched against a real slice and signals a caller bug.
    if not isinstance(expected_size_bytes, int) or expected_size_bytes <= 0:
        raise DeviceGuardError(
            "refusing destructive write: expected_size_bytes must be a positive "
            f"integer, got {expected_size_bytes!r}"
        )

    # Re-read device facts for the destructive checks. validate_device_node
    # already proved _device_info returns a removable-FAT32 dict for this node,
    # but we re-read to extract size/label/identity; an unverifiable read here
    # is refused rather than assumed.
    info = mount_swap._device_info(device_node)
    if info is None:
        raise DeviceGuardError(
            f"refusing destructive write: could not verify {device_node!r} "
            "(diskutil info unavailable)"
        )

    # Step 3: slice size must equal the expected image size (Req 7.3 / 2.4).
    slice_size = _slice_size_bytes(info)
    if slice_size is None:
        raise DeviceGuardError(
            f"refusing destructive write: could not determine the slice size of "
            f"{device_node!r} from diskutil info"
        )
    if slice_size != expected_size_bytes:
        raise DeviceGuardError(
            f"refusing destructive write: {device_node!r} slice size "
            f"{slice_size} bytes != expected image size {expected_size_bytes} "
            "bytes"
        )

    volume_label = info.get("VolumeName")
    identity_id = info.get("VolumeUUID")

    # Step 4: label / identity must match the expected drive (Req 7.4). Each
    # expectation is only enforced when supplied, but an expectation that cannot
    # be verified (the corresponding diskutil key missing) is a refusal, not a
    # silent pass.
    if expected_label is not None:
        if not volume_label:
            raise DeviceGuardError(
                f"refusing destructive write: {device_node!r} has no readable "
                f"volume label to match expected {expected_label!r}"
            )
        if volume_label != expected_label:
            raise DeviceGuardError(
                f"refusing destructive write: {device_node!r} volume label "
                f"{volume_label!r} != expected {expected_label!r}"
            )

    if expected_identity_id is not None:
        if not identity_id:
            raise DeviceGuardError(
                f"refusing destructive write: {device_node!r} has no readable "
                f"identity (VolumeUUID) to match expected "
                f"{expected_identity_id!r}"
            )
        if identity_id != expected_identity_id:
            raise DeviceGuardError(
                f"refusing destructive write: {device_node!r} identity "
                f"{identity_id!r} != expected {expected_identity_id!r}"
            )

    logger.info(
        "device_guard: %s passed (size=%s label=%r identity=%r)",
        device_node,
        slice_size,
        volume_label,
        identity_id,
    )
    return DeviceGuardInfo(
        device_node=device_node,
        is_removable=True,
        is_fat32=True,
        slice_size_bytes=slice_size,
        volume_label=volume_label if isinstance(volume_label, str) else None,
        identity_id=identity_id if isinstance(identity_id, str) else None,
    )


@dataclass
class WriteResult:
    """Outcome of an end-to-end image write (design Data Models).

    Produced by :func:`write`. Mirrors the design's ``WriteResult`` shape:

      * ``device_node`` — the ``/dev/diskNsN`` partition-slice node targeted.
      * ``bytes_written`` — the number of image bytes written to the slice. Set
        to the composed image size on a write that reached ``dd``; ``0`` when
        the guard refused before any ``dd`` ran.
      * ``verified`` — True IFF the raw readback hash matched the composed image
        hash (design Property 3). False on any guard refusal or verify failure.
      * ``status`` — one of:
          - ``"complete"``   : guard passed, ``dd`` ran, and readback verified;
          - ``"incomplete"`` : the write ran but could not be verified (readback
            mismatch / short read), OR the guard refused the target. In both
            cases the LOCAL image and composer state are untouched and recovery
            is a whole-image re-write + re-verify (Req 13);
          - ``"aborted"``    : the ``dd`` itself failed to run / was interrupted
            before completion (Req 5.4) — again, local state untouched.
      * ``error`` — a human-readable reason when the status is not
        ``"complete"``; ``None`` on success.
    """

    device_node: str
    bytes_written: int
    verified: bool
    status: str
    error: Optional[str] = None


def write(
    image_path: str,
    device_node: str,
    expected_size_bytes: Optional[int] = None,
    expected_label: Optional[str] = None,
    expected_identity_id: Optional[str] = None,
    confirmed: bool = False,
    timeout: float = DD_TIMEOUT_SECONDS,
) -> WriteResult:
    """Write a composed image to a drive's raw slice, end to end (Task 7).

    Runs the full guard -> ``dd`` -> verify flow and returns a
    :class:`WriteResult`. The sequence, in order:

      1. **Device guard FIRST** (:func:`device_guard`, Req 7.1). The guard
         confirms the OS-level facts WITHOUT mounting the drive (design
         Property 1): ``/dev/diskNsN`` form + removable + FAT32, the slice byte
         size equals ``expected_size_bytes``, the volume label equals
         ``expected_label`` (when supplied), the OS-level ``VolumeUUID`` equals
         ``expected_identity_id`` (when supplied), and the target was explicitly
         ``confirmed``. On ANY guard refusal (a :class:`PrivilegeError` /
         :class:`DeviceGuardError`) the write is reported INCOMPLETE, NO ``dd``
         is issued, and the local image / composer state is left untouched
         (Req 13.1/13.2) — ``bytes_written == 0``.
      2. **Raw ``dd`` write** (:func:`dd_write_image`, Req 5.1/5.3) to the raw
         slice via the privileged choke point. A ``dd`` that fails to run / is
         interrupted yields status ``"aborted"`` with the local state untouched
         (Req 5.4).
      3. **Raw-readback verify** (:func:`raw_read_source` + :func:`verify`,
         Req 6.1). The written region is read straight back (no mount) and its
         hash compared to the composed image hash. A mismatch / short read is
         reported INCOMPLETE (Req 6.4 / 13.2).

    Identity model (Decision B). The guard matches the drive's OS-level
    ``VolumeUUID`` (via ``diskutil``), passed here as ``expected_identity_id`` —
    NOT the on-drive application identity ``EFIS_DRIVE_ID.json`` (reading that
    would require mounting the drive, violating design Property 1). The
    APP-LEVEL identity is already baked INTO the image by the composer
    (``compose``, Task 1.3) and is confirmed POST-write by the raw-readback hash
    (the verifier) rather than by the guard: the on-drive app identity rides in
    the verified image bytes. So the guard confirms *which physical slice* we
    are about to overwrite (VolumeUUID, no mount); the verifier confirms *the
    exact bytes landed*, app identity included.

    Unit of work (Req 13). The whole image is the unit of work — there is no
    per-family write state. A guard refusal or a verify failure leaves the local
    image unchanged; recovery is simply calling :func:`write` again (re-write +
    re-verify the whole image).

    Args:
      image_path: the composed FAT32 image on the local SSD to write.
      device_node: the ``/dev/diskNsN`` partition-slice target.
      expected_size_bytes: the composed image byte size the slice must equal
        (``ComposeResult.byte_size``). Defaults to the image file's own size
        when not supplied; the guard's size check still applies either way.
      expected_label: the drive's expected ``VolumeName`` (guard-forwarded).
      expected_identity_id: the drive's expected OS-level ``VolumeUUID``
        (guard-forwarded; NOT the on-drive app identity — see Decision B above).
      confirmed: explicit user confirmation of THIS target; the guard refuses
        unless True.
      timeout: per privileged ``dd`` call ceiling (write and readback each).

    Returns:
      A :class:`WriteResult`. ``status == "complete"`` IFF the guard passed, the
      ``dd`` ran, and the readback verified.
    """
    # Default the expected size to the image file's own byte length when the
    # caller did not pass one; the guard's slice-size check still applies.
    if expected_size_bytes is None:
        expected_size_bytes = os.path.getsize(image_path)

    # --- Step 1: device guard FIRST. On refusal, do NOT dd; local state intact.
    try:
        guard_info = device_guard(
            device_node,
            expected_size_bytes=expected_size_bytes,
            expected_label=expected_label,
            expected_identity_id=expected_identity_id,
            confirmed=confirmed,
        )
    except PrivilegeError as exc:
        logger.warning(
            "write: device guard refused %s; no dd issued, local image "
            "untouched: %s",
            device_node,
            exc,
        )
        return WriteResult(
            device_node=device_node,
            bytes_written=0,
            verified=False,
            status="incomplete",
            error=f"device guard refused target: {exc}",
        )

    # The guard confirmed the slice size equals the expected image size, so the
    # byte count the whole-image dd writes is that size.
    image_bytes = guard_info.slice_size_bytes

    # --- Step 2: raw dd write to the slice via the privileged choke point.
    try:
        dd_write_image(image_path, device_node, timeout=timeout)
    except PrivilegeError as exc:
        # The write argv was refused by the privileged choke point (e.g. a
        # non-raw / whole-disk target slipped past). Nothing was written.
        logger.warning(
            "write: privileged dd write refused for %s; local image untouched: %s",
            device_node,
            exc,
        )
        return WriteResult(
            device_node=device_node,
            bytes_written=0,
            verified=False,
            status="incomplete",
            error=f"privileged dd write refused: {exc}",
        )
    except Exception as exc:
        # dd failed to run or was interrupted (USB pulled, non-zero exit raised
        # by the backend). Report aborted; the local image/state are untouched
        # (Req 5.4) and recovery is a whole-image re-write.
        logger.warning(
            "write: dd write to %s failed/interrupted; local image untouched: %s",
            device_node,
            exc,
        )
        return WriteResult(
            device_node=device_node,
            bytes_written=0,
            verified=False,
            status="aborted",
            error=f"dd write failed or was interrupted: {exc}",
        )

    # --- Step 3: raw-readback verify (no mount). Mismatch/short read -> incomplete.
    readback = raw_read_source(device_node, image_bytes, timeout=timeout)
    try:
        verified = write_verifier.verify(image_path, readback)
    except Exception as exc:
        logger.warning(
            "write: raw readback verify errored for %s; treating as incomplete: %s",
            device_node,
            exc,
        )
        return WriteResult(
            device_node=device_node,
            bytes_written=image_bytes,
            verified=False,
            status="incomplete",
            error=f"raw readback verification errored: {exc}",
        )

    if verified:
        logger.info(
            "write: %s complete and verified (%d bytes)", device_node, image_bytes
        )
        return WriteResult(
            device_node=device_node,
            bytes_written=image_bytes,
            verified=True,
            status="complete",
            error=None,
        )

    logger.warning(
        "write: %s wrote %d bytes but readback did NOT verify; re-write + "
        "re-verify the whole image (Req 13)",
        device_node,
        image_bytes,
    )
    return WriteResult(
        device_node=device_node,
        bytes_written=image_bytes,
        verified=False,
        status="incomplete",
        error="raw readback hash did not match composed image; write incomplete",
    )
