# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for run_privileged argument validation (chart-sync-stall-fix 1.4).

Covers task 1.4: the single privilege choke point run_privileged must

  - build an argv ARRAY ["/usr/bin/sudo", "-n", *argv] and hand it to
    subprocess.run (never a shell string, never shell=True);
  - validate BEFORE executing, so a rejected call reaches PrivilegeError and
    NOTHING is executed:
      * a shell string instead of a list,
      * a non-whitelisted binary (e.g. /bin/rm),
      * a mount_msdos whose mountpoint is outside PRIVATE_MOUNT_BASE (or is the
        base itself),
      * a whole-disk device node (/dev/disk4, no sNN),
      * an argv element that is not a string;
  - accept a valid mount_msdos (/dev/diskNsN + a mountpoint under
    PRIVATE_MOUNT_BASE) and a diskutil unmount of a valid removable-FAT32
    device, reaching subprocess.run in both cases.

Device-node validation is exercised by mocking mount_swap._device_info (NOT
real diskutil), returning a removable-FAT32 dict for accept cases and a
non-removable / non-FAT32 dict for the reject case.

Requirements: 8.1
"""

from unittest import mock

import pytest

from efis_data_manager import mount_swap as ms


# Known private base used throughout so mountpoint assertions are stable
# regardless of the module default.
BASE = "/private/tmp/efis-datamanager"

# A diskutil info dict that _is_removable_fat32() accepts.
_REMOVABLE_FAT32 = {
    "Internal": False,
    "RemovableMedia": True,
    "Ejectable": True,
    "FilesystemType": "msdos",
    "Content": "DOS_FAT_32",
    "FilesystemName": "MS-DOS FAT32",
}

# A diskutil info dict that _is_removable_fat32() rejects (internal, non-FAT32).
_NON_REMOVABLE = {
    "Internal": True,
    "RemovableMedia": False,
    "Ejectable": False,
    "FilesystemType": "apfs",
    "Content": "Apple_APFS",
    "FilesystemName": "APFS",
}


@pytest.fixture(autouse=True)
def _pin_base(monkeypatch):
    """Pin PRIVATE_MOUNT_BASE so mountpoint checks are deterministic."""
    monkeypatch.setattr(ms, "PRIVATE_MOUNT_BASE", BASE)


@pytest.fixture
def run_mock():
    """Patch mount_swap.subprocess.run and return the mock.

    A CompletedProcess-shaped return is provided so run_privileged can return
    normally on the accept paths.
    """
    with mock.patch.object(ms.subprocess, "run") as m:
        m.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        yield m


@pytest.fixture
def device_info_removable():
    """Make _device_info report a removable-FAT32 slice for every node."""
    with mock.patch.object(ms, "_device_info", return_value=dict(_REMOVABLE_FAT32)) as m:
        yield m


# --- Accept cases: build argv array and reach subprocess.run ----------------


def test_mount_msdos_builds_sudo_argv_array(run_mock, device_info_removable):
    device = "/dev/disk4s1"
    mountpoint = f"{BASE}/EFIS_3"
    argv = [ms.MOUNT_MSDOS_BIN, device, mountpoint]

    ms.run_privileged(argv, timeout=60)

    run_mock.assert_called_once()
    called_args, called_kwargs = run_mock.call_args
    # First positional arg is the exact argv array (never a shell string).
    assert called_args[0] == ["/usr/bin/sudo", "-n", ms.MOUNT_MSDOS_BIN, device, mountpoint]
    assert isinstance(called_args[0], list)
    # shell=True is never used.
    assert called_kwargs.get("shell") is not True
    assert "shell" not in called_kwargs


def test_diskutil_unmount_removable_reaches_subprocess(run_mock, device_info_removable):
    device = "/dev/disk4s1"
    argv = [ms.DISKUTIL_BIN, "unmount", device]

    ms.run_privileged(argv, timeout=60)

    run_mock.assert_called_once()
    called_args, called_kwargs = run_mock.call_args
    assert called_args[0] == ["/usr/bin/sudo", "-n", ms.DISKUTIL_BIN, "unmount", device]
    assert called_kwargs.get("shell") is not True


def test_argv_is_never_a_shell_string(run_mock, device_info_removable):
    # Even a benign accepted call passes a list, not a joined string.
    argv = [ms.DISKUTIL_BIN, "unmount", "/dev/disk4s1"]
    ms.run_privileged(argv, timeout=60)
    passed = run_mock.call_args[0][0]
    assert not isinstance(passed, str)
    assert all(isinstance(tok, str) for tok in passed)


# --- Reject cases: PrivilegeError raised, subprocess.run NOT called ---------


def test_shell_string_rejected(run_mock, device_info_removable):
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged(f"{ms.MOUNT_MSDOS_BIN} /dev/disk4s1 {BASE}/EFIS_3", timeout=60)
    run_mock.assert_not_called()


def test_non_whitelisted_binary_rejected(run_mock, device_info_removable):
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged(["/bin/rm", "-rf", f"{BASE}/EFIS_3"], timeout=60)
    run_mock.assert_not_called()


def test_mount_msdos_mountpoint_outside_base_rejected(run_mock, device_info_removable):
    argv = [ms.MOUNT_MSDOS_BIN, "/dev/disk4s1", "/Volumes/x"]
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged(argv, timeout=60)
    run_mock.assert_not_called()


def test_mount_msdos_mountpoint_is_base_itself_rejected(run_mock, device_info_removable):
    argv = [ms.MOUNT_MSDOS_BIN, "/dev/disk4s1", BASE]
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged(argv, timeout=60)
    run_mock.assert_not_called()


def test_whole_disk_device_node_rejected(run_mock, device_info_removable):
    # /dev/disk4 has no sNN slice suffix -> refused by the syntactic check.
    argv = [ms.MOUNT_MSDOS_BIN, "/dev/disk4", f"{BASE}/EFIS_3"]
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged(argv, timeout=60)
    run_mock.assert_not_called()


def test_non_string_argv_element_rejected(run_mock, device_info_removable):
    argv = [ms.DISKUTIL_BIN, "unmount", 1234]
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged(argv, timeout=60)
    run_mock.assert_not_called()


def test_non_removable_non_fat32_device_rejected(run_mock):
    # _device_info reports an internal, non-FAT32 slice -> refused.
    with mock.patch.object(ms, "_device_info", return_value=dict(_NON_REMOVABLE)):
        argv = [ms.DISKUTIL_BIN, "unmount", "/dev/disk4s1"]
        with pytest.raises(ms.PrivilegeError):
            ms.run_privileged(argv, timeout=60)
    run_mock.assert_not_called()


def test_device_info_unavailable_rejected(run_mock):
    # A None _device_info (diskutil unavailable) is refused, not executed.
    with mock.patch.object(ms, "_device_info", return_value=None):
        argv = [ms.DISKUTIL_BIN, "unmount", "/dev/disk4s1"]
        with pytest.raises(ms.PrivilegeError):
            ms.run_privileged(argv, timeout=60)
    run_mock.assert_not_called()


def test_empty_argv_rejected(run_mock, device_info_removable):
    with pytest.raises(ms.PrivilegeError):
        ms.run_privileged([], timeout=60)
    run_mock.assert_not_called()
