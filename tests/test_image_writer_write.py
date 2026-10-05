# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the end-to-end image writer (image-based-drive-write task 7).

Covers image_writer.write wiring the guard -> dd -> verify flow and returning a
WriteResult (design Data Models):

  - happy path: guard passes -> dd runs -> readback verifies -> status
    "complete", verified True, bytes_written set to the slice size;
  - guard refusal (PrivilegeError/DeviceGuardError): status "incomplete", NO dd
    issued, local image file untouched, bytes_written 0;
  - verify failure (readback hash mismatch): status "incomplete", verified
    False, error surfaced; the local image file is untouched;
  - dd interrupted / fails to run: status "aborted", local image untouched;
  - whole-image unit of work: a single WriteResult, no per-family state.

Nothing real runs: device_guard / dd_write_image / raw_read_source /
write_verifier.verify are mocked (and the one subprocess-level path is reached
only through those mocks). No drive is touched, no real dd.

Requirements: 2.3, 4.2, 5.1, 5.3, 6.1, 7.1, 13.1, 13.2, 13.3
"""

import hashlib
from unittest import mock

import pytest

from efis_data_manager import image_writer as iw
from efis_data_manager import mount_swap as ms
from efis_data_manager import write_verifier


DEVICE = "/dev/disk4s1"
LABEL = "EFIS_4"
IDENTITY = "9B060025-453D-3841-9EDD-6AE3A3A6E1CB"
IMAGE_CONTENT = b"EFIS image bytes \x00\x01\x02\xff" * 64


@pytest.fixture
def image_file(tmp_path):
    """A real on-disk image file so os.path.getsize and hashing are genuine."""
    path = tmp_path / "EFIS_USB.img"
    path.write_bytes(IMAGE_CONTENT)
    return str(path)


@pytest.fixture
def image_size():
    return len(IMAGE_CONTENT)


def _guard_info(size):
    """A passing DeviceGuardInfo for the matching slice."""
    return iw.DeviceGuardInfo(
        device_node=DEVICE,
        is_removable=True,
        is_fat32=True,
        slice_size_bytes=size,
        volume_label=LABEL,
        identity_id=IDENTITY,
    )


# --- Happy path -------------------------------------------------------------


def test_write_happy_path_complete_and_verified(image_file, image_size):
    """Guard passes -> dd -> readback matches -> status complete."""
    # The raw readback returns the exact image bytes, so verify() matches for
    # real (no mocking of the hash — the readback callable yields image bytes).
    readback = lambda: IMAGE_CONTENT

    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ) as guard, mock.patch.object(
        iw, "dd_write_image"
    ) as dd, mock.patch.object(
        iw, "raw_read_source", return_value=readback
    ) as rrs:
        result = iw.write(
            image_file,
            DEVICE,
            expected_size_bytes=image_size,
            expected_label=LABEL,
            expected_identity_id=IDENTITY,
            confirmed=True,
        )

    assert result.status == "complete"
    assert result.verified is True
    assert result.bytes_written == image_size
    assert result.error is None
    assert result.device_node == DEVICE
    # Guard ran first and received the forwarded expectations (Decision B:
    # expected_identity_id is the OS-level VolumeUUID).
    guard.assert_called_once()
    _, kwargs = guard.call_args
    assert kwargs["expected_size_bytes"] == image_size
    assert kwargs["expected_label"] == LABEL
    assert kwargs["expected_identity_id"] == IDENTITY
    assert kwargs["confirmed"] is True
    dd.assert_called_once()
    rrs.assert_called_once()


def test_write_defaults_expected_size_to_image_file_size(image_file, image_size):
    """expected_size_bytes defaults to the image file's own size."""
    readback = lambda: IMAGE_CONTENT
    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ) as guard, mock.patch.object(iw, "dd_write_image"), mock.patch.object(
        iw, "raw_read_source", return_value=readback
    ):
        result = iw.write(image_file, DEVICE, confirmed=True)

    assert result.status == "complete"
    _, kwargs = guard.call_args
    assert kwargs["expected_size_bytes"] == image_size


# --- Guard refusal: no dd, local image untouched ----------------------------


def test_write_guard_refusal_does_not_dd_and_leaves_image_untouched(
    image_file, image_size
):
    """A guard refusal reports incomplete, issues NO dd, leaves image intact."""
    before = open(image_file, "rb").read()

    with mock.patch.object(
        iw, "device_guard", side_effect=iw.DeviceGuardError("unconfirmed target")
    ), mock.patch.object(iw, "dd_write_image") as dd, mock.patch.object(
        iw, "raw_read_source"
    ) as rrs:
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=False
        )

    assert result.status == "incomplete"
    assert result.verified is False
    assert result.bytes_written == 0
    assert "guard refused" in result.error
    # The dd write and the readback must NOT have been attempted.
    dd.assert_not_called()
    rrs.assert_not_called()
    # Local image byte-identical before and after (Req 13.1/13.2).
    assert open(image_file, "rb").read() == before


def test_write_guard_privilege_error_also_refused(image_file, image_size):
    """A base PrivilegeError (not just DeviceGuardError) is handled as refusal."""
    with mock.patch.object(
        iw, "device_guard", side_effect=ms.PrivilegeError("not a removable slice")
    ), mock.patch.object(iw, "dd_write_image") as dd:
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )
    assert result.status == "incomplete"
    assert result.bytes_written == 0
    dd.assert_not_called()


# --- Verify failure: incomplete, error surfaced, image untouched ------------


def test_write_verify_mismatch_reports_incomplete(image_file, image_size):
    """dd ran but the readback hash mismatches -> incomplete, error surfaced."""
    before = open(image_file, "rb").read()
    # Readback returns DIFFERENT bytes than the image -> verify() returns False
    # for real (no verify mock — exercises the actual hash comparison).
    corrupt = bytearray(IMAGE_CONTENT)
    corrupt[0] ^= 0xFF
    readback = lambda: bytes(corrupt)

    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ), mock.patch.object(iw, "dd_write_image") as dd, mock.patch.object(
        iw, "raw_read_source", return_value=readback
    ):
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )

    assert result.status == "incomplete"
    assert result.verified is False
    assert result.bytes_written == image_size  # the write itself ran
    assert "did not match" in result.error
    dd.assert_called_once()
    # Local image untouched even though the drive readback mismatched.
    assert open(image_file, "rb").read() == before


def test_write_verify_short_read_reports_incomplete(image_file, image_size):
    """A truncated readback (short read) can never match -> incomplete."""
    readback = lambda: IMAGE_CONTENT[: image_size // 2]
    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ), mock.patch.object(iw, "dd_write_image"), mock.patch.object(
        iw, "raw_read_source", return_value=readback
    ):
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )
    assert result.status == "incomplete"
    assert result.verified is False


# --- dd interrupted / fails to run: aborted, image untouched ----------------


def test_write_dd_interrupted_reports_aborted(image_file, image_size):
    """dd raising (USB pulled / non-zero) -> aborted, local image untouched."""
    before = open(image_file, "rb").read()
    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ), mock.patch.object(
        iw, "dd_write_image", side_effect=RuntimeError("dd: /dev/rdisk4s1: I/O error")
    ), mock.patch.object(iw, "raw_read_source") as rrs:
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )

    assert result.status == "aborted"
    assert result.verified is False
    assert result.bytes_written == 0
    assert "interrupted" in result.error or "failed" in result.error
    # Verification is never attempted after an aborted write.
    rrs.assert_not_called()
    assert open(image_file, "rb").read() == before


def test_write_dd_privilege_refusal_reports_incomplete(image_file, image_size):
    """A privileged-choke-point refusal of the dd argv -> incomplete, no verify."""
    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ), mock.patch.object(
        iw, "dd_write_image", side_effect=ms.PrivilegeError("not a raw slice")
    ), mock.patch.object(iw, "raw_read_source") as rrs:
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )
    assert result.status == "incomplete"
    assert result.bytes_written == 0
    rrs.assert_not_called()


# --- Whole-image unit of work -----------------------------------------------


def test_write_returns_single_whole_image_result(image_file, image_size):
    """The result is one WriteResult for the whole image — no per-family state."""
    readback = lambda: IMAGE_CONTENT
    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ), mock.patch.object(iw, "dd_write_image"), mock.patch.object(
        iw, "raw_read_source", return_value=readback
    ):
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )
    assert isinstance(result, iw.WriteResult)
    # bytes_written is the WHOLE image size, not a per-family subtotal.
    assert result.bytes_written == image_size
    # Sanity: the slice size the guard reported is what got written.
    assert result.bytes_written == len(IMAGE_CONTENT)


# --- Decision A sanity: readback source routes binary bytes through verify ---


def test_write_uses_binary_readback_bytes_for_verify(image_file, image_size):
    """The readback callable's bytes are hashed by the real verifier (Decision A)."""
    # Confirm the real verify() compares the image hash to the readback bytes'
    # hash — i.e. the binary readback path feeds genuine bytes to the verifier.
    expected_hash = hashlib.sha256(IMAGE_CONTENT).hexdigest()
    assert write_verifier.hash_image(image_file) == expected_hash

    readback = lambda: IMAGE_CONTENT
    with mock.patch.object(
        iw, "device_guard", return_value=_guard_info(image_size)
    ), mock.patch.object(iw, "dd_write_image"), mock.patch.object(
        iw, "raw_read_source", return_value=readback
    ):
        result = iw.write(
            image_file, DEVICE, expected_size_bytes=image_size, confirmed=True
        )
    assert result.verified is True
