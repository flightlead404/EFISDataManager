# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the nav-only volume label (task 2.2).

Feature: chart-sync-wiring-fix, Property 6: nav-only label is well-formed

For any device friendly name (including blank / whitespace / non-ASCII / very
long) and any nav valid date (a valid ``MM/DD/YYYY`` string, ``None``, or a
malformed string), ``labels.build_nav_volume_label`` returns a ``<PREFIX><MMDD>``
label that is at most 11 characters, composed only of uppercase ``A-Z`` / ``0-9``,
carries the month and day with no year component, and falls back to ``EFISNAV``
(reduced to ``NAV`` when the length limit requires it) when the friendly name
sanitizes to empty.

Validates: Requirements 9.4, 9.5, 9.6
"""

import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.labels import (
    build_nav_volume_label,
    _sanitize_label,
    _mmdd_from_nav_valid_date,
    _LABEL_MAX_LEN,
    _NAV_FALLBACK_PREFIX,
    _NAV_FALLBACK_PREFIX_SHORT,
)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Arbitrary friendly names: blank, whitespace-only, non-ASCII, punctuation-only
# (sanitizes empty), normal mixed-case, and very long strings all in-scope.
friendly_names = st.one_of(
    st.just(""),
    st.just("   "),
    st.text(alphabet=" \t/._-*", min_size=0, max_size=8),  # sanitizes to empty
    st.text(max_size=5),
    st.text(max_size=80),  # very long
    st.sampled_from(
        ["Mini A/P", "Sport SX", "Horizon HXr", "café", "日本語", "G3X"]
    ),
)


def _valid_mmddyyyy(d: datetime.date) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year:04d}"


# Valid MM/DD/YYYY strings drawn from real dates, plus None and malformed junk.
nav_valid_dates = st.one_of(
    st.dates(
        min_value=datetime.date(2020, 1, 1),
        max_value=datetime.date(2099, 12, 31),
    ).map(_valid_mmddyyyy),
    st.none(),
    st.just(""),
    st.just("not-a-date"),
    st.just("13/40/2026"),  # out-of-range -> unparseable
    st.just("2026-10-29"),  # wrong format
    st.text(max_size=12),
)


def _contains_four_digit_year(label: str, nav_valid_date) -> bool:
    """True if the 4-digit year from a parseable date appears in the label."""
    if not nav_valid_date:
        return False
    try:
        parsed = datetime.datetime.strptime(nav_valid_date.strip(), "%m/%d/%Y")
    except (ValueError, TypeError, AttributeError):
        return False
    return f"{parsed.year:04d}" in label


# ---------------------------------------------------------------------------
# Property 6: nav-only label is well-formed
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(friendly_name=friendly_names, nav_valid_date=nav_valid_dates)
def test_nav_label_is_well_formed(friendly_name, nav_valid_date):
    """build_nav_volume_label is always a legal, year-free FAT32 label.

    Validates: Requirements 9.4, 9.5, 9.6
    """
    label = build_nav_volume_label(friendly_name, nav_valid_date)

    # R9.6: at most 11 characters.
    assert len(label) <= _LABEL_MAX_LEN

    # R9.6: only uppercase A-Z and 0-9.
    assert all("A" <= c <= "Z" or "0" <= c <= "9" for c in label), label

    # R9.5: no year component. The label embeds only MMDD, so the full 4-digit
    # year of a parseable date must never appear in the result.
    assert not _contains_four_digit_year(label, nav_valid_date), label

    # R9.4: when the friendly name sanitizes to empty, the prefix is the
    # EFISNAV fallback (reduced to NAV when the MMDD would overflow 11 chars).
    mmdd = _mmdd_from_nav_valid_date(nav_valid_date)
    if not _sanitize_label(friendly_name or ""):
        if len(_NAV_FALLBACK_PREFIX) + len(mmdd) > _LABEL_MAX_LEN:
            expected_prefix = _NAV_FALLBACK_PREFIX_SHORT
        else:
            expected_prefix = _NAV_FALLBACK_PREFIX
        assert label == _sanitize_label(expected_prefix + mmdd), label

    # The label is always PREFIX followed by the MMDD (which is itself [0-9]
    # and at most 4 digits, i.e. month+day only -- never a year).
    assert len(mmdd) in (0, 4)
    if mmdd:
        assert label.endswith(mmdd) or len(_sanitize_label(
            (friendly_name or "")) + mmdd) > _LABEL_MAX_LEN


@settings(max_examples=200)
@given(nav_valid_date=nav_valid_dates)
def test_blank_prefix_falls_back(nav_valid_date):
    """A blank friendly name always yields an EFISNAV/NAV fallback prefix.

    Validates: Requirements 9.4
    """
    label = build_nav_volume_label("", nav_valid_date)
    assert label.startswith(("EFISNAV", "NAV"))
    assert len(label) <= _LABEL_MAX_LEN


# ---------------------------------------------------------------------------
# Representative concrete case
# ---------------------------------------------------------------------------


def test_nav_label_representative_case():
    """The documented example composes to MINIAP1029.

    Validates: Requirements 9.4, 9.5, 9.6
    """
    assert build_nav_volume_label("Mini A/P", "10/29/2026") == "MINIAP1029"
