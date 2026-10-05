# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test: composed image byte size equals the target slice size.

Feature: image-based-drive-write, Property: image sized to slice
(Property family C). Validates: Requirements 2.1 — THE Composer SHALL size the
image to the byte length of the target FAT32 partition slice.

Why this property is SPLIT into a pure part and a few real round-trips
----------------------------------------------------------------------
``create_image`` runs a REAL ``newfs_msdos`` + ``hdiutil`` attach/format/detach
round-trip, and newfs_msdos refuses to build FAT32 below a ~64 MiB minimum.
Driving ``create_image`` itself at 100 Hypothesis examples would therefore be
both very slow (a real format per example) and wrong for most generated sizes
(sub-64-MiB and non-cluster-aligned sizes do not FAT32-format). So the Req 2.1
invariant is exercised in two complementary layers:

  1. PURE size invariant over a WIDE range of sizes, at 100+ Hypothesis
     examples, via ``_allocate_backing_file`` — the exact-size allocation that
     ``create_image`` relies on and that the whole size guarantee reduces to.
     create_image allocates the backing file to EXACTLY ``byte_size`` and
     attaches it as a flat ``CRawDiskImage`` (a 1:1 byte view), so the file size
     is preserved across the format cycle; thus "image byte_size == requested
     slice size" IS "allocated backing file size == requested size". Allocation
     is a sparse ``truncate`` (O(1), no zeros written), so even multi-GiB sizes
     are cheap and no real format is needed. ``finalize_result`` is checked in
     the same loop so ``ComposeResult.byte_size`` is proven to equal the slice
     size too.

  2. A SMALL, PARAMETRIZED handful of REAL ``create_image`` round-trips (NOT
     Hypothesis-driven) at valid FAT32 sizes, asserting the on-disk image is
     EXACTLY the requested size after an actual attach/format/detach. This
     anchors the pure invariant to the real native-tool cycle and is gated with
     the same native-tools skip marker as test_image_composer_lifecycle.py. Each
     real round-trip cleans up its image and asserts no hdiutil attachment leaks.

Boundary: ``_allocate_backing_file`` and ``create_image`` reject non-positive
sizes with ``ImageCreateError`` (example-based, not Hypothesis).
"""

import os
import subprocess
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager import image_composer as ic


# newfs_msdos refuses FAT32 below a minimum cluster count; 64 MiB is the
# smallest that formats cleanly on macOS (per test_image_composer_lifecycle.py).
_MIB = 1024 * 1024
_TINY_FAT32_BYTES = 64 * _MIB

_NATIVE_TOOLS_PRESENT = os.path.exists(ic.NEWFS_MSDOS_BIN) and os.path.exists(
    ic.HDIUTIL_BIN
)

requires_native_tools = pytest.mark.skipif(
    not _NATIVE_TOOLS_PRESENT,
    reason="native newfs_msdos / hdiutil not present",
)


def _attached_images_for(image_path: str) -> int:
    """Count hdiutil attachments whose backing image path matches image_path.

    Mirrors the leak check in test_image_composer_lifecycle.py: after a real
    create_image, the image must not remain attached.
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
# PURE size invariant (Hypothesis, 100+ examples) — no native format needed
# ---------------------------------------------------------------------------


# Size strategy for the finalize-included property. Capped at 8 MiB: wide enough
# to vary across many orders of magnitude (1 byte .. 8 MiB) while keeping each
# example cheap, because finalize_result STREAM-HASHES the whole file (via
# write_verifier.hash_image) — a sparse truncate is O(1) to allocate but the
# hash must still read every byte, so a multi-GiB file here would read GiB of
# zeros per example. Drive-realistic sizes (tens of GiB) are covered allocation-
# only by test_allocation_size_invariant_wide below, which never hashes.
_SIZE_STRATEGY = st.integers(min_value=1, max_value=8 * _MIB)

# A WIDE range for the allocation-only invariant, up to 1 TiB — far larger than
# any real slice. Safe to sweep at 100 examples because the body only truncates
# (O(1), sparse) and reads os.path.getsize; it never hashes.
_WIDE_SIZE_STRATEGY = st.integers(min_value=1, max_value=1024 * 1024 * _MIB)


@settings(max_examples=100)
@given(byte_size=_SIZE_STRATEGY)
def test_allocation_size_invariant(byte_size):
    """For ANY positive byte_size, allocation + finalize report that exact size.

    This is the Req 2.1 invariant that create_image's size guarantee reduces to:
    the backing file is EXACTLY byte_size bytes (what the raw 1:1 attach/format
    preserves), and finalize_result's ComposeResult.byte_size equals it.

    Uses ``tempfile`` inside the body (not a function-scoped fixture) so each
    Hypothesis example gets a fresh path and the function stays fixture-free.
    Size is capped at 8 MiB here because this example also exercises
    finalize_result, which stream-hashes the whole file (see _SIZE_STRATEGY).

    **Validates: Requirements 2.1**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "img.raw")

        ic._allocate_backing_file(path, byte_size)
        assert os.path.getsize(path) == byte_size

        result = ic.finalize_result(path)
        assert result.byte_size == byte_size


@settings(max_examples=100)
@given(byte_size=_WIDE_SIZE_STRATEGY)
def test_allocation_size_invariant_wide(byte_size):
    """Allocation hits EXACTLY byte_size across a WIDE range (1 byte .. 1 TiB).

    The size-preservation core of Req 2.1 over drive-realistic and far-larger
    sizes. Allocation-only (no finalize/hash) so the sparse truncate stays O(1)
    and the full range is cheap to sweep at 100 examples. This is the invariant
    create_image's 1:1 raw attach/format then preserves end to end.

    **Validates: Requirements 2.1**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "img.raw")
        ic._allocate_backing_file(path, byte_size)
        assert os.path.getsize(path) == byte_size


# ---------------------------------------------------------------------------
# Boundary: non-positive sizes are rejected (example-based)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_size", [0, -1, -4096])
def test_allocate_backing_file_rejects_nonpositive(tmp_path, bad_size):
    """_allocate_backing_file raises ImageCreateError for size <= 0."""
    path = str(tmp_path / "img.raw")
    with pytest.raises(ic.ImageCreateError):
        ic._allocate_backing_file(path, bad_size)


@pytest.mark.parametrize("bad_size", [0, -1])
def test_create_image_rejects_nonpositive(tmp_path, bad_size):
    """create_image refuses a non-positive size before touching any tool."""
    path = str(tmp_path / "img.raw")
    with pytest.raises(ic.ImageCreateError):
        ic.create_image(path, bad_size)


# ---------------------------------------------------------------------------
# A FEW REAL round-trips (parametrized, native-tools-gated) — anchors the pure
# invariant to the actual newfs_msdos + hdiutil attach/format/detach cycle
# ---------------------------------------------------------------------------


# A small handful of valid FAT32 sizes: the 64 MiB floor plus a couple of
# larger-but-still-small sizes. Kept tiny on purpose — each is a real format.
_REAL_FAT32_SIZES = [
    _TINY_FAT32_BYTES,          # 64 MiB — the smallest that FAT32-formats here
    96 * _MIB,                  # 96 MiB
    128 * _MIB,                 # 128 MiB
]


@requires_native_tools
@pytest.mark.parametrize("byte_size", _REAL_FAT32_SIZES)
def test_create_image_real_roundtrip_size(tmp_path, byte_size):
    """A real create_image leaves an image of EXACTLY the requested size.

    Proves the Req 2.1 invariant holds across the ACTUAL attach/format/detach
    cycle, not just the pure allocation. Cleans up the image and asserts no
    hdiutil attachment leaks.

    **Validates: Requirements 2.1**
    """
    image_path = str(tmp_path / "EFIS_USB.img")
    try:
        ic.create_image(image_path, byte_size)
        assert os.path.getsize(image_path) == byte_size
        # The whole point of the raw 1:1 view: the real format preserves size.
        assert ic.finalize_result(image_path).byte_size == byte_size
        # No hdiutil attachment left behind by the round-trip.
        assert _attached_images_for(image_path) == 0
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)
