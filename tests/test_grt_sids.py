# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for grt_sids: the GRT SID dictionary (settings-management task 17.1)."""

from efis_data_manager import grt_sids
from efis_data_manager.efis_settings import Settings_Parser
from efis_data_manager.grt_sids import all_sids, sid_info, value_meaning


# --- counts / namespace hygiene -------------------------------------------

def test_no_state_ids_and_no_retired_ids():
    sids = all_sids()
    assert all(i.grt_name.startswith("SID_") for i in sids.values())
    assert not any("STID" in i.grt_name for i in sids.values())
    # Retired (//#define) ids are absent.
    for retired in ("336", "352", "466", "515", "1453"):
        assert retired not in sids
    # A State-file-only id (STID_PDA360_STATE 114) must not appear via STID.
    assert all(i.grt_name != "STID_PDA360_STATE" for i in sids.values())


def test_every_sid_key_is_its_own_id_and_unique():
    sids = all_sids()
    assert all(k == i.sid for k, i in sids.items())
    assert len(sids) > 1000


# --- block expansion -------------------------------------------------------

def test_block_expansion_ranges():
    sids = all_sids()
    for first, count, base in (
        (168, 10, "SID_POWER_RPM"),
        (258, 10, "SID_FDS_LEFT_PROBE"),
        (923, 31, "SID_VPX_DEV_NAME"),
        (551, 8, "SID_IAS_CAL_IAS"),
    ):
        for i in range(count):
            info = sids[str(first + i)]
            assert info.grt_name == f"{base}[{i + 1}]"
    assert str(168 + 10) in sids and sid_info("178").grt_name == "SID_POWER_MAP55[1]"
    assert sid_info("953").grt_name == "SID_VPX_DEV_NAME[31]"
    assert sid_info("559").grt_name != "SID_IAS_CAL_IAS[9]"


def test_block_member_labels_are_numbered_from_one():
    assert sid_info("258").label == "FDS Left Probe (1)"
    assert sid_info("267").label == "FDS Left Probe (10)"


# --- label precedence ------------------------------------------------------

def test_doc_name_wins():
    assert sid_info("123").label == "Max RPM"
    assert sid_info("22").label == "Never exceed speed (Vne)"
    assert sid_info(345).label == "EIS Temperature Units"


def test_curated_power_label_beats_humanized():
    assert sid_info("168").label == "Power Map RPM (row 1)"
    assert sid_info("217").label == "Power Map Delta HP (row 10)"
    assert sid_info("167").label == "Rated HP"
    assert sid_info("168").grt_name == "SID_POWER_RPM[1]"


def test_humanized_fallback():
    assert sid_info("1226").label == "USB FDL Enable"
    assert sid_info("1226").grt_name == "SID_USB_FDL_ENABLE"
    assert grt_sids.humanize("SID_LIM_MAX_RPM") == "Limit Max RPM"
    assert grt_sids.humanize("SID_DIAL1_BUG3_COLOR") == "Dial1 Bug3 Color"


def test_inline_enumeration_dropped_from_label():
    assert sid_info("746").label == "G-Meter Mode"
    assert sid_info("889").label == "Convert Vne from TAS to IAS"
    assert "0=Auto" not in sid_info("387").label


# --- value meanings --------------------------------------------------------

def test_value_meanings():
    assert value_meaning("345", "0") == "Degrees Fahrenheit"
    assert value_meaning("64", "3") == "Amp"
    assert value_meaning("70", "1") == "Manifold Pressure (MAP)"
    assert value_meaning("573", "5") == "Percent Power 1"
    assert value_meaning("889", "1") == "Yes"
    assert value_meaning("746", "3") == "Off"
    assert value_meaning("387", "1") == "Primary"
    assert value_meaning("387", "7") is None


def test_value_meaning_unknown_and_whitespace():
    assert value_meaning("345", "99") is None
    assert value_meaning("123", "5") is None          # no documented values
    assert value_meaning("999999", "0") is None       # unknown sid
    assert value_meaning("345", "  0\r\n") == "Degrees Fahrenheit"


def test_all_mapped_value_sections_resolve():
    for title, sids in grt_sids._VALUE_SECTION_SIDS.items():
        for sid in sids:
            info = sid_info(sid)
            assert info is not None and info.values, (title, sid)


def test_cached_single_build():
    assert grt_sids._build() is grt_sids._build()


# --- sanity: synthetic file with every SID 0..2100 -------------------------

def test_synthetic_file_known_count(tmp_path, capsys):
    p = tmp_path / "Settings-synthetic.dat"
    p.write_text("UPDATE=1\n" + "".join(f"{n}=0\n" for n in range(0, 2101)))
    parsed = Settings_Parser.parse(str(p))
    numeric = [k for k in parsed.sids if k.isdigit()]
    known = [k for k in numeric if sid_info(k) is not None]
    with capsys.disabled():
        print(f"\n[synthetic] {len(known)} of {len(numeric)} SIDs 0..2100 known")
    assert len(numeric) == 2101
    assert 0 < len(known) <= len(numeric)
