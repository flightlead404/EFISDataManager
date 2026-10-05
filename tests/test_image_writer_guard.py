# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the destructive-write device guard (image-based-drive-write 2.1).

Covers task 2.1: image_writer.device_guard must gate every raw ``dd`` by
extending mount_swap.validate_device_node with the destructive checks the design
calls for (design Property 4 — Destructive-write safety):

  - reuse the base checks (/dev/diskNsN + removable + FAT32 via diskutil) so a
    non-slice / non-removable / non-FAT32 / unverifiable node is refused;
  - require an explicit confirmation flag;
  - require the slice byte size to equal the expected image size;
  - require the volume label to equal the expected label (when supplied);
  - require the OS identity (VolumeUUID) to equal the expected identity (when
    supplied);
  - RAISE (DeviceGuardError, a PrivilegeError) on any mismatch or any
    unverifiable check, and return a DeviceGuardInfo only for a fully-matching,
    confirmed node.

Device-node facts are exercised by mocking mount_swap._device_info (NOT real
diskutil), mirroring tests/test_mount_swap_privileged.py. No drive is mounted.

Requirements: 2.4, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6
"""

from unittest import mock

import pytest

from efis_data_manager import image_writer as iw
from efis_data_manager import mount_swap as ms


DEVICE = "/dev/disk4s1"
SIZE = 30783045632
LABEL = "EFIS_4"
IDENTITY = "9B060025-453D-3841-9EDD-6AE3A3A6E1CB"

# A diskutil info dict for a removable-FAT32 slice with a known size, label, and
# identity — the fully-matching target. "Size" is the raw slice byte length dd
# targets (VolumeSize/TotalSize are the smaller usable capacity and must NOT be
# what the guard matches).
_GOOD_INFO = {
    "Internal": False,
    "RemovableMedia": True,
    "Ejectable": True,
    "FilesystemType": "msdos",
    "Content": "DOS_FAT_32",
    "FilesystemName": "MS-DOS FAT32",
    "Size": SIZE,
    "IOKitSize": SIZE,
    "VolumeSize": 30768021504,
    "TotalSize": 30768021504,
    "VolumeName": LABEL,
    "VolumeUUID": IDENTITY,
}

# An internal, non-FAT32 slice that validate_device_node rejects.
_NON_REMOVABLE_INFO = {
    "Internal": True,
    "RemovableMedia": False,
    "Ejectable": False,
    "FilesystemType": "apfs",
    "Content": "Apple_APFS",
    "FilesystemName": "APFS",
}


@pytest.fixture
def device_info_good():
    """Make _device_info report the fully-matching removable-FAT32 slice."""
    with mock.patch.object(ms, "_device_info", return_value=dict(_GOOD_INFO)) as m:
        yield m


# --- Accept case: fully-matching, confirmed node is permitted ---------------


def test_fully_matching_confirmed_node_passes(device_info_good):
    info = iw.device_guard(
        DEVICE,
        expected_size_bytes=SIZE,
        expected_label=LABEL,
        expected_identity_id=IDENTITY,
        confirmed=True,
    )
    assert isinstance(info, iw.DeviceGuardInfo)
    assert info.device_node == DEVICE
    assert info.is_removable is True
    assert info.is_fat32 is True
    assert info.slice_size_bytes == SIZE
    assert info.volume_label == LABEL
    assert info.identity_id == IDENTITY


def test_passes_when_optional_expectations_omitted(device_info_good):
    # Only size + confirmation are mandatory; label/identity default to None
    # (not enforced) and the guard still passes, reporting the observed facts.
    info = iw.device_guard(DEVICE, expected_size_bytes=SIZE, confirmed=True)
    assert info.volume_label == LABEL
    assert info.identity_id == IDENTITY


# --- Reject cases: DeviceGuardError raised ----------------------------------


def test_unconfirmed_node_refused(device_info_good):
    with pytest.raises(iw.DeviceGuardError):
        iw.device_guard(DEVICE, expected_size_bytes=SIZE, confirmed=False)


def test_confirmed_defaults_to_refusal(device_info_good):
    # Omitting confirmed entirely is the same as confirmed=False.
    with pytest.raises(iw.DeviceGuardError):
        iw.device_guard(DEVICE, expected_size_bytes=SIZE)


def test_size_mismatch_refused(device_info_good):
    with pytest.raises(iw.DeviceGuardError):
        iw.device_guard(DEVICE, expected_size_bytes=SIZE + 1, confirmed=True)


def test_label_mismatch_refused(device_info_good):
    with pytest.raises(iw.DeviceGuardError):
        iw.device_guard(
            DEVICE,
            expected_size_bytes=SIZE,
            expected_label="WRONG",
            confirmed=True,
        )


def test_identity_mismatch_refused(device_info_good):
    with pytest.raises(iw.DeviceGuardError):
        iw.device_guard(
            DEVICE,
            expected_size_bytes=SIZE,
            expected_identity_id="00000000-0000-0000-0000-000000000000",
            confirmed=True,
        )


def test_non_removable_non_fat32_refused():
    # Base check (validate_device_node) rejects before any destructive check.
    with mock.patch.object(ms, "_device_info", return_value=dict(_NON_REMOVABLE_INFO)):
        with pytest.raises(ms.PrivilegeError):
            iw.device_guard(DEVICE, expected_size_bytes=SIZE, confirmed=True)


def test_whole_disk_node_refused(device_info_good):
    # /dev/disk4 has no sNN slice suffix -> refused by the syntactic base check.
    with pytest.raises(ms.PrivilegeError):
        iw.device_guard("/dev/disk4", expected_size_bytes=SIZE, confirmed=True)


def test_device_info_unavailable_refused():
    # diskutil info unavailable -> base check already refuses an unverifiable
    # node (raises PrivilegeError), so nothing proceeds to a destructive check.
    with mock.patch.object(ms, "_device_info", return_value=None):
        with pytest.raises(ms.PrivilegeError):
            iw.device_guard(DEVICE, expected_size_bytes=SIZE, confirmed=True)


def test_missing_size_key_refused():
    # A removable-FAT32 slice whose diskutil info lacks a usable size key: the
    # base check passes but the destructive size check cannot be verified ->
    # refuse rather than guess.
    info = {k: v for k, v in _GOOD_INFO.items() if k not in ("Size", "IOKitSize")}
    with mock.patch.object(ms, "_device_info", return_value=info):
        with pytest.raises(iw.DeviceGuardError):
            iw.device_guard(DEVICE, expected_size_bytes=SIZE, confirmed=True)


def test_missing_label_with_expectation_refused():
    # An expected label that cannot be verified (no VolumeName) is a refusal,
    # not a silent pass.
    info = {k: v for k, v in _GOOD_INFO.items() if k != "VolumeName"}
    with mock.patch.object(ms, "_device_info", return_value=info):
        with pytest.raises(iw.DeviceGuardError):
            iw.device_guard(
                DEVICE,
                expected_size_bytes=SIZE,
                expected_label=LABEL,
                confirmed=True,
            )


def test_non_positive_expected_size_refused(device_info_good):
    with pytest.raises(iw.DeviceGuardError):
        iw.device_guard(DEVICE, expected_size_bytes=0, confirmed=True)


def test_guard_error_is_a_privilege_error():
    # The refusal contract: DeviceGuardError must be catchable as PrivilegeError.
    assert issubclass(iw.DeviceGuardError, ms.PrivilegeError)


def test_guard_does_not_mount_the_drive(device_info_good):
    # Property 1: the guard reads facts from diskutil info only; it must not
    # route anything through the privileged mount shim.
    with mock.patch.object(ms, "run_privileged") as run_priv:
        iw.device_guard(DEVICE, expected_size_bytes=SIZE, confirmed=True)
    run_priv.assert_not_called()
