# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the raw-readback verifier (task 3.1).

Covers write_verifier.verify(image_path, readback_source), which confirms a
drive write by hashing the raw readback region and comparing it to the composed
image hash:

  - a byte-buffer readback that equals the image -> verify returns True
    (complete);
  - mutating a single byte of the readback -> verify returns False (incomplete);
  - truncating the readback (short read, as from an interrupted dd) -> verify
    returns False (incomplete).

The readback source is injected as an in-memory buffer / bytes-returning
callable, so NO device and NO /dev node is involved — exactly the testability
property the module was designed for. The image is a small temp file.

Requirements: 6.1, 6.2, 6.3, 6.4, 4.3
"""

import io

import pytest

from efis_data_manager import write_verifier as wv


# Image content a few blocks long so the streamed/block-wise hashing path is
# exercised past a single HASH_BLOCK_SIZE boundary.
IMAGE_BYTES = (b"EFIS_USB image contents \x00\x01\x02\xff" * 4096) + b"MARKER-LAST"


@pytest.fixture
def image_file(tmp_path):
    """A temp file standing in for the composed FAT32 image."""
    path = tmp_path / "EFIS_USB.img"
    path.write_bytes(IMAGE_BYTES)
    return path


def test_matching_readback_reports_complete(image_file):
    """A readback identical to the image hashes equal -> complete (True)."""
    readback = io.BytesIO(IMAGE_BYTES)
    assert wv.verify(image_file, readback) is True


def test_matching_readback_as_callable_reports_complete(image_file):
    """The bytes-returning-callable source shape also matches -> True.

    This is the shape the production raw read of /dev/rdiskNsN will use.
    """
    assert wv.verify(image_file, lambda: IMAGE_BYTES) is True


def test_mutated_byte_reports_incomplete(image_file):
    """Flipping one byte of the readback -> mismatch -> incomplete (False)."""
    mutated = bytearray(IMAGE_BYTES)
    mutated[len(mutated) // 2] ^= 0xFF  # flip a single bit-pattern mid-stream
    readback = io.BytesIO(bytes(mutated))
    assert wv.verify(image_file, readback) is False


def test_truncated_readback_reports_incomplete(image_file):
    """A short read (interrupted/truncated dd) -> incomplete (False)."""
    truncated = io.BytesIO(IMAGE_BYTES[:-64])  # device region ended early
    assert wv.verify(image_file, truncated) is False


def test_bad_source_type_raises(image_file):
    """A readback source that is neither path, stream, nor callable raises."""
    with pytest.raises(TypeError):
        wv.verify(image_file, 12345)


# --- Chunked verify path (image-based-drive-write robustness fix) ------------
#
# verify_chunked consumes a SEGMENTED readback source (a zero-arg callable
# returning an iterable of byte chunks) and still produces one sha256 over the
# whole region to compare against hash_image — so a flaky USB bridge can resume
# per-segment. The existing verify(...) and its three source shapes are
# untouched; the chunked capability is added ALONGSIDE.


def _chunks(data, n):
    """Yield ``data`` split into ``n``-byte segments (the segmented-read shape)."""
    for off in range(0, len(data), n):
        yield data[off : off + n]


def test_verify_chunked_matching_segments_report_complete(image_file):
    """Segments that concatenate to the image hash equal -> complete (True)."""
    assert wv.verify_chunked(image_file, lambda: _chunks(IMAGE_BYTES, 1000)) is True


def test_verify_chunked_single_segment_matches(image_file):
    """A one-segment source (whole region) also matches."""
    assert wv.verify_chunked(image_file, lambda: iter([IMAGE_BYTES])) is True


def test_verify_chunked_mutated_segment_reports_incomplete(image_file):
    """Flipping a byte in one segment -> mismatch -> incomplete (False)."""
    mutated = bytearray(IMAGE_BYTES)
    mutated[len(mutated) // 2] ^= 0xFF
    assert wv.verify_chunked(image_file, lambda: _chunks(bytes(mutated), 1000)) is False


def test_verify_chunked_truncated_reports_incomplete(image_file):
    """A short segmented readback (interrupted dd) -> incomplete (False)."""
    assert (
        wv.verify_chunked(image_file, lambda: _chunks(IMAGE_BYTES[:-64], 1000)) is False
    )


def test_verify_chunked_rejects_non_callable(image_file):
    """A non-callable chunked source raises (it must be a zero-arg callable)."""
    with pytest.raises(TypeError):
        wv.verify_chunked(image_file, [IMAGE_BYTES])


def test_verify_chunked_rejects_non_bytes_chunk(image_file):
    """A source yielding a non-bytes chunk raises."""
    with pytest.raises(TypeError):
        wv.verify_chunked(image_file, lambda: iter(["not-bytes"]))
