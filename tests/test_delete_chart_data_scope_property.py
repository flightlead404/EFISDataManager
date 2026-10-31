# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the scoped ChartData delete (task 3.2).

Feature: chart-sync-wiring-fix, Property 9: adopt ChartData delete scope.

Design Section D.4 — ``_delete_chart_data_tree(mount_point)`` performs the
adopt ``ChartData_Delete``: it removes ONLY the card's ``ChartData/`` tree
(the scanned payload, the ``ChartData/Plates/`` plates payload, and the
``ScannedCharts.sqlite`` / ``Plates.sqlite`` markers that live inside
``ChartData/``). Everything at the volume root — ``NAV.DB``, ``NAV-proc.DB``,
the Identity_File (``EFIS_DRIVE_ID.json``), the Card_Manifest
(``EFIS_MANIFEST.json``), ``GRTCHARTS/``, and every root-level settings /
flight-data / backup file (``Settings.dat``, ``Plan.dat``, ``State.dat``,
``WP.dat``, ``*.bak``) — is physically outside ``ChartData/`` and MUST be left
untouched. The delete is a targeted ``ChartData/`` removal, NOT a format and
NOT a root-level wipe.

Property 9 asserts, across arbitrary card layouts, that after the delete:

  * (R7.1) the ``ChartData/`` tree is entirely gone;
  * (R7.2) every root-level survivor file/dir still exists with identical
    bytes (and survivor directory trees are byte-for-byte preserved);
  * (R7.3 / R10.6) the volume root itself was not wiped or formatted — the
    mount dir still exists and still holds the survivors; and
  * idempotency — a second call on the now-missing ``ChartData/`` is a safe
    no-op that disturbs nothing.

Each example materializes a fresh ``tempfile.mkdtemp`` "card" dir and cleans it
up in a ``finally`` block, rather than a Hypothesis function-scoped fixture
(Hypothesis flags reusing a function-scoped fixture across generated examples
because the fixture is set up only once for the whole test).

Validates: Requirements 7.1, 7.2, 7.3, 10.6
"""

import os
import shutil
import tempfile

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A filesystem-safe path segment (file or dir name). A leading "f" guarantees
# we never produce "." / ".." and never collide with a reserved survivor name.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=("_", "-"),
    ),
    min_size=1,
    max_size=10,
).map(lambda s: "f" + s)

# A relpath of 1-3 segments, used for arbitrary files inside ChartData/.
_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

# Arbitrary file bytes (small; content identity is what matters, not size).
_BYTES = st.binary(min_size=0, max_size=64)

# The root-level survivors that MUST be preserved (R7.2). Fixed-name files,
# GRTCHARTS/ (a directory), and arbitrary *.bak files.
_FIXED_SURVIVOR_FILES = [
    "NAV.DB",
    "NAV-proc.DB",
    "EFIS_DRIVE_ID.json",
    "EFIS_MANIFEST.json",
    "Settings.dat",
    "Plan.dat",
    "State.dat",
    "WP.dat",
]

_BAK_NAME = _SEGMENT.map(lambda s: s + ".bak")


@st.composite
def _card_layout(draw):
    """Generate a card layout: a ChartData/ subtree plus root-level survivors.

    Returns ``(chartdata_files, survivor_files, survivor_dir_files)`` where:

      * ``chartdata_files`` maps a ChartData-relative relpath -> bytes. Always
        includes the ``ScannedCharts.sqlite`` / ``Plates.sqlite`` markers and
        at least some payload under ``SEC/``, ``LO/``, and ``Plates/`` so the
        scanned payload, plates payload, and markers are all exercised.
      * ``survivor_files`` maps a root-relative relpath -> bytes for flat
        survivor files (fixed names + arbitrary ``*.bak``).
      * ``survivor_dir_files`` maps a root-relative relpath -> bytes for files
        living inside survivor directories (e.g. ``GRTCHARTS/...``).
    """
    chartdata_files = {}

    # Markers that live directly inside ChartData/.
    chartdata_files["ScannedCharts.sqlite"] = draw(_BYTES)
    chartdata_files["Plates.sqlite"] = draw(_BYTES)

    # Scanned payload under representative subtrees, plus the plates payload.
    for prefix in ("SEC", "LO", "Plates"):
        count = draw(st.integers(min_value=1, max_value=3))
        for _ in range(count):
            rel = prefix + "/" + draw(_RELPATH)
            chartdata_files[rel] = draw(_BYTES)

    # A sprinkle of arbitrary extra files anywhere in ChartData/.
    for rel in draw(st.lists(_RELPATH, min_size=0, max_size=5, unique=True)):
        chartdata_files[rel] = draw(_BYTES)

    # ---- Root-level survivors (R7.2) ----
    survivor_files = {}

    # An arbitrary, non-empty subset of the fixed survivor files.
    chosen_fixed = draw(
        st.lists(st.sampled_from(_FIXED_SURVIVOR_FILES), min_size=1, unique=True)
    )
    for name in chosen_fixed:
        survivor_files[name] = draw(_BYTES)

    # Arbitrary *.bak backup files at the root.
    for name in draw(st.lists(_BAK_NAME, min_size=0, max_size=4, unique=True)):
        survivor_files[name] = draw(_BYTES)

    # Survivor directory trees. GRTCHARTS/ is always present as a dir; it may
    # hold arbitrary files. Add an optional second arbitrary survivor dir too.
    survivor_dir_files = {}
    survivor_dirs = ["GRTCHARTS"]
    extra_dir = draw(st.one_of(st.none(), _SEGMENT))
    if extra_dir is not None and extra_dir != "GRTCHARTS":
        survivor_dirs.append(extra_dir)
    for d in survivor_dirs:
        for rel in draw(st.lists(_RELPATH, min_size=0, max_size=4, unique=True)):
            survivor_dir_files[d + "/" + rel] = draw(_BYTES)

    return chartdata_files, survivor_files, survivor_dir_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drop_path_collisions(files):
    """Drop relpaths that would collide file-vs-directory on a real filesystem.

    A generated set may contain both ``a/b`` (a file) and ``a/b/c`` (which needs
    ``a/b`` to be a directory). Those cannot coexist on disk. Keep only relpaths
    that are not a strict parent prefix of any other relpath in the set, so the
    surviving set materializes without a FileExistsError. This is purely a
    generator hygiene step; it does not weaken the property.
    """
    keys = list(files)
    kept = {}
    for rel in keys:
        prefix = rel + "/"
        if any(other.startswith(prefix) for other in keys):
            continue  # ``rel`` is a directory for some other file -> skip as a file
        kept[rel] = files[rel]
    return kept


def _write_file(root, relpath, data):
    """Write ``data`` to ``root/relpath``, creating parent dirs as needed."""
    full = os.path.join(root, *relpath.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)


def _materialize(root, chartdata_files, survivor_files, survivor_dir_files):
    """Build the card tree on disk under ``root`` (a temp "mount point")."""
    for rel, data in chartdata_files.items():
        _write_file(os.path.join(root, "ChartData"), rel, data)
    for rel, data in survivor_files.items():
        _write_file(root, rel, data)
    for rel, data in survivor_dir_files.items():
        _write_file(root, rel, data)
    # Ensure GRTCHARTS/ exists as a directory even when it holds no files.
    os.makedirs(os.path.join(root, "GRTCHARTS"), exist_ok=True)


def _clean_layout(layout):
    """Apply path-collision hygiene to a generated layout before materializing.

    ChartData files and survivor-dir files are tree-structured, so they get the
    prefix-collision filter. Survivor files are flat (single-segment) names and
    cannot collide, so they pass through unchanged.
    """
    chartdata_files, survivor_files, survivor_dir_files = layout
    return (
        _drop_path_collisions(chartdata_files),
        survivor_files,
        _drop_path_collisions(survivor_dir_files),
    )
    # Ensure GRTCHARTS/ exists as a directory even when it holds no files.
    os.makedirs(os.path.join(root, "GRTCHARTS"), exist_ok=True)


def _survivor_snapshot(root, survivor_files, survivor_dir_files):
    """Capture the exact bytes of every survivor file for later comparison."""
    snap = {}
    for rel in list(survivor_files) + list(survivor_dir_files):
        full = os.path.join(root, *rel.split("/"))
        with open(full, "rb") as fh:
            snap[rel] = fh.read()
    return snap


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(layout=_card_layout())
def test_delete_chart_data_scope(layout):
    """``_delete_chart_data_tree`` deletes only ChartData/ and is idempotent.

    For an arbitrary card layout (a ChartData/ subtree with SEC/LO/Plates/
    payload and the sqlite markers, plus arbitrary root-level survivors):

      * (R7.1) after the delete, ``ChartData/`` is entirely gone;
      * (R7.2) every survivor file/dir still exists with identical bytes;
      * (R7.3 / R10.6) the volume root still exists and still holds the
        survivors — it was not wiped or formatted; and
      * a second ``_delete_chart_data_tree`` on the now-missing ``ChartData/``
        is a safe no-op that disturbs nothing.

    Validates: Requirements 7.1, 7.2, 7.3, 10.6
    """
    chartdata_files, survivor_files, survivor_dir_files = _clean_layout(layout)

    mount_point = tempfile.mkdtemp(prefix="efis_card_")
    try:
        _materialize(
            mount_point, chartdata_files, survivor_files, survivor_dir_files
        )

        chart_root = os.path.join(mount_point, "ChartData")
        before = _survivor_snapshot(
            mount_point, survivor_files, survivor_dir_files
        )

        # Preconditions: ChartData/ and its markers really exist up front.
        assert os.path.isdir(chart_root)
        assert os.path.isfile(os.path.join(chart_root, "ScannedCharts.sqlite"))
        assert os.path.isfile(os.path.join(chart_root, "Plates.sqlite"))

        # --- act ---
        drive_updater._delete_chart_data_tree(mount_point)

        # (R7.1) ChartData/ is entirely gone — the whole subtree, markers,
        # scanned payload, and Plates/ payload all removed.
        assert not os.path.exists(chart_root)

        # (R7.3 / R10.6) the volume root itself was not wiped or formatted.
        assert os.path.isdir(mount_point)

        # (R7.2) every survivor file/dir still exists with identical bytes.
        after = _survivor_snapshot(
            mount_point, survivor_files, survivor_dir_files
        )
        assert after == before

        # GRTCHARTS/ survives as a directory (R7.2).
        assert os.path.isdir(os.path.join(mount_point, "GRTCHARTS"))

        # (R7.3) the ONLY thing removed under the root is ChartData/: the set of
        # root-level entries after the delete is exactly what it was minus
        # ChartData/.
        root_entries_after = set(os.listdir(mount_point))
        assert "ChartData" not in root_entries_after
        # Each survivor's top-level name is still present at the root.
        for rel in list(survivor_files) + list(survivor_dir_files):
            top = rel.split("/")[0]
            assert top in root_entries_after

        # --- idempotency: a second call on the missing ChartData/ is a no-op ---
        drive_updater._delete_chart_data_tree(mount_point)

        assert not os.path.exists(chart_root)
        assert os.path.isdir(mount_point)
        assert _survivor_snapshot(
            mount_point, survivor_files, survivor_dir_files
        ) == before
        assert set(os.listdir(mount_point)) == root_entries_after
    finally:
        shutil.rmtree(mount_point, ignore_errors=True)
