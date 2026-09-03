# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Focused tests for the v2 id-keyed durable sync-state (Task 15).

The v2 sync-state is a map keyed by the drive's app-generated id, so an
arbitrary number of drives are tracked independently and a swap never
mis-attributes one drive's interrupted-sync state to another. These tests
exercise that keying directly against the helpers (no rsync, no real mount):

  - two distinct drive ids are tracked independently; begin/complete/pending
    for one id never touch the other (Property 6 — the swap/mis-attribution
    guard);
  - pending for an unknown id -> [];
  - completing the last family for a drive prunes its entry; completing the
    last drive removes the file;
  - the legacy v1 single-"mount" object is DISCARDED on read (not migrated);
  - the legacy .sync_in_progress string is DISCARDED (not migrated).

Requirements: 10.5, 10.6, 10.7
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

    return {"dir": state_dir, "state": state_path, "legacy": legacy_path}


# --- Property 6: independent per-id tracking, no mis-attribution ------------


def test_two_drives_tracked_independently(state_paths):
    du.begin_family("id-A", "scanned", mount="/Volumes/EFIS")
    du.begin_family("id-A", "plates", mount="/Volumes/EFIS")
    du.begin_family("id-B", "nav", mount="/Volumes/EFIS_1")

    assert du.pending_families("id-A") == ["scanned", "plates"]
    assert du.pending_families("id-B") == ["nav"]

    # Completing a family on A does not touch B, and vice versa.
    du.complete_family("id-A", "scanned")
    assert du.pending_families("id-A") == ["plates"]
    assert du.pending_families("id-B") == ["nav"]

    du.complete_family("id-B", "nav")
    assert du.pending_families("id-B") == []
    # A is unaffected by B completing its last family.
    assert du.pending_families("id-A") == ["plates"]


def test_same_mount_different_ids_do_not_collide(state_paths):
    # The swap scenario: two physical drives occupy the SAME mount path across
    # sessions but have different ids. State must follow the id, not the mount.
    du.begin_family("id-A", "scanned", mount="/Volumes/EFIS")
    du.begin_family("id-B", "plates", mount="/Volumes/EFIS")  # same mount, other drive

    assert du.pending_families("id-A") == ["scanned"]
    assert du.pending_families("id-B") == ["plates"]

    state = du.read_sync_state()
    assert set(state["drives"]) == {"id-A", "id-B"}
    # The informational last-seen mount is recorded per entry but never keys it.
    assert state["drives"]["id-A"]["mount"] == "/Volumes/EFIS"
    assert state["drives"]["id-B"]["mount"] == "/Volumes/EFIS"


# --- unknown id --------------------------------------------------------------


def test_pending_for_unknown_id_is_empty(state_paths):
    du.begin_family("id-A", "scanned")
    assert du.pending_families("id-UNKNOWN") == []


def test_pending_for_unknown_id_with_no_state_is_empty(state_paths):
    assert du.read_sync_state() is None
    assert du.pending_families("id-UNKNOWN") == []


# --- pruning: last family prunes entry; last drive removes file -------------


def test_last_family_prunes_entry_last_drive_removes_file(state_paths):
    du.begin_family("id-A", "scanned")
    du.begin_family("id-B", "nav")
    assert os.path.exists(state_paths["state"])

    # Completing A's only family prunes A's entry; the file remains for B.
    du.complete_family("id-A", "scanned")
    state = du.read_sync_state()
    assert set(state["drives"]) == {"id-B"}
    assert os.path.exists(state_paths["state"])

    # Completing B's only family removes the last drive -> file deleted.
    du.complete_family("id-B", "nav")
    assert not os.path.exists(state_paths["state"])
    assert du.read_sync_state() is None


# --- legacy formats are DISCARDED, not migrated -----------------------------


def test_legacy_v1_single_mount_object_discarded(state_paths):
    # v1 shape: a single mount object with no "drives" map.
    state_paths["state"].write_text(
        json.dumps(
            {"mount": "/Volumes/EFIS_1", "families": ["scanned"], "started": "x"}
        )
    )
    # Discarded on read: returns None and the unmappable file is removed.
    assert du.read_sync_state() is None
    assert not os.path.exists(state_paths["state"])
    # Nothing is pending for anything (no migration to a drive id occurred).
    assert du.pending_families("/Volumes/EFIS_1") == []


def test_legacy_sync_in_progress_string_discarded(state_paths):
    # The oldest format: a bare mount-path string in .sync_in_progress.
    state_paths["legacy"].write_text("/Volumes/EFIS_1\n")

    assert du.read_sync_state() is None
    # Discarded (removed), NOT migrated into the v2 map.
    assert not os.path.exists(state_paths["legacy"])
    assert not os.path.exists(state_paths["state"])
    assert du.pending_families("/Volumes/EFIS_1") == []


def test_legacy_string_discarded_alongside_live_v2_state(state_paths):
    # A stray legacy string must never corrupt a live v2 map; it is dropped.
    du.begin_family("id-A", "scanned")
    state_paths["legacy"].write_text("/Volumes/EFIS_9\n")

    state = du.read_sync_state()
    assert state is not None
    assert set(state["drives"]) == {"id-A"}
    assert not os.path.exists(state_paths["legacy"])
