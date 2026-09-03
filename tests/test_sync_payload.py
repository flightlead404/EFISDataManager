# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the tree-job payload sync (sync_payload) in drive_updater.

Covers Task 4 of the drive-sync-integrity spec:
  - rsync converges a temp source -> dest (efficient size+mtime delta)
  - a second run with no changes transfers nothing (idempotency)
  - the family commit marker is excluded from the payload copy
  - rsync failures are captured into errors and logged at ERROR
  - the is_aborted predicate terminates the transfer and records an abort

Requirements: 4.1, 4.2, 8.1

These tests run rsync against real temporary directories (no physical USB).
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _tree_job(src_dir, dst_dir, excludes=None):
    """Build a minimal tree SyncJob pointing at two temp dirs."""
    return du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src_dir) + os.sep,
        payload_root_drive=str(dst_dir) + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES) + (excludes or []),
    )


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _rel_files(root):
    """Set of file paths relative to root (posix separators)."""
    out = set()
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            out.add(rel.replace(os.sep, "/"))
    return out


# --- convergence + idempotency ---------------------------------------------


def test_sync_converges_source_to_dest(tmp_path):
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    _write(src / "LO" / "b.tif", "bbbb")
    _write(src / "top.txt", "c")
    dst.mkdir()

    job = _tree_job(src, dst)
    files_updated, errors = du.sync_payload(job)

    assert errors == []
    assert files_updated == 3
    assert _rel_files(dst) == {"SEC/a.tif", "LO/b.tif", "top.txt"}
    assert (dst / "SEC" / "a.tif").read_text() == "aaa"


def test_second_run_is_noop(tmp_path):
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    _write(src / "LO" / "b.tif", "bbbb")
    dst.mkdir()

    job = _tree_job(src, dst)
    first_updated, first_errors = du.sync_payload(job)
    assert first_errors == []
    assert first_updated == 2

    # Nothing changed on the source; the delta strategy must transfer nothing.
    second_updated, second_errors = du.sync_payload(job)
    assert second_errors == []
    assert second_updated == 0
    assert _rel_files(dst) == {"SEC/a.tif", "LO/b.tif"}


def test_delete_removes_extra_dest_files(tmp_path):
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "keep.txt", "keep")
    _write(dst / "keep.txt", "keep")
    _write(dst / "stale.txt", "stale")  # not in source -> should be deleted

    job = _tree_job(src, dst)
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert _rel_files(dst) == {"keep.txt"}


# --- marker exclusion -------------------------------------------------------


def test_marker_is_excluded_from_payload(tmp_path):
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    _write(src / "ScannedCharts.sqlite", "MARKER")  # must NOT be copied
    dst.mkdir()

    # build_jobs puts the marker basename in excludes for the scanned family.
    job = _tree_job(src, dst, excludes=["ScannedCharts.sqlite"])
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert "ScannedCharts.sqlite" not in _rel_files(dst)
    assert "SEC/a.tif" in _rel_files(dst)


# --- error handling ---------------------------------------------------------


def test_rsync_failure_captured_and_logged(tmp_path, caplog):
    # Source directory does not exist -> rsync exits non-zero.
    src = tmp_path / "missing"
    dst = tmp_path / "drive"
    dst.mkdir()

    job = _tree_job(src, dst)
    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        files_updated, errors = du.sync_payload(job)

    assert files_updated == 0
    assert len(errors) == 1
    assert "scanned" in errors[0]
    # Req 8.1: the error is logged at ERROR so Recent Errors mirrors status.
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_non_tree_job_rejected():
    nav = du.SyncJob(name="nav", kind="files", files=["NAV.DB"])
    with pytest.raises(ValueError):
        du.sync_payload(nav)


# --- abort path -------------------------------------------------------------


def test_is_aborted_terminates_and_records_error(tmp_path, caplog):
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    # Enough files that rsync doesn't finish in the very first poll tick.
    for i in range(200):
        _write(src / f"f{i}.bin", "x" * 4096)
    dst.mkdir()

    job = _tree_job(src, dst)

    # Abort on the first poll.
    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        files_updated, errors = du.sync_payload(job, is_aborted=lambda: True)

    assert files_updated == 0
    assert len(errors) == 1
    assert "aborted" in errors[0].lower()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


# --- metadata purge + structural protection (two-tier excludes) -------------
#
# rsync's default --delete PROTECTS excluded files, so macOS "._*" sidecars
# accumulated on the drive. sync_payload now uses --delete-excluded for the
# metadata patterns (.DS_Store, ._*) while PROTECTING structural excludes
# (the sibling family's subtree, the legacy dir, the commit marker).


def test_metadata_sidecars_on_dest_are_purged(tmp_path):
    """Pre-existing ._* / .DS_Store files on the drive are deleted by a sync."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    # Junk metadata already sitting on the drive (as it does in the field).
    _write(dst / "._a.png", "junk")
    _write(dst / "SEC" / "._a.png", "junk")
    _write(dst / ".DS_Store", "junk")
    _write(dst / "SEC" / ".DS_Store", "junk")

    job = _tree_job(src, dst)
    _updated, errors = du.sync_payload(job)

    assert errors == []
    files = _rel_files(dst)
    # Real payload present, all metadata purged.
    assert "SEC/a.png" in files
    assert not any(f.rsplit("/", 1)[-1].startswith("._") for f in files)
    assert not any(f.rsplit("/", 1)[-1] == ".DS_Store" for f in files)


def test_metadata_sidecars_are_never_copied_from_source(tmp_path):
    """._* / .DS_Store on the SOURCE are not pushed to the drive."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    _write(src / "._a.png", "junk")
    _write(src / "SEC" / "._a.png", "junk")
    _write(src / ".DS_Store", "junk")
    dst.mkdir()

    job = _tree_job(src, dst)
    _updated, errors = du.sync_payload(job)

    assert errors == []
    files = _rel_files(dst)
    assert "SEC/a.png" in files
    assert not any(f.rsplit("/", 1)[-1].startswith("._") for f in files)
    assert ".DS_Store" not in files


def test_structural_exclude_subtree_is_protected_from_delete(tmp_path):
    """A structural exclude (sibling family's subtree) is NOT deleted.

    The scanned job excludes ``Plates/``; --delete-excluded must not remove the
    Plates subtree the plates family owns, even though it matches an exclude.
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    # Plates data lives only on the drive (owned by the plates family).
    _write(dst / "Plates" / "KABC.pdf", "plate-bytes")
    # And a metadata file inside the protected subtree — still purged? rsync
    # protects the whole subtree, so it is left untouched here. Assert the
    # payload/pdf survives (the key invariant: no cross-family data loss).

    job = _tree_job(src, dst, excludes=["Plates/", "ScannedCharts.sqlite"])
    _updated, errors = du.sync_payload(job)

    assert errors == []
    files = _rel_files(dst)
    assert "SEC/a.png" in files
    # The sibling family's subtree must survive the scanned job's delete pass.
    assert "Plates/KABC.pdf" in files


def test_commit_marker_on_dest_is_protected_from_delete(tmp_path):
    """The commit marker is a structural exclude and is not deleted.

    (It is written separately as the atomic commit step; a sync of the payload
    must never remove an existing marker.)
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    _write(dst / "ScannedCharts.sqlite", "existing-marker")

    job = _tree_job(src, dst, excludes=["ScannedCharts.sqlite"])
    _updated, errors = du.sync_payload(job)

    assert errors == []
    files = _rel_files(dst)
    assert "SEC/a.png" in files
    assert "ScannedCharts.sqlite" in files  # protected, not deleted
