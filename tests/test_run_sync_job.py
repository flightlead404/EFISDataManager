# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for run_sync_job (commit-marker write + per-job driver).

Covers Task 6 of the drive-sync-integrity spec and design.md Testing strategy
items 1 (commit-marker atomicity) and 2 (per-family independence):

  - a tree job syncs payload, verifies, then writes the commit marker last and
    clears the interrupted state (Property 1)
  - an abort BEFORE the marker leaves the drive marker absent, leaves the
    interrupted state in place, and the quick check reports the family stale
    (Property 1 / Property 2)
  - a payload/verify failure in one family (plates) does NOT touch another
    family's already-written marker; the healthy family stays current
    (Property 4)
  - the nav (files) job copies + verifies by checksum with no separate marker
  - errors are surfaced on the JobResult and logged at >= WARNING

Requirements: 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 5.3

rsync runs against real temp dirs; the mount presence/writability preflight is
mocked (temp dirs are not real mount points). No physical USB is needed.
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du


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
    monkeypatch.setattr(du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress"))
    return state_dir


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths):
    """A local image with all three families and an empty drive to sync into."""
    local = tmp_path / "image"
    drive = tmp_path / "drive"
    base_mtime = 1_000_000

    # scanned family payload + marker
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db", mtime=base_mtime)

    # plates family payload + marker
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db", mtime=base_mtime)

    # nav family files
    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")

    drive.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # The quick currency check now cross-checks the drive file count against the
    # LOCAL manifest (quick-currency-count-size-check bugfix); with no local
    # manifest it fail-safes to not-current. Write local manifests matching the
    # payload so a cleanly-synced drive reads current. scanned = LO/a.png +
    # SEC/b.png -> 2 (Plates/ and marker excluded); plates = p1.pdf -> 1.
    from efis_data_manager import manifest as _mf

    def _entry(rel):
        return _mf.ManifestEntry(relpath=rel, sha256="0" * 64, size=1)

    _mf.Manifest_Store.save_local(
        _mf.FamilyManifest(
            family="scanned",
            cycle=None,
            entries={"LO/a.png": _entry("LO/a.png"), "SEC/b.png": _entry("SEC/b.png")},
        )
    )
    _mf.Manifest_Store.save_local(
        _mf.FamilyManifest(
            family="plates", cycle=None, entries={"p1.pdf": _entry("p1.pdf")}
        )
    )

    return {
        "local": local,
        "drive": drive,
        "mount": str(drive),
        "base_mtime": base_mtime,
    }


def _job(env, name):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == name)


# --- happy path: tree job writes marker LAST + clears interrupted state -----


def test_tree_job_success_writes_marker_and_clears_state(env):
    scanned = _job(env, "scanned")
    result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "updated"
    assert result.verified is True
    assert result.errors == []

    # Payload copied.
    assert (env["drive"] / "ChartData" / "LO" / "a.png").exists()
    assert (env["drive"] / "ChartData" / "SEC" / "b.png").exists()
    # Commit marker written last.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").read_bytes() == b"scanned-db"

    # Interrupted state cleared (family completed).
    assert du.pending_families(env["mount"]) == []
    # And the quick check now reports scanned current.
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is True


def test_second_run_reports_current_no_files(env):
    # First converge: the card manifest is absent, so _chart_family_job diffs
    # the local manifest against an empty card family -> a full manifest write
    # (files_from populated). run_sync_job transfers it and writes the card
    # manifest as the authoritative per-cycle record (manifest-only-chart-sync
    # design Section D).
    first = _job(env, "scanned")
    first_result = du.run_sync_job(first, env["mount"])
    assert first_result.status == "updated"

    # Re-build the job AFTER the first converge wrote the card manifest. Now the
    # local manifest and the card manifest agree, so _chart_family_job diffs to
    # an EMPTY files_from -> no-op. (The old size-only contract reused the same
    # job object; under the manifest-only path the no-op is an empty change set,
    # which only a fresh build_jobs can observe.)
    second = _job(env, "scanned")
    assert second.files_from == []  # empty change set -> nothing to transfer
    result = du.run_sync_job(second, env["mount"])
    assert result.status == "current"
    assert result.files_updated == 0
    assert result.verified is True


# --- Property 1 / 2: abort BEFORE marker -> marker absent, family stale ------


def test_abort_before_marker_leaves_marker_absent_and_family_stale(env, caplog):
    scanned = _job(env, "scanned")

    # Abort immediately: payload copy is terminated before it completes, so the
    # marker must never be written.
    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"], is_aborted=lambda: True)

    assert result.status == "aborted"
    assert result.verified is False
    assert result.errors  # an abort error is recorded
    # The commit marker MUST NOT exist on the drive.
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Interrupted state persists (family NOT completed).
    assert "scanned" in du.pending_families(env["mount"])

    # Quick check reports the family stale (interrupted forces not-current).
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is False

    # Req 8.1: the abort was logged at >= WARNING.
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_verification_mismatch_does_not_write_marker(env, monkeypatch, caplog):
    scanned = _job(env, "scanned")

    # Force verify_family to report a discrepancy after a clean rsync so we
    # exercise the "verified failed -> no marker" branch deterministically.
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {"missing": ["LO/a.png"], "extra": [], "size_mismatch": []},
    )

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    assert result.verified is False
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert "scanned" in du.pending_families(env["mount"])
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


# --- Property 4: a plates failure leaves the scanned marker valid -----------


def test_plates_failure_leaves_scanned_marker_valid(env, monkeypatch):
    # First, sync scanned successfully so its marker is valid + current.
    scanned = _job(env, "scanned")
    scanned_result = du.run_sync_job(scanned, env["mount"])
    assert scanned_result.status == "updated"
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Now fail the plates job via a forced verification mismatch.
    plates = _job(env, "plates")
    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {"missing": ["p1.pdf"], "extra": [], "size_mismatch": []},
    )
    plates_result = du.run_sync_job(plates, env["mount"])

    assert plates_result.status == "failed"
    # Plates marker NOT written.
    assert not (env["drive"] / "ChartData" / "Plates" / "Plates.sqlite").exists()

    # The scanned marker is untouched and scanned is still reported current.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").read_bytes() == b"scanned-db"
    currency = du.check_drive_currency(env["mount"])
    assert currency["families"]["scanned"]["current"] is True
    # Plates is stale (interrupted + no marker).
    assert currency["families"]["plates"]["current"] is False


# --- nav (files) job: checksum copy + verify, no separate marker ------------


def test_nav_job_copies_and_verifies(env):
    nav = _job(env, "nav")
    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    assert result.verified is True
    assert result.errors == []
    assert (env["drive"] / "NAV.DB").read_bytes() == b"nav-content"
    assert (env["drive"] / "NAV-proc.DB").read_bytes() == b"navproc-content"
    assert du.pending_families(env["mount"]) == []

    # Re-run is a no-op (already current by checksum).
    again = du.run_sync_job(nav, env["mount"])
    assert again.status == "current"
    assert again.files_updated == 0


# --- chart-manifest-sync task 3.6: nav stream preserved, no manifest/cycle ---
# The manifest/cycle work (tasks 3.1-3.5) must NOT touch the nav stream. nav is
# evaluated by size + SHA-256 on every insert (Req 8.1), rewritten on any
# content difference even at identical byte size (Req 8.2 — the --size-only
# gap), deep-checksum verified before completion (Req 8.3), and carries no
# cycle gate and no manifest (Req 2.1).


def test_nav_rewrites_same_size_different_content(env):
    """A nav file changed to the SAME byte size but different content is
    detected by SHA-256 and rewritten (the correctness gap the feature closes).
    """
    nav = _job(env, "nav")
    # First sync brings the drive current.
    du.run_sync_job(nav, env["mount"])
    assert (env["drive"] / "NAV.DB").read_bytes() == b"nav-content"

    # Mutate NAV.DB locally to the SAME size (11 bytes) but different bytes.
    new_bytes = b"NAV-CONTENT"
    assert len(new_bytes) == len(b"nav-content")
    _write(str(env["local"] / "NAV.DB"), new_bytes)

    result = du.run_sync_job(nav, env["mount"])

    # Size matched but checksum differed -> rewritten, verified deep, updated.
    assert result.status == "updated"
    assert result.files_updated == 1
    assert result.verified is True
    assert (env["drive"] / "NAV.DB").read_bytes() == new_bytes


def test_nav_verifies_by_deep_checksum_before_completion(env, monkeypatch):
    """The nav family is verified by a DEEP checksum pass before complete_family
    is reached, and a deep-verify discrepancy fails the job without completing.
    """
    nav = _job(env, "nav")
    seen = {}

    real_verify = du.verify_family

    def _spy_verify(job, mount, deep=False):
        seen["deep"] = deep
        return real_verify(job, mount, deep=deep)

    monkeypatch.setattr(du, "verify_family", _spy_verify)
    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    assert seen["deep"] is True  # nav verifies by deep checksum (Req 8.3)
    assert du.pending_families(env["mount"]) == []


def test_nav_job_carries_no_manifest_or_cycle_coupling(env):
    """The nav SyncJob is a plain files job: no manifest-driven files_from,
    no removed set, no commit marker, no payload tree — nothing the cycle/
    manifest path (tasks 3.1-3.5) attached.
    """
    nav = _job(env, "nav")

    assert nav.kind == "files"
    assert nav.files == ["NAV.DB", "NAV-proc.DB"]
    # Manifest-driven transfer fields are untouched for nav.
    assert nav.files_from is None
    assert nav.removed is None
    # No commit marker and no tree payload root.
    assert nav.marker_src is None
    assert nav.marker_dst is None
    assert nav.payload_root_local == ""
    assert nav.payload_root_drive == ""


def test_decide_streams_nav_always_evaluate_no_cycle(env, monkeypatch):
    """decide_streams always returns nav as 'evaluate' with no cycle, whether a
    card manifest is present or absent — independent of the chart cycle gate.
    """
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)

    from efis_data_manager.manifest import CardManifest

    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    # With a card manifest whose cycles match (charts/plates would skip).
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    nav_dec = next(d for d in du.decide_streams(env["mount"], local, card)
                   if d.family == "nav")
    assert nav_dec.action == "evaluate"
    assert nav_dec.local_cycle is None
    assert nav_dec.card_cycle is None

    # With no card manifest (first sync / corrupt) nav still just evaluates.
    nav_dec_none = next(d for d in du.decide_streams(env["mount"], local, None)
                        if d.family == "nav")
    assert nav_dec_none.action == "evaluate"


# --- preflight failure -------------------------------------------------------


def test_preflight_failure_when_mount_absent(env, monkeypatch, caplog):
    scanned = _job(env, "scanned")
    monkeypatch.setattr(du.os.path, "ismount", lambda p: False)

    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        result = du.run_sync_job(scanned, env["mount"])

    assert result.status == "failed"
    assert result.errors
    assert not (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


# NOTE: The former stall-detector tests (genuine-wedge, scan-phase CPU, and
# growing-stdout cases) were removed with the stall detector itself. A
# wedged-but-mounted drive is no longer force-aborted on a progress timeout;
# drive removal and sleep are handled by the MountWatchdog `is_aborted` path,
# which is exercised by the abort-path tests above and in test_sync_payload.py.
