# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preservation tests for manifest-only-chart-sync (task 6, bugfix 3.1-3.5).

The size-only TRANSFER mechanism was retired, but the surrounding invariants
must be unchanged. Each test here pins one preserved behavior from the bugfix's
"Unchanged Behavior (Regression Prevention)" section:

  * R2.5 — ``_prune_removed`` still unlinks every relpath in ``diff.removed``
    (and prunes the directories they leave empty).
  * R3.1 — on a clean chart-family converge, ``_run_tree_job`` writes the commit
    marker, THEN the card manifest, THEN calls ``complete_family``, in that
    exact order.
  * R3.4 — the nav family still syncs per-file by size + SHA-256 with no
    manifest and no cycle gate.
  * R3.5 — the cycle gate still permits a skip: when the local cycle equals the
    card's recorded cycle, ``_chart_family_job`` returns ``None`` (the transfer
    job is omitted). The retirement is of the size-only TRANSFER, not the gate.
  * R5 / design Section D — a clean, already-current insert performs NO card
    rescan: ``_card_manifest_from_card`` is NOT called on the normal
    (non-repair) insert path.

Follows the established patterns in test_run_sync_job.py /
test_verify_repair_legacy.py / test_manifest_only_chart_sync.py (real temp dirs,
mocked mount preflight, conftest.py isolates LOCAL_MANIFEST_DIR).

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 5
"""

import os
import shutil

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Builder,
    Manifest_Store,
)


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


# --- helpers / fixtures -----------------------------------------------------


def _write(path, content=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
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


def _build_scanned_local_manifest(local_root, cycle="2611"):
    """Build + prune a scanned FamilyManifest from the SSD ChartData/ tree."""
    fam = Manifest_Builder.build(
        "scanned", os.path.join(local_root, "ChartData"), cycle=cycle
    )
    fam.entries = {
        rel: e
        for rel, e in fam.entries.items()
        if not (rel == "Plates" or rel.startswith("Plates/"))
    }
    return fam


# ---------------------------------------------------------------------------
# R2.5 — _prune_removed still prunes diff.removed
# ---------------------------------------------------------------------------


def test_prune_removed_unlinks_removed_set_and_prunes_empty_dirs(tmp_path):
    """R2.5: every relpath in the removed set is unlinked; empty dirs pruned.

    This is the explicit removal pass the manifest path relies on in place of
    ``--delete`` (which cannot prune under ``--files-from``).

    Validates: Requirements 2.5
    """
    drive_root = tmp_path / "ChartData"
    # Files to be pruned, plus one survivor that must stay.
    _write(str(drive_root / "SEC" / "gone.png"), b"stale")
    _write(str(drive_root / "OLD" / "old.png"), b"stale")
    _write(str(drive_root / "OLD" / "nested" / "deep.png"), b"stale")
    _write(str(drive_root / "SEC" / "keep.png"), b"keep")

    removed = ["SEC/gone.png", "OLD/old.png", "OLD/nested/deep.png"]
    du._prune_removed(str(drive_root), removed)

    # Every removed relpath is gone.
    assert not (drive_root / "SEC" / "gone.png").exists()
    assert not (drive_root / "OLD" / "old.png").exists()
    assert not (drive_root / "OLD" / "nested" / "deep.png").exists()
    # The now-empty OLD/ subtree is pruned bottom-up.
    assert not (drive_root / "OLD").exists()
    # SEC/ still holds keep.png, so it (and the survivor) remain.
    assert (drive_root / "SEC" / "keep.png").read_bytes() == b"keep"
    # The family root itself is never removed.
    assert drive_root.is_dir()


def test_prune_removed_is_invoked_on_a_clean_manifest_transfer(tmp_path):
    """R2.5: sync_payload runs _prune_removed(job.removed) after a clean rsync.

    End-to-end guard that the manifest path still wires the explicit prune: a
    file listed only in ``removed`` is gone from the drive after sync_payload.

    Validates: Requirements 2.5
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(str(src / "a.png"), b"aaa")
    _write(str(dst / "a.png"), b"aaa")
    _write(str(dst / "stale.png"), b"stale")  # only in removed

    job = du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src) + os.sep,
        payload_root_drive=str(dst) + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES),
        files_from=[],
        removed=["stale.png"],
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert not (dst / "stale.png").exists()
    assert (dst / "a.png").read_bytes() == b"aaa"


# ---------------------------------------------------------------------------
# R3.1 — commit-marker -> card-manifest -> complete_family ordering
# ---------------------------------------------------------------------------


def test_clean_converge_order_marker_then_card_manifest_then_complete(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """R3.1: a clean chart converge writes marker, THEN card manifest, THEN
    complete_family, in that order.

    The ordering is the atomic-commit invariant: the card manifest (the
    authoritative per-cycle record) is only written after the commit marker is
    on the card, and the interrupted-sync marker is only cleared
    (complete_family) after both. We record the call order of the three steps on
    a real clean converge via spies.

    Validates: Requirements 3.1
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # A matching local manifest so _chart_family_job diffs cleanly (card absent
    # -> full write of LO/a.png).
    Manifest_Store.save_local(_build_scanned_local_manifest(str(local)))

    events = []

    real_copy = du._copy_and_fsync

    def _spy_copy(src, dst):
        if str(dst).endswith("ScannedCharts.sqlite"):
            events.append("marker")
        return real_copy(src, dst)

    real_write_card = du._write_card_manifest_for_family

    def _spy_write_card(mount_point, family):
        events.append("card_manifest")
        return real_write_card(mount_point, family)

    real_complete = du.complete_family

    def _spy_complete(drive_id, family):
        events.append("complete_family")
        return real_complete(drive_id, family)

    monkeypatch.setattr(du, "_copy_and_fsync", _spy_copy)
    monkeypatch.setattr(du, "_write_card_manifest_for_family", _spy_write_card)
    monkeypatch.setattr(du, "complete_family", _spy_complete)

    job = du.build_jobs(str(drive), ["scanned"])[0]
    result = du.run_sync_job(job, str(drive))

    assert result.status in ("updated", "current")
    assert result.verified is True
    # The three commit steps fired in the preserved order.
    assert events == ["marker", "card_manifest", "complete_family"], events


# ---------------------------------------------------------------------------
# R3.4 — nav sync behavior unchanged (no manifest, per-file checksum)
# ---------------------------------------------------------------------------


def test_nav_sync_is_per_file_checksum_with_no_manifest(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """R3.4: the nav family copies NAV.DB / NAV-proc.DB by checksum, no manifest.

    The nav job carries no files_from / removed and no cycle gate; it is a
    ``kind == "files"`` job that copies each present NAV file and verifies by
    size + SHA-256. A same-size content change on the drive is overwritten
    (checksum-driven), and a second run is a no-op.

    Validates: Requirements 3.4
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    _write(str(local / "NAV.DB"), b"NAVDATA-1")
    _write(str(local / "NAV-proc.DB"), b"PROCDATA")
    drive.mkdir()
    # Drive has a SAME-size but different NAV.DB -> must be overwritten.
    _write(str(drive / "NAV.DB"), b"NAVDATA-9")  # same length, different content
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    nav = du.build_jobs(str(drive), ["nav"])[0]
    # The nav job is manifest-independent.
    assert nav.kind == "files"
    assert nav.files == list(du.NAV_FILES)
    assert nav.files_from is None
    assert nav.removed is None

    result = du.run_sync_job(nav, str(drive))
    assert result.status == "updated"
    assert result.verified is True
    # The same-size content change was corrected (per-file checksum, not size).
    assert (drive / "NAV.DB").read_bytes() == b"NAVDATA-1"
    assert (drive / "NAV-proc.DB").read_bytes() == b"PROCDATA"

    # Second run: everything matches by checksum -> no files copied.
    second = du.run_sync_job(du.build_jobs(str(drive), ["nav"])[0], str(drive))
    assert second.status == "current"
    assert second.files_updated == 0


# ---------------------------------------------------------------------------
# R3.5 — cycle gate still permits a skip when local cycle == card cycle
# ---------------------------------------------------------------------------


def test_cycle_gate_skips_when_local_cycle_equals_card_cycle(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """R3.5: equal local/card cycle -> _chart_family_job returns None (skip).

    The size-only transfer was retired, but the cycle gate is untouched: when
    the local cycle equals the card's recorded cycle, the family is known
    unchanged by construction and the transfer job is omitted entirely.

    Validates: Requirements 3.5
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # Local scanned manifest stamped cycle "2611".
    Manifest_Store.save_local(_build_scanned_local_manifest(str(local), cycle="2611"))

    # Card manifest records the SAME scanned cycle "2611" -> skip-cycle.
    card = CardManifest(
        charts_cycle="2611",
        plates_cycle=None,
        families={
            "scanned": FamilyManifest(family="scanned", cycle="2611", entries={})
        },
    )

    job = du._chart_family_job("scanned", str(drive), str(local), card)
    # Equal cycles -> the transfer job is omitted (None), not a size-only job.
    assert job is None

    # And build_jobs omits the scanned family entirely for this card.
    Manifest_Store.save_card(str(drive), card)
    names = [j.name for j in du.build_jobs(str(drive), ["scanned"])]
    assert "scanned" not in names


# ---------------------------------------------------------------------------
# R5 / design Section D — clean current insert performs NO card rescan
# ---------------------------------------------------------------------------


def test_clean_insert_performs_no_card_rescan(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """R5: the normal (non-repair) insert path never calls _card_manifest_from_card.

    The card rescan is the accepted expensive step, confined to the
    interrupted-sync repair path (design Section D). A normal insert sync —
    build_jobs + run_sync_job — must never trigger it, so an already-current
    drive adds no hashing cost. We spy on _card_manifest_from_card and assert it
    is not called across a full converge + a second (no-op) converge.

    Validates: Requirements 5
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"nav-proc")
    drive.mkdir()
    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # Local manifests so chart families diff cleanly (card absent -> full write
    # on the first converge, then a no-op on the second).
    Manifest_Store.save_local(_build_scanned_local_manifest(str(local)))
    plates_fam = Manifest_Builder.build(
        "plates", os.path.join(str(local), "ChartData", "Plates"), cycle="2611"
    )
    Manifest_Store.save_local(plates_fam)

    calls = {"rescan": 0}
    real_rescan = du._card_manifest_from_card

    def _spy_rescan(mount_point, progress_callback=None):
        calls["rescan"] += 1
        return real_rescan(mount_point, progress_callback=progress_callback)

    monkeypatch.setattr(du, "_card_manifest_from_card", _spy_rescan)

    # First converge (full write) via the normal insert path.
    for name in ("scanned", "plates", "nav"):
        jobs = du.build_jobs(str(drive), [name])
        if jobs:  # skip-cycle may omit a chart family (not here, card absent)
            du.run_sync_job(jobs[0], str(drive))

    # Second converge: everything is now current -> no-op manifest jobs.
    for name in ("scanned", "plates", "nav"):
        jobs = du.build_jobs(str(drive), [name])
        if jobs:
            du.run_sync_job(jobs[0], str(drive))

    # The expensive card rescan was NEVER triggered on the normal insert path.
    assert calls["rescan"] == 0
