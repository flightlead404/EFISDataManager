# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test: chart sync never deletes root-level flight data.

Feature: chart-sync-wiring-fix, Property 5: chart sync never deletes root-level
flight data. Updated for manifest-only-chart-sync R2.1: the legacy
``--size-only --delete`` whole-tree reconcile is RETIRED, so this property now
exercises the SOLE transfer mode — the manifest ``--files-from=-`` transfer plus
the explicit ``_prune_removed`` removal pass (``job.files_from is not None``).

This is the key safety invariant R10.5: a chart-family sync through the manifest
path MUST NOT delete, move, or modify root-level flight data or anything outside
the family's payload root. Because ``--files-from`` scopes the transfer to
exactly the listed family-root-relative relpaths and ``_prune_removed`` only
unlinks relpaths under the family payload root, nothing at the mount root (the
PARENT of ``ChartData/``) can ever be touched. For the scanned family the
payload root on the drive is
``<mount>/ChartData/`` (R2.4), so the family ``--delete`` and the ``removed``
prune are scoped WITHIN ``ChartData/``. Root-level survivors -- ``NAV.DB``,
``Settings.dat``, ``State.dat``, ``WP.dat``, ``Plan.dat``, ``*.bak``,
``EFIS_DRIVE_ID.json``, ``EFIS_MANIFEST.json``, and the ``GRTCHARTS/`` tree --
live at the mount root (the PARENT of ``ChartData/``), outside
``payload_root_drive`` entirely, and must be byte-for-byte untouched after a
sync.

The test drives the REAL ``sync_payload`` end to end against temp dirs standing
in for the local SSD image and the EFIS card mount (requires rsync; skipped if
absent, exactly like ``tests/test_sync_payload.py``). For each generated
example it:

  - builds an arbitrary local scanned family tree under ``local/ChartData/``;
  - seeds the drive with an arbitrary scanned tree under ``drive/ChartData/``
    AND an arbitrary set of root-level survivor files/dirs under ``drive/``
    (the mount root, OUTSIDE the ``ChartData/`` payload root);
  - runs a manifest-driven job with an arbitrary ``files_from`` (added+changed)
    set and an arbitrary ``removed`` set, both of whose relpaths stay WITHIN the
    family root (never ``"../"`` escaping);
  - asserts every root-level survivor still exists with identical bytes.

Mirrors the ``tests/test_sync_payload.py`` helper (``_files_from_job``,
``_rel_files``).

Validates: Requirements 10.5
"""

import os
import shutil

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


# --- helpers mirrored from tests/test_sync_payload.py -----------------------


def _files_from_job(src_dir, dst_dir, files_from, removed=None, excludes=None):
    """Build a manifest-driven (``--files-from=-``) scanned tree SyncJob."""
    return du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src_dir) + os.sep,
        payload_root_drive=str(dst_dir) + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES) + (excludes or []),
        files_from=list(files_from),
        removed=list(removed or []),
    )


def _rel_files(root):
    """Set of file paths relative to root (posix separators)."""
    out = set()
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            out.add(rel.replace(os.sep, "/"))
    return out


def _write_bytes(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def _snapshot(root):
    """Map relpath -> bytes for every regular file under ``root``."""
    snap = {}
    for rel in _rel_files(root):
        with open(os.path.join(root, *rel.split("/")), "rb") as f:
            snap[rel] = f.read()
    return snap


# --- generators -------------------------------------------------------------

# A filesystem-safe path segment. Leading "f" guarantees no "." / ".." / dotfile
# so a generated name never collides with a metadata exclude or the family root.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789" + "".join(("_", "-")),
    ),
    min_size=1,
    max_size=6,
).map(lambda s: "f" + s)

# A family-root-relative POSIX relpath (1-3 segments). These stay WITHIN the
# ChartData/ payload root -- they never contain "../", so a removal under the
# family root can never escape to the mount root.
_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map("/".join)


def _prefix_free(relpaths):
    """Keep only relpaths where none is a directory-prefix of another.

    On a real filesystem a single name cannot be both a file and a directory,
    so ``{"f0", "f0/f0"}`` is not a realizable tree. This filter drops any
    relpath that is a parent-directory of, or a child of, another kept relpath
    (a modeling constraint, not a product constraint).
    """
    kept = []
    for rel in relpaths:
        parts = rel.split("/")
        clash = False
        for other in kept:
            oparts = other.split("/")
            n = min(len(parts), len(oparts))
            if parts[:n] == oparts[:n]:
                clash = True
                break
        if not clash:
            kept.append(rel)
    return kept

# Small file contents.
_CONTENT = st.binary(min_size=0, max_size=24)

# The canonical root-level survivors R10.5 / R7.2 enumerate as flight data and
# provisioning files that must NEVER be touched by a chart sync, plus arbitrary
# extras (bak files, settings) at the mount root.
_FIXED_SURVIVORS = [
    "NAV.DB",
    "NAV-proc.DB",
    "Settings.dat",
    "State.dat",
    "WP.dat",
    "Plan.dat",
    "Settings.bak",
    "State.bak",
    "EFIS_DRIVE_ID.json",
    "EFIS_MANIFEST.json",
    "GRTCHARTS/keep.cfg",  # a file inside the GRTCHARTS/ dir at the root
]


@st.composite
def _survivor_set(draw):
    """An arbitrary set of root-level survivor relpaths -> bytes.

    Always includes a non-empty subset of the canonical flight-data files, plus
    arbitrary extra root-level files (including ``*.bak`` names and nested dirs
    that are still at the mount root, OUTSIDE ``ChartData/``).
    """
    chosen = draw(
        st.lists(st.sampled_from(_FIXED_SURVIVORS), min_size=1, unique=True)
    )
    extras = draw(
        st.lists(
            st.one_of(
                _SEGMENT.map(lambda s: s + ".bak"),
                _SEGMENT.map(lambda s: s + ".dat"),
                _RELPATH,  # arbitrary nested names, still at the mount root
            ),
            min_size=0,
            max_size=4,
            unique=True,
        )
    )
    # Never let an "extra" collide with the ChartData/ payload root, then make
    # the whole set prefix-free so it is a realizable on-disk tree.
    candidates = [
        rel
        for rel in list(chosen) + list(extras)
        if rel.split("/", 1)[0] != "ChartData"
    ]
    return {rel: draw(_CONTENT) for rel in _prefix_free(candidates)}


@st.composite
def _family_trees(draw):
    """A shared, prefix-free namespace split into the local and drive payloads.

    Local and drive scanned payloads are drawn from ONE prefix-free pool so a
    relpath is never a file on one side and a directory on the other -- an
    unrealizable tree that rsync legitimately rejects (a modeling constraint,
    not a product constraint). The two payloads otherwise overlap arbitrarily
    (shared, local-only, drive-only), exercising adds, same-path changes, and
    drive-only files that a sync would prune.

    Returns ``(local_tree, drive_tree, removed)`` where ``removed`` is a subset
    of family relpaths (always within the family root, never ``"../"``).
    """
    pool = _prefix_free(
        draw(st.lists(_RELPATH, min_size=0, max_size=8, unique=True))
    )
    local_tree = {}
    drive_tree = {}
    for rel in pool:
        in_local = draw(st.booleans())
        in_drive = draw(st.booleans())
        if in_local:
            local_tree[rel] = draw(_CONTENT)
        if in_drive:
            # A shared path may carry different bytes on the drive (a change).
            drive_tree[rel] = draw(_CONTENT)
    # removed set: an arbitrary subset of the family namespace.
    removed = draw(st.lists(st.sampled_from(pool), unique=True)) if pool else []
    return local_tree, drive_tree, removed


# --- the property -----------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    trees=_family_trees(),
    survivors=_survivor_set(),
    data=st.data(),
)
def test_chart_sync_never_deletes_root_flight_data(
    tmp_path_factory, trees, survivors, data
):
    """A manifest-driven scanned sync leaves every root-level survivor intact.

    The family payload roots are ``local/ChartData/`` and ``drive/ChartData/``,
    so the manifest ``--files-from`` transfer and its explicit ``removed`` prune
    (``_prune_removed``) are confined to ``ChartData/``. Root-level survivors sit
    at the mount root -- the parent of ``ChartData/`` -- outside the payload
    root, and must be byte-for-byte unchanged after the sync, no matter what the
    diff adds/removes inside the family. (The retired legacy ``--delete`` mode is
    gone; the manifest path never issues a whole-tree delete.)

    Validates: Requirements 10.5
    """
    local_tree, drive_tree, removed = trees

    base = tmp_path_factory.mktemp("rootdata")
    local = base / "local"
    drive = base / "drive"

    # Local scanned family payload (what the sync would push).
    local_chart = local / "ChartData"
    for rel, content in local_tree.items():
        _write_bytes(str(local_chart / rel.replace("/", os.sep)), content)
    local_chart.mkdir(parents=True, exist_ok=True)

    # Drive: seed its own ChartData/ payload...
    drive_chart = drive / "ChartData"
    for rel, content in drive_tree.items():
        _write_bytes(str(drive_chart / rel.replace("/", os.sep)), content)
    drive_chart.mkdir(parents=True, exist_ok=True)

    # ...and the root-level survivors at the MOUNT ROOT (outside ChartData/).
    for rel, content in survivors.items():
        _write_bytes(str(drive / rel.replace("/", os.sep)), content)

    before = _snapshot(str(drive))
    # Every survivor is physically outside the payload root.
    for rel in survivors:
        assert not rel.startswith("ChartData/")

    # Arbitrary added+changed set (a subset of the local payload) and an
    # arbitrary removed set -- all relpaths stay WITHIN the family root
    # (no "../"), so _prune_removed can only ever touch ChartData/.
    local_rels = sorted(local_tree)
    files_from = (
        data.draw(st.lists(st.sampled_from(local_rels), unique=True))
        if local_rels
        else []
    )
    job = _files_from_job(
        local_chart,
        drive_chart,
        files_from=files_from,
        removed=removed,
        excludes=["Plates/", "ScannedCharts.sqlite"],
    )

    _updated, errors = du.sync_payload(job)
    assert errors == []

    # THE INVARIANT (R10.5): every root-level survivor still exists with
    # identical bytes. Nothing outside the ChartData/ payload root was deleted,
    # pruned, or modified by the manifest sync.
    after = _snapshot(str(drive))
    for rel, content in survivors.items():
        assert rel in after, (
            f"root-level survivor {rel!r} was deleted by a manifest chart sync "
            f"(R10.5 violation)"
        )
        assert after[rel] == content, (
            f"root-level survivor {rel!r} was modified by a manifest chart sync "
            f"(R10.5 violation)"
        )
        # And it is byte-identical to the pre-sync snapshot at the mount root.
        assert after[rel] == before[rel]
