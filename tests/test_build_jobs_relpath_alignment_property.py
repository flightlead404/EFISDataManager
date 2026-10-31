# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for diff relpath / payload-root alignment (task 6.5).

Feature: chart-sync-wiring-fix, Property 4: diff relpaths align with family
payload roots.

For any chart-family diff job ``drive_updater.build_jobs`` produces, every
relpath in ``files_from`` and ``removed`` is interpreted relative to that
family's payload root (scanned = ``ChartData/`` with ``Plates/`` pruned; plates
= ``ChartData/Plates/``), so that joining the relpath under
``payload_root_local`` / ``payload_root_drive`` lands at the correct on-card
location with no remapping (design Section B, R2.4).

The scenario is built realistically: a temp local root carries a real
``ChartData`` tree (scanned files, a pruned ``Plates/`` sibling, and plates
files inside ``ChartData/Plates/``); the local family manifest is produced by
the REAL currency pipeline (``currency._build_family_manifest``), so its keys
are genuinely family-root-relative exactly as production keys them. A card
manifest with a ROLLED cycle is saved so ``decide_streams`` returns ``"diff"``
and the gate actually diffs.

The property then asserts:

  (a) ``payload_root_local`` ends with ``ChartData/`` (scanned) or
      ``ChartData/Plates/`` (plates), and ``payload_root_drive`` likewise under
      the mount — the payload root IS the family root.
  (b) for every relpath in ``files_from`` and ``removed``,
      ``os.path.join(payload_root, relpath)`` normalizes to a path INSIDE the
      family root (no ``../`` escape, still prefixed by the payload root); and
      for the scanned family NO ``files_from`` / ``removed`` relpath starts with
      ``Plates/`` (the pruned sibling owned by the plates family).
  (c) the relpaths are EXACTLY ``diff_family(local, card_entries)`` keyed the
      same way the manifest keys them — proving the diff keys map 1:1 onto the
      ``--files-from`` roots with no remapping.

Each example uses fresh temp dirs (isolated local manifest dir, a temp card
mount, and a temp local root with a real ChartData tree) cleaned up in a
``finally`` block. ``load_config`` is patched so ``usb_image_path`` points at
the throwaway local root, and ``_volume_name`` is patched to ``None`` so the
gate's label cross-check never shells out to ``diskutil``.

Validates: Requirements 2.4
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from hypothesis import given, settings, strategies as st

from efis_data_manager import currency as cu
from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Store,
    ManifestEntry,
)


# --- generators -------------------------------------------------------------

# A single filesystem-safe path segment. A leading "f" guarantees we never
# produce "." / ".." nor a leading dot, so every relpath stays inside its root.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=("_", "-"),
    ),
    min_size=1,
    max_size=8,
).map(lambda s: "f" + s)

# A relative POSIX path of 1-3 segments (e.g. "SEC/x", "LO/y/z").
_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

# A lowercase-hex SHA-256 digest (64 hex chars), matching the builder.
_SHA256 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

_SIZE = st.integers(min_value=0, max_value=2**16)

# A 4-digit cycle number (e.g. "2611").
_CYCLE = st.text(alphabet="0123456789", min_size=4, max_size=4)


def _rolled(cycle: str) -> str:
    """Return a 4-digit cycle guaranteed to differ from ``cycle`` so the gate
    chooses ``"diff"`` (never ``"skip-cycle"``) for the family."""
    first = "0" if cycle[0] != "0" else "1"
    return first + cycle[1:]


def _disjoint(relpaths):
    """Return ``relpaths`` rewritten so none is a path-prefix of another.

    The generated relpaths are arbitrary, so a pair like ``f0`` and ``f0/f1``
    collide on a real filesystem (``f0`` cannot be both a file and a directory).
    Prefixing each with a unique index segment (``d0/``, ``d1/`` ...) guarantees
    disjoint directory trees while keeping every relpath family-root-relative
    and multi-segment — which is exactly the shape the property cares about.
    """
    return [f"d{i}/{rel}" for i, rel in enumerate(relpaths)]


def _write_tree(root: str, relpaths, payload: bytes) -> None:
    """Materialize each POSIX ``relpath`` under ``root`` as a small file."""
    for rel in relpaths:
        abspath = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "wb") as fh:
            fh.write(payload)


def _is_inside(payload_root: str, relpath: str) -> bool:
    """True iff joining ``relpath`` under ``payload_root`` stays inside it."""
    joined = os.path.normpath(os.path.join(payload_root, relpath))
    base = os.path.normpath(payload_root)
    # Either the joined path equals the root (never, since relpaths are
    # non-empty) or it is strictly under it: base + os.sep must prefix it.
    return joined == base or joined.startswith(base + os.sep)


# --- the property -----------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    family=st.sampled_from(["scanned", "plates"]),
    scanned_rels=st.lists(_RELPATH, min_size=0, max_size=6, unique=True),
    plates_rels=st.lists(_RELPATH, min_size=0, max_size=6, unique=True),
    card_entries_raw=st.lists(_RELPATH, min_size=0, max_size=6, unique=True),
    card_cycle=_CYCLE,
)
def test_diff_relpaths_align_with_payload_roots(
    family, scanned_rels, plates_rels, card_entries_raw, card_cycle
):
    """Every diff relpath is family-root-relative and lands inside the payload
    root, and equals the diff_family output keyed as the manifest keys it.

    Validates: Requirements 2.4
    """
    local_manifest_dir = tempfile.mkdtemp(prefix="efis_manifests_")
    card_mount = tempfile.mkdtemp(prefix="efis_card_")
    local_root = tempfile.mkdtemp(prefix="efis_local_")
    try:
        # Build a REAL ChartData tree on the local root:
        #   ChartData/<scanned files>         -> scanned family (Plates/ pruned)
        #   ChartData/Plates/<plates files>   -> plates family
        chart_data = os.path.join(local_root, "ChartData")
        plates_dir = os.path.join(chart_data, "Plates")
        scanned_rels = _disjoint(scanned_rels)
        plates_rels = _disjoint(plates_rels)
        _write_tree(chart_data, scanned_rels, b"scanned-bytes")
        _write_tree(plates_dir, plates_rels, b"plates-bytes")

        # Build the local family manifest with the REAL currency pipeline so the
        # relpath keys are produced exactly as production produces them (scanned
        # has the Plates/ subtree pruned; plates is rooted at ChartData/Plates/).
        local_cycle = _rolled(card_cycle)
        local_fam = cu._build_family_manifest(family, local_root, local_cycle)

        # A card family with a DIFFERENT (rolled) cycle and arbitrary entries so
        # the diff has both "to transfer" (added/changed) and "removed" parts.
        card_fam_entries = {
            rel: ManifestEntry(relpath=rel, sha256=("a" * 64), size=1)
            for rel in card_entries_raw
        }
        card_fam = FamilyManifest(
            family=family,
            cycle=card_cycle,
            entries=dict(card_fam_entries),
            built="2026-10-24T18:40:52-07:00",
        )
        card = CardManifest(
            charts_cycle=(card_cycle if family == "scanned" else None),
            plates_cycle=(card_cycle if family == "plates" else None),
            families={family: card_fam},
        )

        with mock.patch.object(mf, "LOCAL_MANIFEST_DIR", Path(local_manifest_dir)):
            Manifest_Store.save_local(local_fam)
            Manifest_Store.save_card(card_mount, card)

            # (c) The exact expected diff, keyed the same way the manifest keys.
            expected = mf.diff_family(local_fam, card_fam.entries)

            with mock.patch.object(
                du, "load_config", lambda: {"usb_image_path": local_root}
            ), mock.patch.object(du, "_volume_name", lambda mp: None):
                jobs = du.build_jobs(card_mount, [family])

        # The gate diffed (rolled cycle), so exactly one job comes back.
        assert len(jobs) == 1
        job = jobs[0]
        assert job.name == family
        assert job.kind == "tree"
        assert job.files_from is not None
        assert job.removed is not None

        # (a) The payload roots ARE the family roots, trailing-separated.
        if family == "scanned":
            expected_local = os.path.join(local_root, "ChartData") + os.sep
            expected_drive = os.path.join(card_mount, "ChartData") + os.sep
        else:
            expected_local = os.path.join(local_root, "ChartData", "Plates") + os.sep
            expected_drive = os.path.join(card_mount, "ChartData", "Plates") + os.sep
        assert job.payload_root_local == expected_local
        assert job.payload_root_drive == expected_drive
        assert job.payload_root_local.endswith(os.sep)
        assert job.payload_root_drive.endswith(os.sep)

        # (b) Every files_from / removed relpath, joined under either payload
        # root, stays INSIDE the family root (no "../" escape), and for the
        # scanned family none of them reach into the pruned Plates/ sibling.
        for rel in list(job.files_from) + list(job.removed):
            assert not os.path.isabs(rel), f"relpath must be relative: {rel!r}"
            assert _is_inside(job.payload_root_local, rel), (
                f"{rel!r} escapes local payload root {job.payload_root_local!r}"
            )
            assert _is_inside(job.payload_root_drive, rel), (
                f"{rel!r} escapes drive payload root {job.payload_root_drive!r}"
            )
            if family == "scanned":
                assert rel != "Plates" and not rel.startswith("Plates/"), (
                    f"scanned relpath must not reach the pruned Plates/ sibling: {rel!r}"
                )

        # (c) The diff keys map 1:1 onto the --files-from roots with no
        # remapping: the job carries exactly diff_family's output.
        assert job.files_from == list(expected.files_from)
        assert job.removed == list(expected.removed)
    finally:
        shutil.rmtree(local_manifest_dir, ignore_errors=True)
        shutil.rmtree(card_mount, ignore_errors=True)
        shutil.rmtree(local_root, ignore_errors=True)
