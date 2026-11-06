# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for non-destructive same-date snapshot sequencing.

# Feature: settings-management, Property 3

Design Property 3 — Non-destructive same-date sequencing: for any sequence of N
distinct same-date changes for a device and family, the DM produces N snapshots
and never overwrites or discards an existing same-date snapshot.

This test archives N content-distinct Settings slots through
``archiver.archive_efis_drive(mount, link_id=..., today=...)`` under a SINGLE
forced ``today`` date into one per-device directory. Because every archived
slot has a distinct Content_Hash, each one is a real "change" vs. the device's
most-recent archive, so each must be written. With the date held constant the
writer must sequence them non-destructively:
``Settings-DATE.ext``, ``Settings-DATE-2.ext``, ``Settings-DATE-3.ext``, ...

The property asserts:
- exactly N snapshots exist for that family in that device's dir;
- their names are the expected same-date sequence (no clobber, no gap);
- the N distinct contents are all preserved (nothing overwritten/discarded).

Each generated example uses its own temp archive root + mount, and patches the
archiver's ``load_config`` to point at that root, so examples never collide.

Validates: Requirements 1.6
"""

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager import archiver
from efis_data_manager.settings_archive import device_settings_dir

_FORCED_DATE = "2026-07-04"


def _grt_body(update: int, vne: int) -> str:
    """A realistic GRT settings body whose config content varies with ``vne``.

    ``UPDATE=`` is excluded from the Content_Hash, so varying ``vne`` is what
    makes two slots content-distinct (distinct Content_Hash).
    """
    lines = [
        "CHECKTYPE=STD",
        "120=25",
        "151=400",
        "144=1550",
        f"22={vne}",
        f"UPDATE={update}",
        "CHECKSIZE=1234",
        "CHECKSUM=ABCD",
    ]
    return "\n".join(lines) + "\n"


def _no_update(body: str) -> str:
    """Strip the volatile ``UPDATE=`` line so two bodies can be content-compared."""
    return "\n".join(l for l in body.splitlines() if not l.startswith("UPDATE="))


def _expected_name(seq: int, ext: str = ".bak") -> str:
    """The archiver's same-date snapshot name for the ``seq``-th change."""
    if seq <= 1:
        return f"Settings-{_FORCED_DATE}{ext}"
    return f"Settings-{_FORCED_DATE}-{seq}{ext}"


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    # N DISTINCT Vne values → N content-distinct slots (distinct Content_Hash).
    # A set guarantees distinctness; sort for a deterministic archive order.
    vnes=st.lists(
        st.integers(min_value=100, max_value=400),
        min_size=1,
        max_size=8,
        unique=True,
    ),
    link_id=st.sampled_from(["1", "2", "3"]),
)
def test_same_date_distinct_changes_sequence_non_destructively(vnes, link_id):
    """N distinct same-date changes → N sequenced snapshots, none overwritten."""
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        archive_root = root_path / "archive"
        archive_root.mkdir()
        mount = root_path / "EFIS"
        mount.mkdir()

        def fake_load_config():
            return {"archive_path": str(archive_root)}

        with patch.object(archiver, "load_config", fake_load_config):
            # Archive each distinct content under the SAME forced date. A
            # bumped UPDATE= keeps the files plausible but does not affect the
            # content-aware dedup (UPDATE is excluded from the hash).
            for i, vne in enumerate(vnes):
                (mount / "Settings.bak").write_text(
                    _grt_body(update=i + 1, vne=vne)
                )
                results = archiver.archive_efis_drive(
                    str(mount), today=_FORCED_DATE, link_id=link_id
                )
                # Each distinct content is a real change → exactly one write.
                assert results["settings_copied"] == 1, (
                    f"change {i} (vne={vne}) was not archived"
                )
                assert results["errors"] == []

        sdir = device_settings_dir(archive_root, link_id)
        n = len(vnes)

        # Exactly N snapshots exist for the family — one per distinct change.
        snaps = sorted(p.name for p in sdir.glob("Settings-*.bak"))
        assert len(snaps) == n

        # Names are the expected non-destructive same-date sequence: the first
        # un-suffixed, then -2, -3, ... with no gaps and no clobber.
        expected = sorted(_expected_name(i + 1) for i in range(n))
        assert snaps == expected

        # Every distinct content is preserved — nothing overwritten/discarded.
        # The set of archived (UPDATE-stripped) bodies equals the set written.
        archived_contents = {
            _no_update((sdir / name).read_text()) for name in snaps
        }
        written_contents = {
            _no_update(_grt_body(update=1, vne=vne)) for vne in vnes
        }
        assert archived_contents == written_contents
        # Distinctness sanity: N distinct contents, N distinct archive hashes.
        archive_hashes = {
            hashlib.sha256((sdir / name).read_bytes()).hexdigest() for name in snaps
        }
        assert len(archive_hashes) == n
