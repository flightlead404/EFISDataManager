# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for ``build_volume_label`` after relocation to ``labels``.

The FAT volume-label helpers were moved out of ``image_composer`` into the small,
dependency-light ``efis_data_manager.labels`` module (chart-manifest-sync task
5.1) so the post-sync relabel keeps them after the image-dd path is retired.

These tests import ``build_volume_label`` from the CANONICAL new location
(``efis_data_manager.labels``) — not via the legacy ``image_composer``
re-export that ``tests/test_image_composer_volume_label.py`` exercises — and pin
the label-building contract at its new home:

  - both cycles known -> ``C<charts>P<plates>`` (``C2611P2612``);
  - single-cycle degradation -> just that component (``C2611`` / ``P2612``);
  - neither cycle known -> the ``EFIS<YYMMDD>`` compose-date fallback.

Validates: Requirements 6.4
"""

import datetime

from efis_data_manager.labels import build_volume_label


# ---------------------------------------------------------------------------
# Both cycles known -> C<charts>P<plates>
# ---------------------------------------------------------------------------


def test_build_volume_label_both_cycles():
    """Both cycles present compose to C<charts>P<plates>."""
    assert build_volume_label("2611", "2612") == "C2611P2612"


# ---------------------------------------------------------------------------
# Single-cycle degradation -> just that component
# ---------------------------------------------------------------------------


def test_build_volume_label_only_charts():
    """Only the charts cycle known -> C<charts> alone."""
    assert build_volume_label("2611", None) == "C2611"


def test_build_volume_label_only_plates():
    """Only the plates cycle known -> P<plates> alone."""
    assert build_volume_label(None, "2612") == "P2612"


# ---------------------------------------------------------------------------
# Neither known -> EFIS<YYMMDD> compose-date fallback
# ---------------------------------------------------------------------------


def test_build_volume_label_neither_falls_back_to_compose_date():
    """Neither cycle known -> EFIS<YYMMDD> stamp, never blank / "NO NAME"."""
    label = build_volume_label(None, None, now=datetime.date(2026, 11, 15))
    assert label == "EFIS261115"
    assert label  # non-empty so newfs_msdos never leaves "NO NAME"
    assert len(label) <= 11
    assert all(c.isalnum() for c in label)
