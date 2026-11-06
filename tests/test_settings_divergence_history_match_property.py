# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the divergence history match.

# Feature: settings-management, Property 11

Design Property 11 - History match is a pure hash lookup: when a returning
card's Settings diverge from the latest archived version,
``DivergenceResult.matched_archive_date`` is the Archive_Date of the most
recent OLDER archived snapshot whose Content_Hash equals the card's, or
``None`` when none match, regardless of ``UPDATE=`` values or file mtimes.

Each example builds a device history with distinct (Archive_Date, sequence)
keys, SID content drawn from a small pool (so matches occur), randomized
``UPDATE=`` values and mtimes, and a card slot whose content is equal to some
snapshot(s), equal to the latest, or novel (outside the pool).

Each example uses its own tempfile directories (no shared function-scoped
fixtures across generated examples).

Validates: Requirements 7.3, 7.7
"""

import os
import shutil
import tempfile
from datetime import date

from hypothesis import given, settings, strategies as st

from efis_data_manager.settings_archive import device_settings_dir
from efis_data_manager.settings_writer import diverges

_POOL_SIZE = 4  # content ids 0..3 are archived; ids >= 4 are "novel"
_NOVEL_IDS = (4, 5)

_DATES = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_KEYS = st.tuples(_DATES, st.integers(min_value=1, max_value=9))
_MTIMES = st.integers(min_value=1_000_000_000, max_value=2_000_000_000)
_UPDATES = st.integers(min_value=0, max_value=1_000_000)
_EXTS = st.sampled_from([".bak", ".dat"])


def _name_for(d: date, seq: int, ext: str) -> str:
    stamp = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if seq <= 1:
        return f"Settings-{stamp}{ext}"
    return f"Settings-{stamp}-{seq}{ext}"


def _content(content_id: int, update_value: int) -> str:
    """SID content keyed by ``content_id``; UPDATE= is the volatile part."""
    return f"151={400 + content_id}\n152={content_id * 7}\nUPDATE={update_value}\n"


@settings(max_examples=200, deadline=None)
@given(
    history=st.dictionaries(
        keys=_KEYS,
        values=st.tuples(
            st.integers(min_value=0, max_value=_POOL_SIZE - 1),  # content id
            _MTIMES,
            _UPDATES,
            _EXTS,
        ),
        min_size=1,
        max_size=10,
    ),
    card_id=st.integers(min_value=0, max_value=_POOL_SIZE - 1 + len(_NOVEL_IDS)),
    card_update=_UPDATES,
    card_mtime=_MTIMES,
    card_ext=_EXTS,
    link_id=st.sampled_from(["1", "2", "7", "42"]),
)
def test_history_match_is_pure_hash_lookup(
    history, card_id, card_update, card_mtime, card_ext, link_id
):
    """matched_archive_date == date of newest older snapshot with equal content."""
    archive_root = tempfile.mkdtemp(prefix="efis_histmatch_arch_")
    mount = tempfile.mkdtemp(prefix="efis_histmatch_drv_")
    try:
        device_dir = device_settings_dir(archive_root, link_id)
        device_dir.mkdir(parents=True, exist_ok=True)

        for (d, seq), (cid, mtime, upd, ext) in history.items():
            path = device_dir / _name_for(d, seq, ext)
            path.write_text(_content(cid, upd))
            os.utime(path, (mtime, mtime))

        card = os.path.join(mount, f"Settings{card_ext}")
        with open(card, "w") as fh:
            fh.write(_content(card_id, card_update))
        os.utime(card, (card_mtime, card_mtime))

        ordered = sorted(history.keys(), reverse=True)  # newest first
        latest_key = ordered[0]
        latest_id = history[latest_key][0]

        result = diverges(archive_root, link_id, mount)

        if card_id == latest_id:
            assert result.diverged is False
            assert result.matched_archive_date is None
        else:
            assert result.diverged is True
            expected = None
            for key in ordered[1:]:
                if history[key][0] == card_id:
                    expected = key[0]
                    break
            assert result.matched_archive_date == expected
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)
        shutil.rmtree(mount, ignore_errors=True)
