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
import plistlib
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple

from efis_data_manager import mount_swap, write_verifier
from efis_data_manager.mount_swap import DD_BIN, DISKUTIL_BIN, PrivilegeError

logger = logging.getLogger(__name__)

# dd block size for the raw write and raw readback. 1 MiB matches the verifier's
# HASH_BLOCK_SIZE and is a good sequential-transfer balance for an SSD source /
# USB raw destination. Passed as a string because dd operands are string tokens.
DD_BLOCK_SIZE = "1m"

# Ceiling for a single privileged dd call. A whole-image write/read of a ~30 GB
# slice over USB can take a while, so this is generous compared to the mount
# timeouts; it exists only so a wedged dd cannot hang the menu-bar tool forever.
DD_TIMEOUT_SECONDS = 3600.0

# dd block size expressed in BYTES. Kept in lockstep with DD_BLOCK_SIZE ("1m"):
# the chunked segment math works in bs units, so the segment offsets/counts are
# computed against this byte figure and handed to dd as skip=/seek=/count= block
# counts.
DD_BLOCK_BYTES = 1024 * 1024

# Segment size for the CHUNKED raw write and raw readback, in bytes. Default
# 1 GiB. The real-hardware harness (design "Hardware harness findings
# (2026-10-09)") proved a single long raw dd is fragile on the SanDisk-through-
# Henge-dock USB bridge: the bridge RESETS mid-transfer (raw reads reset at
# ~3 GB, some writes at 2-9 GB with "Device not configured"), after which the
# device RE-ENUMERATES at a NEW /dev/diskN. Writing/reading in fixed segments
# makes a transient reset cost one segment (re-resolve + retry) rather than the
# whole 30 GB transfer. A whole number of DD_BLOCK_BYTES blocks so every segment
# boundary except the final image tail is block-aligned.
DD_CHUNK_BYTES = 1024 * 1024 * 1024

# Per-segment retry budget. On a bus reset the failed segment is re-driven after
# re-resolving the raw node (the device re-enumerates at a new /dev/diskN) and
# re-running the device guard against the re-enumerated node. If a segment still
# cannot be driven within this many tries the whole write/read is aborted — a
# bounded budget so a drive that has genuinely dropped off the bus does not loop
# forever.
DD_MAX_RETRIES = 5

# Base backoff (seconds) between segment retries. The actual sleep grows with
# the attempt number (DD_RETRY_BACKOFF_SECONDS * attempt) so a bridge that needs
# a moment to re-enumerate after a reset gets increasing time to come back
# before the next try, mirroring the mount-swap layer's settle-then-backoff
# shape (mount_swap.SETTLE_WAIT_SECONDS).
DD_RETRY_BACKOFF_SECONDS = 2.0


class ChunkedTransferError(RuntimeError):
    """Raised when a chunked raw write/read cannot complete within the budget.

    A segment reset (bus reset / "Device not configured" / re-enumeration) is
    normally recoverable: the raw node is re-resolved, the device guard is
    re-run against the re-enumerated node, and the segment is re-driven. This is
    raised only when a segment still cannot be driven after :data:`DD_MAX_RETRIES`
    tries, OR when the drive cannot be re-found after a reset within the retry
    budget. It signals the end-to-end :func:`write` to report the write
    ``"aborted"`` (dd leg) / the verify leg to report ``"incomplete"`` — the
    whole-image unit of work did not converge (Req 13), local state untouched.
    """


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


# ---------------------------------------------------------------------------
# Chunked raw write-with-resume and chunked raw readback (robustness fix)
# ---------------------------------------------------------------------------
#
# The real-hardware harness (design "Hardware harness findings (2026-10-09)")
# proved a single long raw dd is fragile on the SanDisk-through-Henge-dock USB
# bridge: the bridge RESETS mid-transfer and the device RE-ENUMERATES at a NEW
# /dev/diskN. The chunked path writes/reads the image in fixed DD_CHUNK_BYTES
# segments; a transient reset costs one segment, which is re-driven after
# re-resolving the raw node and re-running the device guard against the
# re-enumerated node. The WHOLE IMAGE remains the unit of work (Req 13):
# segments are an internal progress/resume mechanism, not per-family state.
#
# A node resolver is a zero-arg callable returning the CURRENT /dev/diskNsN node
# of the drive being written (the drive re-enumerates at a new node after a
# reset, so the resolver is re-queried on every retry — never cache the node).
# A guard re-check is a callable taking that node and raising if it is not the
# SAME drive (same slice size + VolumeUUID the guard matched). Both are injected
# so the chunked logic is headless-testable without a device or real diskutil.
NodeResolver = Callable[[], str]
GuardRecheck = Callable[[str], None]


def plan_segments(total_bytes: int) -> List[Tuple[int, int]]:
    """Split ``total_bytes`` into ``(byte_offset, byte_length)`` segments.

    Each segment maps to EXACTLY ONE dd invocation, so every segment is either:

      * a whole-block, block-ALIGNED chunk of up to :data:`DD_CHUNK_BYTES` bytes
        (expressed to dd as ``bs=1m skip/seek/count`` in block units); or
      * the final sub-block TAIL (``total_bytes`` not a block multiple),
        expressed as its OWN segment with ``bs=1`` byte-granular operands.

    Because :data:`DD_CHUNK_BYTES` is a whole number of :data:`DD_BLOCK_BYTES`
    blocks, every segment boundary is block-aligned except the start of the
    trailing tail segment — and that tail is driven in byte units, so a single
    dd invocation never needs to carry two operand triples. Splitting the tail
    into its own segment (rather than appending a second operand set to the last
    chunk's argv, which dd cannot execute) is what keeps each segment one dd.
    """
    if not isinstance(total_bytes, int) or total_bytes <= 0:
        raise ValueError(f"total_bytes must be a positive integer, got {total_bytes!r}")

    aligned_bytes = (total_bytes // DD_BLOCK_BYTES) * DD_BLOCK_BYTES
    tail = total_bytes - aligned_bytes

    segments: List[Tuple[int, int]] = []
    offset = 0
    while offset < aligned_bytes:
        length = min(DD_CHUNK_BYTES, aligned_bytes - offset)
        segments.append((offset, length))
        offset += length
    if tail:
        segments.append((aligned_bytes, tail))
    return segments


def _segment_dd_geometry(byte_offset: int, byte_length: int) -> Tuple[int, bool]:
    """Validate a segment and classify it as block-aligned or a byte tail.

    Returns ``(block_offset, aligned)`` where ``aligned`` is True iff BOTH the
    offset and the length are whole :data:`DD_BLOCK_BYTES` multiples (the
    ``bs=1m skip/seek/count`` fast path). A segment whose length is not a block
    multiple is the final sub-block TAIL (offset is still block-aligned — the
    tail starts at the aligned end), driven with ``bs=1`` byte operands.
    Raises ``ValueError`` on a non-positive length or a misaligned start, which
    :func:`plan_segments` never produces.
    """
    if byte_offset < 0 or byte_length <= 0:
        raise ValueError(
            f"invalid segment: offset={byte_offset!r} length={byte_length!r}"
        )
    block_offset, off_rem = divmod(byte_offset, DD_BLOCK_BYTES)
    if off_rem != 0:
        raise ValueError(
            f"segment byte_offset {byte_offset} is not a multiple of the dd "
            f"block size {DD_BLOCK_BYTES}"
        )
    aligned = (byte_length % DD_BLOCK_BYTES) == 0
    return block_offset, aligned


def build_dd_write_segment_argv(
    image_path: str, device_node: str, byte_offset: int, byte_length: int
) -> list:
    """Build the ``/bin/dd`` argv that writes ONE segment of the image.

    Produces an argv that copies the ``[byte_offset, byte_offset+byte_length)``
    region of ``image_path`` to the SAME region of the raw slice:
    ``dd if=<image> of=/dev/rdiskNsN bs=1m skip=<blk> seek=<blk> count=<blks>``.
    ``skip=`` positions the read on the input image and ``seek=`` positions the
    write on the output slice (both in ``bs`` units) so the segment lands at the
    right absolute offset. A block-aligned segment uses the ``bs=1m`` fast path;
    the final sub-block TAIL (:func:`plan_segments` emits it as its own segment)
    uses a single ``bs=1`` byte-granular copy so the segment length is honoured
    exactly. Each segment is EXACTLY ONE dd invocation (one operand triple).

    Only the argv is built here; the raw-slice safety check runs in
    :func:`mount_swap.run_privileged` (``_validate_dd_argv``) before execution,
    and the extra ``skip=``/``seek=``/``count=`` operands are tolerated there
    (that validator only constrains the ``of=``/``if=`` device operand).
    """
    raw_node = _raw_slice_node(device_node)
    block_offset, aligned = _segment_dd_geometry(byte_offset, byte_length)
    if aligned:
        full_blocks = byte_length // DD_BLOCK_BYTES
        return [
            DD_BIN,
            f"if={image_path}",
            f"of={raw_node}",
            f"bs={DD_BLOCK_SIZE}",
            f"skip={block_offset}",
            f"seek={block_offset}",
            f"count={full_blocks}",
        ]
    # Byte-granular tail segment: a single bs=1 copy at the absolute byte offset.
    return [
        DD_BIN,
        f"if={image_path}",
        f"of={raw_node}",
        f"bs=1",
        f"skip={byte_offset}",
        f"seek={byte_offset}",
        f"count={byte_length}",
    ]


def build_dd_read_segment_argv(
    device_node: str, byte_offset: int, byte_length: int
) -> list:
    """Build the ``/bin/dd`` argv that reads ONE segment of the slice to stdout.

    Produces ``dd if=/dev/rdiskNsN bs=1m skip=<blk> count=<blks>`` (plus a
    byte-granular tail for a final sub-block remainder) reading exactly the
    ``[byte_offset, byte_offset+byte_length)`` region of the raw slice to stdout.
    No ``of=`` — the bytes are captured from the privileged call's stdout and
    streamed through the verifier's hash. Mirrors
    :func:`build_dd_write_segment_argv`'s alignment contract.
    """
    raw_node = _raw_slice_node(device_node)
    block_offset, aligned = _segment_dd_geometry(byte_offset, byte_length)
    if aligned:
        full_blocks = byte_length // DD_BLOCK_BYTES
        return [
            DD_BIN,
            f"if={raw_node}",
            f"bs={DD_BLOCK_SIZE}",
            f"skip={block_offset}",
            f"count={full_blocks}",
        ]
    # Byte-granular tail segment read: a single ibs=1 read at the byte offset.
    return [
        DD_BIN,
        f"if={raw_node}",
        f"ibs=1",
        f"skip={byte_offset}",
        f"count={byte_length}",
    ]


def _is_bus_reset_error(exc: Optional[BaseException], returncode: Optional[int]) -> bool:
    """Heuristic: does this failure look like a transient USB bus reset?

    A bus reset shows up either as a non-zero dd exit (the device vanished
    mid-transfer) or as a raised exception whose text mentions the macOS kernel
    message "Device not configured" / "not configured" / an I/O error. We treat
    ANY segment failure as potentially transient and retry it (bounded), so this
    is used only for logging precision — the retry loop re-drives on any failure
    within the budget regardless.
    """
    if returncode is not None and returncode != 0:
        return True
    if exc is not None:
        text = str(exc).lower()
        return (
            "not configured" in text
            or "i/o error" in text
            or "input/output error" in text
            or "no such file" in text
        )
    return False


def _scan_node_by_identity(
    expected_identity_id: Optional[str], expected_size_bytes: int
) -> Optional[str]:
    """Scan diskutil for the /dev/diskNsN slice matching identity + size, or None.

    After a bus reset the drive re-enumerates at a NEW whole-disk node, so the
    stable way to re-find "the drive we are writing" is its OS-level
    ``VolumeUUID`` plus its slice byte size (the two facts the device guard
    matched). This lists the partition slices via ``diskutil list -plist`` and
    returns the first slice node whose ``diskutil info`` ``VolumeUUID`` equals
    ``expected_identity_id`` and whose slice size equals ``expected_size_bytes``.
    When no identity is supplied it matches on size alone (best effort).

    Unprivileged, read-only, and None-safe like
    :func:`mount_swap.resolve_device_node`: any diskutil error / unparseable
    plist yields None so the caller treats the drive as not-yet-re-found and
    keeps retrying within the budget rather than writing to a stale node.
    """
    try:
        result = subprocess.run(
            [DISKUTIL_BIN, "list", "-plist"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        listing = plistlib.loads(result.stdout)
    except Exception:
        return None

    candidate_nodes: List[str] = []
    for disk in listing.get("AllDisksAndPartitions", []):
        for part in disk.get("Partitions", []):
            ident = part.get("DeviceIdentifier")
            if isinstance(ident, str) and ident:
                candidate_nodes.append(f"/dev/{ident}")
    # Fallback: some diskutil versions expose a flat AllDisks list of identifiers.
    if not candidate_nodes:
        for ident in listing.get("AllDisks", []):
            if isinstance(ident, str) and "s" in ident.split("disk")[-1]:
                candidate_nodes.append(f"/dev/{ident}")

    for node in candidate_nodes:
        info = mount_swap._device_info(node)
        if info is None:
            continue
        if _slice_size_bytes(info) != expected_size_bytes:
            continue
        if expected_identity_id is not None:
            if info.get("VolumeUUID") != expected_identity_id:
                continue
        logger.info(
            "re-resolved drive after re-enumeration: %s (identity=%r size=%s)",
            node,
            expected_identity_id,
            expected_size_bytes,
        )
        return node
    return None


def _run_segment_with_resume(
    build_argv: Callable[[str], list],
    initial_node: str,
    resolve_node: NodeResolver,
    verify_guard: GuardRecheck,
    *,
    binary: bool,
    timeout: float,
    what: str,
) -> subprocess.CompletedProcess:
    """Drive ONE segment's dd through run_privileged with resume-on-reset.

    ``build_argv(node)`` builds the segment's dd argv for a given raw/slice node.
    The segment is attempted up to :data:`DD_MAX_RETRIES` times. On the first
    try the ``initial_node`` is used; on any failure (raised exception OR
    non-zero dd exit — both treated as a possible bus reset) the drive is
    RE-RESOLVED via ``resolve_node`` (the device re-enumerates at a new
    /dev/diskN after a reset, so the old node is never reused) and
    ``verify_guard(node)`` is re-run to confirm it is the SAME drive before the
    segment is re-driven. A short growing backoff separates tries.

    Returns the successful :class:`CompletedProcess`. Raises
    :class:`ChunkedTransferError` if the segment cannot be driven within the
    budget OR the drive cannot be re-found / re-guarded after a reset — the
    caller then aborts the whole transfer (whole-image unit of work, Req 13).
    """
    node = initial_node
    last_error: Optional[str] = None
    for attempt in range(1, DD_MAX_RETRIES + 1):
        exc: Optional[BaseException] = None
        result: Optional[subprocess.CompletedProcess] = None
        try:
            argv = build_argv(node)
            result = mount_swap.run_privileged(argv, timeout=timeout, binary=binary)
        except PrivilegeError:
            # A refused argv is NOT a transient reset — the target is wrong.
            # Do not retry; let it propagate so the write reports the refusal.
            raise
        except Exception as e:  # noqa: BLE001 - any dd failure is a retry candidate
            exc = e

        returncode = None if result is None else result.returncode
        if result is not None and returncode == 0:
            if attempt > 1:
                logger.info(
                    "%s: segment succeeded on attempt %s via node %s",
                    what,
                    attempt,
                    node,
                )
            return result

        reset_like = _is_bus_reset_error(exc, returncode)
        last_error = (
            str(exc)
            if exc is not None
            else f"dd exit {returncode}: {getattr(result, 'stderr', b'')!r}"
        )
        logger.warning(
            "%s: segment attempt %s/%s on %s failed (%s%s); will re-resolve and "
            "retry",
            what,
            attempt,
            DD_MAX_RETRIES,
            node,
            "bus-reset-like, " if reset_like else "",
            last_error,
        )

        if attempt == DD_MAX_RETRIES:
            break

        # Back off, then re-resolve the (re-enumerated) node and re-guard it
        # before the next try. The resolver returning None / raising, or the
        # guard raising, means we could not confirm the same drive — keep
        # retrying within the remaining budget rather than touch a stale node.
        time.sleep(DD_RETRY_BACKOFF_SECONDS * attempt)
        try:
            new_node = resolve_node()
            if not new_node:
                raise ChunkedTransferError(
                    "drive could not be re-found after reset (resolver returned "
                    "no node)"
                )
            verify_guard(new_node)
        except ChunkedTransferError as e:
            logger.warning("%s: re-resolve/guard failed: %s", what, e)
            continue
        except PrivilegeError as e:
            # The re-enumerated node did NOT pass the guard — it is not the same
            # drive. Keep retrying within budget; never write to it.
            logger.warning(
                "%s: re-resolved node did not pass the device guard: %s", what, e
            )
            continue
        except Exception as e:  # noqa: BLE001
            logger.warning("%s: re-resolve raised: %s", what, e)
            continue
        logger.info("%s: re-resolved drive to %s after reset", what, new_node)
        node = new_node

    raise ChunkedTransferError(
        f"{what}: segment did not converge after {DD_MAX_RETRIES} attempts; last "
        f"error: {last_error}"
    )


def chunked_dd_write(
    image_path: str,
    device_node: str,
    image_bytes: int,
    resolve_node: NodeResolver,
    verify_guard: GuardRecheck,
    timeout: float = DD_TIMEOUT_SECONDS,
) -> int:
    """Write the image to the raw slice in resumable segments; return bytes written.

    Splits the image into :data:`DD_CHUNK_BYTES` segments (:func:`plan_segments`)
    and writes each via :func:`build_dd_write_segment_argv` through the
    privileged choke point, re-driving any segment that resets after
    re-resolving the raw node and re-running the device guard
    (:func:`_run_segment_with_resume`). Returns ``image_bytes`` once every
    segment has landed. Raises :class:`ChunkedTransferError` if any segment
    cannot be driven within the retry budget — the whole-image write did not
    converge and the caller reports it aborted (Req 13), local state untouched.
    """
    segments = plan_segments(image_bytes)
    for index, (offset, length) in enumerate(segments, start=1):
        logger.info(
            "chunked_dd_write: segment %s/%s [%d, %d) (%d bytes)",
            index,
            len(segments),
            offset,
            offset + length,
            length,
        )
        _run_segment_with_resume(
            lambda node, _o=offset, _l=length: build_dd_write_segment_argv(
                image_path, node, _o, _l
            ),
            initial_node=device_node,
            resolve_node=resolve_node,
            verify_guard=verify_guard,
            binary=False,
            timeout=timeout,
            what=f"chunked_dd_write seg {index}/{len(segments)}",
        )
    logger.info("chunked_dd_write: all %s segments written", len(segments))
    return image_bytes


def iter_chunked_readback(
    device_node: str,
    image_bytes: int,
    resolve_node: NodeResolver,
    verify_guard: GuardRecheck,
    timeout: float = DD_TIMEOUT_SECONDS,
) -> Iterator[bytes]:
    """Yield the slice's bytes segment by segment, re-driving a segment that resets.

    A generator so the verifier can stream each segment's bytes through its hash
    without ever holding the whole (tens-of-GB) region in memory. Each segment
    is read via :func:`build_dd_read_segment_argv` through the privileged choke
    point in binary mode, with the same re-resolve-on-reset resume logic as the
    write (:func:`_run_segment_with_resume`). Raises :class:`ChunkedTransferError`
    if a segment cannot be read within the retry budget.
    """
    segments = plan_segments(image_bytes)
    for index, (offset, length) in enumerate(segments, start=1):
        logger.info(
            "iter_chunked_readback: segment %s/%s [%d, %d) (%d bytes)",
            index,
            len(segments),
            offset,
            offset + length,
            length,
        )
        result = _run_segment_with_resume(
            lambda node, _o=offset, _l=length: build_dd_read_segment_argv(
                node, _o, _l
            ),
            initial_node=device_node,
            resolve_node=resolve_node,
            verify_guard=verify_guard,
            binary=True,
            timeout=timeout,
            what=f"iter_chunked_readback seg {index}/{len(segments)}",
        )
        stdout = result.stdout
        if not isinstance(stdout, (bytes, bytearray)):
            raise TypeError(
                "chunked raw readback requires binary stdout from run_privileged; "
                f"got {type(stdout).__name__}"
            )
        yield bytes(stdout)


def chunked_raw_read_source(
    device_node: str,
    image_bytes: int,
    resolve_node: NodeResolver,
    verify_guard: GuardRecheck,
    timeout: float = DD_TIMEOUT_SECONDS,
) -> Callable[[], Iterator[bytes]]:
    """Return a zero-arg callable yielding a fresh segmented-readback generator.

    This is the chunked analogue of :func:`raw_read_source`. Where
    ``raw_read_source`` returns a callable producing the whole region as one
    ``bytes`` blob, this returns a callable producing an ITERATOR of segment
    byte-chunks (:func:`iter_chunked_readback`), which the verifier's chunked
    path streams through a single sha256 so the comparison matches
    :func:`write_verifier.hash_image` without loading the region whole. The read
    is deferred into the closure so the verifier controls WHEN the (slow,
    privileged) readback happens, matching the injectable-source contract.
    """

    def _source() -> Iterator[bytes]:
        return iter_chunked_readback(
            device_node,
            image_bytes,
            resolve_node=resolve_node,
            verify_guard=verify_guard,
            timeout=timeout,
        )

    return _source


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


def _production_resolve_node(
    expected_identity_id: Optional[str], expected_size_bytes: int
) -> NodeResolver:
    """Build the production node resolver closure (scans diskutil by identity).

    After a bus reset the drive re-enumerates at a NEW ``/dev/diskN``, so the
    resolver re-finds "the drive we are writing" by its OS-level ``VolumeUUID``
    + slice size (:func:`_scan_node_by_identity`). Raises
    :class:`ChunkedTransferError` when the drive cannot be re-found, which the
    segment resume loop treats as "not back yet" and keeps retrying within the
    budget. Injected into :func:`chunked_dd_write` / :func:`iter_chunked_readback`
    so tests can simulate a re-enumeration (node changing) without diskutil.
    """

    def _resolve() -> str:
        node = _scan_node_by_identity(expected_identity_id, expected_size_bytes)
        if not node:
            raise ChunkedTransferError(
                "drive could not be re-found after reset by identity "
                f"{expected_identity_id!r} / size {expected_size_bytes}"
            )
        return node

    return _resolve


def _production_verify_guard(
    expected_size_bytes: int,
    expected_label: Optional[str],
    expected_identity_id: Optional[str],
) -> GuardRecheck:
    """Build the production guard-recheck closure (re-runs the full device guard).

    After the node is re-resolved post-reset, the SAME-drive invariant is
    confirmed by re-running :func:`device_guard` against the re-enumerated node
    with the same expectations (slice size + label + VolumeUUID) and
    ``confirmed=True`` (the user already confirmed THIS physical drive before the
    write began; the re-enumerated node is the same drive, so the confirmation
    carries). A mismatch raises :class:`DeviceGuardError` (a ``PrivilegeError``)
    which the resume loop treats as "not the same drive — do not write to it".
    """

    def _recheck(node: str) -> None:
        device_guard(
            node,
            expected_size_bytes=expected_size_bytes,
            expected_label=expected_label,
            expected_identity_id=expected_identity_id,
            confirmed=True,
        )

    return _recheck


def write(
    image_path: str,
    device_node: str,
    expected_size_bytes: Optional[int] = None,
    expected_label: Optional[str] = None,
    expected_identity_id: Optional[str] = None,
    confirmed: bool = False,
    timeout: float = DD_TIMEOUT_SECONDS,
    resolve_node: Optional[NodeResolver] = None,
    verify_guard: Optional[GuardRecheck] = None,
) -> WriteResult:
    """Write a composed image to a drive's raw slice, end to end (Task 7).

    Runs the full guard -> CHUNKED ``dd`` -> CHUNKED verify flow and returns a
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
      2. **CHUNKED raw ``dd`` write with resume** (:func:`chunked_dd_write`,
         Req 5.1/5.3). The image is written to the raw slice in fixed
         :data:`DD_CHUNK_BYTES` segments through the privileged choke point; a
         segment that hits a transient USB bus reset is re-driven after
         re-resolving the re-enumerated node and re-running the device guard. If
         a segment cannot converge within :data:`DD_MAX_RETRIES`
         (a :class:`ChunkedTransferError`), the write is reported ``"aborted"``
         with the local state untouched (Req 5.4). A privileged refusal of a
         segment argv (:class:`PrivilegeError`) is reported ``"incomplete"``.
      3. **CHUNKED raw-readback verify** (:func:`chunked_raw_read_source` +
         :func:`write_verifier.verify_chunked`, Req 6.1). The written region is
         read straight back (no mount) IN THE SAME SEGMENTS — a segment that
         resets is re-driven — and the whole region's streamed hash is compared
         to the composed image hash. A mismatch / short read / unrecoverable
         reset is reported INCOMPLETE (Req 6.4 / 13.2).

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

    Resume model (robustness fix). The chunked write/read re-resolve the raw
    node after a reset via ``resolve_node`` and confirm the same drive via
    ``verify_guard``. Both default to production closures
    (:func:`_production_resolve_node` scans ``diskutil`` by VolumeUUID + size;
    :func:`_production_verify_guard` re-runs :func:`device_guard`); tests inject
    fakes to simulate a re-enumeration (node changing from /dev/disk20s1 to
    /dev/disk22s1) without a device.

    Unit of work (Req 13). The whole image is the unit of work — segments are an
    internal progress/resume mechanism, not per-family state. A guard refusal, an
    aborted write, or a verify failure leaves the local image unchanged; recovery
    is simply calling :func:`write` again (re-write + re-verify the whole image).

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
      timeout: per privileged ``dd`` call ceiling (per segment, write and read).
      resolve_node: optional injected re-resolver for the re-enumerated node;
        defaults to the diskutil-scanning production closure.
      verify_guard: optional injected same-drive guard re-check; defaults to the
        production closure that re-runs :func:`device_guard`.

    Returns:
      A :class:`WriteResult`. ``status == "complete"`` IFF the guard passed, the
      chunked ``dd`` wrote every segment, and the chunked readback verified.
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

    # Build the production resume closures (re-resolve the re-enumerated node by
    # identity + size; re-run the guard against it) unless the caller injected
    # fakes for testing. The guard already matched this drive's VolumeUUID/size,
    # so the resolver keys off those stable facts.
    if resolve_node is None:
        resolve_node = _production_resolve_node(
            guard_info.identity_id or expected_identity_id, image_bytes
        )
    if verify_guard is None:
        verify_guard = _production_verify_guard(
            image_bytes, expected_label, expected_identity_id
        )

    # --- Step 2: CHUNKED raw dd write with resume via the privileged choke point.
    try:
        chunked_dd_write(
            image_path,
            device_node,
            image_bytes,
            resolve_node=resolve_node,
            verify_guard=verify_guard,
            timeout=timeout,
        )
    except PrivilegeError as exc:
        # A segment write argv was refused by the privileged choke point (e.g. a
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
        # A segment could not converge within the retry budget, OR dd failed /
        # was interrupted (USB pulled, device gone). Report aborted; the local
        # image/state are untouched (Req 5.4) and recovery is a whole-image
        # re-write (Req 13).
        logger.warning(
            "write: chunked dd write to %s failed/did not converge; local image "
            "untouched: %s",
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

    # --- Step 3: CHUNKED raw-readback verify (no mount). Mismatch/short read/
    # --- unrecoverable reset -> incomplete.
    readback = chunked_raw_read_source(
        device_node,
        image_bytes,
        resolve_node=resolve_node,
        verify_guard=verify_guard,
        timeout=timeout,
    )
    try:
        verified = write_verifier.verify_chunked(image_path, readback)
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
