# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for verify_drive()'s repair legacy-full behavior (task 7.2).

Task 7.1 changed ``verify_drive``'s repair branch so a discrepant CHART family
is re-run via a LEGACY full job (``_legacy_tree_job``, ``files_from=None``)
rather than the diff-aware job ``build_jobs`` produced. The whole family tree is
reconciled via ``rsync --size-only --delete``, so orphans a stale/empty manifest
diff would miss are cleaned (design Section B.4; R2.3, R10.2).

Two complementary tests:

1. ``test_repair_uses_legacy_job_not_diff`` — a scenario where ``build_jobs``
   WOULD produce a diff job (valid card manifest with a ROLLED cycle + a saved
   local manifest). ``verify_family`` is forced to report a discrepancy and
   ``run_sync_job`` is spied on; the captured chart-family repair job's
   ``files_from`` is asserted ``None`` (legacy), NOT a diff job with
   ``files_from`` set.

2. ``test_repair_deletes_unrecorded_orphan`` — end-to-end against temp dirs with
   an orphan file on the card that is NOT in the (stale) manifest diff's removed
   set. After ``verify_drive(repair=True)`` the orphan is gone, proving
   ``--delete`` ran (the diff path's ``_prune_removed`` would not touch an
   unrecorded orphan).

Requirements: 2.3
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


# --- Test 1: repair spies on run_sync_job -> legacy job, not a diff job ------


def test_repair_uses_legacy_job_not_diff(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """A repaired scanned family runs a LEGACY job (files_from is None).

    ``build_jobs`` would hand verify_drive a DIFF job for scanned (rolled cycle
    + local manifest present). We force verify_family to report a discrepancy
    and spy on run_sync_job; the captured scanned repair job must be LEGACY
    (``files_from is None``), NOT the diff job build_jobs produced — so the whole
    family tree reconciles via ``--size-only --delete``.

    Requirements: 2.3
    """
    env = _make_diff_scenario(tmp_path, monkeypatch, local_manifest_dir)

    # Sanity: confirm build_jobs really produces a DIFF scanned job in this
    # scenario (files_from populated), so the test proves the repair branch
    # overrides it rather than there being no diff to begin with.
    built = {j.name: j for j in du.build_jobs(env["mount"], ["scanned"])}
    assert "scanned" in built
    assert built["scanned"].files_from is not None  # diff job, not legacy

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

    # The crux: the repaired scanned family ran a LEGACY full job, NOT the diff
    # job build_jobs produced. files_from None => --size-only --delete whole-tree
    # reconcile.
    assert repair_job.files_from is None
    assert repair_job.removed is None
    assert repair_job.kind == "tree"
    assert repair_job.name == "scanned"


# --- Test 2: end-to-end, repair deletes an orphan absent from the diff -------


def test_repair_deletes_unrecorded_orphan(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """Repair deletes a card orphan the stale manifest diff would miss.

    The card carries an orphan file that is NOT in the card manifest (so a diff
    path's removed-set would never prune it). Because repair forces a legacy
    full job (``--size-only --delete``), the orphan is deleted and the drive
    converges to the local image. This proves ``--delete`` ran — the diff path's
    ``_prune_removed`` would have left the unrecorded orphan in place.

    Requirements: 2.3
    """
    env = _make_diff_scenario(tmp_path, monkeypatch, local_manifest_dir)

    # Seed the drive's scanned payload by a direct copy (NOT via run_sync_job,
    # which would rewrite the card manifest and stamp the current cycle, turning
    # the gate into "skip-cycle"). Copying by hand leaves the STALE card manifest
    # (cycle "2610") intact, so build_jobs still resolves scanned to a DIFF job.
    _write(str(env["drive"] / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(env["drive"] / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    assert (env["drive"] / "ChartData" / "LO" / "a.png").exists()

    # Plant an orphan on the card that is NOT in the card manifest's entries and
    # NOT in any diff's removed set.
    orphan = env["drive"] / "ChartData" / "SEC" / "orphan.png"
    _write(str(orphan), b"orphan")
    assert orphan.exists()

    # Sanity: build_jobs still resolves scanned to a DIFF job (files_from set),
    # whose stale/empty removed-set does NOT name the unrecorded orphan — so
    # only the forced legacy repair can prune it.
    diff_job = du.build_jobs(env["mount"], ["scanned"])[0]
    assert diff_job.files_from is not None
    assert os.path.join("SEC", "orphan.png") not in (diff_job.removed or [])

    # Repair the scanned family. verify_family will see the orphan as "extra",
    # triggering repair; the forced legacy job then --delete's it.
    report = du.verify_drive(env["mount"], repair=True)

    assert report["errors"] == []
    assert "scanned" in report["repaired"]
    assert report["clean"] is True

    # The crux: the unrecorded orphan is gone (proving --delete ran), and the
    # tracked payload survived.
    assert not orphan.exists()
    assert (env["drive"] / "ChartData" / "LO" / "a.png").read_bytes() == b"aaaa"
