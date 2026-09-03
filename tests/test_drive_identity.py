# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the drive-identity primitives in drive_updater.

Covers Task 14 of the drive-sync-integrity spec:
  - read_identity / write_identity (atomic round-trip, no partial file)
  - update_identity_provenance (merge without clobbering id/kind)
  - resolve_drive_id: read existing id; lazy adoption when file missing but the
    volume is a recognized EFIS drive; None when kind wrong / file corrupt /
    not an EFIS drive; VolumeUUID-mismatch WARNING.

No physical USB is required: the mount root is ``tmp_path`` and diskutil is
mocked via monkeypatching ``_volume_uuid`` / ``_volume_name`` and
``is_efis_drive``.

Requirements: 10.1, 10.2, 10.3, 10.4, 10.7, 10.9
"""

import json
import logging
import os

import pytest

from efis_data_manager import drive_updater as du


@pytest.fixture
def mount(tmp_path):
    """A ready, writable, listable mount root that passes wait_for_mount_ready."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _fast_mount_ready(monkeypatch):
    """resolve_drive_id calls wait_for_mount_ready first; short-circuit it.

    tmp_path is a real directory but not an OS mount point, so the real
    wait_for_mount_ready would spin the full budget and return False. Treat the
    temp mount as ready for these tests.
    """
    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _no_diskutil(monkeypatch):
    """Default: no diskutil. Tests that need a VolumeUUID override this."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


def _write_identity_file(mount, data):
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        json.dump(data, f)


# --- read / write round-trip ------------------------------------------------


def test_write_identity_roundtrips(mount):
    data = du._new_identity(mount)
    du.write_identity(mount, data)

    read_back = du.read_identity(mount)
    assert read_back == data
    assert read_back["kind"] == du.IDENTITY_KIND
    assert read_back["schema_version"] == 1


def test_write_identity_atomic_no_partial_file(mount, monkeypatch):
    """A failure mid-write must leave neither a partial identity nor a temp file."""
    # First write a good identity so we can assert it is untouched by a later
    # failed write.
    good = du._new_identity(mount)
    du.write_identity(mount, good)

    # Force json.dump to blow up partway through the temp write.
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(du.json, "dump", _boom)
    with pytest.raises(RuntimeError):
        du.write_identity(mount, {"id": "x", "kind": du.IDENTITY_KIND})

    # The previously-written identity is intact (os.replace never happened).
    assert du.read_identity(mount) == good
    # No leftover temp file from the aborted write.
    leftovers = [n for n in os.listdir(mount) if n.startswith(".efis_drive_id.")]
    assert leftovers == []


def test_read_identity_missing_returns_none(mount):
    assert du.read_identity(mount) is None


def test_read_identity_corrupt_returns_none(mount):
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        f.write("{ not valid json")
    assert du.read_identity(mount) is None


# --- update_identity_provenance ---------------------------------------------


def test_update_provenance_merges_fields(mount):
    du.write_identity(mount, du._new_identity(mount))
    original_id = du.read_identity(mount)["id"]

    du.update_identity_provenance(
        mount,
        last_sync_result="clean",
        sync_count=3,
        data_cycle="2410",
        last_sync_families=["scanned", "plates"],
    )

    updated = du.read_identity(mount)
    assert updated["last_sync_result"] == "clean"
    assert updated["sync_count"] == 3
    assert updated["data_cycle"] == "2410"
    assert updated["last_sync_families"] == ["scanned", "plates"]
    # Identity key preserved.
    assert updated["id"] == original_id


def test_update_provenance_does_not_clobber_id_or_kind(mount):
    du.write_identity(mount, du._new_identity(mount))
    original = du.read_identity(mount)

    # Even if a caller passes a conflicting id/kind, they are preserved.
    du.update_identity_provenance(
        mount, id="attacker", kind="not-ours", sync_count=1
    )

    updated = du.read_identity(mount)
    assert updated["id"] == original["id"]
    assert updated["kind"] == du.IDENTITY_KIND
    assert updated["sync_count"] == 1


def test_update_provenance_noop_without_identity(mount):
    # No identity file present -> no-op, no file created.
    du.update_identity_provenance(mount, sync_count=5)
    assert du.read_identity(mount) is None
    assert not os.path.exists(os.path.join(mount, du.IDENTITY_FILENAME))


# --- resolve_drive_id: existing identity ------------------------------------


def test_resolve_returns_existing_id(mount, monkeypatch):
    data = du._new_identity(mount)
    du.write_identity(mount, data)
    # is_efis_drive must NOT be needed when a valid identity already exists.
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive",
        lambda mp: (_ for _ in ()).throw(AssertionError("should not adopt")),
    )
    assert du.resolve_drive_id(mount) == data["id"]


def test_resolve_wrong_kind_and_not_efis_returns_none(mount, monkeypatch):
    _write_identity_file(
        mount, {"schema_version": 1, "id": "abc", "kind": "something-else"}
    )
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: False
    )
    assert du.resolve_drive_id(mount) is None


def test_resolve_corrupt_identity_not_efis_returns_none(mount, monkeypatch):
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        f.write("garbage{")
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: False
    )
    assert du.resolve_drive_id(mount) is None


def test_resolve_not_efis_drive_returns_none(mount, monkeypatch):
    # No identity file, not a recognized EFIS drive -> fail safe.
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: False
    )
    assert du.resolve_drive_id(mount) is None


# --- resolve_drive_id: lazy adoption ----------------------------------------


def test_resolve_adopts_recognized_unmarked_drive(mount, monkeypatch):
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: True
    )
    assert du.read_identity(mount) is None  # unmarked to start

    drive_id = du.resolve_drive_id(mount)

    assert drive_id is not None
    written = du.read_identity(mount)
    assert written is not None
    assert written["id"] == drive_id
    assert written["kind"] == du.IDENTITY_KIND
    assert written["schema_version"] == 1
    # Provenance fields initialised.
    assert written["sync_count"] == 0
    assert written["last_sync_result"] is None
    assert written["last_sync_families"] == []
    assert written["data_cycle"] is None


def test_resolve_adopt_is_stable_across_calls(mount, monkeypatch):
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive", lambda mp: True
    )
    first = du.resolve_drive_id(mount)
    second = du.resolve_drive_id(mount)
    assert first == second


# --- resolve_drive_id: VolumeUUID mismatch WARNING --------------------------


def test_resolve_volume_uuid_mismatch_warns(mount, monkeypatch, caplog):
    data = du._new_identity(mount)
    data["volume_uuid"] = "STORED-UUID"
    du.write_identity(mount, data)

    # diskutil now reports a different VolumeUUID (cloned drive / copied file).
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: "OBSERVED-UUID")

    with caplog.at_level(logging.WARNING, logger="efis_data_manager.drive_updater"):
        drive_id = du.resolve_drive_id(mount)

    # The app id still wins; mismatch does not fail resolution.
    assert drive_id == data["id"]
    assert any(
        "VolumeUUID mismatch" in rec.getMessage() for rec in caplog.records
    )


def test_resolve_volume_uuid_match_no_warning(mount, monkeypatch, caplog):
    data = du._new_identity(mount)
    data["volume_uuid"] = "SAME-UUID"
    du.write_identity(mount, data)
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: "SAME-UUID")

    with caplog.at_level(logging.WARNING, logger="efis_data_manager.drive_updater"):
        drive_id = du.resolve_drive_id(mount)

    assert drive_id == data["id"]
    assert not any(
        "VolumeUUID mismatch" in rec.getMessage() for rec in caplog.records
    )
