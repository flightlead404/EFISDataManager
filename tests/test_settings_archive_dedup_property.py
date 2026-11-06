# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for content-aware per-device dedup in the archiver.

# Feature: settings-management, Property 2

Design Property 2 — Content-aware dedup per device: for any device and family,
re-archiving content whose Content_Hash equals the device's latest archived
version writes no new snapshot, and archiving content whose Content_Hash differs
writes exactly one new snapshot.

The archiver (``archive_efis_drive(mount, link_id=...)``) routes a resolved
device's settings into its per-device location and dedups each family against
the Most_Recent_Archive *in that device's dir* via ``content_hash`` — which
excludes the volatile ``UPDATE=``, ``CHECKSIZE=``, and ``CHECKSUM=`` keys. So a
returning slot that changed only its ``UPDATE=`` counter (same SID content) is
"identical" for dedup and must write nothing; a slot whose SID content differs
must write exactly one new snapshot.

The test generates arbitrary settings content, archives it once into a
per-device dir, then exercises both arms:
  (a) re-archive content with the SAME Content_Hash (only UPDATE= bumped) on a
      later date — assert NO new snapshot is written.
  (b) archive content with a DIFFERING Content_Hash — assert exactly ONE new
      snapshot is written.

Each generated example uses its own temp archive root + mount (the
Hypothesis + function-scoped-fixture reuse pitfall), and patches
``archiver.load_config`` to point ``archive_path`` at that root.

Validates: Requirements 1.4, 1.5
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from hypothesis import given, settings, strategies as st

from efis_data_manager import archiver
from efis_data_manager.settings_archive import device_settings_dir


# A GRT SID is a small integer key; values are arbitrary short tokens. We keep
# the generated bodies realistic but deliberately vary the SID/value content
# (what Content_Hash is computed over) independently of the UPDATE= counter.
_SIDS = st.integers(min_value=100, max_value=999).map(str)
_VALUES = st.integers(min_value=0, max_value=5000).map(str)
_UPDATES = st.integers(min_value=0, max_value=1_000_000)
_LINK_IDS = st.sampled_from(["1", "2", "3", "7", "42"])

# A non-empty mapping of SID -> value: the configuration content of a slot.
_CONTENT = st.dictionaries(keys=_SIDS, values=_VALUES, min_size=1, max_size=12)


def _grt_body(content: dict, update: int) -> str:
    """Build a GRT settings-format body from SID/value content + an UPDATE=.

    CHECKTYPE/CHECKSIZE/CHECKSUM frame the body like a real file; UPDATE= is the
    volatile counter that Content_Hash ignores.
    """
    lines = ["CHECKTYPE=STD"]
    lines += [f"{sid}={value}" for sid, value in content.items()]
    lines += [f"UPDATE={update}", "CHECKSIZE=1234", "CHECKSUM=ABCD"]
    return "\n".join(lines) + "\n"


def _snapshot_count(settings_dest: Path, family: str = "Settings") -> int:
    """Count archived snapshots for ``family`` under a per-device dir."""
    if not settings_dest.is_dir():
        return 0
    return len(list(settings_dest.glob(f"{family}-*")))


@settings(max_examples=150)
@given(
    base=_CONTENT,
    delta_sid=_SIDS,
    delta_value=_VALUES,
    update_a=_UPDATES,
    update_b=_UPDATES,
    update_c=_UPDATES,
    link_id=_LINK_IDS,
)
def test_content_aware_dedup_per_device(
    base, delta_sid, delta_value, update_a, update_b, update_c, link_id
):
    """Identical content re-archives nothing; differing content writes one.

    # Feature: settings-management, Property 2
    Validates: Requirements 1.4, 1.5
    """
    # Construct a "differing" content that is guaranteed to have a different
    # Content_Hash from ``base``: flip one SID's value to a token that cannot
    # collide with any generated value (values are purely numeric above).
    differing = dict(base)
    differing[delta_sid] = f"{delta_value}x"

    archive_root = Path(tempfile.mkdtemp(prefix="efis_dedup_root_"))
    mount = Path(tempfile.mkdtemp(prefix="efis_dedup_mount_"))
    try:
        def fake_load_config():
            return {"archive_path": str(archive_root)}

        with mock.patch.object(archiver, "load_config", fake_load_config):
            settings_dest = device_settings_dir(archive_root, link_id)

            # --- First archive: establishes the device's latest version. ---
            (mount / "Settings.bak").write_text(_grt_body(base, update_a))
            first = archiver.archive_efis_drive(
                str(mount), today="2026-01-01", link_id=link_id
            )
            assert first["settings_copied"] == 1
            assert _snapshot_count(settings_dest) == 1

            # --- (a) Re-archive IDENTICAL content (only UPDATE= bumped). ---
            # Same SID content => same Content_Hash => no new snapshot (Req 1.5).
            (mount / "Settings.bak").write_text(_grt_body(base, update_b))
            second = archiver.archive_efis_drive(
                str(mount), today="2026-02-01", link_id=link_id
            )
            assert second["settings_copied"] == 0
            assert _snapshot_count(settings_dest) == 1

            # --- (b) Archive DIFFERING content => exactly one new snapshot. ---
            (mount / "Settings.bak").write_text(_grt_body(differing, update_c))
            third = archiver.archive_efis_drive(
                str(mount), today="2026-03-01", link_id=link_id
            )
            assert third["settings_copied"] == 1
            assert _snapshot_count(settings_dest) == 2
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)
        shutil.rmtree(mount, ignore_errors=True)
