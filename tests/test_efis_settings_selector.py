# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for Settings_Selector (efis_settings.py).

Property-based tests (Hypothesis, Property 6 — pure UPDATE=-based selection)
plus supporting examples for the deterministic tiebreak. GRT checksum
verification was removed (the algorithm is undocumented and this EFIS line is no
longer developed), so there is no integrity gate here.

Requirements: 2.1, 2.2, 2.3
"""

from efis_data_manager.efis_settings import (
    ParsedBackup,
    SelectionResult,
    Settings_Selector,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _backup(path: str, update_value) -> ParsedBackup:
    return ParsedBackup(
        path=path,
        sids={"151": "400"},
        update_value=update_value,
        line_count=1,
        valid_pairs=1,
    )


# ---------------------------------------------------------------------------
# Property 6: Selector picks the higher UPDATE_Value
# Feature: efis-settings-import, Property 6: Selector picks the higher UPDATE_Value
# Validates: Requirements 2.1, 2.2
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(a=st.integers(-10**6, 10**6), b=st.integers(-10**6, 10**6))
def test_property6_selector_higher_update(a, b):
    # Distinct update values -> the higher UPDATE= wins (Req 2.1).
    if a == b:
        b = a + 1
    bak = _backup("Settings.bak", a)
    dat = _backup("Settings.dat", b)

    result = Settings_Selector.select([bak, dat])

    expected = bak if a > b else dat
    assert result.current is expected

    # Single candidate is chosen (Req 2.2).
    single = Settings_Selector.select([bak])
    assert single.current is bak


# ---------------------------------------------------------------------------
# Property 6 (tiebreak): equal or absent UPDATE= -> deterministic tiebreak
# Feature: efis-settings-import, Property 6: Selector deterministic tiebreak
# Validates: Requirements 2.3
# ---------------------------------------------------------------------------
@settings(max_examples=200)
@given(
    u=st.one_of(st.none(), st.integers(-10**6, 10**6)),
    swap=st.booleans(),
)
def test_property6_equal_or_none_update_prefers_dat(u, swap):
    # Both candidates carry the SAME update_value (equal, or both None): the
    # deterministic tiebreak prefers the .dat slot (Req 2.3), independent of
    # the order the candidates are supplied in.
    bak = _backup("Settings.bak", u)
    dat = _backup("Settings.dat", u)
    candidates = [dat, bak] if swap else [bak, dat]

    result = Settings_Selector.select(candidates)

    assert result.current is dat


@settings(max_examples=200)
@given(swap=st.booleans())
def test_property6_all_none_tiebreak_lexical_path(swap):
    # Two .bak candidates with no UPDATE= -> tiebreak falls through to the
    # lexically-first path (Req 2.3), order-independent.
    a = _backup("A-Settings.bak", None)
    z = _backup("Z-Settings.bak", None)
    candidates = [z, a] if swap else [a, z]

    result = Settings_Selector.select(candidates)

    assert result.current is a


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
