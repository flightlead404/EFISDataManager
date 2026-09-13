# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for archiver .dat settings copying (archiver.py).

``archive_efis_drive`` was extended so that, alongside the existing ``.bak``
settings files, it also copies the GRT ``.dat`` settings siblings
(Settings.dat / State.dat / WP.dat / Plan.dat) into ``<archive>/Settings/``
with a date stamp, using ``_copy_with_datestamp`` (copy-only, size-based skip).

These tests set up a temp "USB" mount directory with known bytes, monkeypatch
``config.load_config`` (which archiver imports) to point ``archive_path`` at a
temp archive dir, and assert:

  (a) a date-stamped ``.dat`` copy lands under ``<archive>/Settings/``,
  (b) the source file on the USB is byte-identical afterwards (copy, not move),
  (c) a second run skips the already-present same-size dated copy.

Requirements: 3.1, 3.2, 3.3
"""

import hashlib
from datetime import datetime

import pytest

from efis_data_manager import archiver


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dated(name: str) -> str:
    """Return the date-stamped archive name for a source file name."""
    base, ext = name.rsplit(".", 1)
    today = datetime.now().strftime("%Y-%m-%d")
    return f"{base}-{today}.{ext}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp USB mount + temp archive dir with load_config monkeypatched.

    archiver.py does ``from efis_data_manager.config import load_config`` at
    module import, so we patch the name in the archiver namespace.
    """
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archive_root = tmp_path / "archive"
    archive_root.mkdir()

    def fake_load_config():
        return {"archive_path": str(archive_root)}

    monkeypatch.setattr(archiver, "load_config", fake_load_config)
    return mount, archive_root


# ---------------------------------------------------------------------------
# (a) dated .dat copies appear under <archive>/Settings/
# ---------------------------------------------------------------------------

def test_dat_files_copied_with_datestamp(env):
    """Settings.dat / State.dat land in the Settings archive with a date stamp."""
    mount, archive_root = env
    (mount / "Settings.dat").write_bytes(b"SID stuff for settings\n")
    (mount / "State.dat").write_bytes(b"state payload bytes\n")

    results = archiver.archive_efis_drive(str(mount))

    settings_dir = archive_root / "Settings"
    assert (settings_dir / _dated("Settings.dat")).is_file()
    assert (settings_dir / _dated("State.dat")).is_file()
    # Two .dat files copied (no .bak present in this scenario).
    assert results["settings_copied"] == 2
    assert results["errors"] == []


def test_bak_and_dat_copied_together(env):
    """A Settings.bak and Settings.dat pair are both archived (Req 3.1)."""
    mount, archive_root = env
    (mount / "Settings.bak").write_bytes(b"bak content\n")
    (mount / "Settings.dat").write_bytes(b"dat content\n")

    results = archiver.archive_efis_drive(str(mount))

    settings_dir = archive_root / "Settings"
    assert (settings_dir / _dated("Settings.bak")).is_file()
    assert (settings_dir / _dated("Settings.dat")).is_file()
    assert results["settings_copied"] == 2


# ---------------------------------------------------------------------------
# (b) the source on the USB is unchanged — copy, not move (Req 3.2)
# ---------------------------------------------------------------------------

def test_source_dat_unchanged_after_archive(env):
    """The .dat source on the USB is byte-identical afterwards (read-only)."""
    mount, archive_root = env
    payload = b"immutable settings dat payload \x00\x01\x02\n"
    src = mount / "Settings.dat"
    src.write_bytes(payload)
    before = _sha256_bytes(src.read_bytes())

    archiver.archive_efis_drive(str(mount))

    # Source still present and byte-identical (copy, not move).
    assert src.is_file()
    after = _sha256_bytes(src.read_bytes())
    assert after == before
    # And the archived copy matches the source content.
    archived = archive_root / "Settings" / _dated("Settings.dat")
    assert _sha256_bytes(archived.read_bytes()) == before


# ---------------------------------------------------------------------------
# (c) a pre-existing same-size dated copy is skipped on re-run (Req 3.3)
# ---------------------------------------------------------------------------

def test_second_run_skips_existing_dated_dat(env):
    """Running archive twice skips the already-present same-size dated copy."""
    mount, archive_root = env
    (mount / "Settings.dat").write_bytes(b"same size payload here\n")
    (mount / "State.dat").write_bytes(b"another payload value!\n")

    first = archiver.archive_efis_drive(str(mount))
    assert first["settings_copied"] == 2
    first_skipped = first["skipped"]

    # Second run: same source files (same size) -> both skipped, no new copies.
    second = archiver.archive_efis_drive(str(mount))
    assert second["settings_copied"] == 0
    assert second["skipped"] == first_skipped + 2

    # Exactly one dated copy per source (no duplicate created).
    settings_dir = archive_root / "Settings"
    assert len(list(settings_dir.glob("Settings-*.dat"))) == 1
    assert len(list(settings_dir.glob("State-*.dat"))) == 1
