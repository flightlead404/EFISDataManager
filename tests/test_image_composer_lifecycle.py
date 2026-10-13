# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the image-lifecycle step (task 1.2).

Covers image_composer's create/attach/detach lifecycle and ComposeResult:

  - create_image(image_path, byte_size): allocates a backing file at EXACTLY
    byte_size, attaches it raw via hdiutil, formats FAT32 with newfs_msdos, and
    detaches — leaving a correctly-sized FAT32 image file.
  - attach_for_population(image_path, mountpoint): context manager that mounts
    the image and ALWAYS detaches on exit, even on exception.
  - finalize_result(image_path): builds ComposeResult with byte_size and a
    sha256 computed via write_verifier.hash_image (the same hash the verifier
    uses on the raw readback).

The core test does a REAL round-trip (newfs_msdos + hdiutil attach / populate /
detach) on a tiny image under a tmp dir, because that actually proves the
lifecycle on this Mac; it is marked so it can be skipped if the native tools are
absent. Fast, device-free tests cover the size invariant and error paths with
the subprocess layer mocked.

Requirements: 1.1, 2.1, 2.2
"""

import os
import subprocess

import pytest

from efis_data_manager import image_composer as ic
from efis_data_manager import write_verifier


# newfs_msdos refuses to build a FAT32 filesystem below a minimum cluster count;
# 32 MiB and 48 MiB are rejected on macOS, 64 MiB is the smallest that formats
# cleanly here. Use it as the tiny round-trip size.
_TINY_FAT32_BYTES = 64 * 1024 * 1024

_NATIVE_TOOLS_PRESENT = os.path.exists(ic.NEWFS_MSDOS_BIN) and os.path.exists(
    ic.HDIUTIL_BIN
)

requires_native_tools = pytest.mark.skipif(
    not _NATIVE_TOOLS_PRESENT,
    reason="native newfs_msdos / hdiutil not present",
)


def _attached_images_for(image_path: str) -> int:
    """Count hdiutil-attached entries whose image path matches image_path.

    Used by the leak check: after create_image / attach_for_population finish,
    the image must not remain attached. hdiutil info lists the backing image
    path for each attachment; we count exact matches.
    """
    try:
        out = subprocess.run(
            [ic.HDIUTIL_BIN, "info"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if image_path in line)


# ---------------------------------------------------------------------------
# Real round-trip (device-backed) — proves the actual lifecycle
# ---------------------------------------------------------------------------


@requires_native_tools
def test_create_image_sized_and_fat32_roundtrip(tmp_path):
    """create_image produces a FAT32 image of EXACTLY the requested size.

    Real newfs_msdos + hdiutil round-trip: the finished image file is exactly
    _TINY_FAT32_BYTES (Req 2.1), carries a FAT32 boot signature (0x55AA at the
    end of sector 0, Req 2.2), and leaves nothing attached afterwards.
    """
    image_path = str(tmp_path / "EFIS_USB.img")
    try:
        ic.create_image(image_path, _TINY_FAT32_BYTES)

        # Req 2.1: byte size equals the requested slice size, exactly.
        assert os.path.getsize(image_path) == _TINY_FAT32_BYTES

        # Req 2.2: FAT32 formatted — the boot sector ends with the 0x55AA sig,
        # and the FAT32 filesystem type label sits at offset 0x52.
        with open(image_path, "rb") as fh:
            boot = fh.read(512)
        assert boot[510:512] == b"\x55\xaa"
        assert boot[0x52:0x5A].rstrip(b"\x00 ") == b"FAT32"

        # No attachment left behind.
        assert _attached_images_for(image_path) == 0
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


@requires_native_tools
def test_attach_populate_detach_roundtrip(tmp_path):
    """attach_for_population mounts, populate copies in, and detach always runs.

    Full lifecycle: create -> attach(mounted) -> populate a source tree ->
    verify files landed sidecar-free -> detach. Confirms the image is NOT left
    attached after the context manager exits, and that finalize_result hashes
    the finished image with write_verifier.hash_image.
    """
    image_path = str(tmp_path / "EFIS_USB.img")
    source = tmp_path / "src"
    (source / "ChartData").mkdir(parents=True)
    (source / "ChartData" / "plate.pdf").write_bytes(b"a-real-plate")
    (source / "NAV.DB").write_bytes(b"nav-db-bytes")
    mountpoint = str(tmp_path / "mnt")

    try:
        ic.create_image(image_path, _TINY_FAT32_BYTES)

        with ic.attach_for_population(image_path, mountpoint) as root:
            assert root == mountpoint
            assert os.path.ismount(root)
            ic.populate(str(source), root)
            # Files landed and no sidecars survived the populate.
            assert os.path.exists(os.path.join(root, "NAV.DB"))
            assert os.path.exists(os.path.join(root, "ChartData", "plate.pdf"))
            assert ic.find_sidecars(root) == []

        # Context manager detached: nothing left attached, mount gone.
        assert _attached_images_for(image_path) == 0
        assert not os.path.ismount(mountpoint)

        # finalize_result: size + hash via the verifier's own hash function.
        result = ic.finalize_result(image_path, families_included=["charts", "nav"])
        assert isinstance(result, ic.ComposeResult)
        assert result.byte_size == _TINY_FAT32_BYTES
        assert result.image_sha256 == write_verifier.hash_image(image_path)
        assert result.families_included == ["charts", "nav"]
        assert result.composed_at  # non-empty ISO-8601 timestamp
        assert result.image_path == os.path.abspath(image_path)
    finally:
        # Belt-and-braces cleanup in case an assertion left it attached.
        if os.path.ismount(mountpoint):
            subprocess.run(
                [ic.HDIUTIL_BIN, "detach", mountpoint, "-force"],
                capture_output=True,
            )
        if os.path.exists(image_path):
            os.remove(image_path)


@requires_native_tools
def test_attach_for_population_detaches_on_exception(tmp_path):
    """attach_for_population detaches even when the body raises.

    The context manager's finally must release the image on an exception, so a
    failing populate never leaks an attachment.
    """
    image_path = str(tmp_path / "EFIS_USB.img")
    mountpoint = str(tmp_path / "mnt")
    try:
        ic.create_image(image_path, _TINY_FAT32_BYTES)
        with pytest.raises(RuntimeError, match="boom"):
            with ic.attach_for_population(image_path, mountpoint):
                raise RuntimeError("boom")
        assert _attached_images_for(image_path) == 0
        assert not os.path.ismount(mountpoint)
    finally:
        if os.path.ismount(mountpoint):
            subprocess.run(
                [ic.HDIUTIL_BIN, "detach", mountpoint, "-force"],
                capture_output=True,
            )
        if os.path.exists(image_path):
            os.remove(image_path)


# ---------------------------------------------------------------------------
# Device-free tests — size invariant + error paths (no newfs/hdiutil)
# ---------------------------------------------------------------------------


def test_allocate_backing_file_exact_size(tmp_path):
    """The backing file is created at exactly the requested byte length."""
    path = str(tmp_path / "img.raw")
    ic._allocate_backing_file(path, 1234567)
    assert os.path.getsize(path) == 1234567


def test_allocate_backing_file_rejects_nonpositive(tmp_path):
    path = str(tmp_path / "img.raw")
    with pytest.raises(ic.ImageCreateError):
        ic._allocate_backing_file(path, 0)
    with pytest.raises(ic.ImageCreateError):
        ic._allocate_backing_file(path, -1)


def test_create_image_rejects_bad_size(tmp_path):
    with pytest.raises(ic.ImageCreateError):
        ic.create_image(str(tmp_path / "img.raw"), 0)


def test_create_image_passes_sanitized_label_to_newfs(tmp_path, monkeypatch):
    """create_image threads a sanitized ``-v LABEL`` into the newfs_msdos argv.

    Device-free: the attach/detach and the tool run are stubbed so we only
    assert the argv the format step would receive. A lowercase/dirty label is
    sanitized (upper, [A-Z0-9], <=11) before ``-v``.
    """
    monkeypatch.setattr(ic, "_allocate_backing_file", lambda p, n: None)
    monkeypatch.setattr(ic, "_attach_raw", lambda p, **k: "/dev/diskX")
    monkeypatch.setattr(ic, "_detach", lambda d: None)
    monkeypatch.setattr(os.path, "getsize", lambda p: 64 * 1024 * 1024)

    seen = {}

    def fake_run(argv, timeout, what):
        seen["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ic, "_run_tool", fake_run)

    ic.create_image(str(tmp_path / "img.img"), 64 * 1024 * 1024, volume_label="c2609p2610")

    argv = seen["argv"]
    assert "-v" in argv
    assert argv[argv.index("-v") + 1] == "C2609P2610"
    # The device node is still the final argument.
    assert argv[-1] == "/dev/diskX"


def test_create_image_omits_label_when_none_or_empty(tmp_path, monkeypatch):
    """No ``-v`` is passed when the label is None or sanitizes to empty."""
    monkeypatch.setattr(ic, "_allocate_backing_file", lambda p, n: None)
    monkeypatch.setattr(ic, "_attach_raw", lambda p, **k: "/dev/diskX")
    monkeypatch.setattr(ic, "_detach", lambda d: None)
    monkeypatch.setattr(os.path, "getsize", lambda p: 64 * 1024 * 1024)

    seen = {}

    def fake_run(argv, timeout, what):
        seen.setdefault("argvs", []).append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ic, "_run_tool", fake_run)

    # None -> no -v (unchanged pre-label behavior).
    ic.create_image(str(tmp_path / "a.img"), 64 * 1024 * 1024)
    # All-special label sanitizes to empty -> also no -v.
    ic.create_image(str(tmp_path / "b.img"), 64 * 1024 * 1024, volume_label="!!!")

    for argv in seen["argvs"]:
        assert "-v" not in argv


def _fat_volume_label_from_boot_sector(image_path: str) -> str:
    """Read the FAT32 volume label out of the boot sector of ``image_path``.

    For FAT32 the 11-byte volume label lives at offset 0x47 of sector 0. Trailing
    pad spaces are stripped. Used by the round-trip test to confirm the label we
    set is what newfs_msdos actually wrote.
    """
    with open(image_path, "rb") as fh:
        boot = fh.read(512)
    return boot[0x47:0x47 + 11].rstrip(b" \x00").decode("ascii", "replace")


@requires_native_tools
def test_create_image_sets_fat_volume_label_roundtrip(tmp_path):
    """A real newfs_msdos round-trip writes the C<charts>P<plates> label.

    create_image(..., volume_label="C2609P2610") must produce a FAT32 image
    whose on-disk volume label reads back as exactly C2609P2610 (uppercase,
    <=11), proving the ``-v`` wiring survives the format.
    """
    image_path = str(tmp_path / "EFIS_USB.img")
    try:
        ic.create_image(image_path, _TINY_FAT32_BYTES, volume_label="C2609P2610")
        assert os.path.getsize(image_path) == _TINY_FAT32_BYTES
        label = _fat_volume_label_from_boot_sector(image_path)
        assert label == "C2609P2610"
        assert len(label) <= 11
        assert label == label.upper()
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


def test_run_tool_maps_nonzero_exit_to_imagecreateerror(monkeypatch):
    """A non-zero tool exit becomes ImageCreateError carrying the stderr."""

    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(
            args=["x"], returncode=1, stdout="", stderr="disk busy"
        )

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    with pytest.raises(ic.ImageCreateError, match="disk busy"):
        ic._run_tool(["x"], 1.0, "hdiutil attach")


def test_run_tool_maps_missing_binary(monkeypatch):
    def fake_run(*_a, **_k):
        raise FileNotFoundError()

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    with pytest.raises(ic.ImageCreateError, match="binary not found"):
        ic._run_tool(["/nope"], 1.0, "newfs_msdos")


def test_finalize_result_hashes_with_verifier(tmp_path):
    """finalize_result size + hash match the file and write_verifier.hash_image."""
    image_path = str(tmp_path / "img.raw")
    with open(image_path, "wb") as fh:
        fh.write(b"\x00" * 4096)
    result = ic.finalize_result(image_path)
    assert result.byte_size == 4096
    assert result.image_sha256 == write_verifier.hash_image(image_path)
    assert result.families_included == []
    assert result.composed_at
