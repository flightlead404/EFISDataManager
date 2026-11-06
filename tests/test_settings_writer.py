# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the byte-preserving sole-slot settings writer.

``settings_writer.sole_slot_write`` writes an archived settings version onto a
drive as the SOLE slot of its family, reusing the archived file's original bytes
verbatim (checksum wall), verifying the written bytes by SHA-256, and only after
a passing verify removing the other ``.bak``/``.dat`` slot. ``restore_version``
is the Settings-only wrapper.

Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.9, 8.1, 8.2, 8.3
"""

import hashlib
import os

from efis_data_manager import settings_writer
from efis_data_manager.settings_writer import (
    WriteResult,
    restore_version,
    sole_slot_write,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grt_body(update: int, vne: int = 180) -> bytes:
    """A realistic GRT settings body with the volatile checksum lines."""
    lines = [
        "CHECKTYPE=STD",
        "151=400",
        f"22={vne}",
        f"UPDATE={update}",
        "CHECKSIZE=1234",
        "CHECKSUM=ABCD",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archived(tmp_path, ext=".bak", update=1, vne=180):
    """Write an archived snapshot file and return (path, original bytes)."""
    path = tmp_path / "archive" / f"Settings-2026-01-01{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _grt_body(update=update, vne=vne)
    path.write_bytes(body)
    return path, body


# ---------------------------------------------------------------------------
# Byte preservation (R5.3, R8.1, R8.3)
# ---------------------------------------------------------------------------

def test_written_slot_is_byte_identical_to_source(tmp_path):
    """The written drive slot is byte-for-byte equal to the archived source."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archived, body = _archived(tmp_path, ext=".bak")

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "written"
    written = mount / "Settings.bak"
    assert written.is_file()
    assert written.read_bytes() == body
    # Checksum-wall lines are physically untouched.
    assert b"UPDATE=1" in written.read_bytes()
    assert b"CHECKSUM=ABCD" in written.read_bytes()


def test_archived_source_is_not_modified(tmp_path):
    """The archived source file is unchanged after a write (read-only source)."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archived, body = _archived(tmp_path, ext=".dat")
    before = _sha256(archived.read_bytes())

    sole_slot_write(archived, str(mount), "Settings")

    assert _sha256(archived.read_bytes()) == before


# ---------------------------------------------------------------------------
# Written slot takes the archived file's own extension
# ---------------------------------------------------------------------------

def test_written_slot_uses_archived_extension_dat(tmp_path):
    """A .dat archived file writes a .dat slot on the drive."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archived, _ = _archived(tmp_path, ext=".dat")

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "written"
    assert result.written_path == str(mount / "Settings.dat")
    assert (mount / "Settings.dat").is_file()
    assert not (mount / "Settings.bak").exists()


# ---------------------------------------------------------------------------
# Sole slot: the other slot is removed after a verified write (R5.4)
# ---------------------------------------------------------------------------

def test_other_slot_removed_leaving_sole_slot(tmp_path):
    """Writing .bak removes a pre-existing .dat so exactly one slot remains."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    # Pre-existing OTHER-extension slot on the drive.
    (mount / "Settings.dat").write_bytes(_grt_body(update=99, vne=200))
    archived, body = _archived(tmp_path, ext=".bak")

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "written"
    assert (mount / "Settings.bak").read_bytes() == body
    assert not (mount / "Settings.dat").exists()
    assert result.removed_slots == [str(mount / "Settings.dat")]


def test_same_extension_slot_is_overwritten_not_reported_removed(tmp_path):
    """Writing the same extension overwrites in place; no 'other' slot removed."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    (mount / "Settings.bak").write_bytes(_grt_body(update=1, vne=111))
    archived, body = _archived(tmp_path, ext=".bak", vne=222)

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "written"
    assert (mount / "Settings.bak").read_bytes() == body
    assert result.removed_slots == []


# ---------------------------------------------------------------------------
# Write failure retains a valid slot (R5.6)
# ---------------------------------------------------------------------------

def test_verify_failure_unlinks_new_file_and_keeps_other_slot(tmp_path, monkeypatch):
    """On a byte-verify mismatch, the new file is removed and the other slot survives."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    pre_existing = _grt_body(update=42, vne=175)
    (mount / "Settings.dat").write_bytes(pre_existing)
    archived, _ = _archived(tmp_path, ext=".bak")

    # Force the verify to see different hashes for source vs target.
    real_hash = settings_writer._sha256_file
    target_path = str(mount / "Settings.bak")

    def fake_hash(path):
        if path == target_path:
            return "deadbeef"  # pretend the written bytes differ
        return real_hash(path)

    monkeypatch.setattr(settings_writer, "_sha256_file", fake_hash)

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "failed"
    assert "verify" in result.error
    # New file removed, pre-existing other slot intact and unchanged.
    assert not (mount / "Settings.bak").exists()
    assert (mount / "Settings.dat").read_bytes() == pre_existing


def test_copy_failure_does_not_touch_other_slot(tmp_path, monkeypatch):
    """A copy failure leaves the pre-existing other slot intact and reports failure."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    pre_existing = _grt_body(update=7, vne=160)
    (mount / "Settings.dat").write_bytes(pre_existing)
    archived, _ = _archived(tmp_path, ext=".bak")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(settings_writer.shutil, "copy2", boom)

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "failed"
    assert "copy failed" in result.error
    assert not (mount / "Settings.bak").exists()
    assert (mount / "Settings.dat").read_bytes() == pre_existing


# ---------------------------------------------------------------------------
# Unrecognized extension is rejected without writing
# ---------------------------------------------------------------------------

def test_unrecognized_extension_fails_without_writing(tmp_path):
    """An archived file without a .bak/.dat extension is rejected."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    (mount / "Settings.dat").write_bytes(_grt_body(update=1))
    bad = tmp_path / "Settings-2026-01-01.txt"
    bad.write_bytes(_grt_body(update=2))

    result = sole_slot_write(bad, str(mount), "Settings")

    assert result.status == "failed"
    assert result.written_path is None
    # Nothing on the drive was changed.
    assert (mount / "Settings.dat").is_file()


# ---------------------------------------------------------------------------
# restore_version is the Settings-only wrapper (R5.9)
# ---------------------------------------------------------------------------

def test_restore_version_writes_settings_slot(tmp_path):
    """restore_version writes the Settings family via sole_slot_write."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archived, body = _archived(tmp_path, ext=".bak")

    result = restore_version(archived, str(mount))

    assert isinstance(result, WriteResult)
    assert result.status == "written"
    assert (mount / "Settings.bak").read_bytes() == body


def test_no_slots_present_still_writes_sole_slot(tmp_path):
    """With no pre-existing slots, a verified write leaves exactly the new slot."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    archived, body = _archived(tmp_path, ext=".dat")

    result = sole_slot_write(archived, str(mount), "Settings")

    assert result.status == "written"
    assert result.removed_slots == []
    slots = sorted(p.name for p in mount.iterdir())
    assert slots == ["Settings.dat"]


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

import tempfile

from hypothesis import given, settings as hyp_settings, strategies as st


# The four initial drive slot states Property 6 must hold across: neither slot
# present, only the .bak slot, only the .dat slot, or both slots present.
_INITIAL_SLOT_STATES = st.sampled_from(("none", "bak", "dat", "both"))


@hyp_settings(max_examples=100)
@given(
    initial=_INITIAL_SLOT_STATES,
    written_ext=st.sampled_from([".bak", ".dat"]),
    update=st.integers(min_value=0, max_value=10_000),
    vne=st.integers(min_value=100, max_value=300),
)
def test_sole_slot_write_leaves_exactly_one_slot(initial, written_ext, update, vne):
    """# Feature: settings-management, Property 6

    For any initial drive slot state (neither slot, only .bak, only .dat, or
    both) and any written extension, after a successful ``sole_slot_write``
    exactly the written extension exists for the family on the drive and the
    other slot is absent.

    Validates: Requirements 5.4
    """
    other_ext = ".dat" if written_ext == ".bak" else ".bak"

    # Fresh temp dirs per generated example sidestep the Hypothesis +
    # function-scoped-fixture reuse pitfall.
    with tempfile.TemporaryDirectory() as mount_dir, \
            tempfile.TemporaryDirectory() as arch_dir:
        mount = os.path.join(mount_dir, "EFIS")
        os.mkdir(mount)

        # Seed the drive with the arbitrary initial slot state. The seeded
        # content is intentionally different from the archived source so a
        # written .bak/.dat is a genuine replacement, not a coincidental match.
        if initial in ("bak", "both"):
            with open(os.path.join(mount, "Settings.bak"), "wb") as fh:
                fh.write(_grt_body(update=1, vne=111))
        if initial in ("dat", "both"):
            with open(os.path.join(mount, "Settings.dat"), "wb") as fh:
                fh.write(_grt_body(update=2, vne=222))

        # The archived source carries the written extension and its own bytes.
        archived = os.path.join(arch_dir, f"Settings-2026-01-01{written_ext}")
        body = _grt_body(update=update, vne=vne)
        with open(archived, "wb") as fh:
            fh.write(body)

        result = sole_slot_write(archived, mount, "Settings")

        assert result.status == "written"

        written_slot = os.path.join(mount, f"Settings{written_ext}")
        other_slot = os.path.join(mount, f"Settings{other_ext}")

        # Exactly the written extension exists and holds the archived bytes.
        assert os.path.isfile(written_slot)
        with open(written_slot, "rb") as fh:
            assert fh.read() == body
        # The other slot is absent.
        assert not os.path.exists(other_slot)
        # Exactly one Settings slot remains on the drive.
        settings_slots = sorted(
            name for name in os.listdir(mount)
            if name in ("Settings.bak", "Settings.dat")
        )
        assert settings_slots == [f"Settings{written_ext}"]


# ---------------------------------------------------------------------------
# Property 5: Sole-slot write preserves original bytes
# ---------------------------------------------------------------------------
#
# Feature: settings-management, Property 5
#
# Design Property 5: For any archived settings file, after ``sole_slot_write``
# the written drive slot is byte-identical to the archived source, so the
# ``UPDATE=``, ``CHECKSIZE=``, and ``CHECKSUM=`` values are unchanged.
#
# Validates: Requirements 5.3, 8.1, 8.3

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _archived_body(update, checksize, checksum, extra_lines):
    """Build arbitrary archived settings bytes INCLUDING the volatile lines.

    The body is assembled from Hypothesis-chosen SID/value content plus the
    three volatile keys the checksum wall forbids us from touching
    (``UPDATE=`` / ``CHECKSIZE=`` / ``CHECKSUM=``). These are embedded verbatim
    as bytes so the property can assert they survive the write unchanged.
    """
    lines = [b"CHECKTYPE=STD"]
    lines.extend(extra_lines)
    lines.append(b"UPDATE=" + str(update).encode("ascii"))
    lines.append(b"CHECKSIZE=" + str(checksize).encode("ascii"))
    lines.append(b"CHECKSUM=" + checksum)
    return b"\n".join(lines) + b"\n"


# A single "SID=value" content line with no newline/control bytes so the body
# stays a well-formed, line-oriented settings file while still varying widely.
_sid_value_line = st.builds(
    lambda sid, val: str(sid).encode("ascii") + b"=" + val,
    st.integers(min_value=0, max_value=9999),
    st.binary(min_size=0, max_size=16).filter(lambda b: b"\n" not in b and b"\r" not in b),
)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    ext=st.sampled_from([".bak", ".dat"]),
    update=st.integers(min_value=0, max_value=2**31),
    checksize=st.integers(min_value=0, max_value=2**31),
    checksum=st.binary(min_size=1, max_size=8).filter(
        lambda b: b"\n" not in b and b"\r" not in b
    ),
    extra_lines=st.lists(_sid_value_line, min_size=0, max_size=12),
)
def test_sole_slot_write_preserves_original_bytes(
    ext, update, checksize, checksum, extra_lines
):
    """For ANY archived settings bytes/extension, the written slot is identical.

    Generates arbitrary archived file byte contents (including the volatile
    UPDATE/CHECKSIZE/CHECKSUM lines) with an arbitrary ``.bak``/``.dat``
    extension, writes it to a fresh temp drive via ``sole_slot_write``, and
    asserts the written slot is byte-identical to the archived source — so the
    checksum-wall lines are physically unchanged (R5.3, R8.1, R8.3).

    A fresh ``TemporaryDirectory`` per example gives each generated input an
    isolated archive + drive, so one example never observes another's slots.
    """
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        archive_dir = root_path / "archive"
        archive_dir.mkdir()
        mount = root_path / "EFIS"
        mount.mkdir()

        body = _archived_body(update, checksize, checksum, extra_lines)
        archived = archive_dir / f"Settings-2026-01-01{ext}"
        archived.write_bytes(body)

        result = sole_slot_write(archived, str(mount), "Settings")

        assert result.status == "written"
        written = mount / f"Settings{ext}"
        assert written.is_file()
        # Byte-identical to the archived source (R8.3).
        assert written.read_bytes() == body
        # The archived source itself is untouched (read-only source).
        assert archived.read_bytes() == body
        # The volatile checksum-wall lines survive verbatim (R5.3, R8.1).
        assert b"UPDATE=" + str(update).encode("ascii") in written.read_bytes()
        assert b"CHECKSIZE=" + str(checksize).encode("ascii") in written.read_bytes()
        assert b"CHECKSUM=" + checksum in written.read_bytes()

# ---------------------------------------------------------------------------
# Property 7: Write failure retains a valid slot (R5.6)
# ---------------------------------------------------------------------------
#
# Feature: settings-management, Property 7
#
# For any drive with a pre-existing settings slot, if the post-write byte verify
# fails, the pre-existing other slot survives byte-unchanged and the result
# status is "failed".
#
# Validates: Requirements 5.6

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


# The archived snapshot is written with the OPPOSITE extension to the drive's
# pre-existing slot, so the write targets the empty slot and the pre-existing
# "other" slot is the one whose survival Property 7 asserts. Each generated
# example gets its own isolated temp tree (Hypothesis does not replay
# function-scoped fixtures across examples).
@settings(max_examples=100)
@given(
    # Arbitrary pre-existing other-slot contents (any bytes, possibly empty).
    pre_existing=st.binary(min_size=0, max_size=4096),
    # Arbitrary archived snapshot bytes to attempt to write.
    archived_body=st.binary(min_size=0, max_size=4096),
    # Which extension the pre-existing slot occupies; the archived file (and
    # thus the write target) takes the complementary extension.
    pre_ext=st.sampled_from([".bak", ".dat"]),
)
def test_property_7_verify_failure_retains_pre_existing_slot(
    pre_existing, archived_body, pre_ext
):
    """Property 7: a forced verify failure keeps the pre-existing slot intact.

    Feature: settings-management, Property 7
    Validates: Requirements 5.6
    """
    target_ext = ".dat" if pre_ext == ".bak" else ".bak"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mount = root / "EFIS"
        mount.mkdir()

        # Pre-existing OTHER-extension slot on the drive.
        pre_slot = mount / f"Settings{pre_ext}"
        pre_slot.write_bytes(pre_existing)

        # Archived snapshot carries the complementary extension, so the write
        # targets the empty slot and never overwrites the pre-existing one.
        archived = root / "archive" / f"Settings-2026-01-01{target_ext}"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(archived_body)

        # Force the post-write byte verify to see a mismatching hash for the
        # written target, mirroring the example test's monkeypatch pattern.
        target_path = str(mount / f"Settings{target_ext}")
        real_hash = settings_writer._sha256_file

        def fake_hash(path, _target=target_path, _real=real_hash):
            if path == _target:
                return "deadbeef"  # pretend the written bytes differ
            return _real(path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings_writer, "_sha256_file", fake_hash)
            result = sole_slot_write(archived, str(mount), "Settings")

        # The write is reported failed and names the verify mismatch.
        assert result.status == "failed"
        assert result.error is not None
        assert "verify" in result.error

        # The forced-failure target was unlinked; it is not presented as current.
        assert not (mount / f"Settings{target_ext}").exists()

        # The pre-existing other slot survives byte-for-byte unchanged (R5.6):
        # the drive always retains a valid, loadable slot after a failed write.
        assert pre_slot.is_file()
        assert pre_slot.read_bytes() == pre_existing


# ---------------------------------------------------------------------------
# Property 8: Population idempotency (R6.1, R6.2)
# ---------------------------------------------------------------------------
#
# Feature: settings-management, Property 8
#
# Design Property 8: For any device and bound drive, ``populate_latest``
# performs no write when the drive already carries content equal to the
# device's latest archived version (equal Content_Hash -> "skipped-equal"),
# and performs exactly one write otherwise (status "written", the drive now
# carries the archived bytes as its sole Settings slot). Immediately re-running
# ``populate_latest`` after a "written" result is a no-op ("skipped-equal").
#
# Validates: Requirements 6.1, 6.2

import tempfile
from pathlib import Path

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from efis_data_manager import settings_writer
from efis_data_manager.efis_settings import Settings_Parser, content_hash
from efis_data_manager.settings_writer import populate_latest


# A content SID line "SID=value" with no newline/CR so the body stays a
# well-formed, line-oriented settings file. The SID is drawn from a modest pool
# so distinct content vs. equal content can both occur across examples.
_content_line = st.builds(
    lambda sid, val: f"{sid}={val}",
    st.integers(min_value=0, max_value=999),
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=0,
        max_size=12,
    ),
)


def _settings_body(content_lines, update, checksize="1234", checksum="ABCD"):
    """Assemble GRT settings bytes from content lines + the volatile keys.

    The three volatile keys (UPDATE/CHECKSIZE/CHECKSUM) are excluded from
    Content_Hash by key name, so varying ``update`` independently of the content
    lines lets the test exercise "same content, different UPDATE=" cases.
    """
    lines = ["CHECKTYPE=STD"]
    lines.extend(content_lines)
    lines.append(f"UPDATE={update}")
    lines.append(f"CHECKSIZE={checksize}")
    lines.append(f"CHECKSUM={checksum}")
    return ("\n".join(lines) + "\n").encode("ascii")


def _write_archived(archive_root, link_id, body, ext):
    """Write a latest archived Settings snapshot for ``link_id`` and return it."""
    device_dir = Path(archive_root) / "Settings" / f"link{link_id}"
    device_dir.mkdir(parents=True, exist_ok=True)
    path = device_dir / f"Settings-2026-01-01{ext}"
    path.write_bytes(body)
    return path


@hyp_settings(max_examples=100)
@given(
    link_id=st.integers(min_value=1, max_value=50).map(str),
    archived_lines=st.lists(_content_line, min_size=0, max_size=10),
    archived_ext=st.sampled_from([".bak", ".dat"]),
    archived_update=st.integers(min_value=0, max_value=10_000),
    # Drive seeding: "equal" carries content with the SAME Content_Hash as the
    # archived version (but an independently chosen UPDATE=); "differ" carries
    # different content; "empty" carries no slot at all.
    drive_state=st.sampled_from(("equal", "differ", "empty")),
    drive_slot_ext=st.sampled_from([".bak", ".dat"]),
    drive_update=st.integers(min_value=0, max_value=10_000),
    differ_lines=st.lists(_content_line, min_size=1, max_size=10),
)
def test_populate_latest_idempotency(
    link_id,
    archived_lines,
    archived_ext,
    archived_update,
    drive_state,
    drive_slot_ext,
    drive_update,
    differ_lines,
):
    """# Feature: settings-management, Property 8

    For any device/drive, ``populate_latest`` is a no-op ("skipped-equal") when
    the drive already carries content equal to the latest archived version
    (equal Content_Hash, regardless of UPDATE=), and performs exactly one write
    ("written", archived bytes become the sole Settings slot) otherwise. A write
    is immediately idempotent: re-running yields "skipped-equal".

    Validates: Requirements 6.1, 6.2
    """
    # Fresh temp tree per example — no cross-example slot/archive bleed-through.
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        archive_root = root_path / "archive"
        archive_root.mkdir()
        mount = root_path / "EFIS"
        mount.mkdir()

        archived_body = _settings_body(archived_lines, update=archived_update)
        archived_path = _write_archived(
            archive_root, link_id, archived_body, archived_ext
        )
        archived_hash = content_hash(Settings_Parser.parse(str(archived_path)))

        # Seed the drive's Settings slot according to the generated state.
        if drive_state == "equal":
            # Same content lines (same Content_Hash) but a possibly different
            # UPDATE= counter and possibly a different slot extension/bytes.
            equal_body = _settings_body(archived_lines, update=drive_update)
            drive_slot = mount / f"Settings{drive_slot_ext}"
            drive_slot.write_bytes(equal_body)
            # Sanity: the seeded slot truly has the same Content_Hash.
            assert (
                content_hash(Settings_Parser.parse(str(drive_slot))) == archived_hash
            )
        elif drive_state == "differ":
            # Content guaranteed to differ: append a marker line absent from the
            # archived content, so the Content_Hash cannot coincide.
            diff_lines = list(differ_lines) + ["424242=__property8_marker__"]
            differ_body = _settings_body(diff_lines, update=drive_update)
            drive_slot = mount / f"Settings{drive_slot_ext}"
            drive_slot.write_bytes(differ_body)
            assert (
                content_hash(Settings_Parser.parse(str(drive_slot))) != archived_hash
            )
        # "empty" -> no slot written at all.

        # Snapshot the drive's slot bytes before populate, to assert no-write.
        before = {
            name: (mount / name).read_bytes()
            for name in ("Settings.bak", "Settings.dat")
            if (mount / name).is_file()
        }

        result = populate_latest(str(archive_root), link_id, str(mount))

        if drive_state == "equal":
            # Already current: no write, bytes unchanged (R6.1/R6.2).
            assert result.status == "skipped-equal"
            after = {
                name: (mount / name).read_bytes()
                for name in ("Settings.bak", "Settings.dat")
                if (mount / name).is_file()
            }
            assert after == before
        else:
            # Differing or empty: exactly one write; the archived bytes become
            # the sole Settings slot on the drive.
            assert result.status == "written"
            written_slot = mount / f"Settings{archived_ext}"
            assert written_slot.is_file()
            assert written_slot.read_bytes() == archived_body
            # Exactly one Settings slot remains (sole-slot write).
            remaining = sorted(
                name for name in ("Settings.bak", "Settings.dat")
                if (mount / name).is_file()
            )
            assert remaining == [f"Settings{archived_ext}"]

            # Re-running immediately is idempotent: now the drive carries the
            # archived content, so populate is a "skipped-equal" no-op (R6.2).
            rerun_before = written_slot.read_bytes()
            rerun = populate_latest(str(archive_root), link_id, str(mount))
            assert rerun.status == "skipped-equal"
            assert written_slot.read_bytes() == rerun_before
            rerun_remaining = sorted(
                name for name in ("Settings.bak", "Settings.dat")
                if (mount / name).is_file()
            )
            assert rerun_remaining == [f"Settings{archived_ext}"]


def test_populate_latest_skips_when_no_archive(tmp_path):
    """No archived version for the device -> 'skipped-none', drive untouched."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    mount = tmp_path / "EFIS"
    mount.mkdir()
    (mount / "Settings.bak").write_bytes(_grt_body(update=1, vne=180))
    before = (mount / "Settings.bak").read_bytes()

    result = populate_latest(str(archive_root), "7", str(mount))

    assert result.status == "skipped-none"
    assert (mount / "Settings.bak").read_bytes() == before
    assert not (mount / "Settings.dat").exists()


# ---------------------------------------------------------------------------
# Property 9: Divergence is a pure hash mismatch (R7.1, R7.4, R7.5)
# ---------------------------------------------------------------------------
#
# Feature: settings-management, Property 9
#
# Design Property 9: For any pair of (drive current Settings content, latest
# archived version), ``diverges`` is True exactly when their Content_Hashes
# differ, and the decision never consults file timestamps or UPDATE_Value.
#
# Validates: Requirements 7.1, 7.4, 7.5

import os as _os
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.efis_settings import Settings_Parser, content_hash
from efis_data_manager.settings_writer import diverges


def _settings_bytes(sid_pairs, update, checksize, checksum):
    """Build a GRT settings body from SID content plus the volatile lines.

    ``sid_pairs`` are the Content_Hash-relevant ``SID=value`` lines; ``update``,
    ``checksize`` and ``checksum`` populate the three volatile keys the hash
    excludes. Lines are emitted in insertion order so content order never
    affects the (order-independent) Content_Hash.
    """
    lines = [f"{sid}={value}" for sid, value in sid_pairs]
    lines.append(f"UPDATE={update}")
    lines.append(f"CHECKSIZE={checksize}")
    lines.append(f"CHECKSUM={checksum}")
    return ("\n".join(lines) + "\n").encode("ascii")


# One "SID=value" content pair, both ASCII tokens with no "=" / newline so the
# parsed SID content is well-formed and varies widely. SIDs avoid the three
# excluded keys so generated content is genuinely hash-relevant.
_sid = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=90),
    min_size=1,
    max_size=6,
).filter(lambda s: s not in ("UPDATE", "CHECKSIZE", "CHECKSUM") and "=" not in s)
_val = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=90),
    min_size=0,
    max_size=8,
)
# A dict keeps SIDs unique (last value wins), matching the parser's own
# dict semantics, so a generated body has a well-defined Content_Hash.
_sid_content = st.dictionaries(_sid, _val, min_size=0, max_size=10).map(
    lambda d: sorted(d.items())
)

# Arbitrary, independent volatile values for the archived vs drive files, so a
# match/mismatch of the volatile lines is decoupled from the SID content.
_update = st.integers(min_value=0, max_value=2**31)
_checksize = st.integers(min_value=0, max_value=2**31)
_checksum = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=70),
    min_size=1,
    max_size=8,
)


def _write_archive(device_dir, content, update, checksize, checksum, ext=".bak"):
    """Write a dated archived Settings snapshot into the device dir."""
    device_dir.mkdir(parents=True, exist_ok=True)
    path = device_dir / f"Settings-2026-01-01{ext}"
    path.write_bytes(_settings_bytes(content, update, checksize, checksum))
    return path


def _write_drive_slot(mount, content, update, checksize, checksum, mtime, ext=".bak"):
    """Write the drive's current Settings slot and randomize its mtime."""
    slot = mount / f"Settings{ext}"
    slot.write_bytes(_settings_bytes(content, update, checksize, checksum))
    # Randomized mtime proves the decision never consults file timestamps.
    _os.utime(slot, (mtime, mtime))
    return slot


@settings(max_examples=100)
@given(
    archived_content=_sid_content,
    drive_content=_sid_content,
    arch_update=_update,
    arch_checksize=_checksize,
    arch_checksum=_checksum,
    drive_update=_update,
    drive_checksize=_checksize,
    drive_checksum=_checksum,
    drive_mtime=st.integers(min_value=0, max_value=2_000_000_000),
    link_id=st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=57),
        min_size=1,
        max_size=6,
    ),
)
def test_divergence_is_pure_hash_mismatch(
    archived_content,
    drive_content,
    arch_update,
    arch_checksize,
    arch_checksum,
    drive_update,
    drive_checksize,
    drive_checksum,
    drive_mtime,
    link_id,
):
    """# Feature: settings-management, Property 9

    For arbitrary archived Settings content and arbitrary drive-slot content,
    ``diverges`` reports ``diverged=True`` exactly when the two Content_Hashes
    differ. The archived and drive files carry independent, randomized
    UPDATE=/CHECKSIZE=/CHECKSUM= values and the drive slot a randomized mtime,
    so the equality of the verdict with the pure hash comparison proves the
    decision never consults timestamps or UPDATE_Value (R7.1, R7.4, R7.5).

    Validates: Requirements 7.1, 7.4, 7.5
    """
    from efis_data_manager.settings_archive import device_settings_dir

    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        archive_root = root_path / "archive"
        mount = root_path / "EFIS"
        mount.mkdir()

        device_dir = device_settings_dir(archive_root, link_id)
        archived_path = _write_archive(
            device_dir, archived_content, arch_update, arch_checksize, arch_checksum
        )
        drive_slot = _write_drive_slot(
            mount,
            drive_content,
            drive_update,
            drive_checksize,
            drive_checksum,
            drive_mtime,
        )

        # The oracle: a pure Content_Hash comparison (UPDATE/CHECKSIZE/CHECKSUM
        # excluded), computed independently of the file timestamps.
        expected = content_hash(
            Settings_Parser.parse(str(drive_slot))
        ) != content_hash(Settings_Parser.parse(str(archived_path)))

        result = diverges(archive_root, link_id, str(mount))

        assert result.diverged == expected
        # When a comparison happened, the baseline's Archive_Date is reported
        # and the check stays scoped to the requested device.
        assert result.latest_archive_date is not None
        assert result.link_id == link_id


@settings(max_examples=100)
@given(
    base_content=_sid_content,
    arch_update=_update,
    arch_checksize=_checksize,
    arch_checksum=_checksum,
    drive_update=_update,
    drive_checksize=_checksize,
    drive_checksum=_checksum,
    drive_mtime=st.integers(min_value=0, max_value=2_000_000_000),
)
def test_divergence_ignores_volatile_lines_and_mtime(
    base_content,
    arch_update,
    arch_checksize,
    arch_checksum,
    drive_update,
    drive_checksize,
    drive_checksum,
    drive_mtime,
):
    """# Feature: settings-management, Property 9

    A drive slot that shares the archived version's SID content but differs ONLY
    in UPDATE=/CHECKSIZE=/CHECKSUM= (and carries an arbitrary mtime) is NOT
    diverged. Identical SID content with independently randomized volatile lines
    and timestamp must never read as divergence, proving those signals are never
    consulted (R7.4, R7.5).

    Validates: Requirements 7.4, 7.5
    """
    from efis_data_manager.settings_archive import device_settings_dir

    link_id = "7"
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        archive_root = root_path / "archive"
        mount = root_path / "EFIS"
        mount.mkdir()

        device_dir = device_settings_dir(archive_root, link_id)
        _write_archive(
            device_dir, base_content, arch_update, arch_checksize, arch_checksum
        )
        # Same SID content, different volatile lines, randomized mtime.
        _write_drive_slot(
            mount, base_content, drive_update, drive_checksize, drive_checksum, drive_mtime
        )

        result = diverges(archive_root, link_id, str(mount))

        assert result.diverged is False


@settings(max_examples=100)
@given(
    content=_sid_content,
    update=_update,
    checksize=_checksize,
    checksum=_checksum,
    present=st.sampled_from(("archive_only", "drive_only")),
)
def test_divergence_false_when_a_side_is_missing(
    content, update, checksize, checksum, present
):
    """# Feature: settings-management, Property 9

    Divergence short-circuits to ``False`` when either side of the comparison is
    absent: no latest archived version (no baseline) or no drive slot (nothing
    to compare) both yield ``diverged=False`` (R7.1 short-circuit).

    Validates: Requirements 7.1
    """
    from efis_data_manager.settings_archive import device_settings_dir

    link_id = "3"
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        archive_root = root_path / "archive"
        mount = root_path / "EFIS"
        mount.mkdir()

        if present == "archive_only":
            # Baseline exists, drive carries no Settings slot.
            device_dir = device_settings_dir(archive_root, link_id)
            _write_archive(device_dir, content, update, checksize, checksum)
        else:
            # Drive slot exists, no archived baseline for the device.
            _write_drive_slot(mount, content, update, checksize, checksum, 1_000_000)

        result = diverges(archive_root, link_id, str(mount))

        assert result.diverged is False


# ---------------------------------------------------------------------------
# DivergenceResult.matched_archive_date (task 10.1, R7)
# ---------------------------------------------------------------------------

from datetime import date as _date


def _snap(device_dir, stamp, content, update=1, ext=".bak"):
    device_dir.mkdir(parents=True, exist_ok=True)
    p = device_dir / f"Settings-{stamp}{ext}"
    p.write_bytes(_settings_bytes(content, update, 10, "AB"))
    return p


def _matched_setup(tmp_path, drive_content):
    device_dir = tmp_path / "archive" / "Settings" / "link7"
    mount = tmp_path / "mount"
    mount.mkdir()
    _write_drive_slot(mount, drive_content, 99, 10, "AB", 1_000_000)
    return tmp_path / "archive", device_dir, mount


_CARD = [("A", "1")]
_OTHER = [("A", "2")]
_THIRD = [("A", "3")]


def test_matched_archive_date_older_snapshot(tmp_path):
    root, d, mount = _matched_setup(tmp_path, _CARD)
    _snap(d, "2026-01-01", _CARD)
    _snap(d, "2026-02-01", _OTHER)
    _snap(d, "2026-03-01", _THIRD)  # latest
    result = diverges(root, "7", str(mount))
    assert result.diverged is True
    assert result.latest_archive_date == _date(2026, 3, 1)
    assert result.matched_archive_date == _date(2026, 1, 1)


def test_matched_archive_date_most_recent_match_wins(tmp_path):
    root, d, mount = _matched_setup(tmp_path, _CARD)
    _snap(d, "2026-01-01", _CARD)
    _snap(d, "2026-02-01", _CARD, update=500)  # different UPDATE, same content
    _snap(d, "2026-03-01", _OTHER)
    _snap(d, "2026-04-01", _THIRD)  # latest
    result = diverges(root, "7", str(mount))
    assert result.matched_archive_date == _date(2026, 2, 1)


def test_matched_archive_date_none_when_no_match(tmp_path):
    root, d, mount = _matched_setup(tmp_path, _CARD)
    _snap(d, "2026-01-01", _OTHER)
    _snap(d, "2026-02-01", _THIRD)
    result = diverges(root, "7", str(mount))
    assert result.diverged is True
    assert result.matched_archive_date is None


def test_matched_archive_date_skips_unparseable_snapshot(tmp_path):
    root, d, mount = _matched_setup(tmp_path, _CARD)
    _snap(d, "2026-01-01", _CARD)
    (d / "Settings-2026-02-01.bak").write_bytes(b"not a settings file\n")
    _snap(d, "2026-03-01", _OTHER)  # latest
    result = diverges(root, "7", str(mount))
    assert result.diverged is True
    assert result.matched_archive_date == _date(2026, 1, 1)


def test_matched_archive_date_none_when_not_diverged(tmp_path):
    root, d, mount = _matched_setup(tmp_path, _CARD)
    _snap(d, "2026-01-01", _CARD)
    _snap(d, "2026-02-01", _CARD)  # latest equals card
    result = diverges(root, "7", str(mount))
    assert result.diverged is False
    assert result.matched_archive_date is None
