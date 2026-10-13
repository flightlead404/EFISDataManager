# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Manual hardware harness for the image-based drive write (task 12).

This is a **human-run, on-demand** harness that performs the real end-to-end
image-write path on physical hardware: compose a FAT32 image on the local SSD,
``dd`` it onto a drive's RAW partition slice ``/dev/rdiskNsN``, confirm the
write by raw-readback hash (no mount), and then (a human step) read the drive in
the actual GRT EFIS. It records the image-write timings and, when a baseline is
supplied, compares them against the current rsync+swap path.

**THIS HARNESS PERFORMS A REAL ``dd`` THAT ERASES THE TARGET SLICE.** It is
scoped to a disposable scratch drive (e.g. ``EFIS_3``). It is NEVER wired into
the live app, never runs at import, and is **excluded from CI** — exactly like
the existing :mod:`efis_data_manager.repro_harness`. The only CI-safe surface is
``--self-test``, a dry run over the pure gate/report logic that touches no
device and needs no root.

Why staged. The orchestrator runs this ONE STAGE AT A TIME and inspects between
stages, so each stage is a separately-callable function with no hidden coupling:

  1. :func:`inspect_target` — READ-ONLY ``diskutil info`` of the target slice
     (size, label, VolumeUUID, removable, FAT32, mountpoint, parent whole disk).
     Returns the facts the device guard will later match. No write.
  2. :func:`compose_image` — build the local FAT32 image sized to the ACTUAL
     slice byte size from step 1 (NOT a config default), via
     :func:`image_composer.compose`; time it; return the ``ComposeResult``. The
     ``image_sha256`` captured here is the hash the raw readback is compared
     against, so the image MUST NOT be re-attached/mounted after compose (that
     would mutate its bytes and break the readback match).
  3. :func:`unmount_slice` — ``diskutil unmount`` the FAT volume (a normal
     unmount, NOT an eject and NOT a privileged ``mount_msdos``) so ``dd`` can
     reach the raw slice.
  4. :func:`confirm_and_write` — the DESTRUCTIVE step. Calls
     :func:`image_writer.write`, which runs the full device guard (removable +
     FAT32 + size + label + VolumeUUID + explicit confirmation) and only then
     ``dd``'s to ``/dev/rdiskNsN`` and raw-readback-verifies. It REFUSES unless
     the caller passes ``confirmed=True`` AND the guard passes — a ``dd`` can
     never run by accident.
  5. :func:`evaluate_acceptance` / :func:`format_report` — PURE gate + report:
     guard passed, ``dd`` completed, ``WriteResult.status == "complete"``,
     verified True, plus dd throughput (bytes/sec) and total time, with an
     OPTIONAL speedup ratio vs. a supplied rsync+swap baseline. Pure (no device)
     so ``--self-test`` exercises it.

Integration seams (all reused unchanged — this module only CALLS them):

* composer — :func:`image_composer.compose` (+ ``ComposeResult``);
* writer — :func:`image_writer.write` (guard -> ``dd`` -> verify), which itself
  routes the privileged ``dd`` through :func:`mount_swap.run_privileged`;
* verifier — :func:`write_verifier.hash_image` (the compose-side hash the
  readback is compared against; ``image_writer.write`` runs the verify leg);
* device resolution — :func:`mount_swap.resolve_device_node` /
  :func:`mount_swap._device_info` for the read-only ``inspect_target`` facts.

The pure logic (acceptance gate, throughput calc, speedup, report formatting) is
factored out of the side-effecting device path so it is testable with no drive
present; :func:`main` exposes a ``--self-test`` dry run that exercises exactly
that logic.

Entry point: :func:`main` / ``python -m efis_data_manager.image_write_harness``.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Where acceptance artifacts are written. Matches the repro_harness convention
# (~/EFIS/Reports/); created on demand. Overridable on the CLI so a dry-run
# self-test can drop its artifact under ./temp/.
REPORTS_DIR = os.path.expanduser("~/EFIS/Reports")

# diskutil, used ONLY for the read-only slice inspection (inspect_target) and
# the normal (unprivileged) volume unmount (unmount_slice). Pinned absolute
# path, matching mount_swap.DISKUTIL_BIN. The destructive dd itself is NEVER run
# from this module directly — it is reached only through image_writer.write ->
# mount_swap.run_privileged's audited allow-list.
DISKUTIL_BIN = "/usr/sbin/diskutil"


# ---------------------------------------------------------------------------
# Stage 1: read-only target inspection (NO write)
# ---------------------------------------------------------------------------


def inspect_target(device_node: str) -> dict:
    """Read-only inspection of the target slice; returns the guard-relevant facts.

    Runs ``diskutil info`` on ``device_node`` (a ``/dev/diskNsN`` partition
    slice) via :func:`mount_swap._device_info` and distils the facts the device
    guard will later match, so the orchestrator can eyeball them BEFORE anything
    destructive happens:

      * ``device_node`` — the slice node as given;
      * ``slice_size_bytes`` — the raw slice byte length (``diskutil`` ``Size`` /
        ``IOKitSize``) that the image MUST be sized to and that the guard will
        require the image to equal (Req 2.1 / 7.3);
      * ``volume_label`` — ``VolumeName`` (the guard's expected label, Req 7.4);
      * ``volume_uuid`` — ``VolumeUUID``, the OS-level identity the guard matches
        (NOT the on-drive ``EFIS_DRIVE_ID.json`` — reading that would need a
        mount, Req 7.4 / design Property 1);
      * ``is_removable`` / ``is_fat32`` — whether the base guard checks will pass
        (Req 7.2);
      * ``mountpoint`` — current ``/Volumes`` mount, if any (what
        :func:`unmount_slice` will unmount so ``dd`` can reach the raw slice);
      * ``parent_whole_disk`` — the parent whole-disk node, surfaced so a human
        can confirm the slice belongs to the expected disposable drive and NOT
        write the whole-disk node (Req 2.3).

    Purely read-only and unprivileged — it mounts nothing and writes nothing.
    Returns a plain dict (JSON-serialisable) so the orchestrator can print it.
    Raises :class:`RuntimeError` if diskutil cannot describe the node, so an
    unverifiable target is surfaced rather than silently returned empty.
    """
    # Deferred import: keep module import clean / side-effect-free.
    from efis_data_manager import mount_swap

    info = mount_swap._device_info(device_node)
    if info is None:
        raise RuntimeError(
            f"inspect_target: diskutil could not describe {device_node!r} "
            "(unverifiable target — refusing to proceed)"
        )

    slice_size = None
    for key in ("Size", "IOKitSize"):
        value = info.get(key)
        if isinstance(value, int) and value > 0:
            slice_size = value
            break

    removable = (
        bool(info.get("RemovableMedia"))
        or bool(info.get("RemovableMediaOrExternalDevice"))
        or bool(info.get("Ejectable"))
    ) and info.get("Internal") is not True

    content = str(info.get("Content") or "")
    fs_name = str(info.get("FilesystemName") or "")
    is_fat32 = info.get("FilesystemType") == "msdos" and (
        "DOS_FAT_32" in content or "FAT32" in fs_name.upper()
    )

    facts = {
        "device_node": device_node,
        "slice_size_bytes": slice_size,
        "volume_label": info.get("VolumeName"),
        "volume_uuid": info.get("VolumeUUID"),
        "is_removable": removable,
        "is_fat32": bool(is_fat32),
        "mountpoint": info.get("MountPoint") or None,
        "parent_whole_disk": info.get("ParentWholeDisk"),
    }
    logger.info("inspect_target: %s", facts)
    return facts


# ---------------------------------------------------------------------------
# Stage 2: compose the image sized to the ACTUAL slice (local SSD; no device)
# ---------------------------------------------------------------------------


@dataclass
class ComposeTiming:
    """A :class:`ComposeResult` plus how long composition took.

    ``result`` is the composer's ``ComposeResult`` (carrying ``image_path``,
    ``image_sha256``, ``byte_size``, ``families_included``, ``composed_at``);
    ``elapsed_seconds`` is the wall time the compose took. The result is held
    as-is so the orchestrator can read ``image_sha256`` — the hash the raw
    readback is later compared against.
    """

    result: object
    elapsed_seconds: float


def compose_image(
    image_path: str,
    byte_size: int,
    usb_image_path: Optional[str] = None,
    identity: Optional[dict] = None,
) -> ComposeTiming:
    """Compose the local FAT32 image sized to the ACTUAL target slice; time it.

    Thin, timed wrapper over :func:`image_composer.compose`. CRITICAL contract:

      * ``byte_size`` MUST be the REAL slice byte size the caller read from
        :func:`inspect_target` (``slice_size_bytes``), NOT a config default. The
        image is sized to exactly this so the guard's slice-size check passes and
        the ``dd`` preserves the partition scheme (Req 2.1 / 7.3).
      * The returned ``ComposeResult.image_sha256`` is the hash the raw readback
        is compared against. **Do NOT re-attach or re-mount the image after
        compose** — any attach/mount can mutate FAT bytes (last-mount
        timestamps, dirty bit) and break the readback match. The orchestrator
        must hand the composed image straight to :func:`confirm_and_write`.

    Composition is drive-independent (design Property 5): it reads only the local
    chart/nav source under ``usb_image_path`` and writes only into the locally-
    attached image; it touches no ``/dev/disk`` USB node and no ``/Volumes`` USB
    mount, so it is safe to run with the target drive attached but is in no way
    dependent on it.

    Args:
      image_path: where to write the composed image on the local SSD.
      byte_size: the ACTUAL target slice size from :func:`inspect_target`.
      usb_image_path: root of the current chart/nav source; composer default
        (config ``usb_image_path``) when None.
      identity: the target drive's identity dict when known, else None to mint a
        fresh one — never sourced from a drive.

    Returns:
      A :class:`ComposeTiming` wrapping the ``ComposeResult`` and the elapsed
      wall time.
    """
    if not isinstance(byte_size, int) or byte_size <= 0:
        raise ValueError(
            "compose_image: byte_size must be the ACTUAL positive slice size "
            f"from inspect_target, got {byte_size!r}"
        )

    # Deferred import so the module imports cleanly / self-test needs no composer
    # side effects.
    from efis_data_manager import image_composer

    logger.info(
        "compose_image: composing %s sized to %d bytes (actual slice size)",
        image_path,
        byte_size,
    )
    started = time.monotonic()
    result = image_composer.compose(
        image_path,
        byte_size,
        usb_image_path=usb_image_path,
        identity=identity,
    )
    elapsed = max(0.0, time.monotonic() - started)
    logger.info(
        "compose_image: done in %.1fs (sha256=%s, byte_size=%d) — DO NOT "
        "re-mount the image before the dd or its bytes change",
        elapsed,
        getattr(result, "image_sha256", "?"),
        getattr(result, "byte_size", -1),
    )
    return ComposeTiming(result=result, elapsed_seconds=elapsed)


# ---------------------------------------------------------------------------
# Stage 3: unmount the FAT volume so dd can reach the raw slice (NOT eject)
# ---------------------------------------------------------------------------


def unmount_slice(device_node: str, mountpoint: Optional[str]) -> bool:
    """Unmount (NOT eject) the FAT volume so ``dd`` can access the raw slice.

    ``dd`` to ``/dev/rdiskNsN`` needs the slice NOT mounted — a mounted msdos
    volume holds the device busy and the kernel's cached FAT would also diverge
    from what we are about to write. This runs a NORMAL ``diskutil unmount``
    (prefers the ``mountpoint`` when known, else the ``device_node``), which
    detaches the filesystem but LEAVES THE DEVICE on the bus (an eject would
    remove it and we could not then ``dd`` the slice). It is a plain unprivileged
    unmount — NOT the privileged ``mount_msdos`` swap and NOT routed through
    :func:`mount_swap.run_privileged`.

    Returns True on a successful unmount (or when there was nothing mounted to
    unmount), False if the unmount failed — the orchestrator inspects the result
    and does NOT proceed to the destructive write on a False.
    """
    target = mountpoint or device_node
    if mountpoint is None:
        logger.info(
            "unmount_slice: no mountpoint recorded for %s; attempting unmount "
            "by device node",
            device_node,
        )
    cmd = [DISKUTIL_BIN, "unmount", target]
    logger.info("unmount_slice: %s", cmd)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("unmount_slice: failed to run diskutil unmount: %s", exc)
        return False
    if result.returncode != 0:
        logger.error(
            "unmount_slice: diskutil unmount %r failed (rc=%d): %s",
            target,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    logger.info("unmount_slice: %r unmounted (device left on the bus)", target)
    return True


# ---------------------------------------------------------------------------
# Stage 4: the DESTRUCTIVE write (guard -> dd -> verify), gated by confirmation
# ---------------------------------------------------------------------------


@dataclass
class WriteTiming:
    """A :class:`WriteResult` plus how long the write+verify took.

    ``result`` is :class:`image_writer.WriteResult` (``device_node``,
    ``bytes_written``, ``verified``, ``status``, ``error``); ``elapsed_seconds``
    is the wall time the whole guard -> ``dd`` -> readback-verify flow took,
    used to derive ``dd`` throughput in the acceptance report. ``refused`` is
    True when :func:`confirm_and_write` declined to even call the writer because
    the caller did not pass ``confirmed=True`` — in that case no ``dd`` ran.
    """

    result: object
    elapsed_seconds: float
    refused: bool = False


def confirm_and_write(
    image_path: str,
    device_node: str,
    expected_size_bytes: int,
    expected_label: Optional[str],
    expected_identity_id: Optional[str],
    *,
    confirmed: bool,
) -> WriteTiming:
    """DESTRUCTIVE: guard -> ``dd`` -> verify the composed image onto the slice.

    This is the one function in the harness that can erase the drive. It is
    double-gated:

      1. ``confirmed`` MUST be True. If it is not, this function REFUSES up
         front — it does NOT call the writer, no ``dd`` is issued, and it returns
         a :class:`WriteTiming` with ``refused=True`` and a ``status ==
         "incomplete"`` placeholder result. The CLI only sets ``confirmed=True``
         when BOTH ``--write`` and the explicit erase-confirmation flag are
         given, so a ``dd`` can never run by accident or at import.
      2. Even when ``confirmed`` is True, the actual ``dd`` is still gated by
         :func:`image_writer.write`'s full device guard (removable + FAT32 +
         slice size == ``expected_size_bytes`` + label == ``expected_label`` +
         VolumeUUID == ``expected_identity_id`` + confirmed). ANY guard mismatch
         makes the writer refuse and NO ``dd`` runs.

    So a ``dd`` happens IFF ``confirmed is True`` AND the guard passes. On
    success the writer also raw-reads the slice back and hash-compares it to the
    composed image (``WriteResult.verified`` / ``status == "complete"``). The
    whole write+verify is timed for the throughput figure.

    Args:
      image_path: the composed image from :func:`compose_image` (NOT re-mounted
        since compose — see that function's contract).
      device_node: the ``/dev/diskNsN`` slice target (from
        :func:`inspect_target`).
      expected_size_bytes: the ACTUAL slice size the guard must match.
      expected_label: the drive's expected ``VolumeName`` (guard-forwarded).
      expected_identity_id: the drive's expected OS-level ``VolumeUUID``.
      confirmed: explicit confirmation of THIS target; MUST be True to proceed.

    Returns:
      A :class:`WriteTiming` wrapping the :class:`WriteResult` and elapsed time
      (or a refusal placeholder when ``confirmed`` is not True).
    """
    if confirmed is not True:
        logger.warning(
            "confirm_and_write: REFUSING — confirmed is not True; no dd issued "
            "to %s, local image untouched",
            device_node,
        )
        # Build a refusal placeholder WriteResult without importing side effects
        # beyond the dataclass; mirrors the writer's "guard refused" shape.
        from efis_data_manager.image_writer import WriteResult

        placeholder = WriteResult(
            device_node=device_node,
            bytes_written=0,
            verified=False,
            status="incomplete",
            error="refused: explicit confirmation (confirmed=True) required; "
            "no dd issued",
        )
        return WriteTiming(result=placeholder, elapsed_seconds=0.0, refused=True)

    # Deferred import so the destructive writer is only pulled in when a real,
    # confirmed write is actually requested.
    from efis_data_manager import image_writer

    logger.warning(
        "confirm_and_write: CONFIRMED — handing %s -> %s to image_writer.write "
        "(device guard runs there BEFORE any dd)",
        image_path,
        device_node,
    )
    started = time.monotonic()
    result = image_writer.write(
        image_path,
        device_node,
        expected_size_bytes=expected_size_bytes,
        expected_label=expected_label,
        expected_identity_id=expected_identity_id,
        confirmed=True,
    )
    elapsed = max(0.0, time.monotonic() - started)
    logger.info(
        "confirm_and_write: writer returned status=%r verified=%s "
        "bytes_written=%d in %.1fs",
        getattr(result, "status", "?"),
        getattr(result, "verified", None),
        getattr(result, "bytes_written", -1),
        elapsed,
    )
    return WriteTiming(result=result, elapsed_seconds=elapsed, refused=False)


# ---------------------------------------------------------------------------
# Stage 5: PURE acceptance gate + report (no device — self-test exercises this)
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceInputs:
    """The measured facts the acceptance gate is computed from (all pure data).

    * ``guard_passed`` — did the device guard pass (i.e. the writer did NOT
      refuse the target before ``dd``)? Derived by the caller from the
      :class:`WriteResult` (a guard refusal yields status "incomplete" with a
      "device guard refused" error and ``bytes_written == 0``).
    * ``dd_completed`` — did the ``dd`` actually run to completion (bytes were
      written)?
    * ``status`` — the ``WriteResult.status`` ("complete" / "incomplete" /
      "aborted").
    * ``verified`` — the ``WriteResult.verified`` (raw readback hashed equal to
      the composed image).
    * ``bytes_written`` — image bytes written to the slice.
    * ``compose_seconds`` — wall time of the compose stage.
    * ``write_seconds`` — wall time of the guard -> ``dd`` -> verify stage.
    * ``baseline_seconds`` — OPTIONAL rsync+swap baseline total time to compare
      against; None means "no baseline supplied, just report image-write
      timings".
    """

    guard_passed: bool
    dd_completed: bool
    status: str
    verified: bool
    bytes_written: int
    compose_seconds: float
    write_seconds: float
    baseline_seconds: Optional[float] = None


@dataclass
class AcceptanceResult:
    """The pass/fail verdict plus the itemised reasons and timing metrics.

    ``passed`` is the AND of the acceptance conditions; ``reasons`` lists every
    condition with its individual pass/fail so a failing run says exactly which
    condition broke. ``metrics`` carries the derived throughput + timing figures
    (and the speedup ratio when a baseline was supplied).
    """

    passed: bool
    guard_passed: bool
    dd_completed: bool
    status_complete: bool
    verified: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)


def compute_metrics(inputs: AcceptanceInputs) -> Dict[str, float]:
    """Derive throughput + timing metrics from the measured facts (pure).

    Returns a dict with:

      * ``bytes_written`` / ``compose_seconds`` / ``write_seconds`` — echoed
        from the inputs;
      * ``total_seconds`` — compose + write;
      * ``dd_bytes_per_second`` — ``bytes_written / write_seconds`` (0.0 when the
        write time is 0 or nothing was written) — the headline ``dd`` throughput;
      * ``image_write_seconds`` — alias of ``total_seconds`` for the vs-baseline
        comparison (the image-write path's end-to-end cost);
      * ``baseline_seconds`` / ``speedup_ratio`` — ONLY present when a baseline
        was supplied; ``speedup_ratio = baseline_seconds / total_seconds`` (how
        many times faster image-write was; >1 means faster). Omitted entirely
        when no baseline is given (the task: "if none given, just report the
        image-write timings").

    Pure and side-effect-free so the self-test can exercise it with synthetic
    inputs.
    """
    write_seconds = max(0.0, inputs.write_seconds)
    compose_seconds = max(0.0, inputs.compose_seconds)
    total_seconds = compose_seconds + write_seconds
    bytes_written = max(0, inputs.bytes_written)

    dd_bps = bytes_written / write_seconds if write_seconds > 0 else 0.0

    metrics: Dict[str, float] = {
        "bytes_written": float(bytes_written),
        "compose_seconds": compose_seconds,
        "write_seconds": write_seconds,
        "total_seconds": total_seconds,
        "dd_bytes_per_second": dd_bps,
        "image_write_seconds": total_seconds,
    }

    if inputs.baseline_seconds is not None:
        baseline = max(0.0, float(inputs.baseline_seconds))
        metrics["baseline_seconds"] = baseline
        metrics["speedup_ratio"] = (
            baseline / total_seconds if total_seconds > 0 else 0.0
        )

    return metrics


def evaluate_acceptance(inputs: AcceptanceInputs) -> AcceptanceResult:
    """Compute the pass/fail acceptance gate from measured facts (pure).

    PASS iff ALL of:

      1. **guard passed** — the device guard permitted the write (the writer did
         not refuse the target);
      2. **``dd`` completed** — the ``dd`` ran and wrote bytes;
      3. **status complete** — ``WriteResult.status == "complete"``;
      4. **verified** — the raw readback hashed equal to the composed image.

    Pure and side-effect-free. Returns an :class:`AcceptanceResult` carrying the
    overall verdict, the component booleans, human-readable reasons, and the
    derived timing/throughput metrics (via :func:`compute_metrics`).
    """
    guard_passed = bool(inputs.guard_passed)
    dd_completed = bool(inputs.dd_completed)
    status_complete = inputs.status == "complete"
    verified = bool(inputs.verified)

    reasons: List[str] = []
    reasons.append(
        f"[{'PASS' if guard_passed else 'FAIL'}] device guard permitted the "
        "write (removable + FAT32 + size + label + identity + confirmed)"
        + ("" if guard_passed else " (the guard refused the target; no dd ran)")
    )
    reasons.append(
        f"[{'PASS' if dd_completed else 'FAIL'}] dd wrote the image to the raw "
        "slice"
        + ("" if dd_completed else " (no bytes were written)")
    )
    reasons.append(
        f"[{'PASS' if status_complete else 'FAIL'}] write status is 'complete'"
        + ("" if status_complete else f" (status={inputs.status!r})")
    )
    reasons.append(
        f"[{'PASS' if verified else 'FAIL'}] raw readback verified against the "
        "composed image hash"
        + ("" if verified else " (readback did not match — incomplete)")
    )

    passed = guard_passed and dd_completed and status_complete and verified
    return AcceptanceResult(
        passed=passed,
        guard_passed=guard_passed,
        dd_completed=dd_completed,
        status_complete=status_complete,
        verified=verified,
        reasons=reasons,
        metrics=compute_metrics(inputs),
    )


def acceptance_from_timings(
    compose: ComposeTiming,
    write: WriteTiming,
    baseline_seconds: Optional[float] = None,
) -> AcceptanceResult:
    """Build :class:`AcceptanceInputs` from the live stage results and score it.

    Bridges the side-effecting stage outputs (:class:`ComposeTiming` /
    :class:`WriteTiming`) into the PURE :func:`evaluate_acceptance`. The guard
    pass/fail is inferred from the :class:`WriteResult`: a guard refusal is the
    writer's "incomplete" status with a "device guard refused" error and zero
    bytes written, whereas any run that got past the guard either verified
    ("complete") or failed verification with bytes written. A harness-level
    refusal (``WriteTiming.refused``) is likewise not a guard pass.
    """
    result = write.result
    status = getattr(result, "status", "incomplete")
    bytes_written = int(getattr(result, "bytes_written", 0) or 0)
    verified = bool(getattr(result, "verified", False))
    error = getattr(result, "error", None) or ""

    guard_refused = (
        write.refused
        or "guard refused" in error.lower()
        or (status != "complete" and bytes_written == 0 and "guard" in error.lower())
    )
    guard_passed = not guard_refused
    dd_completed = bytes_written > 0

    inputs = AcceptanceInputs(
        guard_passed=guard_passed,
        dd_completed=dd_completed,
        status=status,
        verified=verified,
        bytes_written=bytes_written,
        compose_seconds=compose.elapsed_seconds,
        write_seconds=write.elapsed_seconds,
        baseline_seconds=baseline_seconds,
    )
    return evaluate_acceptance(inputs)


def _fmt_bytes(nbytes: float) -> str:
    """Render a byte count as a compact human string (B/KB/MB/GB)."""
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_report(
    result: AcceptanceResult,
    *,
    device_node: str = "",
    image_sha256: str = "",
    families_included: Optional[List[str]] = None,
    captured_at: Optional[str] = None,
) -> str:
    """Render an :class:`AcceptanceResult` as a human-readable report (pure).

    Pure (no I/O) so it is unit-testable. Leads with the PASS/FAIL verdict, then
    the acceptance gate itemisation, the composed-image facts, and the timing /
    throughput block (including the vs-baseline speedup when one was supplied).
    """
    m = result.metrics
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    lines: List[str] = []
    lines.append("# Image-based drive-write hardware acceptance report")
    lines.append("")
    lines.append(f"- **Result:** {'PASS' if result.passed else 'FAIL'}")
    lines.append(f"- **Target:** {device_node or '(n/a)'}")
    lines.append(f"- **Image sha256:** {image_sha256 or '(n/a)'}")
    lines.append(
        f"- **Families included:** "
        f"{', '.join(families_included or []) or '(n/a)'}"
    )
    lines.append(f"- **Captured (UTC):** {captured_at}")
    lines.append("")

    lines.append("## Acceptance gate")
    for reason in result.reasons:
        lines.append(f"- {reason}")
    lines.append("")

    lines.append("## Timing / throughput (Req 5.1, 6.1, 6.3, 2.3)")
    lines.append(f"- compose: {m.get('compose_seconds', 0.0):.1f} s")
    lines.append(f"- dd write + verify: {m.get('write_seconds', 0.0):.1f} s")
    lines.append(f"- total (compose + write): {m.get('total_seconds', 0.0):.1f} s")
    lines.append(f"- bytes written: {_fmt_bytes(m.get('bytes_written', 0.0))}")
    lines.append(
        f"- dd throughput: {_fmt_bytes(m.get('dd_bytes_per_second', 0.0))}/s"
    )
    if "baseline_seconds" in m:
        lines.append("")
        lines.append("## vs. current rsync+swap path")
        lines.append(f"- rsync+swap baseline: {m.get('baseline_seconds', 0.0):.1f} s")
        lines.append(f"- image-write total: {m.get('image_write_seconds', 0.0):.1f} s")
        ratio = m.get("speedup_ratio", 0.0)
        lines.append(
            f"- speedup: {ratio:.2f}x "
            f"({'faster' if ratio > 1 else 'NOT faster'})"
        )
    else:
        lines.append("")
        lines.append("## vs. current rsync+swap path")
        lines.append("- (no baseline supplied — image-write timings only)")
    lines.append("")

    return "\n".join(lines)


def write_report(
    result: AcceptanceResult,
    reports_dir: str = REPORTS_DIR,
    *,
    device_node: str = "",
    image_sha256: str = "",
    families_included: Optional[List[str]] = None,
) -> str:
    """Write the acceptance report artifact; return the path written.

    Creates ``reports_dir`` if needed and writes a timestamped markdown artifact
    (``image-write-acceptance-YYYYMMDD-HHMMSS.md``).
    """
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(reports_dir, f"image-write-acceptance-{stamp}.md")
    body = format_report(
        result,
        device_node=device_node,
        image_sha256=image_sha256,
        families_included=families_included,
    )
    with open(path, "w") as fh:
        fh.write(body)
    logger.info("wrote image-write acceptance report: %s", path)
    return path


# ---------------------------------------------------------------------------
# Dry-run self-test (pure logic only; no drive, no root, no device I/O)
# ---------------------------------------------------------------------------


def self_test(reports_dir: str) -> bool:
    """Exercise the harness's PURE logic with synthetic data; return True if OK.

    Verifies, with NO device and NO privileged calls:

      * :func:`compute_metrics` derives sane throughput + total time, and a
        speedup ratio only when a baseline is supplied;
      * :func:`evaluate_acceptance` PASSES a clean scenario and FAILS each broken
        one (guard refused / no dd / not-complete status / unverified);
      * :func:`acceptance_from_timings` maps synthetic ``ComposeResult`` /
        ``WriteResult`` into the gate correctly, including a harness refusal and
        a guard refusal;
      * :func:`format_report` / :func:`write_report` render + write the artifact.

    This is the only thing the task RUNS — it touches no device and needs no
    root, runnable safely on any machine.
    """
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {msg}")
        if not cond:
            ok = False

    # --- metrics: no baseline ---
    base_inputs = AcceptanceInputs(
        guard_passed=True,
        dd_completed=True,
        status="complete",
        verified=True,
        bytes_written=30_000_000_000,
        compose_seconds=120.0,
        write_seconds=300.0,
        baseline_seconds=None,
    )
    m = compute_metrics(base_inputs)
    check(abs(m["total_seconds"] - 420.0) < 1e-6, "metrics: total == compose+write")
    check(
        abs(m["dd_bytes_per_second"] - 100_000_000.0) < 1.0,
        "metrics: dd throughput == 100 MB/s",
    )
    check(
        "speedup_ratio" not in m,
        "metrics: no speedup when no baseline supplied",
    )

    # --- metrics: with baseline (speedup) ---
    m2 = compute_metrics(
        AcceptanceInputs(
            guard_passed=True,
            dd_completed=True,
            status="complete",
            verified=True,
            bytes_written=30_000_000_000,
            compose_seconds=120.0,
            write_seconds=300.0,
            baseline_seconds=1260.0,
        )
    )
    check(
        abs(m2.get("speedup_ratio", 0.0) - 3.0) < 1e-6,
        "metrics: speedup == baseline/total == 3.0x",
    )

    # --- gate: clean PASS ---
    passing = evaluate_acceptance(base_inputs)
    check(passing.passed, "gate: clean scenario PASSES")

    # --- gate: each broken condition FAILS ---
    check(
        not evaluate_acceptance(
            AcceptanceInputs(False, True, "complete", True, 1, 1.0, 1.0)
        ).passed,
        "gate: guard refusal FAILS the gate",
    )
    check(
        not evaluate_acceptance(
            AcceptanceInputs(True, False, "complete", True, 0, 1.0, 1.0)
        ).passed,
        "gate: no dd FAILS the gate",
    )
    check(
        not evaluate_acceptance(
            AcceptanceInputs(True, True, "incomplete", True, 1, 1.0, 1.0)
        ).passed,
        "gate: non-complete status FAILS the gate",
    )
    check(
        not evaluate_acceptance(
            AcceptanceInputs(True, True, "complete", False, 1, 1.0, 1.0)
        ).passed,
        "gate: unverified FAILS the gate",
    )

    # --- acceptance_from_timings: synthetic complete write ---
    @dataclass
    class _FakeCompose:
        image_path: str
        image_sha256: str
        byte_size: int
        families_included: list
        composed_at: str

    @dataclass
    class _FakeWrite:
        device_node: str
        bytes_written: int
        verified: bool
        status: str
        error: Optional[str] = None

    good_compose = ComposeTiming(
        result=_FakeCompose(
            image_path="/tmp/EFIS_USB.img",
            image_sha256="deadbeef",
            byte_size=30_000_000_000,
            families_included=["charts", "nav", "identity", "marker"],
            composed_at="2026-10-03T00:00:00+00:00",
        ),
        elapsed_seconds=120.0,
    )
    good_write = WriteTiming(
        result=_FakeWrite(
            device_node="/dev/disk9s1",
            bytes_written=30_000_000_000,
            verified=True,
            status="complete",
        ),
        elapsed_seconds=300.0,
    )
    good_result = acceptance_from_timings(good_compose, good_write, baseline_seconds=1260.0)
    check(good_result.passed, "from_timings: complete+verified -> PASS")
    check(
        abs(good_result.metrics.get("speedup_ratio", 0.0) - 3.0) < 1e-6,
        "from_timings: speedup carried through == 3.0x",
    )

    # --- acceptance_from_timings: harness refusal (confirmed not True) ---
    refused_write = WriteTiming(
        result=_FakeWrite(
            device_node="/dev/disk9s1",
            bytes_written=0,
            verified=False,
            status="incomplete",
            error="refused: explicit confirmation (confirmed=True) required",
        ),
        elapsed_seconds=0.0,
        refused=True,
    )
    refused_result = acceptance_from_timings(good_compose, refused_write)
    check(
        not refused_result.passed and not refused_result.guard_passed,
        "from_timings: harness refusal -> FAIL, guard not passed",
    )

    # --- acceptance_from_timings: guard refusal from the writer ---
    guard_refused_write = WriteTiming(
        result=_FakeWrite(
            device_node="/dev/disk9s1",
            bytes_written=0,
            verified=False,
            status="incomplete",
            error="device guard refused target: slice size mismatch",
        ),
        elapsed_seconds=0.0,
    )
    guard_refused_result = acceptance_from_timings(good_compose, guard_refused_write)
    check(
        not guard_refused_result.passed and not guard_refused_result.guard_passed,
        "from_timings: writer guard refusal -> FAIL, guard not passed",
    )

    # --- report formatting + write ---
    md = format_report(
        good_result,
        device_node="/dev/disk9s1",
        image_sha256="deadbeef",
        families_included=["charts", "nav", "identity", "marker"],
    )
    check("Result:** PASS" in md, "report: markdown leads with PASS verdict")
    check("dd throughput" in md, "report: markdown includes throughput")
    check("speedup" in md, "report: markdown includes vs-baseline speedup")
    md_path = write_report(
        good_result,
        reports_dir=reports_dir,
        device_node="/dev/disk9s1",
        image_sha256="deadbeef",
        families_included=["charts", "nav", "identity", "marker"],
    )
    check(os.path.isfile(md_path), f"report: markdown written to {md_path}")

    return ok


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

# The explicit erase-confirmation flag. Spelled out so a human must type the
# consequence; confirm_and_write only receives confirmed=True when this AND
# --write are both present.
ERASE_CONFIRM_FLAG = "--i-understand-this-erases-the-drive"

SAFETY_NOTICE = (
    "SAFETY: this harness performs a REAL dd that ERASES the target slice.\n"
    "It is scoped to a DISPOSABLE scratch drive, is NEVER wired into the app,\n"
    "and NEVER runs in CI. The only safe surface is --self-test (dry, no\n"
    "device). A destructive write requires BOTH --write and\n"
    f"{ERASE_CONFIRM_FLAG}; without the confirmation flag the write refuses.\n"
    "The orchestrator runs the stages one at a time under human confirmation."
)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI for the manual image-write hardware harness.

    Examples::

        # Safe dry run of the pure gate/metrics/report logic (NO device):
        ./venv/bin/python -m efis_data_manager.image_write_harness --self-test

        # Read-only inspect of a target slice (no write):
        ./venv/bin/python -m efis_data_manager.image_write_harness \\
            --device /dev/disk9s1 --inspect

        # Full DESTRUCTIVE run against a scratch drive (double-gated):
        ./venv/bin/python -m efis_data_manager.image_write_harness \\
            --device /dev/disk9s1 --write \\
            --i-understand-this-erases-the-drive

    With no flags, prints usage + the safety notice and exits without touching
    any device.
    """
    parser = argparse.ArgumentParser(
        prog="image_write_harness",
        description="MANUAL hardware harness for the image-based drive write: "
        "compose -> dd to /dev/rdiskNsN -> raw-readback verify, timed vs the "
        "rsync+swap path. Human-run, scratch-drive-only, excluded from CI. The "
        "destructive write is double-gated (--write + "
        f"{ERASE_CONFIRM_FLAG}).",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run the pure-logic dry-run self-test (NO device, no root). "
        "Writes a sample artifact under --reports-dir. The only CI-safe surface.",
    )
    parser.add_argument(
        "--device", default=None,
        help="Target /dev/diskNsN partition-slice node (a DISPOSABLE scratch "
        "drive). Required for --inspect or --write.",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="Read-only: print the diskutil facts the guard will match. No write.",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Run the DESTRUCTIVE compose -> unmount -> dd -> verify flow. Also "
        f"requires {ERASE_CONFIRM_FLAG}.",
    )
    parser.add_argument(
        ERASE_CONFIRM_FLAG, dest="erase_confirmed", action="store_true",
        help="Explicit confirmation that --write will ERASE the target slice. "
        "Without it, the write refuses.",
    )
    parser.add_argument(
        "--image", default=None,
        help="Path to compose the local FAT32 image at (for --write).",
    )
    parser.add_argument(
        "--usb-image-path", default=None,
        help="Root of the current chart/nav source; composer default when "
        "omitted.",
    )
    parser.add_argument(
        "--baseline-seconds", type=float, default=None,
        help="Optional rsync+swap baseline total time to compare against; "
        "omitted -> report image-write timings only.",
    )
    parser.add_argument(
        "--reports-dir", default=REPORTS_DIR,
        help=f"Where to write the artifact. Default: {REPORTS_DIR}",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.self_test:
        print("Running image_write_harness self-test (pure logic; no device)...")
        ok = self_test(args.reports_dir)
        print(f"Self-test: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    if args.inspect:
        if not args.device:
            parser.error("--inspect requires --device /dev/diskNsN")
        facts = inspect_target(args.device)
        print(json.dumps(facts, indent=2, default=str))
        return 0

    if args.write:
        if not args.device:
            parser.error("--write requires --device /dev/diskNsN")
        if not args.erase_confirmed:
            parser.error(
                f"--write is DESTRUCTIVE and requires {ERASE_CONFIRM_FLAG} to "
                "confirm; refusing"
            )
        if not args.image:
            parser.error("--write requires --image <path> to compose the image")

        # Stage 1: inspect (read-only) to learn the real slice size + guard facts.
        facts = inspect_target(args.device)
        print(json.dumps(facts, indent=2, default=str))
        slice_size = facts.get("slice_size_bytes")
        if not isinstance(slice_size, int) or slice_size <= 0:
            parser.error(
                "could not determine the target slice size; refusing to compose "
                "or write"
            )

        # Stage 2: compose the image sized to the ACTUAL slice size.
        compose = compose_image(
            args.image,
            slice_size,
            usb_image_path=args.usb_image_path,
        )

        # Stage 3: unmount so dd can reach the raw slice.
        if not unmount_slice(args.device, facts.get("mountpoint")):
            parser.error("could not unmount the target slice; refusing to dd")

        # Stage 4: the destructive write (confirmed=True — both gates satisfied).
        write = confirm_and_write(
            compose.result.image_path,
            args.device,
            expected_size_bytes=slice_size,
            expected_label=facts.get("volume_label"),
            expected_identity_id=facts.get("volume_uuid"),
            confirmed=True,
        )

        # Stage 5: score + report.
        result = acceptance_from_timings(
            compose, write, baseline_seconds=args.baseline_seconds
        )
        path = write_report(
            result,
            reports_dir=args.reports_dir,
            device_node=args.device,
            image_sha256=getattr(compose.result, "image_sha256", ""),
            families_included=list(
                getattr(compose.result, "families_included", []) or []
            ),
        )
        print(f"Result: {'PASS' if result.passed else 'FAIL'}")
        for reason in result.reasons:
            print(f"  {reason}")
        print(f"Report: {path}")
        return 0 if result.passed else 1

    # No actionable flags: print usage + the safety notice, touch nothing.
    parser.print_usage()
    print()
    print(SAFETY_NOTICE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
