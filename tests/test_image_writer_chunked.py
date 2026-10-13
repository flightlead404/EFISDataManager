# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit + property tests for CHUNKED raw write-with-resume and chunked raw
readback-verify (image-based-drive-write robustness fix).

Design driver — "Hardware harness findings (2026-10-09)": a single long raw dd
is fragile on the SanDisk-through-Henge-dock USB bridge, which RESETS mid-
transfer ("Device not configured") and RE-ENUMERATES the device at a NEW
/dev/diskN. The fix writes/reads the image in fixed segments and re-drives a
segment that resets, re-resolving the re-enumerated node and re-running the
device guard before each retry. These tests prove that resume logic WITHOUT any
real device or real dd:

  * the device is an in-memory bytearray a FAKE run_privileged writes into at
    seek*bs / reads from at skip*bs (honouring the chunked dd argv), and that
    "resets" (raises / returns nonzero) at INJECTED segment hits;
  * a reset re-enumerates the drive to a NEW node string; the injected resolver
    returns that new node and the injected guard confirms the same drive.

Covered cases:
  * happy path: N segments all succeed first try -> all bytes written, chunked
    verify matches;
  * a segment resets once then succeeds on retry (resolver returns a new node)
    -> write completes; the retry re-resolved the node and re-ran the guard;
  * a segment resets past the retry budget -> write aborted; nothing claims
    success;
  * chunked readback with a mid-stream reset that recovers -> verify True;
    unrecoverable reset -> ChunkedTransferError (write maps it to incomplete);
  * property (Hypothesis, >=100 ex): for any image length and any set of
    segment-reset points that clear within budget, the chunked write writes
    every byte exactly once and the chunked readback hash equals hash_image.

Feature: image-based-drive-write, Property: chunked write/read resume.
Validates: Requirements 5.1, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 13.1, 13.2
"""

import contextlib
import hashlib
import os
import tempfile
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from efis_data_manager import image_writer as iw
from efis_data_manager import write_verifier as wv


BLOCK = iw.DD_BLOCK_BYTES  # 1 MiB


# ---------------------------------------------------------------------------
# A tiny chunk size so tests exercise MANY segments without MiB-scale buffers.
# Patched onto image_writer per-test via monkeypatch so plan_segments splits
# small images into several segments.
# ---------------------------------------------------------------------------
SMALL_CHUNK = 256  # bytes per segment
SMALL_BLOCK = 64  # bytes per dd block


def _parse_dd(argv):
    """Parse a dd argv built by image_writer into a dict of operands."""
    out = {}
    for tok in argv[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


class FakeBus:
    """An in-memory stand-in for the raw USB slice + a resettable dd backend.

    The device content is a single bytearray addressed by the SAME physical
    drive regardless of which /dev node names it (the drive re-enumerates at a
    new node after a reset, but it is the same bytes). ``current_node`` is the
    node the drive is reachable at right now; a reset advances it to a new node
    string so a resolver can hand the chunked code the re-enumerated node.

    ``fake_run_privileged`` is patched over mount_swap.run_privileged. It honours
    the chunked dd argv (skip/seek/count in bs units, bs=1 tails) against an
    image bytes buffer (for writes) and the device buffer (for reads), and it
    "resets" on an injected set of (op, byte_offset) hits — each hit fires ONCE
    then clears, modelling a transient reset that recovers on retry.
    """

    def __init__(self, image_bytes, size, block=SMALL_BLOCK):
        self.image = bytearray(image_bytes)
        self.device = bytearray(size)
        self.block = block
        self.current_node = "/dev/disk20s1"
        self._node_seq = 20
        # Injected resets: set of (op, segment_byte_offset) that should fail. Each
        # is popped on use so the retry after re-enumeration succeeds.
        self.resets = set()
        # When True, EVERY attempt for a matching (op, offset) fails (models an
        # unrecoverable drive, exhausting the budget).
        self.permanent_resets = set()
        self.write_log = []  # (offset, length) actually written, for exactly-once

    # -- resolver / guard injected into write() / chunked helpers --------------
    def resolve_node(self):
        """Return the drive's current (possibly re-enumerated) node."""
        return self.current_node

    def verify_guard(self, node):
        """Confirm the node is the drive's current node; else raise like the guard."""
        if node != self.current_node:
            raise iw.DeviceGuardError(
                f"re-resolved node {node!r} is not the drive {self.current_node!r}"
            )

    def _reenumerate(self):
        self._node_seq += 2
        self.current_node = f"/dev/disk{self._node_seq}s1"

    # -- the fake privileged dd backend ---------------------------------------
    def fake_run_privileged(self, argv, timeout, binary=False):
        ops = _parse_dd(argv)
        bs = ops.get("bs")
        is_write = "of" in ops and ops["of"].startswith("/dev/")
        is_read = "if" in ops and ops["if"].startswith("/dev/")

        # Byte offset of this dd invocation on the device (seek for write, skip
        # for read), in bytes. For bs=1m-style blocks skip/seek are in blocks.
        if is_write:
            if bs == "1":  # byte-granular tail
                dev_off = int(ops["seek"])
                img_off = int(ops["skip"])
                length = int(ops["count"])
            else:
                blk = self.block
                dev_off = int(ops["seek"]) * blk
                img_off = int(ops["skip"]) * blk
                length = int(ops["count"]) * blk
            seg_off = dev_off
            op = "write"
        elif is_read:
            if ops.get("ibs") == "1":
                dev_off = int(ops["skip"])
                length = int(ops["count"])
            else:
                blk = self.block
                dev_off = int(ops["skip"]) * blk
                length = int(ops["count"]) * blk
            seg_off = dev_off
            op = "read"
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected dd argv: {argv}")

        # Injected reset: fire a transient (pop) or permanent failure.
        key = (op, seg_off)
        if key in self.permanent_resets:
            self._reenumerate()
            raise RuntimeError("dd: /dev/rdiskNsN: Device not configured")
        if key in self.resets:
            self.resets.discard(key)
            self._reenumerate()
            raise RuntimeError("dd: /dev/rdiskNsN: Device not configured")

        if op == "write":
            self.device[dev_off : dev_off + length] = self.image[
                img_off : img_off + length
            ]
            self.write_log.append((dev_off, length))
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")
        else:  # read
            data = bytes(self.device[dev_off : dev_off + length])
            return mock.Mock(returncode=0, stdout=data, stderr=b"")


@pytest.fixture
def small_chunks(monkeypatch):
    """Shrink the chunk + block sizes so small images span several segments."""
    monkeypatch.setattr(iw, "DD_CHUNK_BYTES", SMALL_CHUNK)
    monkeypatch.setattr(iw, "DD_BLOCK_BYTES", SMALL_BLOCK)
    monkeypatch.setattr(iw, "DD_BLOCK_SIZE", str(SMALL_BLOCK))
    # Zero backoff so retry tests do not sleep.
    monkeypatch.setattr(iw, "DD_RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(iw, "DD_RETRY_BACKOFF_SECONDS", 0)


def _make_image(tmp_path, length):
    """Write a deterministic pseudo-random image of ``length`` bytes; return path."""
    content = bytes((i * 7 + 13) % 256 for i in range(length))
    path = tmp_path / "EFIS_USB.img"
    path.write_bytes(content)
    return str(path), content


# ---------------------------------------------------------------------------
# Happy path: all segments succeed first try
# ---------------------------------------------------------------------------


def test_chunked_write_and_verify_happy_path(tmp_path, small_chunks):
    """N segments all succeed -> all bytes written + chunked verify matches."""
    size = SMALL_CHUNK * 3 + SMALL_BLOCK  # several whole segments + a block tail
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)

    with mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        written = iw.chunked_dd_write(
            image_path,
            bus.current_node,
            size,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )
        assert written == size
        # Every image byte landed on the device.
        assert bytes(bus.device) == content

        source = iw.chunked_raw_read_source(
            bus.current_node,
            size,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )
        assert wv.verify_chunked(image_path, source) is True


def test_chunked_write_each_byte_written_once(tmp_path, small_chunks):
    """The segments partition the image: every byte range written exactly once."""
    size = SMALL_CHUNK * 2 + 7  # non-block-multiple tail
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)

    with mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        iw.chunked_dd_write(
            image_path,
            bus.current_node,
            size,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )

    # Reconstruct coverage from the write log; each device byte covered once.
    coverage = bytearray(size)
    for off, length in bus.write_log:
        for i in range(off, off + length):
            coverage[i] += 1
    assert all(c == 1 for c in coverage)


# ---------------------------------------------------------------------------
# A segment resets once then succeeds on retry
# ---------------------------------------------------------------------------


def test_chunked_write_recovers_from_single_reset(tmp_path, small_chunks):
    """A segment resets once; the retry re-resolves the node + re-guards + wins."""
    size = SMALL_CHUNK * 3
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    # The SECOND segment (byte offset SMALL_CHUNK) resets once.
    bus.resets.add(("write", SMALL_CHUNK))

    guard_calls = []
    orig_guard = bus.verify_guard

    def tracking_guard(node):
        guard_calls.append(node)
        orig_guard(node)

    start_node = bus.current_node
    with mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        written = iw.chunked_dd_write(
            image_path,
            start_node,
            size,
            resolve_node=bus.resolve_node,
            verify_guard=tracking_guard,
            timeout=5,
        )

    assert written == size
    assert bytes(bus.device) == content
    # The reset re-enumerated the drive and the retry re-ran the guard against
    # the NEW node (not the original).
    assert bus.current_node != start_node
    assert guard_calls == [bus.current_node]


def test_chunked_read_recovers_from_single_reset(tmp_path, small_chunks):
    """A mid-stream readback reset recovers -> chunked verify still True."""
    size = SMALL_CHUNK * 3
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    bus.device[:] = content  # drive already holds the correct bytes
    # The THIRD read segment resets once.
    bus.resets.add(("read", SMALL_CHUNK * 2))

    with mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        source = iw.chunked_raw_read_source(
            bus.current_node,
            size,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )
        assert wv.verify_chunked(image_path, source) is True


# ---------------------------------------------------------------------------
# A segment resets every time past the budget -> aborted, no success claimed
# ---------------------------------------------------------------------------


def test_chunked_write_aborts_when_segment_never_recovers(tmp_path, small_chunks):
    """A permanently-resetting segment exhausts the budget -> ChunkedTransferError."""
    size = SMALL_CHUNK * 2
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    bus.permanent_resets.add(("write", SMALL_CHUNK))  # second segment never clears

    with mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        with pytest.raises(iw.ChunkedTransferError):
            iw.chunked_dd_write(
                image_path,
                bus.current_node,
                size,
                resolve_node=bus.resolve_node,
                verify_guard=bus.verify_guard,
                timeout=5,
            )


def test_chunked_read_unrecoverable_raises(tmp_path, small_chunks):
    """A permanently-resetting read segment raises ChunkedTransferError."""
    size = SMALL_CHUNK * 2
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    bus.device[:] = content
    bus.permanent_resets.add(("read", 0))

    with mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        source = iw.chunked_raw_read_source(
            bus.current_node,
            size,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )
        with pytest.raises(iw.ChunkedTransferError):
            wv.verify_chunked(image_path, source)


# ---------------------------------------------------------------------------
# End-to-end write() with chunking: reset recovers / unrecoverable -> status
# ---------------------------------------------------------------------------


def test_write_end_to_end_recovers_and_completes(tmp_path, small_chunks):
    """write() with an injected reset recovers and reports complete."""
    size = SMALL_CHUNK * 3
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    bus.resets.add(("write", SMALL_CHUNK))  # one transient write reset

    guard_info = iw.DeviceGuardInfo(
        device_node=bus.current_node,
        is_removable=True,
        is_fat32=True,
        slice_size_bytes=size,
        volume_label="C2609P2610",
        identity_id="UUID-1",
    )

    with mock.patch.object(
        iw, "device_guard", return_value=guard_info
    ), mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        result = iw.write(
            image_path,
            bus.current_node,
            expected_size_bytes=size,
            confirmed=True,
            # Inject the fake bus's resolver/guard so no diskutil is touched.
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )

    assert result.status == "complete"
    assert result.verified is True
    assert result.bytes_written == size
    assert bytes(bus.device) == content


def test_write_end_to_end_unrecoverable_reports_aborted(tmp_path, small_chunks):
    """write() with a permanent write reset reports aborted, nothing claims success."""
    size = SMALL_CHUNK * 2
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    bus.permanent_resets.add(("write", SMALL_CHUNK))

    guard_info = iw.DeviceGuardInfo(
        device_node=bus.current_node,
        is_removable=True,
        is_fat32=True,
        slice_size_bytes=size,
        volume_label="C2609P2610",
        identity_id="UUID-1",
    )

    with mock.patch.object(
        iw, "device_guard", return_value=guard_info
    ), mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        result = iw.write(
            image_path,
            bus.current_node,
            expected_size_bytes=size,
            confirmed=True,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )

    assert result.status == "aborted"
    assert result.verified is False
    assert result.bytes_written == 0


def test_write_end_to_end_readback_reset_unrecoverable_incomplete(
    tmp_path, small_chunks
):
    """A write that lands but whose readback cannot recover -> incomplete."""
    size = SMALL_CHUNK * 2
    image_path, content = _make_image(tmp_path, size)
    bus = FakeBus(content, size)
    bus.permanent_resets.add(("read", 0))  # readback never recovers

    guard_info = iw.DeviceGuardInfo(
        device_node=bus.current_node,
        is_removable=True,
        is_fat32=True,
        slice_size_bytes=size,
        volume_label="C2609P2610",
        identity_id="UUID-1",
    )

    with mock.patch.object(
        iw, "device_guard", return_value=guard_info
    ), mock.patch.object(
        iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
    ):
        result = iw.write(
            image_path,
            bus.current_node,
            expected_size_bytes=size,
            confirmed=True,
            resolve_node=bus.resolve_node,
            verify_guard=bus.verify_guard,
            timeout=5,
        )

    # The write landed, but verify could not complete -> incomplete (Req 6.4/13.2).
    assert result.status == "incomplete"
    assert result.verified is False
    assert bytes(bus.device) == content  # the write itself did land


# ---------------------------------------------------------------------------
# plan_segments math
# ---------------------------------------------------------------------------


def test_plan_segments_partitions_exactly(small_chunks):
    size = SMALL_CHUNK * 2 + 5
    segs = iw.plan_segments(size)
    # Contiguous, non-overlapping, covering [0, size).
    assert segs[0][0] == 0
    total = 0
    prev_end = 0
    for off, length in segs:
        assert off == prev_end
        prev_end = off + length
        total += length
    assert total == size
    assert prev_end == size
    # Each non-final segment is a full chunk.
    for off, length in segs[:-1]:
        assert length == SMALL_CHUNK


def test_plan_segments_rejects_nonpositive():
    with pytest.raises(ValueError):
        iw.plan_segments(0)


# ---------------------------------------------------------------------------
# Property test (>=100 examples): chunked write writes every byte exactly once
# and the chunked readback hash equals hash_image, for any image length and any
# set of reset points that clear within the retry budget.
#
# Feature: image-based-drive-write, Property: chunked write/read resume.
# Validates: Requirements 5.1, 5.3, 6.2, 6.3, 13.1
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _small_chunk_env():
    """Shrink chunk/block + zero backoff for one generated example and restore.

    A context manager (NOT a function-scoped fixture) so Hypothesis gets fresh,
    correctly-reset state per @given input — mirrors the pattern in
    tests/test_write_verifier_property.py that avoids the fixture-reuse pitfall.
    """
    saved = {
        name: getattr(iw, name)
        for name in ("DD_CHUNK_BYTES", "DD_BLOCK_BYTES", "DD_BLOCK_SIZE",
                     "DD_RETRY_BACKOFF_SECONDS")
    }
    iw.DD_CHUNK_BYTES = SMALL_CHUNK
    iw.DD_BLOCK_BYTES = SMALL_BLOCK
    iw.DD_BLOCK_SIZE = str(SMALL_BLOCK)
    iw.DD_RETRY_BACKOFF_SECONDS = 0
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(iw, name, value)


@settings(max_examples=120, deadline=None)
@given(
    length=st.integers(min_value=1, max_value=SMALL_CHUNK * 4 + 17),
    reset_frac=st.lists(st.floats(min_value=0.0, max_value=0.999), max_size=6),
    data=st.data(),
)
def test_property_chunked_resume_writes_every_byte_and_verifies(
    length, reset_frac, data
):
    content = bytes(
        data.draw(st.lists(st.integers(0, 255), min_size=length, max_size=length))
    )
    fd, image_path = tempfile.mkstemp(prefix="efis_chunk_", suffix=".img")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)

        with _small_chunk_env():
            segments = iw.plan_segments(length)
            seg_offsets = [off for off, _ in segments]

            bus = FakeBus(content, length)
            # Turn each reset fraction into a TRANSIENT reset on a real segment
            # offset (always clears within budget: one transient hit per segment).
            chosen = set()
            for frac in reset_frac:
                idx = min(int(frac * len(seg_offsets)), len(seg_offsets) - 1)
                chosen.add(seg_offsets[idx])
            for off in chosen:
                bus.resets.add(("write", off))
                bus.resets.add(("read", off))

            with mock.patch.object(
                iw.mount_swap, "run_privileged", side_effect=bus.fake_run_privileged
            ):
                written = iw.chunked_dd_write(
                    image_path,
                    bus.current_node,
                    length,
                    resolve_node=bus.resolve_node,
                    verify_guard=bus.verify_guard,
                    timeout=5,
                )
                assert written == length

                # Every byte written exactly once.
                coverage = bytearray(length)
                for off, seg_len in bus.write_log:
                    for i in range(off, off + seg_len):
                        coverage[i] += 1
                assert all(c == 1 for c in coverage)
                assert bytes(bus.device) == content

                # Chunked readback hash equals the image hash.
                source = iw.chunked_raw_read_source(
                    bus.current_node,
                    length,
                    resolve_node=bus.resolve_node,
                    verify_guard=bus.verify_guard,
                    timeout=5,
                )
                assert wv.verify_chunked(image_path, source) is True
                assert wv.hash_readback_chunks(
                    lambda: iter([bytes(bus.device)])
                ) == hashlib.sha256(content).hexdigest()
    finally:
        os.unlink(image_path)
