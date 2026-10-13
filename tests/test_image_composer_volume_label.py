# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the FAT volume-label helpers (hardware-harness DECISION).

The composed drive's FAT volume label carries BOTH currency cycles in the form
``C<charts>P<plates>`` (e.g. ``C2609P2610``) — charts/nav run a 56-day cycle and
plates a 28-day cycle, so a single number would be misleading. These tests pin:

  - extract_cycle: pull a 4-digit YYCC cycle from a currency ``valid_dates``
    value. The real stored shape is a PROSE date range
    ("Thursday, September 3, 2026 through Wednesday, October 28, 2026") whose
    only 4-digit tokens are the YEARS, so the cycle is derived from the START
    date; unparseable input degrades to None.
  - build_volume_label: assemble + sanitize the label (both cycles, one
    missing, neither -> compose-date fallback; uppercasing, specials stripped,
    truncated to 11 chars).
  - _cycles_from_metadata: classify charts (SEC/LO/scanned) vs plates cycles
    from currency.load_cycle_metadata().

Design: image-based-drive-write "Hardware harness findings (2026-10-09)".
"""

import datetime

from efis_data_manager import image_composer as ic


# ---------------------------------------------------------------------------
# extract_cycle
# ---------------------------------------------------------------------------


def test_extract_cycle_from_real_prose_ranges():
    """The real stored prose date ranges map to their YYCC cycle via start date."""
    assert (
        ic.extract_cycle(
            "Thursday, July 9, 2026 through Wednesday, September 2, 2026"
        )
        == "2607"
    )
    assert (
        ic.extract_cycle(
            "Thursday, August 6, 2026 through Wednesday, September 2, 2026"
        )
        == "2608"
    )
    assert (
        ic.extract_cycle(
            "Thursday, September 3, 2026 through Wednesday, October 28, 2026"
        )
        == "2609"
    )
    assert (
        ic.extract_cycle(
            "Thursday, October 1, 2026 through Wednesday, October 28, 2026"
        )
        == "2610"
    )


def test_extract_cycle_year_boundary():
    """Cycle numbering restarts across the year boundary (YY changes, CC resets)."""
    # First cycle of 2026 starts 2026-01-22 (the anchor).
    assert ic.extract_cycle("Thursday, January 22, 2026 through x") == "2601"
    # Last cycle wholly starting in 2026.
    assert ic.extract_cycle("Wednesday, December 24, 2026 through x") == "2613"
    # First cycle starting in 2027.
    assert ic.extract_cycle("Thursday, January 21, 2027 through x") == "2701"


def test_extract_cycle_mid_range_date_snaps_to_cycle():
    """A date a few days inside a cycle still resolves to that cycle."""
    # Sep 5 2026 is two days into the 2609 cycle (starts Sep 3).
    assert ic.extract_cycle("Saturday, September 5, 2026 through x") == "2609"


def test_extract_cycle_bare_token_fallback_without_month():
    """A bare 4-digit token (no month prose) is used directly."""
    assert ic.extract_cycle("2610") == "2610"
    assert ic.extract_cycle("cycle 2609 current") == "2609"


def test_extract_cycle_unparseable_returns_none():
    """Unparseable or empty input degrades to None, never raises."""
    assert ic.extract_cycle("") is None
    assert ic.extract_cycle("no cycle here") is None
    assert ic.extract_cycle("Blursday, Octember 99, 2026 through x") is None


# ---------------------------------------------------------------------------
# build_volume_label
# ---------------------------------------------------------------------------


def test_build_volume_label_both_cycles():
    assert ic.build_volume_label("2609", "2610") == "C2609P2610"


def test_build_volume_label_only_charts():
    assert ic.build_volume_label("2609", None) == "C2609"


def test_build_volume_label_only_plates():
    assert ic.build_volume_label(None, "2610") == "P2610"


def test_build_volume_label_neither_falls_back_to_compose_date():
    """Neither cycle known -> EFIS<YYMMDD> stamp, never blank / "NO NAME"."""
    label = ic.build_volume_label(None, None, now=datetime.date(2026, 10, 15))
    assert label == "EFIS261015"
    assert label  # non-empty so newfs_msdos never leaves "NO NAME"


def test_build_volume_label_is_fat_safe():
    """Every build is uppercase, A-Z/0-9 only, and at most 11 chars."""
    label = ic.build_volume_label("2609", "2610")
    assert label.isupper() or label.isdigit() or all(
        c.isupper() or c.isdigit() for c in label
    )
    assert len(label) <= 11
    assert all(c.isalnum() for c in label)


def test_build_volume_label_sanitizes_and_truncates():
    """Lowercase -> upper, specials stripped, result capped at 11 chars."""
    # Degenerate inputs carrying case + punctuation; result stays FAT-safe.
    label = ic.build_volume_label("ab/cd", "e f!2")
    assert label == label.upper()
    assert all(c.isalnum() for c in label)
    assert len(label) <= 11
    # Over-long inputs are truncated to exactly 11.
    long_label = ic.build_volume_label("123456789", "987654321")
    assert len(long_label) == 11


# ---------------------------------------------------------------------------
# _sanitize_label
# ---------------------------------------------------------------------------


def test_sanitize_label_rules():
    assert ic._sanitize_label("c2609p2610") == "C2609P2610"
    assert ic._sanitize_label("hello world!") == "HELLOWORLD"
    assert ic._sanitize_label("a_b-c.d") == "ABCD"
    assert ic._sanitize_label("0123456789ABCDEF") == "0123456789A"  # 11 cap
    assert ic._sanitize_label("") == ""
    assert ic._sanitize_label("!!!") == ""  # all-special -> empty


# ---------------------------------------------------------------------------
# _cycles_from_metadata (classification against the currency metadata source)
# ---------------------------------------------------------------------------


def test_cycles_from_metadata_classifies_charts_and_plates(monkeypatch):
    """SEC/LO/scanned entries feed the charts cycle; plates feed the plates cycle."""
    fake_meta = {
        "url/2609.SEC": {
            "description": "VFR Sectionals",
            "valid_dates": "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
        },
        "url/2609.LO": {
            "description": "IFR Low Altitude Charts",
            "valid_dates": "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
        },
        "url/2609.Scanned": {
            "description": "Scanned Charts DB",
            "valid_dates": "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
        },
        "url/2610.Plates": {
            "description": "IFR Approach Plates and Airport Diagrams",
            "valid_dates": "Thursday, October 1, 2026 through Wednesday, October 28, 2026",
        },
    }
    from efis_data_manager import currency

    monkeypatch.setattr(currency, "load_cycle_metadata", lambda: fake_meta)
    charts, plates = ic._cycles_from_metadata()
    assert charts == "2609"
    assert plates == "2610"
    assert ic.build_volume_label(charts, plates) == "C2609P2610"


def test_cycles_from_metadata_prefers_newest(monkeypatch):
    """When several plates/charts cycles are stored, the newest wins."""
    fake_meta = {
        "a": {
            "description": "IFR Approach Plates and Airport Diagrams",
            "valid_dates": "Thursday, August 6, 2026 through x",  # 2608
        },
        "b": {
            "description": "IFR Approach Plates and Airport Diagrams",
            "valid_dates": "Thursday, October 1, 2026 through x",  # 2610
        },
        "c": {
            "description": "VFR Sectionals",
            "valid_dates": "Thursday, July 9, 2026 through x",  # 2607
        },
        "d": {
            "description": "VFR Sectionals",
            "valid_dates": "Thursday, September 3, 2026 through x",  # 2609
        },
    }
    from efis_data_manager import currency

    monkeypatch.setattr(currency, "load_cycle_metadata", lambda: fake_meta)
    charts, plates = ic._cycles_from_metadata()
    assert charts == "2609"
    assert plates == "2610"


def test_cycles_from_metadata_handles_missing_or_broken(monkeypatch):
    """A metadata read failure degrades to (None, None); label falls back."""
    from efis_data_manager import currency

    def _boom():
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(currency, "load_cycle_metadata", _boom)
    charts, plates = ic._cycles_from_metadata()
    assert charts is None
    assert plates is None
    # Label still builds (compose-date fallback), never raising.
    assert ic.build_volume_label(charts, plates)
