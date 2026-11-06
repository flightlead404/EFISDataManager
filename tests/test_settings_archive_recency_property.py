# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for settings-archive recency ordering.

# Feature: settings-management, Property 4

Design Property 4 — Recency ordering ignores mtime and UPDATE: for any
device/family history, ``settings_archive.latest_version`` returns the snapshot
with the maximum (Archive_Date, sequence) and never orders by file mtime or by
the ``UPDATE=`` value inside the file.

This test builds arbitrary snapshot histories: a set of distinct
(Archive_Date, sequence) pairs written as ``Settings-YYYY-MM-DD[-N].<ext>``
files, each given a RANDOMIZED file mtime and a RANDOMIZED ``UPDATE=`` value in
its contents. It then asserts that ``latest_version`` returns exactly the file
whose (Archive_Date, sequence) is the maximum — proving mtime and UPDATE are
never consulted for ordering.

To sidestep the Hypothesis + function-scoped-fixture reuse pitfall (a single
``tmp_path`` shared across generated examples), each example creates and tears
down its own temp archive root with ``tempfile``.

Validates: Requirements 2.1, 1.7
"""

import os
import shutil
import tempfile
from datetime import date

from hypothesis import given, settings, strategies as st

from efis_data_manager.settings_archive import device_settings_dir, latest_version

# A distinct (Archive_Date, sequence) key. Dates stay inside a valid, modest
# window; sequence 1 maps to an un-suffixed filename, >=2 to a "-N" suffix.
_DATES = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_SEQUENCES = st.integers(min_value=1, max_value=9)
_KEYS = st.tuples(_DATES, _SEQUENCES)

# Randomized, ordering-irrelevant inputs: file mtime and the UPDATE= value.
_MTIMES = st.integers(min_value=1_000_000_000, max_value=2_000_000_000)
_UPDATES = st.integers(min_value=0, max_value=1_000_000)

_EXTS = st.sampled_from([".bak", ".dat"])


def _name_for(d: date, seq: int, ext: str) -> str:
    """Build the archiver's snapshot filename for a (date, sequence) key."""
    stamp = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if seq <= 1:
        return f"Settings-{stamp}{ext}"
    return f"Settings-{stamp}-{seq}{ext}"


@settings(max_examples=200)
@given(
    # A non-empty set of DISTINCT (date, sequence) keys so there is always a
    # single unambiguous maximum. Hypothesis draws a dict so keys are unique;
    # each key carries its own (mtime, update, ext) tuple.
    history=st.dictionaries(
        keys=_KEYS,
        values=st.tuples(_MTIMES, _UPDATES, _EXTS),
        min_size=1,
        max_size=12,
    ),
    link_id=st.sampled_from(["1", "2", "7", "42"]),
)
def test_latest_version_is_max_by_date_then_sequence(history, link_id):
    """latest_version == max by (Archive_Date, sequence); mtime/UPDATE ignored."""
    archive_root = tempfile.mkdtemp(prefix="efis_recency_")
    try:
        device_dir = device_settings_dir(archive_root, link_id)
        device_dir.mkdir(parents=True, exist_ok=True)

        for (d, seq), (mtime, update_value, ext) in history.items():
            path = device_dir / _name_for(d, seq, ext)
            # Randomized UPDATE= value: must never influence ordering.
            path.write_text(f"151=400\nUPDATE={update_value}\n")
            # Randomized mtime: must never influence ordering.
            os.utime(path, (mtime, mtime))

        expected_key = max(history.keys())  # max by (date, sequence)
        expected_name = _name_for(
            expected_key[0], expected_key[1], history[expected_key][2]
        )

        latest = latest_version(archive_root, link_id, "Settings")
        assert latest is not None
        assert (latest.archive_date, latest.sequence) == expected_key
        assert latest.path.name == expected_name
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)
