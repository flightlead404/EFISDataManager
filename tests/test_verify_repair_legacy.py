# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for verify_drive()'s repair behavior (manifest-only-chart-sync).

manifest-only-chart-sync R2.4 RETIRED the forced legacy-full repair job. Before
the fix ``verify_drive``'s repair branch re-ran a discrepant CHART family via
``_legacy_tree_job`` (``--size-only --delete``, ``files_from=None``), which
compared by byte size only and silently skipped a same-size content change — the
flight-safety gap this spec closes. The repair branch now diffs the local
manifest against a FRESH content-hash rescan of the card
(``_card_manifest_from_card``) and emits a ``--files-from`` manifest job.

Two complementary tests:

1. ``test_repair_uses_manifest_job_not_legacy`` — a scenario where ``build_jobs``
   produces a diff job (valid card manifest with a ROLLED cycle + a saved local
   manifest). ``verify_family`` is forced to report a discrepancy and
   ``run_sync_job`` is spied on; the captured chart-family repair job's
   ``files_from`` is asserted NOT ``None`` (a manifest change set), never the
   retired legacy size-only job.

2. ``test_repair_deletes_unrecorded_orphan`` — end-to-end against temp dirs with
   an orphan file on the card that is absent from the local image. The fresh
   card rescan records the orphan; diffing local vs the rescan places it in the
   change set's ``removed``, so ``_prune_removed`` unlinks it. After
   ``verify_drive(repair=True)`` the orphan is gone — the manifest path converges
   the card to the local image without any ``--delete``.

Requirements: 2.4
"""

import os
import shutil
from unittest import mock

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Store,
    ManifestEntry,
)


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


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


def _make_diff_scenario(tmp_path, monkeypatch, local_manifest_dir):
    """Set up a scenario where build_jobs WOULD produce a scanned DIFF job.

    Writes a local image + a drive with a valid card manifest whose scanned
    cycle has ROLLED relative to a saved local manifest, so ``decide_streams``
    returns ``"diff"`` (never ``"skip-cycle"``) and ``build_jobs`` populates the
    scanned job's ``files_from`` / ``removed``. Returns the env dict.
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    # scanned payload + marker
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    # plates payload + marker (kept clean so only scanned is discrepant)
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    # nav
    _write(str(local / "NAV.DB"), b"nav-content")

    drive.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # A saved LOCAL scanned manifest at cycle "2611".
    local_fam = FamilyManifest(
        family="scanned",
        cycle="2611",
        entries={
            "LO/a.png": ManifestEntry(relpath="LO/a.png", sha256="0" * 64, size=4),
        },
        built="2026-10-24T18:03:11-07:00",
    )
    Manifest_Store.save_local(local_fam)

    # A CARD manifest recording a DIFFERENT (older) scanned cycle "2610" with a
    # single entry. Because the cycles differ, decide_streams -> "diff", so
    # build_jobs populates a diff job (files_from set) absent the 7.1 fix.
    card_fam = FamilyManifest(
        family="scanned",
        cycle="2610",
        entries={
            "LO/a.png": ManifestEntry(relpath="LO/a.png", sha256="1" * 64, size=99),
        },
        built="2026-10-20T10:00:00-07:00",
    )
    card = CardManifest(
        charts_cycle="2610",
        plates_cycle=None,
        families={"scanned": card_fam},
    )
    Manifest_Store.save_card(str(drive), card)

    return {"local": local, "drive": drive, "mount": str(drive)}


# --- Test 1: repair spies on run_sync_job -> manifest job, never legacy ------


def test_repair_uses_manifest_job_not_legacy(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """A repaired scanned family runs a MANIFEST job (files_from is NOT None).

    manifest-only-chart-sync R2.4 retired the forced ``_legacy_tree_job``
    (``--size-only --delete``, ``files_from=None``) repair path: it compared by
    byte size only and silently skipped a same-size content change — the
    flight-safety gap this spec closes. The repair branch now diffs the local
    manifest against a FRESH content-hash rescan of the card
    (``_card_manifest_from_card``) and emits a ``--files-from`` manifest job.

    We force verify_family to report a scanned discrepancy and spy on
    run_sync_job; the captured scanned repair job must carry a manifest change
    set (``files_from is not None``), NOT a legacy size-only job.

    Requirements: 2.4
    """
    env = _make_diff_scenario(tmp_path, monkeypatch, local_manifest_dir)

    # Seed the card's scanned payload so the fresh card rescan
    # (_card_manifest_from_card) has real bytes to hash — otherwise the rescan
    # would see an empty card family. The on-card bytes differ from the local
    # image, so the repair diff yields a non-empty change set.
    _write(str(env["drive"] / "ChartData" / "LO" / "a.png"), b"STALE")

    # Force the scanned family to look discrepant so repair fires for it, and
    # keep plates/nav clean so only scanned is repaired.
    def _fake_verify_family(job, mount_point, deep=False):
        if job.name == "scanned":
            return {"missing": ["LO/a.png"], "extra": [], "size_mismatch": []}
        return {"missing": [], "extra": [], "size_mismatch": []}

    monkeypatch.setattr(du, "verify_family", _fake_verify_family)

    # Spy on run_sync_job: capture the job it's handed, return a clean result,
    # and do NOT actually transfer anything.
    captured = {}

    def _spy_run_sync_job(job, mount_point, **kwargs):
        captured[job.name] = job
        return du.JobResult(name=job.name, status="updated", verified=True)

    monkeypatch.setattr(du, "run_sync_job", _spy_run_sync_job)

    report = du.verify_drive(env["mount"], repair=True)

    assert "scanned" in report["repaired"]
    assert "scanned" in captured
    repair_job = captured["scanned"]

    # The crux: the repaired scanned family ran a MANIFEST job, never a legacy
    # size-only one. files_from is an authoritative change set (a list), so the
    # transfer is content-hash driven and a same-size tile change is caught.
    assert repair_job.files_from is not None
    assert isinstance(repair_job.files_from, list)
    assert repair_job.removed is not None
    assert repair_job.kind == "tree"
    assert repair_job.name == "scanned"


# --- Test 2: end-to-end, repair deletes an orphan absent from the diff -------


def test_repair_deletes_unrecorded_orphan(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """Repair deletes a card orphan via the fresh-rescan manifest diff.

    The card carries an orphan file that is absent from the local image. The
    repair path hashes the card's ACTUAL bytes
    (``_card_manifest_from_card``), so the orphan appears in the fresh card
    rescan; diffing the local manifest against that rescan places the orphan in
    the change set's ``removed``, and ``_prune_removed`` unlinks it. After
    ``verify_drive(repair=True)`` the orphan is gone and the tracked payload
    survives — the manifest path converges the card to the local image with no
    ``--delete``.

    Requirements: 2.4
    """
    env = _make_diff_scenario(tmp_path, monkeypatch, local_manifest_dir)

    # Seed the drive's scanned payload by a direct copy (NOT via run_sync_job,
    # which would rewrite the card manifest and stamp the current cycle, turning
    # the gate into "skip-cycle"). Copying by hand leaves the STALE card manifest
    # (cycle "2610") intact, so build_jobs still resolves scanned to a DIFF job.
    _write(str(env["drive"] / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(env["drive"] / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    assert (env["drive"] / "ChartData" / "LO" / "a.png").exists()

    # Plant an orphan on the card that is NOT recorded in the STALE card
    # manifest's entries. The build_jobs (read-side) diff against the stale card
    # manifest therefore never names it in removed — only the repair path's
    # FRESH card rescan (which hashes the card's actual bytes) sees it and prunes
    # it.
    orphan = env["drive"] / "ChartData" / "SEC" / "orphan.png"
    _write(str(orphan), b"orphan")
    assert orphan.exists()

    # Sanity: the read-side build_jobs diff (against the stale card manifest)
    # resolves scanned to a DIFF job whose removed-set does NOT name the
    # unrecorded orphan — so only the repair path's fresh card rescan can prune
    # it.
    diff_job = du.build_jobs(env["mount"], ["scanned"])[0]
    assert diff_job.files_from is not None
    assert os.path.join("SEC", "orphan.png") not in (diff_job.removed or [])

    # Repair the scanned family. verify_family sees the orphan as "extra",
    # triggering repair; the repair job diffs the local manifest against the
    # fresh card rescan, placing the orphan in diff.removed, and _prune_removed
    # unlinks it.
    report = du.verify_drive(env["mount"], repair=True)

    assert report["errors"] == []
    assert "scanned" in report["repaired"]
    assert report["clean"] is True

    # The crux: the unrecorded orphan is gone (pruned via the fresh-rescan diff),
    # and the tracked payload survived.
    assert not orphan.exists()
    assert (env["drive"] / "ChartData" / "LO" / "a.png").read_bytes() == b"aaaa"
