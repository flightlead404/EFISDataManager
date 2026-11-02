# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for ``drive_updater._build_local_manifest`` (task 2.1 / 2.2).

``_build_local_manifest(name, local_root)`` is the build-if-missing source the
manifest-only chart-sync path uses when the persisted local manifest is absent
or corrupt: it hashes the SSD family tree and returns a ``FamilyManifest`` that
matches ``Manifest_Builder.build`` plus the scanned ``Plates/`` prune, so the
sync path can diff against it instead of falling back to a size-only transfer.

It delegates to ``currency._build_family_manifest(name, local_root, cycle=None)``.
These tests lock down the observable contract the design (Section A) specifies:

  * scanned is rooted at ``ChartData`` and its entries EXCLUDE the ``Plates/``
    subtree (owned by the plates family) and the ``ScannedCharts.sqlite`` marker;
  * plates is rooted at ``ChartData/Plates`` so its entries are relative to that
    root and EXCLUDE the ``Plates.sqlite`` marker;
  * entries match ``Manifest_Builder.build`` run against the family root, plus
    the scanned ``Plates/`` prune (same relpaths, sha256, size);
  * the stamped cycle is best-effort and tolerated as ``None`` (the helper has
    no Seattle Avionics entry to harvest a cycle from, and the diff does not
    depend on the cycle).

Follows the existing patterns: real temp dirs, ``conftest.py`` isolation of
``LOCAL_MANIFEST_DIR``, and the FAT32-safe uppercase-alphanumeric path alphabet
from ``test_card_manifest_from_card_property.py``.

Requirements: 2.2, 2.3
"""

import os

from hypothesis import HealthCheck, given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager.manifest import Manifest_Builder


# --- helpers ----------------------------------------------------------------


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _ssd_image_with_charts(root):
    """Build a small ChartData tree on the SSD image.

    scanned payload (SEC/LO tiles) + the scanned marker, a Plates/ subtree
    (plate files + the plates marker). Returns the image root path.
    """
    local = os.path.join(root, "image")
    # scanned family payload under ChartData/ (minus Plates/, minus marker).
    _write(os.path.join(local, "ChartData", "SEC", "W085", "a.SEC.0.png"), b"sec-a")
    _write(os.path.join(local, "ChartData", "LO", "W085", "b.LO.0.png"), b"lo-bbbb")
    _write(os.path.join(local, "ChartData", "ScannedCharts.sqlite"), b"scanned-db")
    # plates family payload under ChartData/Plates/ + its marker.
    _write(os.path.join(local, "ChartData", "Plates", "US", "p1.png"), b"plate-1")
    _write(os.path.join(local, "ChartData", "Plates", "US", "p2.png"), b"plate-22")
    _write(os.path.join(local, "ChartData", "Plates", "Plates.sqlite"), b"plates-db")
    return local


# --- scanned: excludes Plates/ subtree and the marker -----------------------


def test_scanned_excludes_plates_subtree_and_marker(tmp_path):
    local = _ssd_image_with_charts(str(tmp_path))

    fam = du._build_local_manifest("scanned", local)

    assert fam.family == "scanned"
    rels = set(fam.entries)
    # scanned tiles are present, keyed relative to ChartData/.
    assert "SEC/W085/a.SEC.0.png" in rels
    assert "LO/W085/b.LO.0.png" in rels
    # the whole Plates/ subtree is pruned (owned by the plates family)...
    assert not any(r == "Plates" or r.startswith("Plates/") for r in rels)
    assert "Plates/US/p1.png" not in rels
    # ...and the scanned commit marker is excluded.
    assert "ScannedCharts.sqlite" not in rels


# --- plates: rooted at ChartData/Plates, marker excluded --------------------


def test_plates_rooted_at_chartdata_plates(tmp_path):
    local = _ssd_image_with_charts(str(tmp_path))

    fam = du._build_local_manifest("plates", local)

    assert fam.family == "plates"
    rels = set(fam.entries)
    # entries are relative to ChartData/Plates (no "Plates/" prefix).
    assert "US/p1.png" in rels
    assert "US/p2.png" in rels
    assert not any(r.startswith("Plates/") for r in rels)
    # the plates commit marker is excluded.
    assert "Plates.sqlite" not in rels


# --- entries match Manifest_Builder.build + the scanned prune ---------------


def test_scanned_entries_match_builder_plus_prune(tmp_path):
    local = _ssd_image_with_charts(str(tmp_path))

    fam = du._build_local_manifest("scanned", local)

    # Reference: build directly against the scanned root, then apply the same
    # Plates/ prune the download-time builder uses.
    ref = Manifest_Builder.build(
        "scanned", os.path.join(local, "ChartData"), cycle=None
    )
    ref_entries = {
        rel: entry
        for rel, entry in ref.entries.items()
        if not (rel == "Plates" or rel.startswith("Plates/"))
    }

    assert set(fam.entries) == set(ref_entries)
    for rel, entry in ref_entries.items():
        assert fam.entries[rel].relpath == entry.relpath
        assert fam.entries[rel].sha256 == entry.sha256
        assert fam.entries[rel].size == entry.size


def test_plates_entries_match_builder(tmp_path):
    local = _ssd_image_with_charts(str(tmp_path))

    fam = du._build_local_manifest("plates", local)

    # Reference: build directly against the plates root (no prune for plates).
    ref = Manifest_Builder.build(
        "plates", os.path.join(local, "ChartData", "Plates"), cycle=None
    )

    assert set(fam.entries) == set(ref.entries)
    for rel, entry in ref.entries.items():
        assert fam.entries[rel].relpath == entry.relpath
        assert fam.entries[rel].sha256 == entry.sha256
        assert fam.entries[rel].size == entry.size


# --- cycle is best-effort / null-tolerated ----------------------------------


def test_cycle_is_null(tmp_path):
    """The helper stamps ``cycle=None`` (best-effort; the diff ignores cycle)."""
    local = _ssd_image_with_charts(str(tmp_path))

    assert du._build_local_manifest("scanned", local).cycle is None
    assert du._build_local_manifest("plates", local).cycle is None


# --- property: arbitrary FAT32-safe trees still obey the contract -----------

# FAT32-safe uppercase-alphanumeric path component (established pattern in
# test_card_manifest_from_card_property.py). Markers/metadata are excluded so
# every generated file is retained by the manifest builder.
_MARKER_BASENAMES = frozenset({"ScannedCharts.sqlite", "Plates.sqlite", ".DS_Store"})


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


_COMPONENT = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=8,
).filter(_is_safe_component)

_RELPATH = st.lists(_COMPONENT, min_size=1, max_size=3).map("/".join)


def _no_ancestor_collisions(rels):
    """Drop relpaths that would collide as both a file and an ancestor dir."""
    comps = {rel: tuple(rel.split("/")) for rel in rels}
    drop = set()
    for a, ca in comps.items():
        for b, cb in comps.items():
            if a is b:
                continue
            if len(ca) < len(cb) and cb[: len(ca)] == ca:
                drop.add(a)
    return [rel for rel in rels if rel not in drop]


# scanned-tree relpaths (never under Plates/) and plates-tree relpaths.
_SCANNED_RELSET = st.lists(_RELPATH, min_size=1, max_size=6, unique=True).map(
    _no_ancestor_collisions
).filter(lambda rs: not any(r == "Plates" or r.startswith("Plates/") for r in rs))
_PLATES_RELSET = st.lists(_RELPATH, min_size=1, max_size=6, unique=True).map(
    _no_ancestor_collisions
)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scanned_rels=_SCANNED_RELSET, plates_rels=_PLATES_RELSET)
def test_build_local_manifest_contract_holds_for_arbitrary_trees(
    scanned_rels, plates_rels, tmp_path_factory
):
    """Over arbitrary FAT32-safe trees the scanned/plates contract holds.

    scanned entries never include a ``Plates/*`` relpath or the marker; plates
    entries are rooted at ``ChartData/Plates`` and exclude its marker; both
    match ``Manifest_Builder.build`` (+ scanned prune). Cycle is ``None``.

    Requirements: 2.2, 2.3
    """
    scanned_rels = [r for r in scanned_rels if r]
    plates_rels = [r for r in plates_rels if r]
    if not scanned_rels or not plates_rels:
        return

    root = tmp_path_factory.mktemp("blm")
    local = os.path.join(str(root), "image")

    for rel in scanned_rels:
        _write(os.path.join(local, "ChartData", *rel.split("/")), b"S-" + rel.encode())
    _write(os.path.join(local, "ChartData", "ScannedCharts.sqlite"), b"scanned-db")
    for rel in plates_rels:
        _write(
            os.path.join(local, "ChartData", "Plates", *rel.split("/")),
            b"P-" + rel.encode(),
        )
    _write(os.path.join(local, "ChartData", "Plates", "Plates.sqlite"), b"plates-db")

    scanned = du._build_local_manifest("scanned", local)
    plates = du._build_local_manifest("plates", local)

    # scanned: Plates/ subtree and marker excluded.
    assert not any(
        r == "Plates" or r.startswith("Plates/") for r in scanned.entries
    )
    assert "ScannedCharts.sqlite" not in scanned.entries
    assert set(scanned.entries) == set(scanned_rels)

    # plates: rooted at ChartData/Plates, marker excluded, relpaths relative.
    assert "Plates.sqlite" not in plates.entries
    assert set(plates.entries) == set(plates_rels)

    # Entries match the builder (+ scanned prune) exactly.
    ref_scanned = Manifest_Builder.build(
        "scanned", os.path.join(local, "ChartData"), cycle=None
    )
    ref_scanned_entries = {
        rel: e
        for rel, e in ref_scanned.entries.items()
        if not (rel == "Plates" or rel.startswith("Plates/"))
    }
    assert {r: (e.sha256, e.size) for r, e in scanned.entries.items()} == {
        r: (e.sha256, e.size) for r, e in ref_scanned_entries.items()
    }
    ref_plates = Manifest_Builder.build(
        "plates", os.path.join(local, "ChartData", "Plates"), cycle=None
    )
    assert {r: (e.sha256, e.size) for r, e in plates.entries.items()} == {
        r: (e.sha256, e.size) for r, e in ref_plates.entries.items()
    }

    # Cycle best-effort/null.
    assert scanned.cycle is None
    assert plates.cycle is None
