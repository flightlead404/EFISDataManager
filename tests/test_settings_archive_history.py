# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for per-device archive location and history (settings_archive).

Covers ``device_settings_dir`` path partitioning, ``family_history`` scanning
and recency ordering (newest→oldest by Archive_Date then sequence, never mtime
or UPDATE_Value), and ``latest_version``.

Requirements: 1.7, 2.1, 2.2, 2.3, 2.4
"""

from datetime import date

from efis_data_manager import settings_archive
from efis_data_manager.settings_archive import (
    ArchivedSnapshot,
    device_settings_dir,
    family_history,
    latest_version,
)


def _write(path, text="151=400\nUPDATE=1\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# device_settings_dir
# ---------------------------------------------------------------------------

def test_device_dir_for_resolved_link(tmp_path):
    assert device_settings_dir(tmp_path, "1") == tmp_path / "Settings" / "link1"
    assert device_settings_dir(tmp_path, "2") == tmp_path / "Settings" / "link2"


def test_device_dir_strips_whitespace(tmp_path):
    assert device_settings_dir(tmp_path, " 3 ") == tmp_path / "Settings" / "link3"


def test_device_dir_none_is_unresolved(tmp_path):
    assert device_settings_dir(tmp_path, None) == tmp_path / "Settings" / "unresolved"


def test_device_dir_empty_is_unresolved(tmp_path):
    assert device_settings_dir(tmp_path, "") == tmp_path / "Settings" / "unresolved"
    assert device_settings_dir(tmp_path, "   ") == tmp_path / "Settings" / "unresolved"


# ---------------------------------------------------------------------------
# family_history
# ---------------------------------------------------------------------------

def test_history_missing_dir_is_empty(tmp_path):
    assert family_history(tmp_path, "1", "Settings") == []


def test_history_newest_to_oldest_by_date(tmp_path):
    d = device_settings_dir(tmp_path, "1")
    _write(d / "Settings-2026-01-01.bak")
    _write(d / "Settings-2026-03-01.bak")
    _write(d / "Settings-2026-02-01.bak")

    hist = family_history(tmp_path, "1", "Settings")
    assert [s.archive_date for s in hist] == [
        date(2026, 3, 1),
        date(2026, 2, 1),
        date(2026, 1, 1),
    ]


def test_history_same_date_ordered_by_sequence_desc(tmp_path):
    d = device_settings_dir(tmp_path, "1")
    _write(d / "Settings-2026-01-01.bak")       # sequence 1
    _write(d / "Settings-2026-01-01-2.bak")     # sequence 2
    _write(d / "Settings-2026-01-01-3.bak")     # sequence 3

    hist = family_history(tmp_path, "1", "Settings")
    assert [s.sequence for s in hist] == [3, 2, 1]
    assert hist[0].path.name == "Settings-2026-01-01-3.bak"


def test_history_filters_by_family(tmp_path):
    d = device_settings_dir(tmp_path, "1")
    _write(d / "Settings-2026-01-01.bak")
    _write(d / "WP-2026-01-01.bak")
    _write(d / "Plan-2026-01-01.bak")

    settings = family_history(tmp_path, "1", "Settings")
    assert len(settings) == 1
    assert settings[0].family == "Settings"

    wp = family_history(tmp_path, "1", "WP")
    assert len(wp) == 1
    assert wp[0].family == "WP"


def test_history_isolated_per_device(tmp_path):
    _write(device_settings_dir(tmp_path, "1") / "Settings-2026-01-01.bak")
    _write(device_settings_dir(tmp_path, "2") / "Settings-2026-05-05.bak")

    h1 = family_history(tmp_path, "1", "Settings")
    h2 = family_history(tmp_path, "2", "Settings")
    assert [s.archive_date for s in h1] == [date(2026, 1, 1)]
    assert [s.archive_date for s in h2] == [date(2026, 5, 5)]


def test_history_ignores_undated_files(tmp_path):
    d = device_settings_dir(tmp_path, "1")
    _write(d / "Settings-2026-01-01.bak")
    _write(d / "Settings.bak")          # no datestamp
    _write(d / "notes.txt")             # unrelated

    hist = family_history(tmp_path, "1", "Settings")
    assert len(hist) == 1
    assert hist[0].archive_date == date(2026, 1, 1)


def test_history_ordering_ignores_mtime(tmp_path):
    """Recency is Archive_Date, not file mtime (Req 1.7)."""
    import os

    d = device_settings_dir(tmp_path, "1")
    old_date = _write(d / "Settings-2026-01-01.bak")
    new_date = _write(d / "Settings-2026-06-01.bak")
    # Make the OLDER-dated file have a much NEWER mtime.
    os.utime(old_date, (2_000_000_000, 2_000_000_000))
    os.utime(new_date, (1_000_000_000, 1_000_000_000))

    hist = family_history(tmp_path, "1", "Settings")
    assert hist[0].archive_date == date(2026, 6, 1)


def test_history_snapshot_fields(tmp_path):
    d = device_settings_dir(tmp_path, "2")
    p = _write(d / "Settings-2026-07-04-2.dat")

    hist = family_history(tmp_path, "2", "Settings")
    snap = hist[0]
    assert isinstance(snap, ArchivedSnapshot)
    assert snap.path == p
    assert snap.family == "Settings"
    assert snap.link_id == "2"
    assert snap.archive_date == date(2026, 7, 4)
    assert snap.sequence == 2


def test_unresolved_history(tmp_path):
    d = device_settings_dir(tmp_path, None)
    _write(d / "Settings-2026-01-01.dat")

    hist = family_history(tmp_path, None, "Settings")
    assert len(hist) == 1
    assert hist[0].link_id is None


# ---------------------------------------------------------------------------
# latest_version
# ---------------------------------------------------------------------------

def test_latest_version_none_when_empty(tmp_path):
    assert latest_version(tmp_path, "1", "Settings") is None


def test_latest_version_is_first_history_item(tmp_path):
    d = device_settings_dir(tmp_path, "1")
    _write(d / "Settings-2026-01-01.bak")
    _write(d / "Settings-2026-01-01-2.bak")
    _write(d / "Settings-2026-02-01.bak")

    latest = latest_version(tmp_path, "1", "Settings")
    assert latest is not None
    assert latest.archive_date == date(2026, 2, 1)
    assert latest.path.name == "Settings-2026-02-01.bak"
