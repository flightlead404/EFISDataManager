# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the mount-swap rollback state machine and robust teardown.

Task 1.5 (chart-sync-stall-fix). Covers three seams in ``mount_swap.py``:

  * :func:`msdos_mount` rollback state machine — for every failure-injection
    point the teardown reaches a terminal state that is EITHER "FSKit restored"
    (a ``diskutil mount`` of the device was attempted) OR an explicit
    needs-reinsert / raised :class:`MountSwapError`. It is NEVER "left unmounted
    silently" and NEVER proceeds as success (Req 7.1, 7.2, 7.5).
  * :func:`robust_unmount` — settles, prefers ``diskutil unmount``, retries with
    bounded backoff, and escalates to ``diskutil unmount force`` only as a last
    resort (Req 3.3, 7.5).
  * :func:`restore_fskit_mount` — success, "already mounted" tolerated as
    success, and genuine failure -> False.

Every privileged call is mocked (``run_privileged``), device resolution is
mocked (``resolve_device_node``), and ``os.path.ismount`` / ``time.sleep`` /
``os.sync`` are patched so no real mount happens and the tests run fast. The
tests drive the module functions directly and never import ``app.py``.
"""

import subprocess
from unittest import mock

import pytest

from efis_data_manager import mount_swap as ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FSKIT_MP = "/Volumes/EFIS_3"
DEVICE = "/dev/disk4s1"


def _cp(returncode=0, stdout="", stderr=""):
    """Build a fake CompletedProcess like run_privileged returns."""
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakePoller:
    """Minimal PollerControl double recording pause/resume calls."""

    def __init__(self):
        self.paused = 0
        self.resumed = 0

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1


def _argv_calls(run_privileged_mock):
    """Return the list of argv arrays each run_privileged call was made with."""
    return [c.args[0] for c in run_privileged_mock.call_args_list]


def _capturing_swapstate(captured):
    """A SwapState factory that records the real instance it builds in ``captured``.

    Patching SwapState does not expose the constructed instance, so we wrap the
    real dataclass and stash the instance so a test can assert on the final
    ``swap_state`` value after teardown. The real class is bound here (before the
    patch is applied) so the factory never re-enters the patched name.
    """
    real_cls = ms.SwapState

    def _factory(*args, **kwargs):
        inst = real_cls(*args, **kwargs)
        captured.append(inst)
        return inst

    return _factory


# ---------------------------------------------------------------------------
# msdos_mount — rollback state machine
# ---------------------------------------------------------------------------


def test_resolve_device_node_none_raises_and_never_unmounts():
    """device node unresolvable -> raise, run_privileged NEVER called.

    Caller stays on FSKit: no unmount was attempted, so nothing to restore.
    """
    with mock.patch.object(ms, "resolve_device_node", return_value=None), \
        mock.patch.object(ms, "run_privileged") as rp, \
        mock.patch("os.path.ismount", return_value=True):
        with pytest.raises(ms.MountSwapError):
            with ms.msdos_mount(FSKIT_MP):
                pytest.fail("body must not run when device node is unresolvable")

    rp.assert_not_called()


def test_fskit_unmount_failure_raises_no_mount_no_restore():
    """FSKit `diskutil unmount` returns non-zero -> raise.

    Because the FSKit unmount failed the volume is still mounted: NO mount_msdos
    is attempted and NO restore (diskutil mount of the device) is needed.
    """
    unmount_fail = _cp(returncode=1, stderr="unmount failed")
    with mock.patch.object(ms, "resolve_device_node", return_value=DEVICE), \
        mock.patch.object(ms, "run_privileged", return_value=unmount_fail) as rp, \
        mock.patch("os.path.ismount", return_value=True):
        with pytest.raises(ms.MountSwapError):
            with ms.msdos_mount(FSKIT_MP):
                pytest.fail("body must not run when FSKit unmount fails")

    argvs = _argv_calls(rp)
    # Exactly one privileged call: the failed FSKit unmount.
    assert len(argvs) == 1
    assert argvs[0][:2] == [ms.DISKUTIL_BIN, "unmount"]
    # No mount_msdos and no `diskutil mount` (restore) were attempted.
    assert not any(a[0] == ms.MOUNT_MSDOS_BIN for a in argvs)
    assert not any(a[:2] == [ms.DISKUTIL_BIN, "mount"] for a in argvs)


def test_mount_msdos_failure_restores_fskit():
    """mount_msdos non-zero after a good FSKit unmount -> raise + restore.

    Teardown must attempt restore_fskit_mount (a `diskutil mount` of the device)
    so the drive is not left unmounted silently.
    """
    def _run(argv, timeout):
        if argv[0] == ms.MOUNT_MSDOS_BIN:
            return _cp(returncode=1, stderr="mount_msdos failed")
        # FSKit unmount and the restore `diskutil mount` both succeed.
        return _cp(returncode=0)

    # No stray /Volumes reappearance; work_mount never becomes a mountpoint.
    with mock.patch.object(ms, "resolve_device_node", return_value=DEVICE), \
        mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch.object(ms, "private_mountpoint", return_value="/private/tmp/efis-datamanager/EFIS_3"), \
        mock.patch("os.path.ismount", return_value=False), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        with pytest.raises(ms.MountSwapError):
            with ms.msdos_mount(FSKIT_MP):
                pytest.fail("body must not run when mount_msdos fails")

    argvs = _argv_calls(rp)
    # FSKit unmount attempted.
    assert argvs[0][:2] == [ms.DISKUTIL_BIN, "unmount"]
    # mount_msdos attempted.
    assert any(a[0] == ms.MOUNT_MSDOS_BIN for a in argvs)
    # Terminal state: restore_fskit_mount -> `diskutil mount <device>`.
    assert [ms.DISKUTIL_BIN, "mount", DEVICE] in argvs


def test_happy_path_yields_work_mount_and_restores_fskit():
    """unmount ok, mount_msdos ok, ismount True -> yields work_mount.

    On context exit teardown unmounts the work mount and restores FSKit, and the
    swap_state ends "restored". The poller pause/resume hooks are exercised.
    """
    work_mount = "/private/tmp/efis-datamanager/EFIS_3"
    poller = _FakePoller()

    # Track ismount so the work mount looks mounted while inside the context and
    # after robust_unmount reports it unmounted during teardown.
    ismount_state = {"work_mounted": True}

    def _ismount(path):
        if path == work_mount:
            return ismount_state["work_mounted"]
        return False

    def _run(argv, timeout):
        # All privileged calls succeed. When work is unmounted during teardown,
        # flip the ismount flag so robust_unmount verifies success.
        if argv[:2] == [ms.DISKUTIL_BIN, "unmount"] and work_mount in argv:
            ismount_state["work_mounted"] = False
        return _cp(returncode=0)

    seen_yield = {}
    captured = []
    with mock.patch.object(ms, "resolve_device_node", return_value=DEVICE), \
        mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch.object(ms, "private_mountpoint", return_value=work_mount), \
        mock.patch.object(ms, "SwapState", side_effect=_capturing_swapstate(captured)), \
        mock.patch("os.path.ismount", side_effect=_ismount), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        with ms.msdos_mount(FSKIT_MP, poller=poller) as wm:
            seen_yield["wm"] = wm

    # Yielded the private work mount.
    assert seen_yield["wm"] == work_mount

    # Swap state advanced to RESTORED on clean teardown.
    state = captured[0]
    assert state.swap_state == ms.SWAP_STATE_RESTORED

    argvs = _argv_calls(rp)
    # FSKit unmount, mount_msdos, work unmount, and FSKit restore all happened.
    assert argvs[0][:2] == [ms.DISKUTIL_BIN, "unmount"]
    assert argvs[0][-1] == FSKIT_MP
    assert any(a[0] == ms.MOUNT_MSDOS_BIN for a in argvs)
    assert any(a[:2] == [ms.DISKUTIL_BIN, "unmount"] and work_mount in a for a in argvs)
    assert [ms.DISKUTIL_BIN, "mount", DEVICE] in argvs

    # Poller was quiesced and un-quiesced exactly once each.
    assert poller.paused == 1
    assert poller.resumed == 1


def test_restore_failure_is_needs_reinsert_not_silent(caplog):
    """If FSKit restore fails, teardown logs needs-reinsert and never reports success.

    The swap_state must NOT advance to RESTORED, proving the drive is not
    silently reported current.
    """
    work_mount = "/private/tmp/efis-datamanager/EFIS_3"
    ismount_state = {"work_mounted": True}

    def _ismount(path):
        if path == work_mount:
            return ismount_state["work_mounted"]
        return False

    def _run(argv, timeout):
        if argv[:2] == [ms.DISKUTIL_BIN, "unmount"] and work_mount in argv:
            ismount_state["work_mounted"] = False
            return _cp(returncode=0)
        if argv[:2] == [ms.DISKUTIL_BIN, "mount"]:
            # The FSKit restore genuinely fails.
            return _cp(returncode=1, stderr="could not mount")
        return _cp(returncode=0)

    captured = []
    with mock.patch.object(ms, "resolve_device_node", return_value=DEVICE), \
        mock.patch.object(ms, "run_privileged", side_effect=_run), \
        mock.patch.object(ms, "private_mountpoint", return_value=work_mount), \
        mock.patch.object(ms, "SwapState", side_effect=_capturing_swapstate(captured)), \
        mock.patch("os.path.ismount", side_effect=_ismount), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        with ms.msdos_mount(FSKIT_MP) as wm:
            assert wm == work_mount

    state = captured[0]
    # Restore failed: state stays MOUNTED (never advances to RESTORED / success).
    assert state.swap_state == ms.SWAP_STATE_MOUNTED
    # A loud needs-reinsert log was emitted.
    assert any("needs reinsertion" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# robust_unmount
# ---------------------------------------------------------------------------


def test_robust_unmount_not_a_mountpoint_returns_true_no_unmount():
    """already-not-a-mountpoint -> True immediately, no unmount call."""
    with mock.patch("os.path.ismount", return_value=False), \
        mock.patch.object(ms, "run_privileged") as rp, \
        mock.patch("time.sleep") as slp, \
        mock.patch("os.sync"):
        assert ms.robust_unmount("/private/tmp/efis-datamanager/EFIS_3", DEVICE) is True

    rp.assert_not_called()
    slp.assert_not_called()


def test_robust_unmount_busy_then_success_prefers_diskutil_and_retries():
    """"Resource busy" on first attempt then success -> True.

    Prefers `diskutil unmount` (never raw umount), retries, and never escalates
    to force because a later bounded attempt succeeds.
    """
    work_mount = "/private/tmp/efis-datamanager/EFIS_3"
    calls = {"n": 0}
    # ismount: True initially; becomes False after the successful (2nd) unmount.
    ismount_seq = {"mounted": True}

    def _ismount(path):
        return ismount_seq["mounted"]

    def _run(argv, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt: busy failure.
            return _cp(returncode=1, stderr="Resource busy -- try again")
        # Second attempt: success; flip ismount so verification passes.
        ismount_seq["mounted"] = False
        return _cp(returncode=0)

    with mock.patch("os.path.ismount", side_effect=_ismount), \
        mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch.object(ms, "_lsof_holders", return_value=""), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        assert ms.robust_unmount(work_mount, DEVICE) is True

    argvs = _argv_calls(rp)
    # Every unmount attempt used `diskutil unmount` (not raw umount) and none
    # used the `force` escalation (success came before that).
    assert all(a[:2] == [ms.DISKUTIL_BIN, "unmount"] for a in argvs)
    assert not any("force" in a for a in argvs)
    # Retried at least twice.
    assert len(argvs) >= 2


def test_robust_unmount_all_busy_escalates_to_force_then_fails():
    """All bounded attempts busy -> escalate to force; force also busy -> False."""
    work_mount = "/private/tmp/efis-datamanager/EFIS_3"

    def _run(argv, timeout):
        # Every call (bounded retries + force) reports busy / non-zero.
        return _cp(returncode=1, stderr="Resource busy")

    with mock.patch("os.path.ismount", return_value=True), \
        mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch.object(ms, "_lsof_holders", return_value=""), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        assert ms.robust_unmount(work_mount, DEVICE) is False

    argvs = _argv_calls(rp)
    # Bounded retries used plain `diskutil unmount`.
    plain = [a for a in argvs if a[:2] == [ms.DISKUTIL_BIN, "unmount"] and "force" not in a]
    assert len(plain) == ms.UNMOUNT_MAX_ATTEMPTS
    # Escalated to `diskutil unmount force <work_mount>` exactly once as a last resort.
    assert [ms.DISKUTIL_BIN, "unmount", "force", work_mount] in argvs


def test_robust_unmount_force_succeeds_returns_true():
    """All bounded attempts busy, but the force escalation clears it -> True."""
    work_mount = "/private/tmp/efis-datamanager/EFIS_3"
    state = {"mounted": True}

    def _ismount(path):
        return state["mounted"]

    def _run(argv, timeout):
        if "force" in argv:
            state["mounted"] = False
            return _cp(returncode=0)
        return _cp(returncode=1, stderr="Resource busy")

    with mock.patch("os.path.ismount", side_effect=_ismount), \
        mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch.object(ms, "_lsof_holders", return_value=""), \
        mock.patch("time.sleep"), \
        mock.patch("os.sync"):
        assert ms.robust_unmount(work_mount, DEVICE) is True

    argvs = _argv_calls(rp)
    assert [ms.DISKUTIL_BIN, "unmount", "force", work_mount] in argvs


# ---------------------------------------------------------------------------
# restore_fskit_mount
# ---------------------------------------------------------------------------


def test_restore_fskit_mount_success():
    """diskutil mount rc==0 on the first attempt -> True with a single call.

    The settle sleep runs before the (successful) mount, so exactly one sleep
    and one privileged call happen.
    """
    with mock.patch.object(ms, "run_privileged", return_value=_cp(returncode=0)) as rp, \
        mock.patch("time.sleep") as slp:
        assert ms.restore_fskit_mount(DEVICE) is True
    assert _argv_calls(rp) == [[ms.DISKUTIL_BIN, "mount", DEVICE]]
    # A single growing-settle sleep preceded the one successful mount attempt.
    assert slp.call_count == 1


def test_restore_fskit_mount_already_mounted_is_success():
    """rc!=0 but output says "already mounted" -> treated as True."""
    already = _cp(returncode=1, stderr="Volume EFIS_3 on disk4s1 is already mounted")
    with mock.patch.object(ms, "run_privileged", return_value=already), \
        mock.patch("time.sleep"):
        assert ms.restore_fskit_mount(DEVICE) is True


def test_restore_fskit_mount_retries_after_transient_failure():
    """rc=1 on the first 2 attempts then rc==0 -> True.

    Models the post-force-unmount transient refusal: the device settles and the
    3rd attempt succeeds. Assert it retried (multiple run_privileged calls) and
    slept a growing settle before each attempt.
    """
    calls = {"n": 0}

    def _run(argv, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            return _cp(returncode=1, stderr="failed to mount; try readOnly")
        return _cp(returncode=0)

    with mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch("time.sleep") as slp:
        assert ms.restore_fskit_mount(DEVICE) is True

    argvs = _argv_calls(rp)
    # Three mount attempts (two transient failures then success).
    assert argvs == [[ms.DISKUTIL_BIN, "mount", DEVICE]] * 3
    # A growing settle preceded every attempt: attempt N waits SETTLE * N.
    assert slp.call_count == 3
    slept = [c.args[0] for c in slp.call_args_list]
    assert slept == [
        ms.SETTLE_WAIT_SECONDS * 1,
        ms.SETTLE_WAIT_SECONDS * 2,
        ms.SETTLE_WAIT_SECONDS * 3,
    ]


def test_restore_fskit_mount_already_mounted_on_retry_is_success():
    """rc=1 transient first, then an "already mounted" result on retry -> True."""
    calls = {"n": 0}

    def _run(argv, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _cp(returncode=1, stderr="failed to mount; try readOnly")
        return _cp(returncode=1, stderr="Volume EFIS_3 on disk4s1 is already mounted")

    with mock.patch.object(ms, "run_privileged", side_effect=_run) as rp, \
        mock.patch("time.sleep"):
        assert ms.restore_fskit_mount(DEVICE) is True

    # Retried once; the retry's "already mounted" is treated as restored.
    assert len(_argv_calls(rp)) == 2


def test_restore_fskit_mount_genuine_failure(caplog):
    """Genuine non-zero failure on ALL attempts -> False and needs-reinsert log.

    Exhausts RESTORE_MAX_ATTEMPTS (one mount call + one settle sleep each) then
    logs the loud needs-reinsert error.
    """
    fail = _cp(returncode=1, stderr="mount failed: device not present")
    with mock.patch.object(ms, "run_privileged", return_value=fail) as rp, \
        mock.patch("time.sleep") as slp:
        assert ms.restore_fskit_mount(DEVICE) is False

    assert len(_argv_calls(rp)) == ms.RESTORE_MAX_ATTEMPTS
    assert slp.call_count == ms.RESTORE_MAX_ATTEMPTS
    assert any("needs reinsertion" in r.getMessage() for r in caplog.records)


def test_restore_fskit_mount_exception_is_failed_attempt():
    """A raised run_privileged is treated as a failed attempt, not a crash.

    All attempts raise -> the loop swallows each, exhausts the retries, and
    returns False without propagating the exception.
    """
    with mock.patch.object(ms, "run_privileged", side_effect=OSError("boom")) as rp, \
        mock.patch("time.sleep"):
        assert ms.restore_fskit_mount(DEVICE) is False
    assert len(_argv_calls(rp)) == ms.RESTORE_MAX_ATTEMPTS
