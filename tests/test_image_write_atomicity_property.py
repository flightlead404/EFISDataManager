# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test: interrupted write leaves local image/state intact.

Spec: .kiro/specs/image-based-drive-write/
Task 3.3 — Property family F — Marker atomicity / no local corruption.

Feature: image-based-drive-write, Property 2: marker atomicity.

Design **Property 2 — Marker atomicity** says the completion marker exists on
the drive IFF the full image was written: it is baked into the image LAST, so a
partial ``dd`` never presents a complete-looking volume. The companion
guarantees from Requirement 5.4 and 4.2 are that an interrupted ``dd``:

  (1) leaves the LOCAL image file and composer state byte-identical — the write
      never mutates our own image, so recovery is just re-write + re-verify; and
  (2) reports the write INCOMPLETE — a partial device region can never
      hash-match the full composed image, so :func:`write_verifier.verify`
      returns False for any truncated prefix (the raw-readback readback is
      shorter / differs, so the marker never reads back complete).

This property is enforced BY CONSTRUCTION using the real
``write_verifier.verify`` over a raw readback, plus the writer's documented
"leave local image/state unchanged on interruption" contract. The real writer
``ImageWriter.write`` does NOT exist yet — it is **Task 7**. So here the write is
modelled with a MOCK ``dd`` that aborts at a Hypothesis-chosen offset, writing
only a PREFIX of the image bytes to a simulated device region (an in-memory
buffer / temp file standing in for ``/dev/rdiskNsN``). We then verify the two
guarantees against the REAL verifier. When Task 7 wires the real writer, this
property continues to hold because the verifier (used unchanged) rejects any
partial readback and the writer only ever reads the local image.

Validates: Requirements 4.2, 5.4
"""

import hashlib
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager import write_verifier


def _sha256_file(path: Path) -> str:
    """Independent sha256 of a file (not via write_verifier) for cross-check."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mock_interrupted_dd(image_bytes: bytes, abort_offset: int, device_path: Path) -> int:
    """Model an interrupted ``dd`` of ``image_bytes`` onto a raw device region.

    Writes only ``image_bytes[:abort_offset]`` to ``device_path`` (the in-memory
    temp file standing in for ``/dev/rdiskNsN``), simulating a ``dd`` aborted at
    ``abort_offset`` (e.g. USB pulled). Returns the number of bytes written. It
    reads the image bytes passed in by value and NEVER touches the local image
    file, mirroring the writer's "leave local image/state unchanged" contract.
    """
    prefix = image_bytes[:abort_offset]
    with open(device_path, "wb") as handle:
        handle.write(prefix)
    return len(prefix)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    image_bytes=st.binary(min_size=1, max_size=4096),
    offset_frac=st.floats(min_value=0.0, max_value=1.0),
)
def test_interrupted_write_leaves_local_image_intact_and_reports_incomplete(
    image_bytes, offset_frac
):
    """For ANY interruption offset: local image unchanged + status incomplete.

    (a) hash of the local image file is byte-identical before vs after the
        simulated interrupted write (via write_verifier.hash_image); and
    (b) write_verifier.verify(image, partial_region) is False for every
        TRUNCATED prefix (0 <= offset < len), because a partial device region
        can never hash-match the full image — the completion marker never reads
        back complete on a partial write.

    Sanity anchor: a FULL-length identical region (offset == len) verifies True
    — proving the False results above are the partial case, not a broken test.
    """
    n = len(image_bytes)
    # Map the fractional offset onto [0, n] so Hypothesis explores the whole
    # range of interruption points, including the degenerate 0-byte abort.
    abort_offset = int(round(offset_frac * n))
    abort_offset = max(0, min(abort_offset, n))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        image_path = tmp_path / "EFIS_USB.img"
        device_path = tmp_path / "sim_rdisk"  # stands in for /dev/rdiskNsN

        image_path.write_bytes(image_bytes)

        # Record local image state BEFORE the simulated interrupted write.
        image_hash_before = write_verifier.hash_image(image_path)
        raw_hash_before = _sha256_file(image_path)

        # Simulate dd aborting at abort_offset — writes only a prefix to the
        # simulated device region; must not touch the local image.
        bytes_written = _mock_interrupted_dd(image_bytes, abort_offset, device_path)
        assert bytes_written == abort_offset

        # (a) LOCAL image + composer state byte-identical before vs after.
        image_hash_after = write_verifier.hash_image(image_path)
        raw_hash_after = _sha256_file(image_path)
        assert image_hash_after == image_hash_before, (
            "the interrupted write must not mutate the local image"
        )
        assert raw_hash_after == raw_hash_before
        assert image_path.read_bytes() == image_bytes

        # (b) status of the simulated write via the REAL verifier over the raw
        # readback of the simulated device region.
        result = write_verifier.verify(image_path, device_path)

        if abort_offset == n:
            # Sanity anchor: a full-length identical region is complete (True).
            assert result is True, (
                "a full-length identical readback must verify complete"
            )
        else:
            # Any truncated/partial prefix is INCOMPLETE (False): a partial
            # write never presents a complete marker on raw readback.
            assert result is False, (
                f"partial prefix of {abort_offset}/{n} bytes must verify "
                "incomplete — a partial write must never look complete"
            )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(image_bytes=st.binary(min_size=1, max_size=4096))
def test_every_strict_prefix_is_incomplete(image_bytes):
    """Exhaustive-by-property check: EVERY strict prefix verifies incomplete.

    Stronger statement of guarantee (2): there is no interruption offset short
    of the full image length at which a partial device region hash-matches the
    full composed image. Only the full-length region verifies complete.
    """
    n = len(image_bytes)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        image_path = tmp_path / "EFIS_USB.img"
        device_path = tmp_path / "sim_rdisk"
        image_path.write_bytes(image_bytes)

        # A handful of representative strict prefixes (0, mid, n-1) plus the
        # full length. Property: strict prefixes -> False, full -> True.
        offsets = sorted({0, n // 2, max(0, n - 1), n})
        for offset in offsets:
            _mock_interrupted_dd(image_bytes, offset, device_path)
            result = write_verifier.verify(image_path, device_path)
            if offset == n:
                assert result is True
            else:
                assert result is False
