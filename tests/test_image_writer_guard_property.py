# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the destructive-write device guard (image-based-drive-write 2.2).

Feature: image-based-drive-write, Property 4: destructive-write safety.

Covers task 2.2 (design Property 4 — Destructive-write safety): the guard must
PERMIT only a fully-matching, confirmed target and must REFUSE a target with any
single attribute wrong. The property is checked across many Hypothesis-generated
targets at >=100 examples.

For each generated case we build a baseline "good" diskutil info dict (a
removable-FAT32 slice with a known size, label, and identity) plus a confirmed
call, which must PASS; and a sibling case where EXACTLY ONE of the guarded
attributes is corrupted — one of:

  * non-removable (base check: RemovableMedia/Internal/Ejectable flipped),
  * non-FAT32 (base check: FilesystemType/Content/FilesystemName changed),
  * wrong size (expected_size_bytes != the slice Size),
  * wrong label (expected_label != the slice VolumeName),
  * wrong identity (expected_identity_id != the slice VolumeUUID),
  * unconfirmed (confirmed=False),

which must RAISE (DeviceGuardError, a PrivilegeError).

Device facts are supplied by mocking mount_swap._device_info (NOT real
diskutil), mirroring tests/test_mount_swap_privileged.py and
tests/test_image_writer_guard.py. No drive is mounted.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.6
"""

from unittest import mock

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

from efis_data_manager import image_writer as iw
from efis_data_manager import mount_swap as ms


DEVICE = "/dev/disk4s1"


def _good_info(size: int, label: str, identity: str) -> dict:
    """A diskutil info dict for a removable-FAT32 slice.

    Pure: builds a fresh dict per call so no example mutates another's state.
    "Size" (== IOKitSize) is the raw slice byte length dd targets; VolumeSize /
    TotalSize are the smaller usable capacity and are intentionally NOT what the
    guard matches.
    """
    return {
        "Internal": False,
        "RemovableMedia": True,
        "Ejectable": True,
        "FilesystemType": "msdos",
        "Content": "DOS_FAT_32",
        "FilesystemName": "MS-DOS FAT32",
        "Size": size,
        "IOKitSize": size,
        "VolumeSize": max(1, size - 15024128),
        "TotalSize": max(1, size - 15024128),
        "VolumeName": label,
        "VolumeUUID": identity,
    }


# Strategies for the baseline-good inputs. Labels/identities are kept to simple
# non-empty tokens so a corrupted sibling can always differ meaningfully.
sizes = st.integers(min_value=1, max_value=2 ** 50)
labels = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=90),  # A-Z
    min_size=1,
    max_size=12,
)
identities = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=70),  # 0-9 A-F
    min_size=1,
    max_size=36,
)

# The single attribute to corrupt in the "must refuse" half.
MISMATCH_KINDS = [
    "non_removable",
    "non_fat32",
    "wrong_size",
    "wrong_label",
    "wrong_identity",
    "unconfirmed",
]


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    size=sizes,
    label=labels,
    identity=identities,
    mismatch=st.sampled_from(MISMATCH_KINDS),
    size_delta=st.integers(min_value=1, max_value=2 ** 20),
)
def test_guard_permits_match_and_refuses_single_mismatch(
    size, label, identity, mismatch, size_delta
):
    """A fully-matching confirmed node passes; exactly one wrong attribute refuses."""

    # --- PERMIT: the uncorrupted, fully-matching, confirmed target passes. ----
    good = _good_info(size, label, identity)
    with mock.patch.object(ms, "_device_info", return_value=dict(good)):
        info = iw.device_guard(
            DEVICE,
            expected_size_bytes=size,
            expected_label=label,
            expected_identity_id=identity,
            confirmed=True,
        )
    assert isinstance(info, iw.DeviceGuardInfo)
    assert info.slice_size_bytes == size
    assert info.volume_label == label
    assert info.identity_id == identity
    assert info.is_removable is True
    assert info.is_fat32 is True

    # --- REFUSE: corrupt EXACTLY ONE guarded attribute, everything else good. -
    # The diskutil info dict and the expected_* arguments both start from the
    # matching baseline; the chosen single corruption is applied to one side.
    info_dict = _good_info(size, label, identity)
    exp_size = size
    exp_label = label
    exp_identity = identity
    confirmed = True

    if mismatch == "non_removable":
        # Base check (validate_device_node) rejects a non-removable slice.
        info_dict["Internal"] = True
        info_dict["RemovableMedia"] = False
        info_dict["Ejectable"] = False
    elif mismatch == "non_fat32":
        # Base check rejects a non-FAT32 slice.
        info_dict["FilesystemType"] = "apfs"
        info_dict["Content"] = "Apple_APFS"
        info_dict["FilesystemName"] = "APFS"
    elif mismatch == "wrong_size":
        # The expected image size differs from the slice Size by a nonzero delta.
        exp_size = size + size_delta
    elif mismatch == "wrong_label":
        # Expected label differs from the slice VolumeName.
        exp_label = label + "_X"
    elif mismatch == "wrong_identity":
        # Expected identity differs from the slice VolumeUUID.
        exp_identity = identity + "_X"
    elif mismatch == "unconfirmed":
        # No explicit confirmation for this specific target.
        confirmed = False

    with mock.patch.object(ms, "_device_info", return_value=dict(info_dict)):
        with pytest.raises(ms.PrivilegeError):
            iw.device_guard(
                DEVICE,
                expected_size_bytes=exp_size,
                expected_label=exp_label,
                expected_identity_id=exp_identity,
                confirmed=confirmed,
            )
