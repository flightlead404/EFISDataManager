# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for cosmetic label building and identity-only drive detection.

Detection is identity-only (Req 10.2): a drive is "managed / ours" IFF it
carries a valid ``EFIS_DRIVE_ID.json`` at its root with
``kind == "efis-chart-drive"``. The volume label plays no role — the old
``EFIS``/``EFIS_N`` regex is gone. ``build_efis_label`` still produces a
cosmetic ``EFIS_<suffix>`` label for Finder convenience, but nothing keys on it.
``is_adoption_candidate`` is used only by Prepare Drive to spot a previously-
used GRT chart drive (GRTCHARTS/ or ChartData/ but no identity) for adopt-vs-
clean.
"""

import json
import os

import pytest

from efis_data_manager import usb_monitor as um


def _write_identity(mount, kind=um.IDENTITY_KIND):
    with open(os.path.join(mount, um.IDENTITY_FILENAME), "w") as f:
        json.dump({"schema_version": 1, "id": "abc", "kind": kind}, f)


# --- build_efis_label (cosmetic label) --------------------------------------


@pytest.mark.parametrize(
    "suffix,expected",
    [
        ("spare", "EFIS_SPARE"),
        ("SPARE", "EFIS_SPARE"),
        ("n1", "EFIS_N1"),
        ("2", "EFIS_2"),
        ("", "EFIS"),          # blank -> bare label, never a dangling "EFIS_"
        ("   ", "EFIS"),
        ("my drive 2", "EFIS_MYDRIV"),  # spaces stripped, upcased, 11-char cap
        ("a/b:c*d", "EFIS_ABCD"),       # FAT32-unsafe chars stripped
        # User typed the full current label instead of just the suffix: strip
        # the redundant EFIS_ prefix rather than double-prefixing.
        ("EFIS_1", "EFIS_1"),
        ("efis_1", "EFIS_1"),
        ("EFIS_SPARE", "EFIS_SPARE"),
        ("EFIS", "EFIS"),               # bare prefix alone -> bare label
        ("EFIS_EFIS_2", "EFIS_2"),      # doubled prefix collapses
    ],
)
def test_build_label(suffix, expected):
    assert um.build_efis_label(suffix) == expected


def test_built_labels_are_fat32_length_bounded():
    long = um.build_efis_label("verylongsuffixbeyondlimit")
    assert len(long) <= um.FAT32_LABEL_MAXLEN


# --- is_managed_drive (identity-only detection) -----------------------------


def test_managed_only_when_valid_identity_present(tmp_path):
    mp = tmp_path / "any-name"
    mp.mkdir()
    # No identity file -> not managed.
    assert um.is_managed_drive(str(mp)) is False
    # Valid identity file -> managed.
    _write_identity(str(mp))
    assert um.is_managed_drive(str(mp)) is True


def test_label_never_matches_without_identity(tmp_path):
    # A bare "EFIS_1" label with no identity file is NOT detected as managed:
    # the volume label plays no role in detection.
    mp = tmp_path / "EFIS_1"
    mp.mkdir()
    assert um.is_managed_drive(str(mp)) is False


def test_grtcharts_without_identity_not_managed(tmp_path):
    mp = tmp_path / "SOMESTICK"
    (mp / "GRTCHARTS").mkdir(parents=True)
    assert um.is_managed_drive(str(mp)) is False


def test_wrong_kind_identity_not_managed(tmp_path):
    mp = tmp_path / "vol"
    mp.mkdir()
    _write_identity(str(mp), kind="something-else")
    assert um.is_managed_drive(str(mp)) is False


def test_corrupt_identity_not_managed(tmp_path):
    mp = tmp_path / "vol"
    mp.mkdir()
    with open(mp / um.IDENTITY_FILENAME, "w") as f:
        f.write("{ not valid json")
    assert um.is_managed_drive(str(mp)) is False


# --- is_adoption_candidate (Prepare Drive only) -----------------------------


def test_adoption_candidate_for_grtcharts_without_identity(tmp_path):
    mp = tmp_path / "vol"
    (mp / "GRTCHARTS").mkdir(parents=True)
    assert um.is_adoption_candidate(str(mp)) is True


def test_adoption_candidate_for_chartdata_without_identity(tmp_path):
    mp = tmp_path / "vol"
    (mp / "ChartData").mkdir(parents=True)
    assert um.is_adoption_candidate(str(mp)) is True


def test_not_adoption_candidate_when_identity_present(tmp_path):
    mp = tmp_path / "vol"
    (mp / "GRTCHARTS").mkdir(parents=True)
    _write_identity(str(mp))
    # A managed drive is never an adoption candidate.
    assert um.is_adoption_candidate(str(mp)) is False


def test_not_adoption_candidate_for_blank_dir(tmp_path):
    mp = tmp_path / "vol"
    mp.mkdir()
    assert um.is_adoption_candidate(str(mp)) is False
