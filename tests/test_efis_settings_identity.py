# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example tests for Source_Key identity and Content_Hash (efis_settings.py).

Identity fallback and hash stability.

Requirements: 14.1, 14.8
"""

from efis_data_manager.efis_settings import (
    ParsedBackup,
    content_hash,
    extract_source_key,
)


def _parsed(sids: dict, update_value=1) -> ParsedBackup:
    return ParsedBackup(
        path="Settings.bak",
        sids={k: str(v) for k, v in sids.items()},
        update_value=update_value,
        checksize="10",
        checksum="ABCD",
        line_count=len(sids),
        valid_pairs=len(sids),
    )


# ---------------------------------------------------------------------------
# Identity fallback (Req 14.1, 14.8)
# ---------------------------------------------------------------------------
def test_both_identity_sids_present():
    ident = extract_source_key(_parsed({"1063": "A60670", "387": "1", "1062": "N123AB"}))
    assert ident.source_key == "A60670:1"
    assert ident.mode_s == "A60670"
    assert ident.link_id == "1"
    assert ident.flight_id == "N123AB"


def test_only_mode_s_present():
    ident = extract_source_key(_parsed({"1063": "A60670"}))
    assert ident.source_key == "A60670"
    assert ident.link_id is None


def test_only_link_id_present():
    ident = extract_source_key(_parsed({"387": "1"}))
    assert ident.source_key == ":1"
    assert ident.mode_s is None


def test_neither_identity_sid_present():
    ident = extract_source_key(_parsed({"151": "400"}))
    assert ident.source_key is None
    assert ident.mode_s is None
    assert ident.link_id is None


def test_flight_id_not_part_of_key():
    ident = extract_source_key(_parsed({"1063": "A60670", "387": "1", "1062": "N999ZZ"}))
    assert "N999ZZ" not in ident.source_key


# ---------------------------------------------------------------------------
# Content_Hash stability (Req 14.8)
# ---------------------------------------------------------------------------
def test_content_hash_order_independent():
    a = _parsed({"151": "400", "144": "1500", "1063": "A60670"})
    b = _parsed({"1063": "A60670", "144": "1500", "151": "400"})
    assert content_hash(a) == content_hash(b)


def test_content_hash_unchanged_when_only_update_differs():
    a = _parsed({"151": "400"}, update_value=10)
    b = _parsed({"151": "400"}, update_value=99)
    assert content_hash(a) == content_hash(b)


def test_content_hash_excludes_checksum_lines():
    a = _parsed({"151": "400"})
    b = ParsedBackup(
        path="Settings.dat",
        sids={"151": "400"},
        update_value=1,
        checksize="DIFFERENT",
        checksum="DIFFERENT",
        line_count=1,
        valid_pairs=1,
    )
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_when_settings_change():
    a = _parsed({"151": "400"})
    b = _parsed({"151": "420"})
    assert content_hash(a) != content_hash(b)
