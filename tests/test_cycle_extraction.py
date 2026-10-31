# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Focused unit test for cycle-number extraction (chart-manifest-sync task 1.8).

The download-time manifest build harvests the governing YYCC cycle from two
sources (design "IMPORTANT — what valid_dates actually contains"):

  * the 4-digit YYCC token embedded in the Seattle Avionics download-URL KEY
    (``currency._cycle_from_url``), and
  * the START date of the ``valid_dates`` prose range, mapped to a cycle via
    the 28-day anchor (``labels.extract_cycle``),

combined by ``currency._harvest_cycle`` (URL preferred, prose fallback). This
test pins the single canonical outcome for cycle ``2611`` from each source and
the ``None`` result when neither carries a cycle. It deliberately uses one
representative cycle (2611) so the assertions read as a concrete contract;
combined URL-wins/fallback ordering is covered in test_currency_manifest_build.

Requirements: 5.1
"""

from efis_data_manager import currency
from efis_data_manager import labels

# Cycle 2611 is the 11th 28-day cycle of 2026; its start date is 2026-10-29
# (anchor 2026-01-22 + 28*10 days), so a valid_dates range opening on that date
# derives to "2611".
_CYCLE_2611 = "2611"
_URL_KEY_2611 = "https://seattleavionics.com/ChartData/MultiDiskImg2611.zip"
_PROSE_2611 = "Thursday, October 29, 2026 through Wednesday, November 25, 2026"


def test_cycle_from_url_key_yields_2611():
    """The YYCC token in the SA download-URL key is harvested as the cycle."""
    assert currency._cycle_from_url(_URL_KEY_2611) == _CYCLE_2611


def test_cycle_from_valid_dates_prose_yields_2611():
    """The start date of the valid_dates prose range derives to the cycle."""
    assert labels.extract_cycle(_PROSE_2611) == _CYCLE_2611


def test_harvest_cycle_yields_2611_from_either_source():
    """_harvest_cycle yields 2611 from the URL key and from the prose alone."""
    # URL key present (prose absent).
    assert currency._harvest_cycle(_URL_KEY_2611, "") == _CYCLE_2611
    # Prose present (no cycle token in the URL).
    assert (
        currency._harvest_cycle(
            "https://seattleavionics.com/ChartData/getfile.aspx", _PROSE_2611
        )
        == _CYCLE_2611
    )


def test_harvest_cycle_none_when_neither_present():
    """No cycle token in the URL and no parseable prose range -> None."""
    assert currency._harvest_cycle("https://seattleavionics.com/x.zip", "") is None
    assert currency._cycle_from_url("https://seattleavionics.com/x.zip") is None
    assert labels.extract_cycle("") is None
