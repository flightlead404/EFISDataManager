# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example tests for identity-file v1 backward-read compatibility.

Covers task 2.2 of the multi-device-settings spec: the one-field
``device_link_id`` extension to the per-drive identity file in drive_updater,
and the guarantee that v1 drives (schema_version 1, no ``device_link_id``)
remain readable and can have a binding added without rewriting their identity.

  - ``_new_identity`` includes ``device_link_id: None`` and bumps
    ``schema_version`` to 2 (``IDENTITY_SCHEMA_VERSION == 2``) while preserving
    every pre-existing field.
  - ``read_identity`` on a hand-written v1 JSON (no ``device_link_id``) parses
    and ``.get("device_link_id")`` is None (no read-time schema gate).
  - ``update_identity_provenance`` can add ``device_link_id`` to a v1 drive
    while preserving id, kind, volume_uuid, and the provenance fields.

No physical USB is required: the mount root is ``tmp_path`` and the v1 identity
is hand-written via json.dump to the identity filename at the mount root.

Requirements: 4.3, 6.6
"""

import json
import os

import pytest

from efis_data_manager import drive_updater as du


@pytest.fixture
def mount(tmp_path):
    """A writable mount root (tmp_path exists, as write_identity requires)."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _no_diskutil(monkeypatch):
    """No diskutil in these tests: _new_identity records volume_uuid as None."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


def _write_v1_identity(mount, data):
    """Hand-write a raw identity JSON to the identity filename at the root."""
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        json.dump(data, f)


# --- _new_identity: device_link_id present, schema bumped to 2 --------------


def test_new_identity_includes_device_link_id_none_and_schema_v2(mount):
    assert du.IDENTITY_SCHEMA_VERSION == 2

    identity = du._new_identity(mount)

    assert "device_link_id" in identity
    assert identity["device_link_id"] is None
    assert identity["schema_version"] == 2


def test_new_identity_preserves_all_preexisting_fields(mount):
    identity = du._new_identity(mount)

    # Every field that survives the device_link_id extension must remain. The
    # stale volume ``label`` field was dropped by the chart-sync-wiring-fix
    # identity-schema change (R8.1): it is NOT a required field any longer.
    for field in (
        "id",
        "kind",
        "volume_uuid",
        "created",
        "prepared_by",
        "last_sync_started",
        "last_sync_completed",
        "last_sync_result",
        "last_sync_families",
        "sync_count",
        "data_cycle",
    ):
        assert field in identity, f"missing pre-existing field: {field}"

    # (R8.1) The stale volume ``label`` is gone; (R8.5) the labeling-only
    # ``chart_capable`` snapshot is now present instead.
    assert "label" not in identity
    assert "chart_capable" in identity


# --- backward-read of a v1 drive --------------------------------------------


def test_read_identity_parses_v1_without_device_link_id(mount):
    # A v1 identity as written by the previous schema: no device_link_id.
    v1 = {
        "schema_version": 1,
        "id": "v1-drive-id",
        "kind": "efis-chart-drive",
        "volume_uuid": "V1-UUID",
        "created": "2025-01-01T00:00:00Z",
        "prepared_by": "EFISDataManager 0.6.0 on oldhost",
        "last_sync_started": None,
        "last_sync_completed": None,
        "last_sync_result": None,
        "last_sync_families": [],
        "sync_count": 0,
        "data_cycle": None,
    }
    _write_v1_identity(mount, v1)

    read_back = du.read_identity(mount)

    # No read-time schema gate: a v1 drive parses.
    assert read_back is not None
    assert read_back["schema_version"] == 1
    # The missing field reads as None via .get (not present in the file).
    assert "device_link_id" not in read_back
    assert read_back.get("device_link_id") is None


# --- update_identity_provenance adds device_link_id to a v1 drive -----------


def test_update_provenance_adds_device_link_id_to_v1_drive(mount):
    v1 = {
        "schema_version": 1,
        "id": "v1-drive-id",
        "kind": "efis-chart-drive",
        "volume_uuid": "V1-UUID",
        "created": "2025-01-01T00:00:00Z",
        "prepared_by": "EFISDataManager 0.6.0 on oldhost",
        "last_sync_started": "2025-01-02T00:00:00Z",
        "last_sync_completed": "2025-01-02T01:00:00Z",
        "last_sync_result": "clean",
        "last_sync_families": ["scanned"],
        "sync_count": 4,
        "data_cycle": "2501",
    }
    _write_v1_identity(mount, v1)

    du.update_identity_provenance(mount, device_link_id="2")

    updated = du.read_identity(mount)
    # The binding was added.
    assert updated.get("device_link_id") == "2"
    # Identity-defining fields preserved unchanged.
    assert updated["id"] == v1["id"]
    assert updated["kind"] == v1["kind"]
    assert updated["volume_uuid"] == v1["volume_uuid"]
    # (R8.1) The stale volume ``label`` field is gone from the identity schema;
    # the merge neither introduces nor requires one, so it stays absent here.
    assert "label" not in updated
    # Provenance fields preserved unchanged.
    assert updated["created"] == v1["created"]
    assert updated["prepared_by"] == v1["prepared_by"]
    assert updated["last_sync_started"] == v1["last_sync_started"]
    assert updated["last_sync_completed"] == v1["last_sync_completed"]
    assert updated["last_sync_result"] == v1["last_sync_result"]
    assert updated["last_sync_families"] == v1["last_sync_families"]
    assert updated["sync_count"] == v1["sync_count"]
    assert updated["data_cycle"] == v1["data_cycle"]
