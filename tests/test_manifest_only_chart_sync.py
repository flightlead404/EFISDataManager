# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exploration tests pinning the manifest-only-chart-sync defect (task 1).

These are BUG-CONDITION EXPLORATION tests. They assert the EXPECTED POST-FIX
behavior and are therefore EXPECTED TO FAIL on the current (unfixed) code — the
failure is the counterexample that confirms the defect.

The defect (bugfix 1.3, 1.4): ``verify_drive(mount, repair=True)`` repairs a
discrepant chart family by UNCONDITIONALLY forcing ``_legacy_tree_job`` with
``files_from=None`` (the ``rsync --size-only --delete`` whole-tree reconcile),
bypassing the SHA-256 manifest ``--files-from`` content-hash diff entirely. The
legacy size-only compare cannot see a chart tile whose content changed while its
byte size stayed the same, so a stale/wrong tile can survive a repair undetected.

Expected post-fix behavior (bugfix 2.1, 2.4):

  * The repair job(s) for a discrepant chart family are MANIFEST-DRIVEN
    (``files_from is not None``).
  * The transferred set equals ``diff(local_manifest, fresh_card_rescan)`` — the
    local manifest diffed against a FRESH content-hash rescan of the card's
    actual bytes (``_card_manifest_from_card``), NOT the possibly-stale card
    manifest file.
  * A tile edited to an IDENTICAL byte size appears in the change set (the
    manifest diff catches it by SHA-256; legacy size-only misses it).

Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.4

Follows the established patterns in test_run_sync_job.py / test_verify_drive.py /
test_verify_repair_legacy.py (real temp dirs, mocked mount preflight, conftest.py
isolates LOCAL_MANIFEST_DIR) and the Hypothesis FAT32-safe alphabet from
test_card_manifest_from_card_property.py.
"""

import os
import shutil

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Builder,
    Manifest_Store,
    ManifestEntry,
    diff_family,
)


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


# --- helpers ----------------------------------------------------------------


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


@pytest.fixture(autouse=True)
def _mount_ok(monkeypatch):
    """Make the preflight pass for temp dirs: pretend they are writable mounts."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)


@pytest.fixture(autouse=True)
def _quiet_label(monkeypatch):
    """Keep decide_streams' label cross-check from shelling out to diskutil."""
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Isolate the durable sync-state file under a temp dir."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(
        du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress")
    )
    return state_dir


@pytest.fixture
def local_manifest_dir(tmp_path, monkeypatch):
    """Isolate the local manifest store under a temp dir."""
    d = tmp_path / "manifests"
    d.mkdir()
    monkeypatch.setattr(mf, "LOCAL_MANIFEST_DIR", d)
    return d


def _fresh_card_rescan_scanned(mount_point):
    """The scanned family entries from a FRESH content-hash rescan of the card.

    Mirrors what the fix is specified to use as repair ground truth
    (``_card_manifest_from_card``): the card's actual bytes hashed now, not the
    stored (possibly stale) card manifest file.
    """
    return du._card_manifest_from_card(mount_point).families["scanned"].entries


# --- Test 1: repair job is manifest-driven (files_from is not None) ----------


def test_repair_job_is_manifest_driven_against_fresh_card_rescan(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """A repaired scanned family runs a MANIFEST job diffed vs a fresh card rescan.

    EXPECTED POST-FIX: the scanned repair job carries ``files_from is not None``
    and its transferred set equals ``diff(local_manifest, fresh_card_rescan)``.

    FAILS ON UNFIXED CODE: today ``verify_drive(repair=True)`` forces
    ``_legacy_tree_job`` (``files_from=None``), so the captured repair job has
    ``files_from is None`` and no per-file change set.

    Requirements: 1.3, 2.1, 2.4
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    # Local SSD scanned payload: two tiles + the marker.
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbbbbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    # Keep plates/nav clean so only scanned is discrepant.
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    _write(str(local / "NAV.DB"), b"nav-content")

    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # Card scanned payload disagrees with local: LO/a.png matches, SEC/b.png is
    # missing on the card entirely. A content-correct repair must transfer
    # exactly SEC/b.png (added) and leave LO/a.png (unchanged by hash).
    _write(str(drive / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")

    # Save a LOCAL scanned manifest that matches the SSD payload exactly (the
    # fix diffs this against the fresh card rescan).
    local_fam = Manifest_Builder.build(
        "scanned", str(local / "ChartData"), cycle="2611"
    )
    # Apply the scanned Plates/ prune the manifest machinery uses.
    local_fam.entries = {
        rel: e
        for rel, e in local_fam.entries.items()
        if not (rel == "Plates" or rel.startswith("Plates/"))
    }
    Manifest_Store.save_local(local_fam)

    # Compute the EXPECTED change set the fix should emit: local vs a fresh
    # content-hash rescan of the card's actual bytes.
    rescan_entries = _fresh_card_rescan_scanned(str(drive))
    expected = diff_family(local_fam, rescan_entries)
    # Sanity: the card is genuinely discrepant (SEC/b.png must be transferred).
    assert os.path.join("SEC", "b.png").replace(os.sep, "/") in expected.files_from

    # Force the scanned family to look discrepant so repair fires for it only.
    def _fake_verify_family(job, mount_point, deep=False):
        if job.name == "scanned":
            return {
                "missing": [os.path.join("SEC", "b.png")],
                "extra": [],
                "size_mismatch": [],
            }
        return {"missing": [], "extra": [], "size_mismatch": []}

    monkeypatch.setattr(du, "verify_family", _fake_verify_family)

    # Spy on run_sync_job: capture the repair job, do not actually transfer.
    captured = {}

    def _spy_run_sync_job(job, mount_point, **kwargs):
        captured[job.name] = job
        return du.JobResult(name=job.name, status="updated", verified=True)

    monkeypatch.setattr(du, "run_sync_job", _spy_run_sync_job)

    du.verify_drive(str(drive), repair=True)

    assert "scanned" in captured, "scanned family should have been repaired"
    repair_job = captured["scanned"]

    # EXPECTED POST-FIX (fails today: repair forces files_from=None legacy job).
    assert repair_job.files_from is not None, (
        "repair must be manifest-driven (files_from is not None), not the "
        "legacy --size-only whole-tree job"
    )
    # The transferred set equals diff(local_manifest, fresh_card_rescan).
    assert set(repair_job.files_from) == set(expected.files_from)
    assert set(repair_job.removed or []) == set(expected.removed)


# --- Test 2: same-size content change appears in the change set --------------


def test_repair_detects_same_size_content_change(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """A tile edited to an IDENTICAL byte size is in the repair change set.

    EXPECTED POST-FIX: the manifest diff catches a same-byte-size content change
    by SHA-256, so the edited relpath is in the repair job's ``files_from``.

    FAILS ON UNFIXED CODE: repair forces a legacy ``--size-only`` job
    (``files_from=None``), which neither lists the tile nor would transfer it
    (equal size => skipped) — the silent-wrong-tile hazard (bugfix 1.4).

    Requirements: 1.4, 2.1, 2.4
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    # Local tile content (4 bytes).
    _write(str(local / "ChartData" / "LO" / "tile.png"), b"GOOD")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    _write(str(local / "NAV.DB"), b"nav-content")

    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # Card has the SAME relpath with DIFFERENT content of the SAME byte size.
    _write(str(drive / "ChartData" / "LO" / "tile.png"), b"BAD!")  # 4 bytes, != GOOD
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")

    changed_rel = "LO/tile.png"
    assert os.path.getsize(str(local / "ChartData" / "LO" / "tile.png")) == os.path.getsize(
        str(drive / "ChartData" / "LO" / "tile.png")
    )

    # Local manifest matches the SSD payload (records the GOOD hash).
    local_fam = Manifest_Builder.build(
        "scanned", str(local / "ChartData"), cycle="2611"
    )
    local_fam.entries = {
        rel: e
        for rel, e in local_fam.entries.items()
        if not (rel == "Plates" or rel.startswith("Plates/"))
    }
    Manifest_Store.save_local(local_fam)

    # The fresh card rescan sees the BAD bytes; local vs rescan must flag the
    # same-size change.
    rescan_entries = _fresh_card_rescan_scanned(str(drive))
    expected = diff_family(local_fam, rescan_entries)
    assert changed_rel in expected.files_from  # SHA-256 catches the same-size change

    def _fake_verify_family(job, mount_point, deep=False):
        # Count+size verify would NOT see this (same size); simulate a deep
        # discrepancy so the repair path fires for scanned.
        if job.name == "scanned":
            return {"missing": [], "extra": [], "size_mismatch": [changed_rel]}
        return {"missing": [], "extra": [], "size_mismatch": []}

    monkeypatch.setattr(du, "verify_family", _fake_verify_family)

    captured = {}

    def _spy_run_sync_job(job, mount_point, **kwargs):
        captured[job.name] = job
        return du.JobResult(name=job.name, status="updated", verified=True)

    monkeypatch.setattr(du, "run_sync_job", _spy_run_sync_job)

    du.verify_drive(str(drive), repair=True)

    assert "scanned" in captured
    repair_job = captured["scanned"]

    # EXPECTED POST-FIX (fails today: files_from is None under the legacy job).
    assert repair_job.files_from is not None, (
        "repair must be manifest-driven so a same-size content change is "
        "transferred, not silently skipped by --size-only"
    )
    assert changed_rel in repair_job.files_from, (
        "a same-byte-size content change must appear in the repair change set"
    )


# --- Property: repair is ALWAYS manifest-driven, never legacy ----------------

# FAT32-safe uppercase-alphanumeric path component (established pattern in
# test_card_manifest_from_card_property.py). Markers/metadata are excluded so
# every generated file is retained by the manifest builder.
_MARKER_BASENAMES = frozenset(
    {"ScannedCharts.sqlite", "Plates.sqlite", ".DS_Store"}
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


_RELSET = st.lists(_RELPATH, min_size=1, max_size=6, unique=True).map(
    _no_ancestor_collisions
)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rels=_RELSET)
def test_repair_never_emits_legacy_size_only_job(rels):
    """For any discrepant scanned family, repair emits a manifest job.

    EXPECTED POST-FIX: across arbitrary discrepant chart trees the scanned
    repair job always carries ``files_from is not None`` (manifest-driven).

    FAILS ON UNFIXED CODE: the forced ``_legacy_tree_job`` yields
    ``files_from is None`` regardless of input.

    Requirements: 1.3, 2.1
    """
    import tempfile
    from unittest import mock

    rels = [r for r in rels if r]
    if not rels:
        return

    root = tempfile.mkdtemp(prefix="efis_mocs_")
    manifests = tempfile.mkdtemp(prefix="efis_mocs_mf_")
    try:
        local = os.path.join(root, "image")
        drive = os.path.join(root, "drive")

        # Local scanned payload for each generated relpath + the marker.
        for rel in rels:
            _write(os.path.join(local, "ChartData", *rel.split("/")), b"LOCAL-" + rel.encode())
        _write(os.path.join(local, "ChartData", "ScannedCharts.sqlite"), b"scanned-db")
        _write(os.path.join(local, "ChartData", "Plates", "p1.pdf"), b"plate")
        _write(os.path.join(local, "ChartData", "Plates", "Plates.sqlite"), b"plates-db")
        _write(os.path.join(local, "NAV.DB"), b"nav")
        os.makedirs(drive, exist_ok=True)
        # Card is empty for scanned (every local relpath must be transferred).
        _write(os.path.join(drive, "ChartData", "ScannedCharts.sqlite"), b"scanned-db")

        with mock.patch.object(mf, "LOCAL_MANIFEST_DIR", __import__("pathlib").Path(manifests)), \
             mock.patch.object(du, "load_config", lambda: {"usb_image_path": local}), \
             mock.patch.object(du.os.path, "ismount", lambda p: True), \
             mock.patch.object(du, "_volume_name", lambda mp: None):

            local_fam = Manifest_Builder.build("scanned", os.path.join(local, "ChartData"), cycle="2611")
            local_fam.entries = {
                r: e
                for r, e in local_fam.entries.items()
                if not (r == "Plates" or r.startswith("Plates/"))
            }
            Manifest_Store.save_local(local_fam)

            def _fake_verify_family(job, mount_point, deep=False):
                if job.name == "scanned":
                    return {"missing": list(rels), "extra": [], "size_mismatch": []}
                return {"missing": [], "extra": [], "size_mismatch": []}

            captured = {}

            def _spy_run_sync_job(job, mount_point, **kwargs):
                captured[job.name] = job
                return du.JobResult(name=job.name, status="updated", verified=True)

            with mock.patch.object(du, "verify_family", _fake_verify_family), \
                 mock.patch.object(du, "run_sync_job", _spy_run_sync_job):
                du.verify_drive(drive, repair=True, families=["scanned"])

            assert "scanned" in captured
            assert captured["scanned"].files_from is not None
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(manifests, ignore_errors=True)
