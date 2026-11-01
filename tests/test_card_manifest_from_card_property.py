# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for from-card manifest build correctness (task 4.2).

Feature: chart-sync-wiring-fix, Property 8: adopt manifest-from-card build
correctness.

``drive_updater._card_manifest_from_card(mount_point, progress_callback=None)``
(design Section D.3, R6.3 / R6.11) builds a truthful ``CardManifest`` by running
``manifest.Manifest_Builder.build`` over the card's own ``ChartData/`` tree:

  - ``scanned`` = ``Manifest_Builder.build("scanned", <ChartData/>, cycle=None)``
    with the ``Plates/`` subtree pruned (``rel == "Plates"`` or
    ``rel.startswith("Plates/")``), matching the prune
    ``currency._build_family_manifest`` applies, so scanned excludes all
    ``Plates/*``.
  - ``plates`` = ``Manifest_Builder.build("plates", <ChartData/Plates/>,
    cycle=None)``.
  - Both per-family cycles are null, because the card carries no trustworthy
    cycle stamp (R6.11).

This test materializes an arbitrary ``ChartData/`` tree (SEC/, LO/, Plates/
files with random relpaths + bytes, plus the ScannedCharts.sqlite /
Plates.sqlite markers) under a fresh temp "card" dir and asserts the built
manifest equals the Manifest_Builder reference with the same prune, that scanned
never carries a ``Plates/`` relpath, and that a recording progress stub was
called at least once (R6.5).

Validates: Requirements 6.3, 6.11
"""

import os
import shutil
import tempfile

from hypothesis import HealthCheck, given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf


# --- generators -------------------------------------------------------------

# A path component: no separators, no NUL, non-empty, and never a basename the
# manifest builder excludes (.DS_Store / ._* / the commit markers) so that the
# generated tree's non-marker files are all retained by both the builder and
# _card_manifest_from_card. Markers are added deterministically, not generated.
_MARKER_BASENAMES = frozenset(
    {
        "ScannedCharts.sqlite",
        "Plates.sqlite",
        ".DS_Store",
    }
)


def _is_safe_component(name: str) -> bool:
    if not name or name in (".", ".."):
        return False
    if "/" in name or os.sep in name or "\x00" in name:
        return False
    if name.startswith("._"):
        return False
    if name in _MARKER_BASENAMES:
        return False
    return True


# Uppercase alphanumeric only. The EFIS drive is FAT32 — case-insensitive
# (case-preserving) — so two names differing only in case (e.g. "Q" vs "q")
# cannot coexist as distinct entries on the real medium, and the ancestor-
# collision guard below would not catch such a pair (it compares case-
# sensitively). Restricting the alphabet to a single case models the target
# filesystem and keeps generated trees materializable without case collisions.
_COMPONENT = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=8,
).filter(_is_safe_component)

# A family-root-relative POSIX relpath: 1-3 components joined by "/".
_RELPATH = st.lists(_COMPONENT, min_size=1, max_size=3).map("/".join)

# File bytes (may be empty).
_FILEBYTES = st.binary(min_size=0, max_size=64)


def _no_ancestor_collisions(files: dict) -> dict:
    """Drop relpaths that would collide on a real filesystem.

    A relpath cannot be both a file and the ancestor directory of another file.
    For any two relpaths where one's components are a strict prefix of the
    other's (e.g. "a" vs "a/b"), the shorter one is a directory and cannot also
    be a file. Keep the longer paths; drop the ancestor file entries.
    """
    comps = {rel: tuple(rel.split("/")) for rel in files}
    drop = set()
    for a, ca in comps.items():
        for b, cb in comps.items():
            if a is b:
                continue
            if len(ca) < len(cb) and cb[: len(ca)] == ca:
                drop.add(a)
    return {rel: data for rel, data in files.items() if rel not in drop}


# A mapping relpath -> bytes for a subtree. Using a dict keeps relpaths unique;
# the filter drops any relpath that is an ancestor directory of another so the
# tree materializes cleanly on a real filesystem.
_SUBTREE = st.dictionaries(_RELPATH, _FILEBYTES, max_size=12).map(
    _no_ancestor_collisions
)


# --- helpers ----------------------------------------------------------------


def _materialize(root: str, files: dict) -> None:
    """Write each relpath -> bytes under ``root``, creating parent dirs."""
    for rel, data in files.items():
        abspath = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(abspath) or root, exist_ok=True)
        with open(abspath, "wb") as f:
            f.write(data)


def _entry_pairs(entries):
    """Normalize an entries map to a comparable set of (relpath, sha256, size)."""
    return {(e.relpath, e.sha256, e.size) for e in entries.values()}


# --- the property -----------------------------------------------------------


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sec=_SUBTREE, lo=_SUBTREE, plates=_SUBTREE)
def test_card_manifest_from_card_build_correctness(sec, lo, plates):
    """_card_manifest_from_card == Manifest_Builder.build + Plates prune.

    Validates: Requirements 6.3, 6.11
    """
    card = tempfile.mkdtemp(prefix="efis_card_")
    try:
        chart_root = os.path.join(card, "ChartData")
        os.makedirs(chart_root, exist_ok=True)

        # SEC/ and LO/ live under ChartData/ (scanned family). Plates/ lives
        # under ChartData/Plates/ (plates family). Random relpaths within each.
        _materialize(os.path.join(chart_root, "SEC"), sec)
        _materialize(os.path.join(chart_root, "LO"), lo)
        plates_root = os.path.join(chart_root, "Plates")
        _materialize(plates_root, plates)

        # The per-family commit markers the builder already excludes.
        with open(os.path.join(chart_root, "ScannedCharts.sqlite"), "wb") as f:
            f.write(b"scanned-marker")
        os.makedirs(plates_root, exist_ok=True)
        with open(os.path.join(plates_root, "Plates.sqlite"), "wb") as f:
            f.write(b"plates-marker")

        # Recording progress stub (R6.5).
        calls = []

        result = du._card_manifest_from_card(card, progress_callback=calls.append)

        # R6.11: card carries no trustworthy cycle stamp.
        assert result.charts_cycle is None
        assert result.plates_cycle is None
        assert result.families["scanned"].cycle is None
        assert result.families["plates"].cycle is None

        # Reference manifests built the same way the helper is specified to.
        ref_scanned = mf.Manifest_Builder.build("scanned", chart_root, cycle=None)
        ref_scanned_pruned = {
            rel: e
            for rel, e in ref_scanned.entries.items()
            if not (rel == "Plates" or rel.startswith("Plates/"))
        }
        ref_plates = mf.Manifest_Builder.build("plates", plates_root, cycle=None)

        # scanned entries EXACTLY equal the pruned reference.
        assert _entry_pairs(result.families["scanned"].entries) == _entry_pairs(
            ref_scanned_pruned
        )
        # plates entries EXACTLY equal the ChartData/Plates/ reference.
        assert _entry_pairs(result.families["plates"].entries) == _entry_pairs(
            ref_plates.entries
        )

        # No scanned entry relpath starts with "Plates/".
        assert all(
            not rel.startswith("Plates/")
            for rel in result.families["scanned"].entries
        )

        # R6.5: progress reported at least once.
        assert len(calls) >= 1
    finally:
        shutil.rmtree(card, ignore_errors=True)
