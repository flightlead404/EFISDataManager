# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for prepare_drive's identity-writing behavior.

Covers Task 17 of the drive-sync-integrity spec: a freshly-prepared drive gets
a durable identity file written before the populate step (Req 10.3).

prepare_drive itself shells out to ``diskutil eraseDisk`` (destructive and
unavailable in tests), so the identity-writing behavior is factored into the
small ``_ensure_identity(mount_point)`` helper that prepare_drive calls. The
helper is exercised directly against a tmp_path mount, plus a smoke test that a
prepared drive resolves to the same stable id.

Requirements: 10.3
"""

import logging
import os

import pytest

from efis_data_manager import drive_updater as du


@pytest.fixture
def mount(tmp_path):
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _no_diskutil(monkeypatch):
    """No real diskutil in tests; identity captures no VolumeUUID/name."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


def test_ensure_identity_writes_valid_file(mount):
    drive_id = du._ensure_identity(mount)

    assert drive_id is not None
    path = os.path.join(mount, du.IDENTITY_FILENAME)
    assert os.path.exists(path)

    written = du.read_identity(mount)
    assert written is not None
    assert written["id"] == drive_id
    assert written["kind"] == du.IDENTITY_KIND
    assert written["schema_version"] == du.IDENTITY_SCHEMA_VERSION
    # Provenance fields initialised for a brand-new drive.
    assert written["sync_count"] == 0
    assert written["last_sync_result"] is None
    assert written["last_sync_families"] == []


def test_ensure_identity_preserves_existing_id(mount):
    """Re-preparing a drive that already has identity keeps its id."""
    first = du._ensure_identity(mount)
    second = du._ensure_identity(mount)
    assert first == second
    # Still exactly one identity file, unchanged id.
    assert du.read_identity(mount)["id"] == first


def test_ensure_identity_failure_does_not_raise(mount, monkeypatch, caplog):
    """A write failure must be swallowed (prepare stays usable) and logged."""
    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(du, "write_identity", _boom)

    with caplog.at_level(logging.WARNING, logger="efis_data_manager.drive_updater"):
        result = du._ensure_identity(mount)

    assert result is None
    assert any(
        "Could not write identity" in rec.getMessage() for rec in caplog.records
    )


def test_prepared_drive_resolves_to_written_id(mount, monkeypatch):
    """Smoke: after _ensure_identity, resolve_drive_id returns the same id.

    resolve_drive_id waits for mount readiness and consults is_efis_drive for
    adoption; here the identity file already exists so neither adoption nor a
    real mount is needed. wait_for_mount_ready is short-circuited because
    tmp_path is not an OS mount point.
    """
    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)
    monkeypatch.setattr(
        "efis_data_manager.usb_monitor.is_efis_drive",
        lambda mp: (_ for _ in ()).throw(AssertionError("should not adopt")),
    )

    written_id = du._ensure_identity(mount)
    assert du.resolve_drive_id(mount) == written_id
