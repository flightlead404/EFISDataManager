# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for find_mounted_managed_drive (usb_monitor.py).

Regression coverage for the "mounted but DM says no drive connected" bug: after
a Prepare/Adopt (or any mount-swap resume), the auto mount callback is bypassed,
so the menu-bar connected-status UI must be reconciled against ground truth by
scanning /Volumes for a mounted MANAGED drive. find_mounted_managed_drive is the
pure helper that reconciliation uses; it is importable headlessly (unlike app.py
which pulls in rumps/AppKit).
"""

import json
import os

from efis_data_manager.usb_monitor import (
    find_mounted_managed_drive,
    IDENTITY_FILENAME,
    IDENTITY_KIND,
)


def _make_managed(vol_dir, name):
    """Create a managed-drive volume dir with a valid identity file."""
    d = vol_dir / name
    d.mkdir()
    (d / IDENTITY_FILENAME).write_text(
        json.dumps({"schema_version": 1, "kind": IDENTITY_KIND, "id": "x"})
    )
    return str(d)


def _make_plain(vol_dir, name):
    """Create a non-managed volume dir (no identity file)."""
    d = vol_dir / name
    d.mkdir()
    return str(d)


def test_returns_managed_mount_when_present(tmp_path):
    managed = _make_managed(tmp_path, "EFIS_1")
    _make_plain(tmp_path, "Macintosh HD")
    assert find_mounted_managed_drive(str(tmp_path)) == managed


def test_returns_none_when_no_managed_drive(tmp_path):
    _make_plain(tmp_path, "Macintosh HD")
    _make_plain(tmp_path, "SomeUSB")
    assert find_mounted_managed_drive(str(tmp_path)) is None


def test_ignores_candidate_without_identity(tmp_path):
    # A drive with chart data but NO identity file is an adoption candidate,
    # not managed -> must not be reported as connected.
    cand = tmp_path / "EFIS_4"
    (cand / "ChartData").mkdir(parents=True)
    assert find_mounted_managed_drive(str(tmp_path)) is None


def test_wrong_kind_identity_not_managed(tmp_path):
    d = tmp_path / "EFIS_X"
    d.mkdir()
    (d / IDENTITY_FILENAME).write_text(json.dumps({"kind": "something-else"}))
    assert find_mounted_managed_drive(str(tmp_path)) is None


def test_missing_volumes_dir_returns_none(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert find_mounted_managed_drive(str(missing)) is None
