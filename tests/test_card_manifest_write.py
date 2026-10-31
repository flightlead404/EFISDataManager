# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the card-manifest write after a verified family sync.

Covers chart-manifest-sync task 3.5 (design "Data Models: Card manifest" +
Error Handling row "Card manifest write fails (after data written)";
Requirements 4.4, 7.2, 7.3, 7.4, 7.5):

  - A converged tree job (scanned/plates) writes EFIS_MANIFEST.json at the card
    volume root: the family's entries equal the local manifest and the family's
    cycle is mirrored into the authoritative top-level slot (charts for scanned,
    plates for plates).
  - Ordering: the card manifest is written AFTER the commit marker and ONLY on a
    verified sync; a verify failure writes neither marker nor card manifest.
  - Card-manifest write FAILURE (after the data + marker are on the card) ->
    family treated as NOT completed: complete_family is not called (family stays
    pending), the job fails, the failure is logged at ERROR. The commit marker
    (data) is still on the card, so the next insert re-converges.
  - nav never writes a card manifest.

rsync runs against real temp dirs; the mount preflight is mocked. No physical
USB is needed.
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as m
from efis_data_manager.manifest import FamilyManifest, ManifestEntry, Manifest_Store


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _write(path, content=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(autouse=True)
def _mount_ok(monkeypatch):
    """Make preflight pass for temp dirs: pretend they are writable mounts."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)


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
    """Redirect the local manifest dir into tmp_path (never touch app-support)."""
    local = tmp_path / "manifests"
    monkeypatch.setattr(m, "LOCAL_MANIFEST_DIR", local)
    return local


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths, local_manifest_dir):
    """A local image with all three families and an empty drive to sync into.

    Local manifests for the two chart families are saved on the (isolated) SSD so
    the card-manifest write has an authoritative family + cycle to record.
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    base_mtime = 1_000_000

    # scanned family payload + marker
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(
        str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime
    )

    # plates family payload + marker
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(
        str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db", mtime=base_mtime
    )

    # nav family files
    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")

    drive.mkdir()
    # The plates job syncs into ChartData/Plates; rsync needs the ChartData
    # parent to exist on the (otherwise empty) drive, mirroring a prepared card.
    (drive / "ChartData").mkdir()

    # Local manifests the card-manifest write records (family entries + cycle).
    Manifest_Store.save_local(
        FamilyManifest(
            family="scanned",
            cycle="2611",
            entries={
                "LO/a.png": ManifestEntry("LO/a.png", "a" * 64, 4),
                "SEC/b.png": ManifestEntry("SEC/b.png", "b" * 64, 4),
            },
            built="2026-10-24T18:03:11-07:00",
        )
    )
    Manifest_Store.save_local(
        FamilyManifest(
            family="plates",
            cycle="2612",
            entries={"p1.pdf": ManifestEntry("p1.pdf", "c" * 64, 5)},
            built="2026-10-24T18:03:11-07:00",
        )
    )

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {
        "local": local,
        "drive": drive,
        "mount": str(drive),
        "base_mtime": base_mtime,
    }


def _job(env, name):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == name)


# --- happy path: converged tree job writes the card manifest ----------------


def test_scanned_sync_writes_card_manifest_with_charts_cycle(env):
    scanned = _job(env, "scanned")
    result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "updated"
    assert result.verified is True

    card = Manifest_Store.load_card(env["mount"])
    assert card is not None
    # The scanned family's cycle lands in the authoritative CHARTS slot.
    assert card.charts_cycle == "2611"
    assert card.plates_cycle is None
    # The card family equals the local manifest (entries + cycle).
    assert set(card.families["scanned"].entries) == {"LO/a.png", "SEC/b.png"}
    assert card.families["scanned"].cycle == "2611"
    assert card.written  # timestamp stamped


def test_plates_sync_writes_card_manifest_with_plates_cycle(env):
    plates = _job(env, "plates")
    result = du.run_sync_job(plates, env["mount"])

    assert result.status == "updated"

    card = Manifest_Store.load_card(env["mount"])
    assert card is not None
    # The plates family's cycle lands in the authoritative PLATES slot.
    assert card.plates_cycle == "2612"
    assert card.charts_cycle is None
    assert set(card.families["plates"].entries) == {"p1.pdf"}
    assert card.families["plates"].cycle == "2612"


def test_both_families_accumulate_in_one_card_manifest(env):
    du.run_sync_job(_job(env, "scanned"), env["mount"])
    du.run_sync_job(_job(env, "plates"), env["mount"])

    card = Manifest_Store.load_card(env["mount"])
    assert card is not None
    # Both families coexist; both top-level cycle slots are populated.
    assert card.charts_cycle == "2611"
    assert card.plates_cycle == "2612"
    assert set(card.families) == {"scanned", "plates"}


# --- ordering: marker THEN card manifest, only on a verified sync -----------


def test_card_manifest_not_written_on_verify_failure(env, monkeypatch, caplog):
    """A verify mismatch writes neither the commit marker nor the card
    manifest, and the family stays pending (Req 7.5 sequencing)."""
    scanned = _job(env, "scanned")
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {
            "missing": ["LO/a.png"],
            "extra": [],
            "size_mismatch": [],
        },
    )

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    # No card manifest written.
    assert Manifest_Store.load_card(env["mount"]) is None
    # No commit marker written.
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert "scanned" in du.pending_families(env["mount"])


def test_card_manifest_written_after_commit_marker(env, monkeypatch):
    """The commit marker must already be on the card when the card-manifest
    write runs (marker THEN manifest)."""
    scanned = _job(env, "scanned")
    marker_dst = env["drive"] / "ChartData" / "ScannedCharts.sqlite"

    real_save = Manifest_Store.save_card
    observed = {}

    def spy_save(mount_point, card):
        observed["marker_present_at_card_write"] = marker_dst.exists()
        return real_save(mount_point, card)

    monkeypatch.setattr(Manifest_Store, "save_card", staticmethod(spy_save))

    result = du.run_sync_job(scanned, env["mount"])
    assert result.status == "updated"
    assert observed["marker_present_at_card_write"] is True


# --- failure path: card-manifest write fails after data is written ----------


def test_card_manifest_write_failure_leaves_family_pending(env, monkeypatch, caplog):
    """The payload + commit marker are on the card, but the card-manifest write
    fails. The family must be treated as NOT completed: complete_family is not
    called (family stays pending), the job fails, and the failure is logged at
    ERROR. The committed data (marker) stays so the next insert re-converges.
    """
    scanned = _job(env, "scanned")

    def boom(mount_point, card):
        raise OSError("simulated card-manifest write failure")

    monkeypatch.setattr(Manifest_Store, "save_card", staticmethod(boom))

    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    assert result.verified is False
    assert any("card manifest" in e for e in result.errors), result.errors

    # The commit marker (data) IS on the card — the data was written.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # But the family is NOT completed: the interrupted marker is left in place
    # (complete_family was never called) so the next insert re-diffs and
    # converges. pending_families is the authoritative not-completed signal.
    assert "scanned" in du.pending_families(env["mount"])

    # Logged at ERROR.
    assert any(
        rec.levelno >= logging.ERROR and "card manifest" in rec.getMessage()
        for rec in caplog.records
    )


def test_card_manifest_write_failure_does_not_relabel(env, monkeypatch):
    """A card-manifest write failure must not trigger a relabel (Req 7.5).

    The relabel is a later step gated on a successful sync; here we assert the
    tree job returns BEFORE any post-completion work by confirming the family is
    not completed. (The relabel itself lands in task 6.1 and runs only after the
    card manifest write succeeds.)
    """
    plates = _job(env, "plates")

    monkeypatch.setattr(
        Manifest_Store,
        "save_card",
        staticmethod(lambda mp, card: (_ for _ in ()).throw(OSError("fail"))),
    )

    result = du.run_sync_job(plates, env["mount"])
    assert result.status == "failed"
    assert "plates" in du.pending_families(env["mount"])


# --- nav never writes a card manifest ---------------------------------------


def test_nav_job_does_not_write_card_manifest(env):
    nav = _job(env, "nav")
    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    # nav has no cycle and no manifest; the card manifest must not appear.
    assert Manifest_Store.load_card(env["mount"]) is None
