# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the sync-state helpers and data models in drive_updater.

Covers Tasks 1 + 15 of the drive-sync-integrity spec:
  - JobResult / SyncJob dataclass shapes
  - v2 id-keyed begin/complete/pending family transitions
  - entry pruning + file cleanup (entry pruned when a drive's families empty;
    file removed when no drives remain)
  - legacy v1 single-"mount" object and `.sync_in_progress` string are
    DISCARDED (not migrated) on read

Requirements: 7.1, 7.7, 10.5, 10.6, 10.7
"""

import json
import os

import pytest

from efis_data_manager import drive_updater as du


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Redirect the module-level sync-state paths into a temp directory."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    state_path = state_dir / ".sync_state.json"
    legacy_path = state_dir / ".sync_in_progress"

    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_path))
    monkeypatch.setattr(du, "LEGACY_SYNC_MARKER_PATH", str(legacy_path))

    return {
        "dir": state_dir,
        "state": state_path,
        "legacy": legacy_path,
    }


# --- Data models ------------------------------------------------------------


def test_jobresult_defaults():
    r = du.JobResult(name="scanned", status="updated")
    assert r.name == "scanned"
    assert r.status == "updated"
    assert r.files_updated == 0
    assert r.errors == []
    assert r.verified is False
    # Each instance gets its own mutable errors list.
    r.errors.append("boom")
    assert du.JobResult(name="plates", status="failed").errors == []


def test_syncjob_shape():
    job = du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local="/local/ChartData",
        payload_root_drive="/Volumes/EFIS/ChartData",
        excludes=["ScannedCharts.sqlite"],
        marker_src="/local/ChartData/ScannedCharts.sqlite",
        marker_dst="/Volumes/EFIS/ChartData/ScannedCharts.sqlite",
    )
    assert job.name == "scanned"
    assert job.kind == "tree"
    assert "ScannedCharts.sqlite" in job.excludes
    assert job.files == []

    nav = du.SyncJob(name="nav", kind="files", files=["NAV.DB", "NAV-proc.DB"])
    assert nav.marker_src is None
    assert nav.files == ["NAV.DB", "NAV-proc.DB"]


# --- begin / complete / pending transitions (v2 id-keyed) -------------------
#
# The helpers now key on an opaque drive_id string. A drive-id value is used
# directly; the tests use ids like "id-A" rather than mount paths.


def test_no_state_initially(state_paths):
    assert du.read_sync_state() is None
    assert du.pending_families("id-A") == []


def test_begin_records_family(state_paths):
    du.begin_family("id-A", "scanned", mount="/Volumes/EFIS_1")

    state = du.read_sync_state()
    assert state is not None
    assert state["schema_version"] == 2
    entry = state["drives"]["id-A"]
    assert entry["families"] == ["scanned"]
    assert entry["mount"] == "/Volumes/EFIS_1"
    assert "started" in entry
    assert du.pending_families("id-A") == ["scanned"]


def test_begin_multiple_families_no_duplicates(state_paths):
    du.begin_family("id-A", "scanned")
    du.begin_family("id-A", "plates")
    du.begin_family("id-A", "scanned")  # duplicate should be ignored

    assert du.pending_families("id-A") == ["scanned", "plates"]


def test_complete_removes_family(state_paths):
    du.begin_family("id-A", "scanned")
    du.begin_family("id-A", "plates")

    du.complete_family("id-A", "scanned")
    assert du.pending_families("id-A") == ["plates"]


def test_pending_only_for_matching_id(state_paths):
    du.begin_family("id-A", "scanned")
    assert du.pending_families("id-B") == []


def test_distinct_ids_tracked_independently(state_paths):
    # Two drives begin syncing different families; each entry is independent
    # (Property 6 — swapping which physical drive is present never mixes state).
    du.begin_family("id-A", "scanned")
    du.begin_family("id-B", "plates")

    assert du.pending_families("id-A") == ["scanned"]
    assert du.pending_families("id-B") == ["plates"]

    state = du.read_sync_state()
    assert set(state["drives"]) == {"id-A", "id-B"}


def test_complete_wrong_id_is_noop(state_paths):
    du.begin_family("id-A", "scanned")
    du.complete_family("id-B", "scanned")
    assert du.pending_families("id-A") == ["scanned"]


# --- pruning + empty-file cleanup -------------------------------------------


def test_completing_last_family_prunes_entry_but_keeps_other_drives(state_paths):
    du.begin_family("id-A", "scanned")
    du.begin_family("id-B", "plates")

    # Completing id-A's last family prunes its entry; id-B stays.
    du.complete_family("id-A", "scanned")

    state = du.read_sync_state()
    assert state is not None
    assert set(state["drives"]) == {"id-B"}
    assert du.pending_families("id-A") == []
    assert du.pending_families("id-B") == ["plates"]
    # File still exists because id-B remains.
    assert os.path.exists(state_paths["state"])


def test_state_file_removed_when_last_drive_completes(state_paths):
    du.begin_family("id-A", "scanned")
    assert os.path.exists(state_paths["state"])

    du.complete_family("id-A", "scanned")

    assert not os.path.exists(state_paths["state"])
    assert du.read_sync_state() is None
    assert du.pending_families("id-A") == []


def test_empty_drives_file_treated_as_no_state(state_paths):
    state_paths["state"].write_text(
        json.dumps({"schema_version": 2, "drives": {}})
    )
    assert du.read_sync_state() is None
    # Reading an empty-drives file cleans it up.
    assert not os.path.exists(state_paths["state"])


def test_drive_entry_with_empty_families_pruned_on_read(state_paths):
    state_paths["state"].write_text(
        json.dumps(
            {
                "schema_version": 2,
                "drives": {"id-A": {"families": [], "started": "x"}},
            }
        )
    )
    assert du.read_sync_state() is None
    assert not os.path.exists(state_paths["state"])


def test_corrupt_state_file_discarded(state_paths):
    state_paths["state"].write_text("{ not valid json")
    assert du.read_sync_state() is None
    assert not os.path.exists(state_paths["state"])


# --- legacy discard (v2 does NOT migrate) -----------------------------------


def test_legacy_v1_single_mount_object_discarded(state_paths):
    # The v1 shape ({"mount": ..., "families": [...]}) has no "drives" map and
    # cannot be mapped to a drive id when the drive may be absent — discard it.
    state_paths["state"].write_text(
        json.dumps(
            {"mount": "/Volumes/EFIS_1", "families": ["scanned"], "started": "x"}
        )
    )
    assert du.read_sync_state() is None
    # The unmappable legacy file is removed, not migrated.
    assert not os.path.exists(state_paths["state"])
    assert du.pending_families("/Volumes/EFIS_1") == []


def test_legacy_sync_in_progress_string_discarded(state_paths):
    # The oldest format is a bare mount-path string in .sync_in_progress. v2
    # discards it (no migration): it cannot be mapped to a drive id.
    state_paths["legacy"].write_text("/Volumes/EFIS_1\n")

    assert du.read_sync_state() is None
    # Legacy string file is removed on read.
    assert not os.path.exists(state_paths["legacy"])
    assert not os.path.exists(state_paths["state"])
    assert du.pending_families("/Volumes/EFIS_1") == []


def test_legacy_string_discarded_even_with_v2_state_present(state_paths):
    # A live v2 state and a stray legacy string can coexist; the legacy string
    # is always dropped and never affects the v2 map.
    du.begin_family("id-A", "scanned")
    state_paths["legacy"].write_text("/Volumes/EFIS_9\n")

    state = du.read_sync_state()
    assert state is not None
    assert set(state["drives"]) == {"id-A"}
    assert not os.path.exists(state_paths["legacy"])
