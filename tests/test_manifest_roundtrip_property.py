# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the manifest round-trip (task 1.4).

Feature: chart-manifest-sync, Property 5: Manifest round-trip.

Design Property 5 — Manifest round-trip: for any ``FamilyManifest`` ``m``,
persisting it with ``Manifest_Store.save_local`` and reading it back with
``Manifest_Store.load_local`` yields an equal manifest. Serialize -> parse is
the identity on the manifest value.

This is the correctness guarantee the cycle gate and diff depend on: the local
manifest written at download time must survive a save/load cycle byte-for-byte
in meaning, so the diff compares the real content the sync will transfer and the
recorded cycle is trustworthy.

The local store keys the JSON file by ``manifest.family`` (``scanned.json`` /
``plates.json``), so each generated manifest carries a valid family name. The
local manifest dir is redirected into a fresh temp dir per example so the real
app-support dir is never touched and examples never collide on disk.

Validates: Requirements 5.3
"""

import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from efis_data_manager import manifest as m
from efis_data_manager.manifest import (
    FamilyManifest,
    ManifestEntry,
    Manifest_Store,
)


# A lowercase-hex SHA-256 digest: exactly 64 hex characters, matching what the
# builder produces (``hashlib.sha256().hexdigest()``).
_SHA256 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

# File sizes are non-negative byte counts.
_SIZE = st.integers(min_value=0, max_value=2**31)

# POSIX relpath keys: 1-3 filesystem-safe segments joined by "/". A leading "f"
# guarantees we never produce "." / ".." and never collide with an excluded
# basename. These are only map keys here (no file is written), so any distinct
# string is fine, but keeping them realistic exercises the real key space.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789" + "".join(("_", "-", ".")),
    ),
    min_size=1,
    max_size=8,
).map(lambda s: "f" + s)

_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

# ISO-8601-ish timestamp strings for the ``built`` field. Kept as free text
# because the store treats ``built`` as an opaque string it round-trips.
_BUILT = st.text(max_size=40)

# The two legal family names (the local store keys the file by this value).
_FAMILY = st.sampled_from(["scanned", "plates"])

# Cycle number: a 4-digit string, or None (first-sync / unknown).
_CYCLE = st.one_of(st.none(), st.text(alphabet="0123456789", min_size=4, max_size=4))


@st.composite
def _entries(draw):
    """A relpath -> ManifestEntry map with consistent keys.

    Each entry's ``relpath`` equals its map key, which is the invariant the
    serializer relies on (the key is the stored relpath; the value re-attaches
    it on load).
    """
    raw = draw(st.dictionaries(_RELPATH, st.tuples(_SHA256, _SIZE), max_size=8))
    return {
        rel: ManifestEntry(relpath=rel, sha256=sha, size=size)
        for rel, (sha, size) in raw.items()
    }


@st.composite
def _family_manifests(draw):
    """Generate an arbitrary FamilyManifest."""
    return FamilyManifest(
        family=draw(_FAMILY),
        cycle=draw(_CYCLE),
        entries=draw(_entries()),
        built=draw(_BUILT),
        schema_version=m.SCHEMA_VERSION,
    )


@settings(max_examples=100, deadline=None)
@given(manifest=_family_manifests())
def test_local_manifest_round_trips(manifest):
    """load_local(save_local(m)) == m for any FamilyManifest (Property 5).

    The local manifest dir is redirected to a fresh temp dir for each generated
    example and restored afterward. A plain attribute swap (not the
    function-scoped ``monkeypatch`` fixture, which Hypothesis will not reset
    between examples) keeps every example isolated on disk.

    Validates: Requirements 5.3
    """
    local_dir = Path(tempfile.mkdtemp(prefix="efis_manifest_roundtrip_"))
    original_dir = m.LOCAL_MANIFEST_DIR
    m.LOCAL_MANIFEST_DIR = local_dir
    try:
        Manifest_Store.save_local(manifest)
        loaded = Manifest_Store.load_local(manifest.family)
        assert loaded == manifest
    finally:
        m.LOCAL_MANIFEST_DIR = original_dir
        shutil.rmtree(local_dir, ignore_errors=True)
