# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for Settings_Selector and best-effort checksum (efis_settings.py).

Property-based tests (Hypothesis, Properties 6-7) plus supporting examples.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5
"""

import efis_data_manager.efis_settings as es
from efis_data_manager.efis_settings import (
    ChecksumStatus,
    ParsedBackup,
    SelectionResult,
    Settings_Selector,
    verify_checksum,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _backup(path: str, update_value) -> ParsedBackup:
    return ParsedBackup(
        path=path,
        sids={"151": "400"},
        update_value=update_value,
        checksize=None,
        checksum=None,
        line_count=1,
        valid_pairs=1,
    )


def test_verify_checksum_defaults_unverifiable():
    assert verify_checksum(_backup("Settings.bak", 1)) == ChecksumStatus.UNVERIFIABLE


# ---------------------------------------------------------------------------
# Property 6: Selector picks the higher UPDATE_Value
# Feature: efis-settings-import, Property 6: Selector picks the higher UPDATE_Value
# Validates: Requirements 2.1, 2.2
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(a=st.integers(-10**6, 10**6), b=st.integers(-10**6, 10**6))
def test_property6_selector_higher_update(a, b):
    # Distinct update values (all UNVERIFIABLE — default checksum).
    if a == b:
        b = a + 1
    bak = _backup("Settings.bak", a)
    dat = _backup("Settings.dat", b)

    result = Settings_Selector.select([bak, dat])

    expected = bak if a > b else dat
    assert result.current is expected

    # Single candidate is chosen.
    single = Settings_Selector.select([bak])
    assert single.current is bak


# ---------------------------------------------------------------------------
# Property 7: Selector honors integrity verification when available
# Feature: efis-settings-import, Property 7: Selector honors integrity verification when available
# Validates: Requirements 2.3, 2.4, 2.5
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    verified_update=st.integers(-10**6, 10**6),
    failed_update=st.integers(-10**6, 10**6),
)
def test_property7_verified_over_failed(verified_update, failed_update):
    verified = _backup("Settings.dat", verified_update)
    failed = _backup("Settings.bak", failed_update)

    def stub(parsed):
        if parsed is verified:
            return ChecksumStatus.VERIFIED
        return ChecksumStatus.FAILED

    original = es.verify_checksum
    es.verify_checksum = stub
    try:
        result = Settings_Selector.select([verified, failed])
        # VERIFIED chosen even when it has a lower UPDATE (Req 2.4).
        assert result.current is verified
    finally:
        es.verify_checksum = original


@settings(max_examples=100)
@given(u1=st.integers(-10**6, 10**6), u2=st.integers(-10**6, 10**6))
def test_property7_all_failed_selects_nothing(u1, u2):
    a = _backup("Settings.bak", u1)
    b = _backup("Settings.dat", u2)

    def stub(parsed):
        return ChecksumStatus.FAILED

    original = es.verify_checksum
    es.verify_checksum = stub
    try:
        result = Settings_Selector.select([a, b])
        assert result.current is None  # Req 2.5
        assert "fail" in result.reason.lower()
    finally:
        es.verify_checksum = original


@settings(max_examples=100)
@given(u1=st.integers(-10**6, 10**6), u2=st.integers(-10**6, 10**6))
def test_property7_unverifiable_only_not_failure(u1, u2):
    if u1 == u2:
        u2 = u1 + 1
    a = _backup("Settings.bak", u1)
    b = _backup("Settings.dat", u2)

    # Default verify_checksum returns UNVERIFIABLE — proceeds on UPDATE=.
    result = Settings_Selector.select([a, b])
    assert result.current is not None
    assert result.current is (a if u1 > u2 else b)


# ---------------------------------------------------------------------------
# Supporting examples
# ---------------------------------------------------------------------------
def test_all_none_update_tiebreak_prefers_dat():
    bak = _backup("Settings.bak", None)
    dat = _backup("Settings.dat", None)
    result = Settings_Selector.select([bak, dat])
    assert result.current is dat


def test_present_update_beats_none():
    bak = _backup("Settings.bak", None)
    dat = _backup("Settings.dat", 5)
    result = Settings_Selector.select([bak, dat])
    assert result.current is dat


def test_no_candidates():
    result = Settings_Selector.select([])
    assert result.current is None


# ---------------------------------------------------------------------------
# Property 38: Cross-date selection uses the latest Archive_Date
# Feature: efis-settings-import, Property 38: Cross-date selection uses the
#   latest Archive_Date
# Validates: Requirements 17.1, 17.2
# ---------------------------------------------------------------------------
from datetime import date, timedelta

from efis_data_manager.efis_settings import (
    ArchivedBackup,
    parse_archive_date,
    select_current_backup,
)


def _archived(archive_date: date, update_value, ext="dat") -> ArchivedBackup:
    stamp = archive_date.isoformat()
    parsed = _backup(f"Settings-{stamp}.{ext}", update_value)
    return ArchivedBackup(path=parsed.path, archive_date=archive_date, parsed=parsed)


@settings(max_examples=200)
@given(
    # A list of (day-offset, update_value) captures for one Source_Key. UPDATE=
    # is arbitrary (including all-equal and an older date carrying a higher one).
    captures=st.lists(
        st.tuples(st.integers(0, 60), st.integers(0, 500)),
        min_size=1, max_size=8,
    ),
)
def test_property38_latest_archive_date_wins(captures):
    base = date(2026, 1, 1)
    archived = [
        _archived(base + timedelta(days=off), upd)
        for off, upd in captures
    ]
    result = select_current_backup(archived)
    assert result.current is not None

    latest = max(ab.archive_date for ab in archived)
    chosen_date = parse_archive_date(result.current.path)
    # Never an earlier date, regardless of any UPDATE= an older date carries.
    assert chosen_date == latest


def test_property38_older_date_higher_update_still_loses():
    # An older date with a MUCH higher UPDATE= must not beat the latest date.
    old = _archived(date(2026, 1, 1), update_value=999)
    new = _archived(date(2026, 6, 1), update_value=1)
    result = select_current_backup([old, new])
    assert result.current is new.parsed


def test_property38_same_date_pair_uses_update():
    # Within the same latest date, UPDATE= disambiguates the .bak/.dat pair.
    d = date(2026, 9, 12)
    bak = _archived(d, update_value=1, ext="bak")
    dat = _archived(d, update_value=2, ext="dat")
    result = select_current_backup([bak, dat])
    assert result.current is dat.parsed


def test_property38_single_backup_selected_unchanged():
    only = _archived(date(2026, 3, 3), update_value=1)
    result = select_current_backup([only])
    assert result.current is only.parsed


def test_parse_archive_date_examples():
    assert parse_archive_date("Settings-2026-09-12.bak") == date(2026, 9, 12)
    assert parse_archive_date("Settings-2026-09-12.dat") == date(2026, 9, 12)
    assert parse_archive_date("Settings.bak") is None
    assert parse_archive_date("Settings-2026-13-99.bak") is None
