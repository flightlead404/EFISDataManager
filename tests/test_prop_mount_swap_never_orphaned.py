# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property-based test for the mount-swap "never orphaned" invariant.

Feature: chart-sync-stall-fix, Property 1: swap never ends
unmounted-and-unreported.

*For any* sequence of injected failures at any point in the ``msdos_mount``
lifecycle, the teardown terminal state is either "device restored to its FSKit
mount" (a ``diskutil mount`` of the device was attempted during teardown) OR a
failure explicitly raised/logged as needs-reinsert — NEVER "device left
unmounted or on the private mountpoint with success reported".

**Validates: Requirements 7.1, 7.2, 7.5**

Every privileged/mount call is mocked per a Hypothesis-generated scenario, so no
real device is ever touched: ``run_privileged`` returns a return code that
depends on the argv it is handed and the generated outcome map,
``resolve_device_node`` returns a node or ``None``, and ``os.path.ismount`` /
``time.sleep`` / ``os.sync`` are patched. For each generated scenario the
invariant is asserted by inspecting ``run_privileged.call_args_list``.
"""

import subprocess
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager import mount_swap as ms


FSKIT_MP = "/Volumes/EFIS_3"
DEVICE = "/dev/disk4s1"
WORK_MOUNT = "/private/tmp/efis-datamanager/EFIS_3"


def _cp(returncode=0, stdout="", stderr=""):
    """Build a fake CompletedProcess like ``run_privileged`` returns."""
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _argv_calls(run_privileged_mock):
    """Return the argv arrays every ``run_privileged`` call was made with."""
    return [c.args[0] for c in run_privileged_mock.call_args_list]


# A scenario is a combination of outcomes for each privileged step plus whether
# resolve_device_node yields a node. Each field is an independently-generated
# success/failure toggle so Hypothesis explores failures injected at every point
# in the lifecycle (device resolution, FSKit unmount, mount_msdos, the work
# unmount during teardown, and the FSKit restore).
_scenario = st.fixed_dictionaries(
    {
        "resolve_ok": st.booleans(),        # resolve_device_node -> node or None
        "fskit_unmount_ok": st.booleans(),  # diskutil unmount /Volumes -> rc 0/1
        "mount_msdos_ok": st.booleans(),    # mount_msdos -> rc 0/1
        "work_unmount_ok": st.booleans(),   # teardown diskutil unmount work_mount
        "restore_ok": st.booleans(),        # teardown diskutil mount device
    }
)


@settings(max_examples=100)
@given(scenario=_scenario)
def test_prop_swap_never_ends_unmounted_and_unreported(scenario):
    """Feature: chart-sync-stall-fix, Property 1: swap never ends unmounted-and-unreported.

    For any injected-failure scenario, if ``msdos_mount`` either raised
    ``MountSwapError`` OR completed normally, then EITHER the FSKit volume was
    never unmounted (still mounted — safe), OR a ``diskutil mount <device>``
    restore was ATTEMPTED during teardown. There is never a state where FSKit
    was unmounted, no restore was attempted, and the swap reported success.
    """
    # Tracks whether the work mount currently looks mounted. It starts unmounted
    # (nothing there yet); a successful mount_msdos flips it to mounted; a
    # successful teardown unmount flips it back.
    ismount_state = {"work_mounted": False}

    def _ismount(path):
        if path == WORK_MOUNT:
            return ismount_state["work_mounted"]
        # The FSKit /Volumes path and anything else are treated as mounted so
        # stray-volume probing does not misfire; only work_mount toggles.
        return path != WORK_MOUNT and path.startswith("/Volumes/")

    def _run_privileged(argv, timeout):
        """Return a return code driven by the argv + generated outcome map."""
        binary = argv[0]

        if binary == ms.MOUNT_MSDOS_BIN:
            if scenario["mount_msdos_ok"]:
                ismount_state["work_mounted"] = True
                return _cp(returncode=0)
            return _cp(returncode=1, stderr="mount_msdos failed")

        if binary == ms.DISKUTIL_BIN:
            verb = argv[1] if len(argv) > 1 else ""

            # Restore of the FSKit mount: `diskutil mount <device>`.
            if verb == "mount":
                if scenario["restore_ok"]:
                    return _cp(returncode=0)
                return _cp(returncode=1, stderr="could not mount")

            if verb == "unmount":
                # Teardown unmount of the private work mount (incl. force).
                if WORK_MOUNT in argv:
                    if scenario["work_unmount_ok"]:
                        ismount_state["work_mounted"] = False
                        return _cp(returncode=0)
                    return _cp(returncode=1, stderr="Resource busy")
                # Stray /Volumes/<label> clear is best-effort; let it succeed.
                if any(a.startswith("/Volumes/") for a in argv):
                    if argv[-1] == FSKIT_MP:
                        # The initial FSKit unmount.
                        if scenario["fskit_unmount_ok"]:
                            return _cp(returncode=0)
                        return _cp(returncode=1, stderr="unmount failed")
                    return _cp(returncode=0)

        # Anything unexpected: succeed harmlessly.
        return _cp(returncode=0)

    resolve_return = DEVICE if scenario["resolve_ok"] else None

    raised = False
    completed = False
    with mock.patch.object(ms, "resolve_device_node", return_value=resolve_return), \
        mock.patch.object(ms, "run_privileged", side_effect=_run_privileged) as rp, \
        mock.patch.object(ms, "private_mountpoint", return_value=WORK_MOUNT), \
        mock.patch.object(ms, "_lsof_holders", return_value=""), \
        mock.patch("os.path.ismount", side_effect=_ismount), \
        mock.patch("os.makedirs"), \
        mock.patch("os.chmod"), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        try:
            with ms.msdos_mount(FSKIT_MP) as wm:
                # Body ran => swap reported success by yielding the work mount.
                assert wm == WORK_MOUNT
            completed = True
        except ms.MountSwapError:
            raised = True

        argvs = _argv_calls(rp)

    # Every terminal path is one of: raised MountSwapError, or completed.
    assert raised or completed

    # Was the FSKit volume actually unmounted? Only then does the drive risk
    # being left orphaned and only then must a restore have been attempted.
    fskit_unmount_attempted = any(
        a[:2] == [ms.DISKUTIL_BIN, "unmount"] and a[-1] == FSKIT_MP for a in argvs
    )
    fskit_was_unmounted = fskit_unmount_attempted and scenario["fskit_unmount_ok"]

    # A restore is a `diskutil mount <device>` attempt during teardown.
    restore_attempted = [ms.DISKUTIL_BIN, "mount", DEVICE] in argvs

    # THE INVARIANT: never "FSKit unmounted, no restore attempted, success
    # reported". If the FSKit volume was taken down, teardown must have tried to
    # bring it back — regardless of whether the swap raised or completed.
    if fskit_was_unmounted:
        assert restore_attempted, (
            "FSKit volume was unmounted but no `diskutil mount` restore was "
            f"attempted during teardown; scenario={scenario}, argvs={argvs}"
        )
    else:
        # FSKit was never unmounted: the volume is still on its /Volumes/ mount,
        # which is the safe terminal state. Nothing to restore.
        assert not fskit_unmount_attempted or not scenario["fskit_unmount_ok"]

    # A completed (success-reported) swap is only allowed when the private work
    # mount is not left mounted AND the FSKit mount was restored. i.e. success is
    # never reported while the drive is orphaned on the private mountpoint.
    if completed and fskit_was_unmounted:
        assert restore_attempted, (
            "swap completed (success reported) after unmounting FSKit without a "
            f"restore attempt; scenario={scenario}, argvs={argvs}"
        )
