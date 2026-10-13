# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Raw-readback verifier for the image-based drive write.

After the writer ``dd``'s the composed FAT32 image onto a drive's raw partition
slice, completion is confirmed by reading the written device region straight
back and comparing its hash to the composed image's hash. A match means the
drive's bytes equal the image we built, so the write is complete; a mismatch or
a short read (the device region ended before the image did — a truncated or
interrupted write) means incomplete.

This is the module that realises design **Property 3 — Verified completion**:
``verify(...)`` reports complete IFF the raw-readback hash equals the composed
image hash. It is also deliberately read-only and mount-free, upholding design
**Property 1 — No mount of the target drive**: nothing here mounts a volume or
touches ``/Volumes/``; the device region is consumed as a raw byte stream.

The key design point is that the **readback source is injectable** so the hash
and comparison logic is fully unit-testable WITHOUT a device. In production the
caller supplies a raw read of ``/dev/rdiskNsN`` (that privileged wiring is Task
5.1, not this module); in tests the caller supplies a byte buffer, a temp file,
or any binary stream / bytes-returning callable. This module never opens a
``/dev/`` node itself.

Both hashes are computed by streaming the data through ``hashlib.sha256`` in
fixed-size blocks (:data:`HASH_BLOCK_SIZE`), so a conceptually tens-of-GB image
is never loaded whole into memory.
"""

import hashlib
import logging
import os
from typing import BinaryIO, Callable, Iterable, Union

logger = logging.getLogger(__name__)

# Fixed block size for streamed hashing. Bounded so neither the image nor the
# raw readback (conceptually tens of GB) is ever read whole into memory; the
# peak working set is one block regardless of total size. 1 MiB is a good
# balance of syscall overhead vs. memory for sequential reads off an SSD/device.
HASH_BLOCK_SIZE = 1024 * 1024

# A readback source is one of:
#   * a path to a readable file (str / os.PathLike) — e.g. a temp file in tests;
#   * an already-open binary stream positioned at the region start (BinaryIO) —
#     e.g. an io.BytesIO buffer in tests, or the production raw-read stream;
#   * a zero-arg callable returning the region's bytes (bytes-returning callable)
#     — e.g. the Task 5.1 privileged raw read of /dev/rdiskNsN.
ReadbackSource = Union[str, os.PathLike, BinaryIO, Callable[[], bytes]]


def _hash_stream(stream: BinaryIO) -> str:
    """Return the sha256 hex digest of ``stream`` read in fixed-size blocks.

    Reads from the stream's current position to EOF in :data:`HASH_BLOCK_SIZE`
    chunks so memory stays bounded regardless of total length. The caller owns
    the stream's lifetime (open/close); this only consumes it forward.
    """
    digest = hashlib.sha256()
    while True:
        block = stream.read(HASH_BLOCK_SIZE)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def _hash_bytes(data: bytes) -> str:
    """Return the sha256 hex digest of an in-memory ``bytes`` object.

    Used for the bytes-returning-callable readback source. The bytes are still
    fed through the hash in fixed-size blocks so the hashing step itself does
    not add an unbounded copy on top of the bytes the callable already returned.
    """
    digest = hashlib.sha256()
    for offset in range(0, len(data), HASH_BLOCK_SIZE):
        digest.update(data[offset : offset + HASH_BLOCK_SIZE])
    return digest.hexdigest()


def _hash_chunk_iter(chunks: Iterable[bytes]) -> str:
    """Return the sha256 hex digest of an iterable of byte chunks, streamed.

    This is the CHUNKED readback path (image-based-drive-write robustness fix):
    the raw readback is driven segment by segment (``image_writer`` yields one
    ``bytes`` per ~1 GiB segment so a transient USB bus reset costs one segment,
    not the whole read), and each segment is fed straight into the running hash.
    The composed-image side is still hashed by :func:`hash_image` in the same
    fixed-size blocks, so the two digests are directly comparable; the whole
    (tens-of-GB) region is never held in memory — only one segment at a time,
    and each segment is itself consumed in :data:`HASH_BLOCK_SIZE` sub-blocks so
    even a large segment does not add an unbounded copy.
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError(
                "chunked readback source must yield bytes, got "
                f"{type(chunk).__name__}"
            )
        view = memoryview(chunk)
        for offset in range(0, len(view), HASH_BLOCK_SIZE):
            digest.update(view[offset : offset + HASH_BLOCK_SIZE])
    return digest.hexdigest()


def hash_image(image_path: Union[str, os.PathLike]) -> str:
    """Return the sha256 hex digest of the composed image file at ``image_path``.

    The image hash is computed over the image file by streaming it in the same
    fixed-size blocks as the readback (see :func:`_hash_stream`), so the two
    sides are hashed identically and are directly comparable. No device is
    involved; this reads a local file only.
    """
    with open(image_path, "rb") as handle:
        return _hash_stream(handle)


def hash_readback(readback_source: ReadbackSource) -> str:
    """Return the sha256 hex digest of a readback source, however it is supplied.

    Dispatches on the injectable source shape so the comparison logic is the
    same whether the bytes come from a test buffer or the production raw read of
    ``/dev/rdiskNsN``:

      * ``str`` / ``os.PathLike`` -> open the path binary and stream-hash it;
      * a binary stream (anything with ``read``) -> stream-hash from its current
        position (the caller positions it at the region start);
      * a zero-arg callable -> call it and hash the returned ``bytes``.

    This never opens a ``/dev/`` node itself: the privileged raw read is wired in
    by the caller (Task 5.1) and handed in as a stream or a bytes-returning
    callable.
    """
    # Path to a file (temp file in tests; never a /dev node here).
    if isinstance(readback_source, (str, os.PathLike)):
        with open(readback_source, "rb") as handle:
            return _hash_stream(handle)

    # Already-open binary stream (io.BytesIO in tests; raw-read stream in prod).
    if hasattr(readback_source, "read"):
        return _hash_stream(readback_source)  # type: ignore[arg-type]

    # Zero-arg callable returning the region bytes (the Task 5.1 raw read).
    if callable(readback_source):
        data = readback_source()
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                "readback callable must return bytes, got "
                f"{type(data).__name__}"
            )
        return _hash_bytes(bytes(data))

    raise TypeError(
        "readback_source must be a path, a binary stream, or a bytes-returning "
        f"callable, got {type(readback_source).__name__}"
    )


def verify(
    image_path: Union[str, os.PathLike], readback_source: ReadbackSource
) -> bool:
    """Return True iff the raw readback matches the composed image, else False.

    Computes the sha256 of the composed image at ``image_path`` and the sha256
    of ``readback_source`` (the raw device region, injected so this is testable
    without a device — see module docstring), both streamed in fixed-size blocks
    for bounded memory, and compares them:

      * **match** -> the drive's bytes equal the image -> write complete -> True;
      * **mismatch** -> the bytes differ -> write incomplete -> False;
      * **short read / truncation** -> a shorter readback hashes differently
        from the full image, so it falls out as a mismatch -> incomplete ->
        False. (A truncated or interrupted ``dd`` can never hash-match the full
        image, so a partial write is never reported complete.)

    Pure read with no mount and no ``/dev/`` access of its own, upholding design
    Properties 1 and 3.
    """
    image_hash = hash_image(image_path)
    readback_hash = hash_readback(readback_source)
    complete = readback_hash == image_hash
    if complete:
        logger.info(
            "verify: raw readback matches composed image (sha256=%s); write complete",
            image_hash,
        )
    else:
        logger.warning(
            "verify: raw readback does NOT match composed image "
            "(image sha256=%s, readback sha256=%s); write incomplete — re-write "
            "and re-verify the whole image",
            image_hash,
            readback_hash,
        )
    return complete


# A CHUNKED readback source is a zero-arg callable returning an ITERATOR (or any
# iterable) of byte segments covering the whole region in order — the shape the
# image-based-drive-write robustness fix uses so a flaky USB bridge's reset
# costs one ~1 GiB segment, not the whole raw read. It is kept DISTINCT from
# ReadbackSource (which yields the whole region as one blob / stream / path) so
# the existing verify(...) signature and its three source shapes are untouched;
# the chunked capability is added ALONGSIDE via verify_chunked(...).
ChunkedReadbackSource = Callable[[], Iterable[bytes]]


def hash_readback_chunks(chunk_source: ChunkedReadbackSource) -> str:
    """Return the sha256 of a chunked readback source, streamed segment by segment.

    Calls ``chunk_source()`` to obtain the iterator of segment byte-chunks (the
    image_writer segmented raw read) and feeds every segment into one running
    sha256 via :func:`_hash_chunk_iter`. The digest is computed over the
    concatenation of all segments in order, so it equals the digest
    :func:`hash_image` computes over the composed image when the drive bytes
    match — without ever holding the whole region in memory.
    """
    if not callable(chunk_source):
        raise TypeError(
            "chunked readback source must be a zero-arg callable returning an "
            f"iterable of bytes, got {type(chunk_source).__name__}"
        )
    chunks = chunk_source()
    return _hash_chunk_iter(chunks)


def verify_chunked(
    image_path: Union[str, os.PathLike], chunk_source: ChunkedReadbackSource
) -> bool:
    """Return True iff the CHUNKED raw readback matches the composed image.

    The chunked analogue of :func:`verify`. The composed image at ``image_path``
    is hashed exactly as in :func:`verify` (:func:`hash_image`, fixed-size
    blocks), and the raw readback is hashed by streaming its ordered segments
    through one sha256 (:func:`hash_readback_chunks`), so the two digests are
    directly comparable:

      * **match** -> the drive's bytes equal the image -> write complete -> True;
      * **mismatch / short read** -> the digests differ -> incomplete -> False.

    Semantics are identical to :func:`verify`; only the readback SOURCE shape
    differs (a segment iterator rather than a whole-region blob/stream/path), so
    a mid-stream bus reset that the segmented source recovers from is invisible
    here — it still produces one hash over the whole region. A reset the source
    cannot recover from propagates as its own exception (e.g.
    ``image_writer.ChunkedTransferError``) for the caller to map to incomplete.

    Pure read with no mount and no ``/dev/`` access of its own (design
    Properties 1 and 3).
    """
    image_hash = hash_image(image_path)
    readback_hash = hash_readback_chunks(chunk_source)
    complete = readback_hash == image_hash
    if complete:
        logger.info(
            "verify_chunked: chunked raw readback matches composed image "
            "(sha256=%s); write complete",
            image_hash,
        )
    else:
        logger.warning(
            "verify_chunked: chunked raw readback does NOT match composed image "
            "(image sha256=%s, readback sha256=%s); write incomplete — re-write "
            "and re-verify the whole image",
            image_hash,
            readback_hash,
        )
    return complete
