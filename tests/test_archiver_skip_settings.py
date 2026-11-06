# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``archive_efis_drive(archive_settings=...)`` (settings-management
task 10.3, Requirements 7.4, 7.5)."""
import pytest

from efis_data_manager import archiver

_BODY = "CHECKTYPE=STD\n151=400\nUPDATE=1\nCHECKSIZE=1\nCHECKSUM=AB\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    monkeypatch.setattr(
        archiver, "load_config", lambda: {"archive_path": str(archive_root)}
    )
    (mount / "Settings.bak").write_text(_BODY)
    (mount / "DEMO-0001.LOG").write_text("demo log data")
    return mount, archive_root


def test_archive_settings_false_skips_settings_but_archives_other_content(env):
    mount, archive_root = env
    results = archiver.archive_efis_drive(
        str(mount), today="2026-01-01", archive_settings=False
    )
    assert not (archive_root / "Settings").exists()
    assert results["settings_copied"] == 0
    assert results["skipped"] == 0
    assert results["errors"] == []
    # Other content still archived; source Settings untouched.
    assert results["demo_moved"] == 1
    assert (archive_root / "Demo" / "2026-01-01" / "DEMO-0001.LOG").is_file()
    assert (mount / "Settings.bak").is_file()


def test_archive_settings_default_true_archives_settings(env):
    mount, archive_root = env
    results = archiver.archive_efis_drive(str(mount), today="2026-01-01")
    snap = archive_root / "Settings" / "unresolved" / "Settings-2026-01-01.bak"
    assert snap.is_file()
    assert results["settings_copied"] == 1
    assert results["demo_moved"] == 1
