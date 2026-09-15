# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mount-swap layer for the chart-sync stall fix.

For the write window only, a managed FAT32 USB drive is unmounted from its
FSKit ``/Volumes/`` mount and remounted via ``/sbin/mount_msdos`` (the
confirmed non-stalling in-kernel msdosfs path) at a private app-owned
mountpoint, then robustly restored to FSKit afterward.

Every privileged operation funnels through the single audited
:func:`run_privileged` shim so the privilege backend is swappable (Phase 1:
narrow ``sudoers.d`` grant; Phase 2: signed ``SMAppService`` helper) with no
change to callers.

Alongside the privileged shim this module carries the pure, side-effect-light
helpers the swap is built from: :class:`SwapState` (the transient per-swap
record whose ``swap_state`` drives rollback), :func:`resolve_device_node`
(mount point -> ``DeviceNode`` string, captured before unmounting, None-safe),
and :func:`private_mountpoint` / :func:`_derive_private_mountpoint` /
:func:`_sanitize_label` (build an app-owned ``0700`` mountpoint under
:data:`PRIVATE_MOUNT_BASE` from an untrusted volume label without allowing path
traversal out of the base).
"""

import logging
import os
import plistlib
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# App-owned base directory for private mountpoints, kept OUTSIDE /Volumes/ so
# neither Finder nor the usb_monitor poller treats a swap mount as a user
# volume. Per-label mountpoints live directly under this base
# (e.g. /private/tmp/efis-datamanager/EFIS_3). Task 1.1's private_mountpoint()
# builds the concrete per-label paths on top of this base.
PRIVATE_MOUNT_BASE = "/private/tmp/efis-datamanager"

# Exact absolute binary paths for every privileged command. These are pinned so
# the sudoers backend (task 2) can grant NOPASSWD for exactly these paths and
# nothing else, and so run_privileged never resolves a binary off $PATH.
MOUNT_MSDOS_BIN = "/sbin/mount_msdos"
DISKUTIL_BIN = "/usr/sbin/diskutil"

# Whitelist of privileged commands this shim will run. A call is refused unless
# its first element is one of these exact absolute paths.
_ALLOWED_BINS = frozenset({MOUNT_MSDOS_BIN, DISKUTIL_BIN})

# A removable-drive BSD *slice* node, e.g. /dev/disk4s1. The mitigation only
# ever operates on a partition slice (diskNsN), never a whole-disk node
# (diskN), so the pattern requires the trailing sNN.
_DEVICE_NODE_RE = re.compile(r"^/dev/disk\d+s\d+$")


class PrivilegeError(RuntimeError):
    """Raised when a privileged call is refused by argument validation.

    Refusal happens BEFORE anything is executed: an unrecognised command, a
    device node that is not a removable FAT32 slice, or a mountpoint outside
    the app's private base all raise this rather than running.
    """


class MountSwapError(RuntimeError):
    """Raised when a mount swap cannot be established or must be aborted.

    This is the typed error :func:`msdos_mount` raises when the swap cannot
    even start (device node unresolvable) or fails partway (FSKit unmount or
    ``mount_msdos`` failed). It signals the caller (``app._run_drive_update``,
    task 5) that the write window did not run: report the sync incomplete,
    leave interrupted sync-state intact, and surface safe-next-step guidance
    (Req 7). By the time this is raised the context manager has already driven
    teardown toward "FSKit restored" (or, if restore itself failed, logged
    loudly and left the device flushed) — it is never raised while leaving the
    drive silently on the private mountpoint.
    """


# ---------------------------------------------------------------------------
# Argument validation (runs BEFORE every privileged call)
# ---------------------------------------------------------------------------
#
# These helpers are the whole reason privileged calls are confined to one
# function: sudoers cannot constrain arguments, so we constrain them here. A
# future signed helper (Phase 2) performs the same checks helper-side, but the
# app-side checks stay regardless.


def _looks_like_device_node(value: str) -> bool:
    """True iff ``value`` is a /dev/diskNsN removable-slice node string."""
    return bool(_DEVICE_NODE_RE.match(value))


def _device_info(device_node: str) -> Optional[dict]:
    """Return parsed ``diskutil info -plist`` for a device node, or None.

    Best-effort and tolerant of failure in the same style as drive_updater's
    identity lookups: any diskutil error, non-zero exit, unparseable plist, or
    timeout yields None rather than raising. This is a read-only, unprivileged
    query and is intentionally NOT routed through run_privileged.
    """
    try:
        result = subprocess.run(
            [DISKUTIL_BIN, "info", "-plist", device_node],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return plistlib.loads(result.stdout)
    except Exception:
        return None


def _is_removable_fat32(info: dict) -> bool:
    """True iff a diskutil info dict describes a removable FAT32 slice.

    Removable: any of RemovableMedia / RemovableMediaOrExternalDevice /
    Ejectable is true AND the media is not Internal. FAT32: FilesystemType is
    ``msdos`` and the content/name identifies FAT32 (guards against a msdos
    slice that is FAT12/FAT16 rather than FAT32).
    """
    if info.get("Internal") is True:
        return False
    removable = (
        bool(info.get("RemovableMedia"))
        or bool(info.get("RemovableMediaOrExternalDevice"))
        or bool(info.get("Ejectable"))
    )
    if not removable:
        return False
    if info.get("FilesystemType") != "msdos":
        return False
    content = str(info.get("Content") or "")
    fs_name = str(info.get("FilesystemName") or "")
    return "DOS_FAT_32" in content or "FAT32" in fs_name.upper()


def validate_device_node(device_node: str) -> None:
    """Validate ``device_node`` is a removable FAT32 slice, else raise.

    Two-stage: cheap syntactic check on the /dev/diskNsN form, then a
    diskutil-backed check that the slice is actually removable and FAT32. If
    diskutil cannot describe the node (query failed), the call is refused
    rather than executed against an unverifiable device.
    """
    if not isinstance(device_node, str) or not _looks_like_device_node(device_node):
        raise PrivilegeError(
            f"refusing privileged call: {device_node!r} is not a /dev/diskNsN "
            "removable-slice device node"
        )
    info = _device_info(device_node)
    if info is None:
        raise PrivilegeError(
            f"refusing privileged call: could not verify {device_node!r} is a "
            "removable FAT32 slice (diskutil info unavailable)"
        )
    if not _is_removable_fat32(info):
        raise PrivilegeError(
            f"refusing privileged call: {device_node!r} is not a removable "
            "FAT32 slice"
        )


def validate_mountpoint(mountpoint: str) -> None:
    """Validate ``mountpoint`` is under the app's private mount base, else raise.

    The comparison is on normalised absolute paths and requires the mountpoint
    to be a strict descendant of PRIVATE_MOUNT_BASE (not the base itself), so a
    privileged mount can never target /Volumes/, a system path, or the base
    directory directly.
    """
    if not isinstance(mountpoint, str) or not mountpoint:
        raise PrivilegeError("refusing privileged call: empty mountpoint")
    base = os.path.normpath(PRIVATE_MOUNT_BASE)
    target = os.path.normpath(mountpoint)
    if target == base or os.path.commonpath([base, target]) != base:
        raise PrivilegeError(
            f"refusing privileged call: mountpoint {mountpoint!r} is not under "
            f"the app private base {PRIVATE_MOUNT_BASE!r}"
        )


def _validate_argv(argv: list) -> None:
    """Validate a privileged argv array before execution.

    Checks, in order:
      * argv is a non-empty list of strings (never a shell string);
      * argv[0] is one of the pinned, whitelisted absolute binaries;
      * every /dev/disk* token is a validated removable FAT32 slice;
      * every mountpoint argument is under the app private base.

    Mountpoint detection is command-specific so we validate the right token:
      * ``/sbin/mount_msdos <device> <mountpoint>`` — last arg is the mountpoint.
      * ``/usr/sbin/diskutil unmount|mount ...`` — diskutil chooses/derives the
        mountpoint itself, so only the device/volume token is constrained.
    """
    if not isinstance(argv, list) or not argv:
        raise PrivilegeError("refusing privileged call: argv must be a non-empty list")
    if not all(isinstance(a, str) for a in argv):
        raise PrivilegeError("refusing privileged call: argv must be all strings")

    binary = argv[0]
    if binary not in _ALLOWED_BINS:
        raise PrivilegeError(
            f"refusing privileged call: {binary!r} is not a whitelisted binary"
        )

    # Any device-node-looking token must pass the removable-FAT32 check.
    device_tokens = [a for a in argv[1:] if a.startswith("/dev/")]
    for token in device_tokens:
        validate_device_node(token)

    if binary == MOUNT_MSDOS_BIN:
        # /sbin/mount_msdos [opts...] <device> <mountpoint>: require the two
        # trailing positional args to be a validated device + private mountpoint.
        if len(argv) < 3:
            raise PrivilegeError(
                "refusing privileged call: mount_msdos requires <device> <mountpoint>"
            )
        device_node, mountpoint = argv[-2], argv[-1]
        validate_device_node(device_node)
        validate_mountpoint(mountpoint)
    # For diskutil unmount/mount, the device/volume token is validated above via
    # device_tokens; diskutil derives the mountpoint under /Volumes/ itself and
    # is not handed an arbitrary path, so there is no mountpoint arg to constrain.


def run_privileged(
    argv: list, timeout: float
) -> subprocess.CompletedProcess:
    """Execute one privileged command — the SOLE privilege-injection choke point.

    Every privileged operation in the mount-swap layer (``mount_msdos``,
    ``diskutil unmount``, ``diskutil mount``) goes through here. This function:

      * accepts an **argv array only** (never a shell string), so there is no
        shell to inject into;
      * **validates every argument BEFORE executing** (:func:`_validate_argv`) —
        a bad device or mountpoint raises :class:`PrivilegeError` and nothing
        runs;
      * prepends the interim ``sudoers`` backend (``sudo -n`` non-interactive;
        the exact binaries are NOPASSWD-granted by the task-2 sudoers.d file),
        using ``sudo -n`` so a missing grant fails fast instead of hanging on a
        password prompt for an unattended menu-bar tool.

    Backend swappability: Phase 2 replaces ONLY the argv-prefixing / dispatch in
    this function (sudoers -> signed helper IPC). Callers, validation, and the
    argv-array contract are unchanged.
    """
    _validate_argv(argv)

    # --- Interim sudoers backend (Phase 1). Swapped for signed-helper IPC in
    # --- Phase 2 without touching callers or validation above.
    full_cmd = ["/usr/bin/sudo", "-n", *argv]

    logger.debug("run_privileged: %s (timeout=%ss)", full_cmd, timeout)
    return subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Transient per-swap state
# ---------------------------------------------------------------------------
#
# One SwapState is created when a swap is entered and threaded through the
# swap/teardown so rollback is deterministic. ``swap_state`` records how far the
# swap progressed; teardown reads it to decide what must be undone (see the
# design's rollback table) so the drive is never left unmounted or on a stale
# mountpoint.

SWAP_STATE_ENTERED = "entered"
SWAP_STATE_MOUNTED = "mounted"
SWAP_STATE_RESTORED = "restored"

# All valid values of SwapState.swap_state, in the order the swap advances
# through them. Kept as a tuple so callers/tests can assert membership without
# risk of mutation.
SWAP_STATES = (SWAP_STATE_ENTERED, SWAP_STATE_MOUNTED, SWAP_STATE_RESTORED)


@dataclass
class SwapState:
    """Transient record for one in-flight mount swap.

    Fields:
      * ``device_node`` — the BSD slice node (``/dev/diskNsN``) resolved from
        the FSKit mount point BEFORE unmounting, so teardown can hand it to
        ``diskutil mount`` to restore FSKit.
      * ``label`` — the volume label (e.g. ``EFIS_3``); names the private
        mountpoint.
      * ``work_mount`` — the private msdos mountpoint under
        :data:`PRIVATE_MOUNT_BASE` (e.g. ``/private/tmp/efis-datamanager/EFIS_3``).
      * ``swap_state`` — how far the swap advanced; one of :data:`SWAP_STATES`
        (``entered`` -> ``mounted`` -> ``restored``). Drives rollback.
    """

    device_node: str
    label: str
    work_mount: str
    swap_state: str = SWAP_STATE_ENTERED


# ---------------------------------------------------------------------------
# Device-node resolution (unprivileged, None-safe)
# ---------------------------------------------------------------------------


def resolve_device_node(mount_point: str) -> Optional[str]:
    """Return the ``DeviceNode`` backing ``mount_point``, or None.

    Runs ``diskutil info -plist <mount_point>`` and reads the ``DeviceNode``
    key. This is captured while the volume is still on its FSKit ``/Volumes/``
    mount, BEFORE unmounting, so teardown can restore the FSKit mount by device.

    Best-effort and None-safe in the same style as :func:`_device_info`: any
    non-zero exit, unparseable plist, missing key, OSError, or SubprocessError
    yields None rather than raising. A None result tells the caller to abort the
    swap and stay on FSKit. This is a read-only, unprivileged query and is
    intentionally NOT routed through :func:`run_privileged`.

    Note this differs from :func:`_device_info`, which takes a device node and
    returns the full info dict; here the input is a MOUNT POINT and the output
    is the ``DeviceNode`` string.
    """
    try:
        result = subprocess.run(
            [DISKUTIL_BIN, "info", "-plist", mount_point],
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
    device_node = info.get("DeviceNode")
    if not isinstance(device_node, str) or not device_node:
        return None
    return device_node


# ---------------------------------------------------------------------------
# Private mountpoint derivation
# ---------------------------------------------------------------------------


def _sanitize_label(label: str) -> str:
    """Reduce an untrusted volume label to one safe path segment.

    A volume label is attacker-influenceable (it comes off the drive), so it is
    never trusted to build a path directly. This strips it to a single path
    component: take the basename (drops any ``/``-separated prefix), remove
    residual separators and ``..`` traversal, and fall back to ``"volume"`` when
    nothing safe remains. The result is always a single segment with no
    separators, so a derived path cannot climb out of
    :data:`PRIVATE_MOUNT_BASE`.
    """
    if not isinstance(label, str):
        label = ""
    # basename drops everything up to and including the last separator, so
    # "/etc/passwd" -> "passwd" and "a/b" -> "b".
    segment = os.path.basename(label).strip()
    # Defensively remove any residual path separators and parent-dir markers
    # that survived (e.g. embedded backslashes, or a bare "..").
    segment = segment.replace("/", "").replace("\\", "")
    if segment in ("", ".", ".."):
        return "volume"
    return segment


def _derive_private_mountpoint(label: str) -> str:
    """Pure derivation of the private mountpoint path for ``label``.

    No side effects: sanitizes the label to a single safe segment and joins it
    under :data:`PRIVATE_MOUNT_BASE`. Split out from :func:`private_mountpoint`
    so the path math is testable without touching the filesystem.
    """
    return os.path.join(PRIVATE_MOUNT_BASE, _sanitize_label(label))


def private_mountpoint(label: str) -> str:
    """Create and return an app-owned ``0700`` mountpoint for ``label``.

    Builds the path via :func:`_derive_private_mountpoint` (which sanitizes the
    untrusted label so the result is always under :data:`PRIVATE_MOUNT_BASE`),
    then ensures the directory exists with mode ``0700`` and, best-effort,
    explicitly ``chmod``s it to ``0700`` so a pre-existing directory created
    with looser permissions is tightened. The mountpoint lives OUTSIDE
    ``/Volumes/`` so neither Finder nor the usb_monitor poller treats it as a
    user volume.
    """
    mountpoint = _derive_private_mountpoint(label)
    os.makedirs(mountpoint, mode=0o700, exist_ok=True)
    # makedirs honours mode only for dirs it creates and is subject to umask, so
    # explicitly tighten to 0700 regardless of umask or a pre-existing dir.
    try:
        os.chmod(mountpoint, 0o700)
    except OSError:
        logger.debug("private_mountpoint: chmod 0700 failed for %s", mountpoint)
    return mountpoint


# ---------------------------------------------------------------------------
# Timeouts for privileged mount/unmount operations
# ---------------------------------------------------------------------------
#
# Bounded so a wedged diskutil/mount_msdos cannot hang the menu-bar tool. These
# are the ceilings for a single privileged call, not the whole swap; the robust
# retry/backoff logic in robust_unmount() may issue several such calls.
UNMOUNT_TIMEOUT_SECONDS = 60.0
MOUNT_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Poller-control seam (task 4.1 / wiring task 5)
# ---------------------------------------------------------------------------
#
# The swap must quiesce the 2s usb_monitor /Volumes/ poller so it does not react
# to the transient disappearance (FSKit unmount) and reappearance (FSKit
# restore) of the volume during the swap. usb_monitor gains pause()/resume() in
# the PARALLEL task 4.1, which is not guaranteed to have landed when this module
# is imported. To avoid a hard cross-module import cycle / task-ordering
# coupling, msdos_mount takes an OPTIONAL poller-control hook rather than
# importing usb_monitor directly: any object exposing pause()/resume() works.
# The app-wiring task (task 5) passes the real monitor instance; unit tests pass
# a mock; a None poller (the default) simply skips quiescing. This is the single
# seam between the swap layer and the monitor.


@runtime_checkable
class PollerControl(Protocol):
    """Minimal interface msdos_mount needs to quiesce the /Volumes/ poller.

    usb_monitor's monitor object satisfies this once task 4.1 adds pause()/
    resume(); tests can supply any object with the same two no-arg methods.
    """

    def pause(self) -> None: ...

    def resume(self) -> None: ...


def _pause_poller(poller: Optional[PollerControl]) -> None:
    """Best-effort quiesce of the poller if one was supplied.

    A missing or misbehaving poller must never abort the swap, so failures are
    logged and swallowed. When ``poller`` is None (no hook wired yet) this is a
    no-op.
    """
    if poller is None:
        return
    try:
        poller.pause()
    except Exception:
        logger.exception("msdos_mount: poller.pause() failed; continuing")


def _resume_poller(poller: Optional[PollerControl]) -> None:
    """Best-effort un-quiesce of the poller if one was supplied.

    Symmetric to :func:`_pause_poller`; called during teardown so the poller
    rebaselines the restored FSKit mount. Never raises.
    """
    if poller is None:
        return
    try:
        poller.resume()
    except Exception:
        logger.exception("msdos_mount: poller.resume() failed; continuing")


# ---------------------------------------------------------------------------
# Stray /Volumes/ reappearance detection
# ---------------------------------------------------------------------------


def _stray_volume_path(label: str) -> Optional[str]:
    """Return a stray ``/Volumes/<label>`` path if one exists, else None.

    After the FSKit unmount, macOS diskarbitration may auto-mount the device
    back under ``/Volumes/`` before we get ``mount_msdos`` in. If a mountpoint
    matching the volume label reappears there, we must unmount it before
    proceeding so the device is free for the private ``mount_msdos`` mount. The
    label is sanitized to a single path segment (same rule the private
    mountpoint uses) so this never probes outside ``/Volumes/``.
    """
    segment = _sanitize_label(label)
    candidate = os.path.join("/Volumes", segment)
    if os.path.ismount(candidate):
        return candidate
    return None


def _clear_stray_volume(label: str) -> None:
    """Unmount a stray ``/Volumes/<label>`` reappearance if present.

    Best-effort: uses ``diskutil unmount`` against the stray path via
    :func:`run_privileged`. A failure here is logged but not fatal on its own —
    the subsequent ``mount_msdos`` will fail loudly if the device is still busy,
    and that failure drives the normal rollback.
    """
    stray = _stray_volume_path(label)
    if stray is None:
        return
    logger.warning(
        "msdos_mount: stray volume reappeared at %s after unmount; clearing", stray
    )
    try:
        run_privileged(
            [DISKUTIL_BIN, "unmount", stray], timeout=UNMOUNT_TIMEOUT_SECONDS
        )
    except Exception:
        logger.exception("msdos_mount: failed to clear stray volume %s", stray)


# ---------------------------------------------------------------------------
# Robust teardown (settle -> retry -> force escalation -> FSKit restore)
# ---------------------------------------------------------------------------
#
# The spike's teardown hiccup drives this design: raw ``umount`` returned
# "Resource busy", and recovery required ``diskutil unmount`` followed by
# ``diskutil mount`` (design: "Avoiding the 'Resource busy' teardown failure").
# robust_unmount() therefore: settles first (os.sync + a short wait so the
# in-kernel msdosfs driver flushes), prefers ``diskutil unmount`` over raw
# ``umount``, logs lingering holders via ``lsof`` for diagnostics, retries with
# bounded backoff, and escalates to ``diskutil unmount force`` only as a last
# resort (logged loudly). restore_fskit_mount() then hands the device back to
# ``diskutil mount`` so the volume reappears under /Volumes/ and Finder/eject
# behave normally. Both are invoked from msdos_mount's finally: block, which
# guarantees the drive is never left unmounted silently.

# Settle window after the last write/marker fsync, before the first unmount
# attempt, so the in-kernel msdosfs driver flushes and releases the mount.
SETTLE_WAIT_SECONDS = 2.0

# Bounded retry/backoff for the "Resource busy" case. Attempts use increasing
# sleeps (SETTLE_WAIT_SECONDS * attempt) so a transient holder gets time to let
# go before we escalate to a forced unmount.
UNMOUNT_MAX_ATTEMPTS = 3

# Marker substrings that indicate the mount is still busy (a lingering holder)
# rather than a hard failure. Matched case-insensitively against diskutil's
# stderr/stdout so we know to run the lsof diagnostic and back off before retry.
_BUSY_MARKERS = ("busy", "in use", "could not be unmounted")


def _lsof_holders(work_mount: str) -> str:
    """Return an ``lsof`` listing of processes holding ``work_mount``, or "".

    Diagnostic only: run ``lsof +D <work_mount>`` (falling back to
    ``lsof <work_mount>``) to surface lingering handles when an unmount reports
    busy. Best-effort and never raises — a missing/erroring lsof yields "". Our
    own rsync logs live in the system temp dir, not on the mount, so any holder
    here is expected to be transient.
    """
    for argv in ([("lsof", "+D", work_mount)], [("lsof", work_mount)]):
        try:
            result = subprocess.run(
                list(argv[0]),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        out = (result.stdout or "").strip()
        if out:
            return out
    return ""


def _looks_busy(result: subprocess.CompletedProcess) -> bool:
    """True iff a diskutil result looks like a "Resource busy" style failure."""
    blob = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(marker in blob for marker in _BUSY_MARKERS)


def robust_unmount(work_mount: str, device_node: str) -> bool:
    """Robustly unmount the private ``work_mount``; return True once unmounted.

    Follows the design's "Avoiding the 'Resource busy' teardown failure" steps:

      1. **Settle first** — :func:`os.sync` then a short wait
         (:data:`SETTLE_WAIT_SECONDS`) so the in-kernel msdosfs driver flushes.
      2. **Prefer ``diskutil unmount``** over raw ``umount`` — the spike showed
         raw ``umount`` returns "Resource busy" while ``diskutil unmount``
         recovers.
      3. **Identify holders and retry** — if an attempt reports busy/fails, log
         lingering holders via :func:`_lsof_holders` (diagnostic), wait with an
         increasing backoff, and retry up to :data:`UNMOUNT_MAX_ATTEMPTS`.
      4. **Bounded escalation** — after the bounded retries, escalate to
         ``diskutil unmount force`` as a last resort, logged loudly.
      5. Success is verified by ``os.path.ismount(work_mount) is False`` (not
         just a zero exit), so a stale mount is never reported as unmounted.

    Returns True once ``work_mount`` is verified no longer a mountpoint, else
    False (caller keeps the "never left unmounted silently" invariant by then
    still restoring the FSKit mount and, if that also fails, telling the user to
    reinsert).
    """
    # If it is somehow already not a mountpoint, there is nothing to unmount.
    if not os.path.ismount(work_mount):
        return True

    # Step 1: settle so the in-kernel driver flushes before we unmount.
    try:
        os.sync()
    except OSError:
        logger.debug("robust_unmount: os.sync() failed; continuing")
    time.sleep(SETTLE_WAIT_SECONDS)

    # Steps 2-3: prefer `diskutil unmount`, retry with bounded backoff.
    for attempt in range(1, UNMOUNT_MAX_ATTEMPTS + 1):
        try:
            result = run_privileged(
                [DISKUTIL_BIN, "unmount", work_mount],
                timeout=UNMOUNT_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception(
                "robust_unmount: diskutil unmount of %s raised (attempt %s/%s)",
                work_mount,
                attempt,
                UNMOUNT_MAX_ATTEMPTS,
            )
            result = None

        if result is not None and result.returncode == 0:
            if not os.path.ismount(work_mount):
                logger.info(
                    "robust_unmount: unmounted %s on attempt %s", work_mount, attempt
                )
                return True
            # Zero exit but still mounted — treat like a busy failure and retry.
            logger.warning(
                "robust_unmount: diskutil reported success but %s is still a "
                "mountpoint (attempt %s/%s)",
                work_mount,
                attempt,
                UNMOUNT_MAX_ATTEMPTS,
            )

        # This attempt did not cleanly unmount. Log holders for diagnosis.
        stderr = "" if result is None else (result.stderr or "").strip()[:500]
        logger.warning(
            "robust_unmount: unmount of %s did not succeed (attempt %s/%s): %s",
            work_mount,
            attempt,
            UNMOUNT_MAX_ATTEMPTS,
            stderr or "no diskutil output",
        )
        holders = _lsof_holders(work_mount)
        if holders:
            logger.warning(
                "robust_unmount: lingering holders on %s:\n%s", work_mount, holders
            )

        # Back off before the next attempt (increasing wait), unless this was
        # the last bounded attempt.
        if attempt < UNMOUNT_MAX_ATTEMPTS:
            time.sleep(SETTLE_WAIT_SECONDS * attempt)

    # Step 4: bounded escalation — forced unmount as a last resort, logged loud.
    logger.error(
        "robust_unmount: %s still mounted after %s attempts; escalating to "
        "`diskutil unmount force`",
        work_mount,
        UNMOUNT_MAX_ATTEMPTS,
    )
    try:
        forced = run_privileged(
            [DISKUTIL_BIN, "unmount", "force", work_mount],
            timeout=UNMOUNT_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "robust_unmount: forced unmount of %s raised", work_mount
        )
        return False

    if forced.returncode == 0 and not os.path.ismount(work_mount):
        logger.warning(
            "robust_unmount: forced unmount of %s succeeded", work_mount
        )
        return True

    logger.error(
        "robust_unmount: forced unmount of %s failed (rc=%s): %s; work mount "
        "could not be released",
        work_mount,
        forced.returncode,
        (forced.stderr or "").strip()[:500],
    )
    return False


def restore_fskit_mount(device_node: str) -> bool:
    """Restore the normal FSKit mount of ``device_node``; return True on success.

    Runs ``diskutil mount <device_node>`` via :func:`run_privileged` so the
    volume reappears under ``/Volumes/`` and Finder/eject behave normally after
    the swap. A redundant mount of an already-mounted device is harmless —
    ``diskutil`` reports it as already mounted — so that is treated as success.

    On genuine failure the problem is logged loudly (the caller/UX then instructs
    the user to reinsert the drive) and False is returned. The data is already
    flushed by the preceding unmount settle, so the drive is safe; it simply
    needs a remount to be usable again.
    """
    try:
        result = run_privileged(
            [DISKUTIL_BIN, "mount", device_node], timeout=MOUNT_TIMEOUT_SECONDS
        )
    except Exception:
        logger.exception(
            "restore_fskit_mount: diskutil mount of %s raised; drive needs "
            "reinsertion",
            device_node,
        )
        return False

    if result.returncode == 0:
        logger.info("restore_fskit_mount: restored FSKit mount of %s", device_node)
        return True

    # A non-zero exit because the device is already mounted is a success for our
    # purposes: the volume is back under /Volumes/.
    blob = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "already mounted" in blob:
        logger.info(
            "restore_fskit_mount: %s already mounted; treating as restored",
            device_node,
        )
        return True

    logger.error(
        "restore_fskit_mount: diskutil mount of %s failed (rc=%s): %s; drive "
        "needs reinsertion",
        device_node,
        result.returncode,
        (result.stderr or "").strip()[:500],
    )
    return False


# ---------------------------------------------------------------------------
# The mount-swap context manager
# ---------------------------------------------------------------------------


@contextmanager
def msdos_mount(
    fskit_mount_point: str, poller: Optional[PollerControl] = None
) -> Iterator[str]:
    """Swap a managed FSKit mount to an in-kernel msdosfs mount for the write window.

    Yields the private ``work_mount`` path the sync engine should run against.
    Sequence (per design's End-to-end flow and Error Handling table):

      1. Resolve the BSD device node from ``fskit_mount_point`` via
         :func:`resolve_device_node` BEFORE any unmount. If it is None, abort
         WITHOUT unmounting (the caller stays on FSKit) by raising
         :class:`MountSwapError`.
      2. Quiesce the usb_monitor poller (:func:`_pause_poller`) if a hook was
         supplied, so the 2s ``/Volumes/`` poller does not react to the
         transient unmount/remount. See the PollerControl seam above.
      3. ``diskutil unmount`` the FSKit volume.
      4. Detect and clear any stray ``/Volumes/<label>`` reappearance
         (:func:`_clear_stray_volume`) before mounting privately.
      5. Create the ``0700`` private work dir via :func:`private_mountpoint`
         (label derived from the FSKit mount point basename).
      6. ``/sbin/mount_msdos <device_node> <work_mount>`` via
         :func:`run_privileged`.
      7. Advance the :class:`SwapState` (``entered`` -> ``mounted``) and
         ``yield work_mount``.
      8. In ``finally``: run teardown so the drive is NEVER left unmounted —
         :func:`robust_unmount` the work mount (only if it was mounted), then
         :func:`restore_fskit_mount` the device (only if the FSKit volume was
         actually unmounted), and un-quiesce the poller. If the FSKit restore
         fails it is logged loudly and the drive is flagged needs-reinsert; the
         drive is never left silently unmounted.

    On any failure between the FSKit unmount and a ready ``work_mount``, the
    ``finally`` teardown restores the FSKit mount and a :class:`MountSwapError`
    is raised, so the caller reports the sync incomplete rather than proceeding.
    This never leaves the volume silently unmounted or on the private mount.
    """
    label = os.path.basename(os.path.normpath(fskit_mount_point))

    # Step 1: resolve BSD node BEFORE any unmount. None => abort, stay on FSKit.
    device_node = resolve_device_node(fskit_mount_point)
    if device_node is None:
        raise MountSwapError(
            f"cannot start mount swap: unable to resolve device node for "
            f"{fskit_mount_point!r}; staying on FSKit mount"
        )

    work_mount = _derive_private_mountpoint(label)
    state = SwapState(
        device_node=device_node,
        label=label,
        work_mount=work_mount,
        swap_state=SWAP_STATE_ENTERED,
    )

    # Step 2: quiesce the poller for the whole swap window (no-op if unwired).
    _pause_poller(poller)

    # Tracks whether we actually unmounted the FSKit /Volumes/ mount. Only then
    # does teardown need to restore it; if the unmount itself failed the volume
    # is still mounted and a restore would be redundant.
    fskit_unmounted = False

    try:
        # Step 3: unmount the FSKit volume. If this fails the volume is still at
        # /Volumes/, so we abort WITHOUT having mounted anything privately.
        unmount = run_privileged(
            [DISKUTIL_BIN, "unmount", fskit_mount_point],
            timeout=UNMOUNT_TIMEOUT_SECONDS,
        )
        if unmount.returncode != 0:
            raise MountSwapError(
                f"mount swap aborted: diskutil unmount of {fskit_mount_point!r} "
                f"failed (rc={unmount.returncode}): "
                f"{(unmount.stderr or '').strip()[:500]}"
            )
        fskit_unmounted = True

        # Step 4: clear a stray /Volumes/<label> reappearance before mounting.
        _clear_stray_volume(label)

        # Step 5: create the 0700 private work dir.
        work_mount = private_mountpoint(label)
        state.work_mount = work_mount

        # Step 6: mount the device via the in-kernel msdosfs path.
        mounted = run_privileged(
            [MOUNT_MSDOS_BIN, device_node, work_mount],
            timeout=MOUNT_TIMEOUT_SECONDS,
        )
        if mounted.returncode != 0:
            raise MountSwapError(
                f"mount swap failed: mount_msdos of {device_node!r} at "
                f"{work_mount!r} failed (rc={mounted.returncode}): "
                f"{(mounted.stderr or '').strip()[:500]}"
            )
        if not os.path.ismount(work_mount):
            raise MountSwapError(
                f"mount swap failed: {work_mount!r} is not a mountpoint after "
                f"mount_msdos of {device_node!r}"
            )

        # Step 7: swap is established; hand the private mount to the engine.
        state.swap_state = SWAP_STATE_MOUNTED
        logger.info(
            "msdos_mount: swapped %s (%s) to private mount %s",
            fskit_mount_point,
            device_node,
            work_mount,
        )
        yield work_mount

    finally:
        # Step 8: robust teardown. The drive must NEVER be left unmounted
        # silently. Precise per swap_state / progress:
        #   * Only the work mount needs unmounting if we reached MOUNTED (i.e.
        #     mount_msdos succeeded). robust_unmount() settles, prefers diskutil
        #     unmount, retries with backoff, and escalates to force.
        #   * The FSKit mount only needs restoring if we actually unmounted it
        #     (fskit_unmounted). If the FSKit unmount itself failed the volume
        #     is still at /Volumes/ and no restore is needed.
        if state.swap_state == SWAP_STATE_MOUNTED:
            robust_unmount(state.work_mount, device_node)

        if fskit_unmounted:
            if restore_fskit_mount(device_node):
                state.swap_state = SWAP_STATE_RESTORED
            else:
                # Loudly-logged needs-reinsert terminal state. The data is
                # flushed (unmount settled) but the volume is not back under
                # /Volumes/; the caller/UX instructs the user to reinsert. The
                # drive is never reported current while in this state.
                logger.error(
                    "msdos_mount teardown: could NOT restore FSKit mount of %s "
                    "(%s); drive needs reinsertion",
                    device_node,
                    label,
                )

        # Un-quiesce the poller last, so it rebaselines the restored FSKit mount.
        _resume_poller(poller)
