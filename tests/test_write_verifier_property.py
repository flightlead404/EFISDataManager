# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the raw-readback verifier (task 3.2).

Feature: image-based-drive-write, Property 3: verified completion.

Design Property 3 — Verified completion: ``WriteResult.status == "complete"``
IFF raw readback of the device matches the composed image hash. Realised by
``write_verifier.verify(image_path, readback_source)``, which hashes the composed
image and the injected raw readback and returns True IFF the two hashes match.

These Hypothesis properties exercise that biconditional across arbitrary image
buffers:

  (a) a readback IDENTICAL to the image -> verify returns True (complete);
  (b) flipping ANY single byte of the readback -> verify returns False;
  (c) truncating the readback by any positive amount -> verify returns False.

The readback source is injected both as an ``io.BytesIO`` stream and as a
bytes-returning callable — the two production-shaped sources the verifier
supports — so the hash-and-compare logic is proven device-free (no mount, no
``/dev`` node), exactly the testability property the module was designed for.

To avoid the Hypothesis + function-scoped-fixture reuse pitfall (a single
``tmp_path`` would be shared across all generated examples), the temp image file
is created INSIDE each test body with ``tempfile`` and removed afterward, so
every generated example writes a fresh image file.

Validates: Requirements 6.2, 6.3, 6.4
"""

import io
import os
import tempfile

from hypothesis import given, settings, strategies as st

from efis_data_manager import write_verifier as wv


# Keep buffers modest but multi-byte so mutation/truncation have room to bite;
# min_size=1 guarantees there is always at least one byte to flip, and a
# truncation by >=1 always yields a strictly shorter (mismatching) readback.
_IMAGE_BYTES = st.binary(min_size=1, max_size=4096)


def _write_image(data: bytes) -> str:
    """Write ``data`` to a fresh temp file and return its path.

    A new file per generated example sidesteps the function-scoped-fixture reuse
    pitfall under Hypothesis. The caller is responsible for removing the file.
    """
    fd, path = tempfile.mkstemp(prefix="efis_verify_", suffix=".img")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        os.unlink(path)
        raise
    return path


@settings(max_examples=100)
@given(image=_IMAGE_BYTES)
def test_identical_readback_reports_complete(image):
    """(a) An identical readback hashes equal -> complete (True).

    Checked for BOTH injectable source shapes the verifier supports: an
    ``io.BytesIO`` stream and a zero-arg bytes-returning callable (the shape the
    production raw read of ``/dev/rdiskNsN`` uses).

    Validates: Requirements 6.2, 6.3
    """
    path = _write_image(image)
    try:
        # Stream source positioned at the region start.
        assert wv.verify(path, io.BytesIO(image)) is True
        # Bytes-returning-callable source (production raw-read shape).
        assert wv.verify(path, lambda: image) is True
    finally:
        os.unlink(path)


@settings(max_examples=100)
@given(image=_IMAGE_BYTES, data=st.data())
def test_mutated_byte_reports_incomplete(image, data):
    """(b) Flipping any single byte of the readback -> incomplete (False).

    A Hypothesis-chosen index selects which byte to flip; XOR 0xFF guarantees
    the byte actually changes, so the readback can never hash-match the image.

    Validates: Requirements 6.2, 6.4
    """
    index = data.draw(st.integers(min_value=0, max_value=len(image) - 1))
    mutated = bytearray(image)
    mutated[index] ^= 0xFF  # guaranteed to differ from the original byte
    mutated_bytes = bytes(mutated)

    path = _write_image(image)
    try:
        assert wv.verify(path, io.BytesIO(mutated_bytes)) is False
        assert wv.verify(path, lambda: mutated_bytes) is False
    finally:
        os.unlink(path)


@settings(max_examples=100)
@given(image=_IMAGE_BYTES, data=st.data())
def test_truncated_readback_reports_incomplete(image, data):
    """(c) Truncating the readback by any positive amount -> incomplete (False).

    Models a short read from an interrupted/truncated ``dd``: the device region
    ended before the image did. A strictly shorter readback (down to and
    including zero length) always hashes differently from the non-empty image,
    so a partial write is never reported complete.

    Validates: Requirements 6.2, 6.4
    """
    drop = data.draw(st.integers(min_value=1, max_value=len(image)))
    truncated = image[: len(image) - drop]  # strictly shorter than the image

    path = _write_image(image)
    try:
        assert wv.verify(path, io.BytesIO(truncated)) is False
        assert wv.verify(path, lambda: truncated) is False
    finally:
        os.unlink(path)
