# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the dd raw-write / raw-read command builders + wrappers
(image-based-drive-write task 5.1).

Covers:
  * build_dd_write_argv / build_dd_read_argv produce the expected argv shape and
    map a /dev/diskNsN node to its RAW /dev/rdiskNsN form;
  * dd_write_image and raw_read_source route through mount_swap.run_privileged
    (the single privilege choke point), so a non-raw / whole-disk target is
    refused there (PrivilegeError, nothing executed);
  * raw_read_source returns a zero-arg callable yielding the device bytes,
    suitable as a write_verifier readback source.

subprocess.run is mocked (no real dd, no device touched).

Requirements: 5.1, 5.2, 5.3, 8.1, 8.2, 8.4, 8.5
"""

from unittest import mock

import pytest

from efis_data_manager import image_writer as iw
from efis_data_manager import mount_swap as ms


@pytest.fixture
def run_mock():
    """Patch mount_swap.subprocess.run; return a bytes-stdout CompletedProcess."""
    with mock.patch.object(ms.subprocess, "run") as m:
        m.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        yield m


# --- argv builders ----------------------------------------------------------


def test_build_write_argv_maps_to_raw_node():
    argv = iw.build_dd_write_argv("/tmp/EFIS_USB.img", "/dev/disk4s1")
    assert argv == [
        ms.DD_BIN,
        "if=/tmp/EFIS_USB.img",
        "of=/dev/rdisk4s1",
        f"bs={iw.DD_BLOCK_SIZE}",
    ]


def test_build_write_argv_accepts_already_raw_node():
    argv = iw.build_dd_write_argv("/tmp/EFIS_USB.img", "/dev/rdisk4s1")
    assert "of=/dev/rdisk4s1" in argv


def test_build_read_argv_targets_raw_node_and_bounds_length():
    block = 1024 * 1024
    # 2.5 blocks: 2 whole blocks + a 0.5-block tail.
    length = 2 * block + block // 2
    argv = iw.build_dd_read_argv("/dev/disk4s1", length)
    assert argv[0] == ms.DD_BIN
    assert "if=/dev/rdisk4s1" in argv
    assert f"count=2" in argv  # two whole 1 MiB blocks
    assert f"count={block // 2}" in argv  # the 1-byte-bs tail remainder


def test_build_read_argv_whole_block_multiple_has_no_tail():
    block = 1024 * 1024
    argv = iw.build_dd_read_argv("/dev/disk4s1", 4 * block)
    assert "ibs=1" not in argv
    assert "count=4" in argv


def test_build_read_argv_rejects_nonpositive_length():
    with pytest.raises(ValueError):
        iw.build_dd_read_argv("/dev/disk4s1", 0)


# --- wrappers route through run_privileged ----------------------------------


def test_dd_write_image_reaches_subprocess(run_mock):
    iw.dd_write_image("/tmp/EFIS_USB.img", "/dev/disk4s1", timeout=60)
    run_mock.assert_called_once()
    passed = run_mock.call_args[0][0]
    assert passed == [
        "/usr/bin/sudo",
        "-n",
        ms.DD_BIN,
        "if=/tmp/EFIS_USB.img",
        "of=/dev/rdisk4s1",
        f"bs={iw.DD_BLOCK_SIZE}",
    ]


def test_dd_write_image_refuses_whole_disk(run_mock):
    # Already-raw whole-disk node -> raw-slice guard in run_privileged refuses.
    with pytest.raises(ms.PrivilegeError):
        iw.dd_write_image("/tmp/EFIS_USB.img", "/dev/rdisk4", timeout=60)
    run_mock.assert_not_called()


def test_raw_read_source_returns_bytes_callable(run_mock):
    payload = b"\x00\x01\x02\xff" * 8
    run_mock.return_value = mock.Mock(returncode=0, stdout=payload, stderr=b"")

    read = iw.raw_read_source("/dev/disk4s1", length_bytes=1024, timeout=60)
    # Deferred: nothing runs until the closure is called.
    run_mock.assert_not_called()

    data = read()
    assert data == payload
    run_mock.assert_called_once()
    passed = run_mock.call_args[0][0]
    assert passed[:3] == ["/usr/bin/sudo", "-n", ms.DD_BIN]
    assert "if=/dev/rdisk4s1" in passed


def test_raw_read_source_rejects_non_binary_stdout(run_mock):
    # text-mode stdout would corrupt raw bytes -> the helper refuses it.
    run_mock.return_value = mock.Mock(returncode=0, stdout="corrupted", stderr="")
    read = iw.raw_read_source("/dev/disk4s1", length_bytes=1024, timeout=60)
    with pytest.raises(TypeError):
        read()
