# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the "Verify Drive" per-family summary formatter (Task 12).

`_format_verify_summary` is the pure formatting helper extracted so the menu
handler in app.py (which pulls in rumps/AppKit and cannot load headless) can be
tested without importing app.py. It turns the "families" sub-dict of a
`verify_drive` result into the concise per-family summary line shown in the
"Verify Drive" notification/status.

Rules it encodes:
  - a family with no discrepancies reads "<name>: clean"
  - otherwise only the non-zero categories are listed (missing / extra /
    size mismatch), with counts
  - families keep the dict's insertion order (build_jobs: scanned, plates, nav)
  - empty input -> "no families"

Requirements: 6.4
"""

from efis_data_manager.drive_updater import _format_verify_summary


def _disc(missing=0, extra=0, size_mismatch=0):
    """Build a verify_family-shaped discrepancy dict from counts."""
    return {
        "missing": [f"m{i}" for i in range(missing)],
        "extra": [f"e{i}" for i in range(extra)],
        "size_mismatch": [f"s{i}" for i in range(size_mismatch)],
    }


# --- clean families ---------------------------------------------------------


def test_all_clean_families():
    families = {
        "scanned": _disc(),
        "plates": _disc(),
        "nav": _disc(),
    }
    assert (
        _format_verify_summary(families)
        == "scanned: clean, plates: clean, nav: clean"
    )


def test_empty_input_returns_no_families():
    assert _format_verify_summary({}) == "no families"


# --- discrepancies reported per-family --------------------------------------


def test_missing_count_reported():
    families = {"scanned": _disc(), "plates": _disc(missing=3), "nav": _disc()}
    assert (
        _format_verify_summary(families)
        == "scanned: clean, plates: 3 missing, nav: clean"
    )


def test_only_nonzero_categories_listed():
    families = {"plates": _disc(missing=2, size_mismatch=1)}
    assert _format_verify_summary(families) == "plates: 2 missing, 1 size mismatch"


def test_all_three_categories_for_one_family():
    families = {"scanned": _disc(missing=1, extra=2, size_mismatch=3)}
    assert (
        _format_verify_summary(families)
        == "scanned: 1 missing, 2 extra, 3 size mismatch"
    )


def test_extra_only():
    families = {"nav": _disc(extra=1)}
    assert _format_verify_summary(families) == "nav: 1 extra"


def test_family_order_preserved():
    # Insertion order (build_jobs: scanned, plates, nav) is kept in the output.
    families = {
        "scanned": _disc(size_mismatch=1),
        "plates": _disc(),
        "nav": _disc(missing=5),
    }
    assert (
        _format_verify_summary(families)
        == "scanned: 1 size mismatch, plates: clean, nav: 5 missing"
    )


# --- tolerates missing keys (defensive) -------------------------------------


def test_missing_keys_treated_as_clean():
    # A family dict lacking some category keys should not raise.
    assert _format_verify_summary({"nav": {}}) == "nav: clean"
