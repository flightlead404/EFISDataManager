# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the content-aware settings archiver (archiver.py).

``archive_efis_drive`` keeps a *chronological record of settings as they
change*, per Archived_Settings_Family (Settings / WP / Plan — State is NOT
archived). For each family it selects the current slot on the drive (higher
UPDATE= of the ``.bak`` / ``.dat`` pair), compares its Content_Hash against the
family's Most_Recent_Archive, and writes a new date-stamped snapshot only when
the content changed (or no archive exists). Source files are never modified.

These tests set up a temp "USB" mount directory with realistic GRT-format file
bodies, monkeypatch ``config.load_config`` (which archiver imports) to point
``archive_path`` at a temp archive dir, and inject the ``today`` date string
explicitly so Archive_Date ordering is deterministic.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import hashlib

import pytest

from efis_data_manager import archiver


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _grt_body(update: int, vne: int = 180, extra_sids: dict | None = None) -> str:
    """Build a realistic GRT settings-format body.

    Lines look like ``151=400``, ``UPDATE=n``, ``CHECKTYPE=...``, plus the
    recognized ``CHECKSIZE=`` / ``CHECKSUM=`` lines (excluded from the
    Content_Hash). ``vne`` and ``extra_sids`` let a test vary the *content*
    independently of the ``UPDATE=`` counter.
    """
    lines = [
        "CHECKTYPE=STD",
        "120=25",       # oil pressure min
        "151=400",      # max CHT
        "144=1550",     # max EGT
        f"22={vne}",    # Vne
        f"UPDATE={update}",
        "CHECKSIZE=1234",
        "CHECKSUM=ABCD",
    ]
    if extra_sids:
        for sid, value in extra_sids.items():
            lines.append(f"{sid}={value}")
    return "\n".join(lines) + "\n"


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


def _settings_dir(archive_root):
    return archive_root / "Settings"


# ---------------------------------------------------------------------------
# Baseline: first capture writes a dated snapshot per present family
# ---------------------------------------------------------------------------

def test_first_capture_writes_snapshot(env):
    """A family with no prior archive is written under the current date (Req 3.3)."""
    mount, archive_root = env
    (mount / "Settings.bak").write_text(_grt_body(update=1))

    results = archiver.archive_efis_drive(str(mount), today="2026-01-01")

    sdir = _settings_dir(archive_root)
    assert (sdir / "Settings-2026-01-01.bak").is_file()
    assert results["settings_copied"] == 1
    assert results["errors"] == []


# ---------------------------------------------------------------------------
# (a) unchanged family mounted on a LATER date is NOT re-archived (Req 3.4)
# ---------------------------------------------------------------------------

def test_unchanged_family_not_rearchived_on_later_date(env):
    """Identical content on a later date → skip (no new snapshot)."""
    mount, archive_root = env
    (mount / "Settings.bak").write_text(_grt_body(update=1))

    first = archiver.archive_efis_drive(str(mount), today="2026-01-01")
    assert first["settings_copied"] == 1

    # Same content, later date, UPDATE= bumped (UPDATE is excluded from hash).
    (mount / "Settings.bak").write_text(_grt_body(update=2))
    second = archiver.archive_efis_drive(str(mount), today="2026-02-01")

    sdir = _settings_dir(archive_root)
    assert second["settings_copied"] == 0
    assert (sdir / "Settings-2026-01-01.bak").is_file()
    assert not (sdir / "Settings-2026-02-01.bak").exists()
    # Exactly one Settings snapshot overall.
    assert len(list(sdir.glob("Settings-*.bak"))) == 1


# ---------------------------------------------------------------------------
# (b) a changed family IS archived under the new date (Req 3.3)
# ---------------------------------------------------------------------------

def test_changed_family_archived_on_new_date(env):
    """Changed content on a later date → new dated snapshot."""
    mount, archive_root = env
    (mount / "Settings.bak").write_text(_grt_body(update=1, vne=180))
    archiver.archive_efis_drive(str(mount), today="2026-01-01")

    # Content change (Vne differs), later date.
    (mount / "Settings.bak").write_text(_grt_body(update=2, vne=190))
    second = archiver.archive_efis_drive(str(mount), today="2026-02-01")

    sdir = _settings_dir(archive_root)
    assert second["settings_copied"] == 1
    assert (sdir / "Settings-2026-01-01.bak").is_file()
    assert (sdir / "Settings-2026-02-01.bak").is_file()


# ---------------------------------------------------------------------------
# (c) A→B→A across three dates yields THREE snapshots (Req 3.6)
# ---------------------------------------------------------------------------

def test_a_b_a_sequence_yields_three_snapshots(env):
    """An A→B→A content sequence records three chronological snapshots."""
    mount, archive_root = env
    sdir = _settings_dir(archive_root)

    # A
    (mount / "Settings.bak").write_text(_grt_body(update=1, vne=180))
    archiver.archive_efis_drive(str(mount), today="2026-01-01")
    # B (different content)
    (mount / "Settings.bak").write_text(_grt_body(update=2, vne=200))
    archiver.archive_efis_drive(str(mount), today="2026-01-02")
    # A again (same content as the first, but differs from most-recent B)
    (mount / "Settings.bak").write_text(_grt_body(update=3, vne=180))
    archiver.archive_efis_drive(str(mount), today="2026-01-03")

    snaps = sorted(p.name for p in sdir.glob("Settings-*.bak"))
    assert snaps == [
        "Settings-2026-01-01.bak",
        "Settings-2026-01-02.bak",
        "Settings-2026-01-03.bak",
    ]
    # First and third are byte-identical content, middle differs.
    first = (sdir / "Settings-2026-01-01.bak").read_text()
    third = (sdir / "Settings-2026-01-03.bak").read_text()
    second = (sdir / "Settings-2026-01-02.bak").read_text()
    # UPDATE= differs between the two A snapshots, but the config content
    # (SIDs) is identical — strip UPDATE= lines to compare content.
    def _no_update(body):
        return "\n".join(l for l in body.splitlines() if not l.startswith("UPDATE="))
    assert _no_update(first) == _no_update(third)
    assert _no_update(first) != _no_update(second)


# ---------------------------------------------------------------------------
# (d) same-date distinct change → -2 suffix, no clobber (Req 3.7)
# ---------------------------------------------------------------------------

def test_same_date_distinct_change_gets_suffix(env):
    """A second distinct change on the same date is written as ``-2`` (no clobber)."""
    mount, archive_root = env
    sdir = _settings_dir(archive_root)

    (mount / "Settings.bak").write_text(_grt_body(update=1, vne=180))
    archiver.archive_efis_drive(str(mount), today="2026-03-01")

    # Distinct change, SAME date.
    (mount / "Settings.bak").write_text(_grt_body(update=2, vne=195))
    results = archiver.archive_efis_drive(str(mount), today="2026-03-01")

    assert results["settings_copied"] == 1
    primary = sdir / "Settings-2026-03-01.bak"
    suffixed = sdir / "Settings-2026-03-01-2.bak"
    assert primary.is_file()
    assert suffixed.is_file()
    # The first snapshot was NOT overwritten (still the vne=180 content).
    assert "22=180" in primary.read_text()
    assert "22=195" in suffixed.read_text()


# ---------------------------------------------------------------------------
# (e) State is NEVER archived (Req 3.8)
# ---------------------------------------------------------------------------

def test_state_family_never_archived(env):
    """State.bak / State.dat present on the mount produce no State-* archive."""
    mount, archive_root = env
    (mount / "State.bak").write_text(_grt_body(update=1))
    (mount / "State.dat").write_text(_grt_body(update=2))
    # Also a legit family so the run does something.
    (mount / "WP.bak").write_text(_grt_body(update=1, extra_sids={"999": "1"}))

    archiver.archive_efis_drive(str(mount), today="2026-04-01")

    sdir = _settings_dir(archive_root)
    assert list(sdir.glob("State-*")) == []
    # WP was archived though.
    assert (sdir / "WP-2026-04-01.bak").is_file()


# ---------------------------------------------------------------------------
# (f) source files on the mount are unchanged (read-only, Req 3.2)
# ---------------------------------------------------------------------------

def test_source_files_unchanged(env):
    """The drive source files are byte-identical after archiving (read-only)."""
    mount, archive_root = env
    bak = mount / "Settings.bak"
    dat = mount / "Settings.dat"
    bak.write_text(_grt_body(update=1))
    dat.write_text(_grt_body(update=2, vne=190))
    before_bak = _sha256_bytes(bak.read_bytes())
    before_dat = _sha256_bytes(dat.read_bytes())

    archiver.archive_efis_drive(str(mount), today="2026-05-01")

    assert bak.is_file() and dat.is_file()
    assert _sha256_bytes(bak.read_bytes()) == before_bak
    assert _sha256_bytes(dat.read_bytes()) == before_dat


# ---------------------------------------------------------------------------
# (g) higher-UPDATE slot of a .bak/.dat pair is the one archived (Req 3.1)
# ---------------------------------------------------------------------------

def test_higher_update_slot_is_archived(env):
    """When both slots differ in UPDATE=, the higher-UPDATE content is archived."""
    mount, archive_root = env
    # .bak has lower UPDATE (vne=180); .dat has higher UPDATE (vne=200).
    (mount / "Settings.bak").write_text(_grt_body(update=5, vne=180))
    (mount / "Settings.dat").write_text(_grt_body(update=9, vne=200))

    results = archiver.archive_efis_drive(str(mount), today="2026-06-01")

    sdir = _settings_dir(archive_root)
    # The chosen slot was the .dat (higher UPDATE), so the snapshot has .dat ext.
    archived = sdir / "Settings-2026-06-01.dat"
    assert archived.is_file()
    assert "22=200" in archived.read_text()
    assert results["settings_copied"] == 1
    # No .bak snapshot was written (the .bak slot lost selection).
    assert not (sdir / "Settings-2026-06-01.bak").exists()
