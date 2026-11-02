# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Error-path tests for manifest-only-chart-sync (task 7, design "Error handling").

The manifest-only retirement makes the sync STRICTER: when it cannot proceed
content-correctly it fails and leaves the family pending — it never silently
falls back to the retired ``--size-only`` transfer (that fallback no longer
exists). These tests pin the two failure modes the design calls out:

  * Local manifest build failure (SSD unreadable): the local manifest must be
    (re)built from the SSD source before a diff can run. If the SSD read raises,
    the repair for that chart family is aborted — the error is surfaced in the
    ``verify_drive`` result, the family is left pending (``complete_family`` is
    never called, so the interrupted-sync marker stays intact), and NO size-only
    transfer occurs.

  * Card rescan failure during repair: the repair ground truth is a FRESH
    content-hash rescan of the card. If that rescan raises, the repair is
    aborted for every discrepant chart family — the error is surfaced, the
    families are left pending (marker intact), and NO size-only transfer occurs.

In both cases "NO size-only transfer" is enforced structurally: the size-only
branch was deleted from ``sync_payload`` (task 5) and a ``files_from=None`` tree
job now raises. We assert both the behavioral contract (error surfaced + family
pending) AND that ``sync_payload`` is never reached with ``files_from=None`` (in
fact never reached at all on these abort paths) and rsync is never invoked.

Requirements: 2.1, 2.4

Follows the established patterns in test_manifest_only_chart_sync.py /
test_manifest_only_preservation.py / test_verify_drive.py (real temp dirs,
mocked mount preflight, conftest.py isolates LOCAL_MANIFEST_DIR, isolated
SYNC_STATE).
"""

import os
import shutil

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.manifest import Manifest_Builder, Manifest_Store


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


# --- helpers / fixtures -----------------------------------------------------


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


@pytest.fixture(autouse=True)
def _no_rsync(monkeypatch):
    """Tripwire: fail loudly if ANY rsync subprocess is spawned on an abort path.

    A size-only (or any) transfer on these failure paths is a contract
    violation. ``sync_payload`` is the only place that shells out to rsync, so
    making ``subprocess.Popen`` raise if invoked proves no transfer occurred.
    """
    import subprocess

    real_popen = subprocess.Popen

    def _guard(cmd, *a, **k):
        argv0 = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        if isinstance(argv0, str) and os.path.basename(argv0) == "rsync":
            raise AssertionError(
                "rsync must NOT be invoked on a manifest-build / card-rescan "
                "failure path (no size-only fallback)"
            )
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", _guard)


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


def _seed_discrepant_scanned(tmp_path, monkeypatch):
    """Seed a local image + a card drive whose scanned family is discrepant.

    Returns ``(local, drive)`` paths. The card is missing a scanned tile so the
    (forced) verify sees a discrepancy and the repair path fires for scanned.
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbbbbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    _write(str(local / "NAV.DB"), b"nav-content")

    drive.mkdir()
    # Card has only LO/a.png (SEC/b.png missing) -> scanned is discrepant.
    _write(str(drive / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})
    return local, drive


def _force_scanned_discrepant(monkeypatch):
    """Make verify_family report scanned discrepant, all other families clean."""

    def _fake_verify_family(job, mount_point, deep=False):
        if job.name == "scanned":
            return {
                "missing": [os.path.join("SEC", "b.png")],
                "extra": [],
                "size_mismatch": [],
            }
        return {"missing": [], "extra": [], "size_mismatch": []}

    monkeypatch.setattr(du, "verify_family", _fake_verify_family)


def _no_sync_payload_tripwire(monkeypatch):
    """Tripwire on sync_payload: it must not run on these abort paths.

    Returns a dict recording any call (with the job's files_from) so a test can
    additionally assert it was never called with files_from=None.
    """
    seen = {"calls": []}
    real = du.sync_payload

    def _spy(job, *a, **k):
        seen["calls"].append(job.files_from)
        return real(job, *a, **k)

    monkeypatch.setattr(du, "sync_payload", _spy)
    return seen


# ---------------------------------------------------------------------------
# Path 1 — local manifest build failure (SSD unreadable) during repair
# ---------------------------------------------------------------------------


def test_local_manifest_build_failure_leaves_family_pending_no_size_only(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """SSD unreadable at manifest-build time: repair aborts, family stays pending.

    The local manifest is absent, so the repair must (re)build it from the SSD
    source. We make that build raise OSError (SSD unreadable). The contract
    (design "Error handling"):

      * the error is SURFACED in ``verify_drive(...)["errors"]`` (not a crash),
      * the scanned family is LEFT PENDING — ``complete_family`` is never called,
        so the interrupted-sync marker recorded by ``begin_family`` stays intact,
      * NO size-only transfer occurs — ``sync_payload`` is never reached and
        rsync is never invoked (the ``_no_rsync`` tripwire would fire).

    Requirements: 2.1, 2.4
    """
    local, drive = _seed_discrepant_scanned(tmp_path, monkeypatch)
    _force_scanned_discrepant(monkeypatch)
    payload = _no_sync_payload_tripwire(monkeypatch)

    # Pre-record an interrupted-sync marker for scanned (as a real begin_family
    # would before a payload copy) so we can assert it is LEFT intact.
    drive_id = du.resolve_sync_state_key(str(drive)) or os.path.realpath(str(drive))
    du.begin_family(drive_id, "scanned", mount=str(drive))
    assert "scanned" in du.pending_families(drive_id)

    # No local manifest exists -> _chart_family_job rebuilds it via
    # _build_local_manifest. Make that build fail as if the SSD were unreadable.
    def _boom(name, local_root):
        raise OSError("SSD unreadable")

    monkeypatch.setattr(du, "_build_local_manifest", _boom)

    # Must NOT raise: the failure is caught and surfaced, not propagated.
    report = du.verify_drive(str(drive), families=["scanned"], repair=True)

    # Error surfaced in the report (not a crash). The build failure is detected
    # while building the family jobs (which rebuilds the missing local manifest
    # from the SSD), so it aborts the verify/repair for the requested families.
    assert report["errors"], "manifest build failure must be surfaced as an error"
    assert any("SSD unreadable" in e for e in report["errors"])
    # scanned did not converge: reported not-clean / pending.
    assert report["clean"] is False
    assert "scanned" not in report["repaired"]

    # Family left pending: the interrupted-sync marker is intact.
    assert "scanned" in du.pending_families(drive_id), (
        "a manifest-build failure must leave the family pending (marker intact)"
    )

    # No size-only transfer: sync_payload was never reached at all, so in
    # particular never with files_from=None.
    assert payload["calls"] == []
    assert None not in payload["calls"]

    # The drive was not mutated by any (non-existent) transfer.
    assert not (drive / "ChartData" / "SEC" / "b.png").exists()


# ---------------------------------------------------------------------------
# Path 2 — card rescan failure during repair
# ---------------------------------------------------------------------------


def test_card_rescan_failure_leaves_family_pending_no_size_only(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """Card unreadable at rescan time: repair aborts, family stays pending.

    Repair builds its ground truth by freshly content-hashing the card
    (``_card_manifest_from_card``). We make that rescan raise OSError (card
    unreadable). The contract (design "Error handling"):

      * the error is SURFACED in ``verify_drive(...)["errors"]`` (not a crash),
      * the scanned family is LEFT PENDING — the interrupted-sync marker stays
        intact (``complete_family`` is never called),
      * NO size-only transfer occurs — ``sync_payload`` is never reached and
        rsync is never invoked.

    Requirements: 2.1, 2.4
    """
    local, drive = _seed_discrepant_scanned(tmp_path, monkeypatch)
    _force_scanned_discrepant(monkeypatch)
    payload = _no_sync_payload_tripwire(monkeypatch)

    # A valid local manifest exists (so the only failure is the card rescan).
    Manifest_Store.save_local(_build_scanned_local_manifest(str(local)))

    drive_id = du.resolve_sync_state_key(str(drive)) or os.path.realpath(str(drive))
    du.begin_family(drive_id, "scanned", mount=str(drive))
    assert "scanned" in du.pending_families(drive_id)

    # The repair-time fresh card rescan fails as if the card were unreadable.
    def _boom(mount_point, progress_callback=None):
        raise OSError("card unreadable")

    monkeypatch.setattr(du, "_card_manifest_from_card", _boom)

    report = du.verify_drive(str(drive), families=["scanned"], repair=True)

    assert report["errors"], "card rescan failure must be surfaced as an error"
    assert any("card unreadable" in e for e in report["errors"])

    # Family left pending: marker intact.
    assert "scanned" in du.pending_families(drive_id), (
        "a card-rescan failure must leave the family pending (marker intact)"
    )

    # No size-only transfer: sync_payload never reached, never files_from=None.
    assert payload["calls"] == []
    assert None not in payload["calls"]
    assert not (drive / "ChartData" / "SEC" / "b.png").exists()


def test_card_rescan_failure_does_not_falsely_mark_family_repaired(
    tmp_path, monkeypatch, state_paths, local_manifest_dir
):
    """A card rescan failure must not report the family as 'repaired'.

    The repaired list is the record of families actually re-run. When the rescan
    fails the chart family is NOT re-run (no transfer), so it must not appear in
    ``repaired`` — otherwise a caller would believe the drive was converged.

    Requirements: 2.4
    """
    local, drive = _seed_discrepant_scanned(tmp_path, monkeypatch)
    _force_scanned_discrepant(monkeypatch)
    Manifest_Store.save_local(_build_scanned_local_manifest(str(local)))

    monkeypatch.setattr(
        du,
        "_card_manifest_from_card",
        lambda mount_point, progress_callback=None: (_ for _ in ()).throw(
            OSError("card unreadable")
        ),
    )

    report = du.verify_drive(str(drive), families=["scanned"], repair=True)

    assert "scanned" not in report["repaired"]
    # The family is still reported discrepant (not clean) so the caller knows
    # the drive did not converge.
    assert report["clean"] is False


# ---------------------------------------------------------------------------
# Regression guard — the retired size-only path is truly gone
# ---------------------------------------------------------------------------


def test_files_from_none_tree_job_raises_never_size_only(tmp_path):
    """A tree job with files_from=None is rejected (no size-only fallback).

    This is the structural guarantee behind "never falls back to size-only":
    even if a None-change-set job somehow reached ``sync_payload``, it raises
    rather than running the retired ``--size-only --delete`` transfer.

    Requirements: 2.1
    """
    job = du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(tmp_path / "src") + os.sep,
        payload_root_drive=str(tmp_path / "dst") + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES),
        files_from=None,
    )
    with pytest.raises(ValueError, match="files_from"):
        du.sync_payload(job)
