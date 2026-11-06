# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for per-device settings archive isolation.

# Feature: settings-management, Property 1

Design Property 1 — Per-device archive isolation: for any returning drive
resolved to a Link_ID, archiving writes new snapshots only under that Link_ID's
per-device directory (``archive_root/Settings/link{N}/``) and never under
another Link_ID's directory.

This test generates an arbitrary Link_ID and arbitrary settings content, sets
up a temp "USB" mount carrying a ``Settings`` slot with that content, calls
``archiver.archive_efis_drive`` with the Link_ID, and asserts the newly written
snapshot lives only under ``device_settings_dir(archive_root, link_id)`` — and
that no other ``link{M}`` directory received anything.

To sidestep the Hypothesis + function-scoped-fixture reuse pitfall, each
example builds and tears down its own temp archive root and mount with
``tempfile`` and patches ``archiver.load_config`` for the duration of the call
(not via a pytest fixture).

Validates: Requirements 1.1
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from efis_data_manager import archiver
from efis_data_manager.settings_archive import device_settings_dir

# Arbitrary Link_IDs. The GRT inter-display link position is 1-3 in practice,
# but the per-device directory scheme (``link{N}``) accepts any value, so we
# exercise a wider spread to prove isolation is purely keyed by the supplied
# Link_ID string.
_LINK_IDS = st.sampled_from(["1", "2", "3", "7", "42"])

# Arbitrary settings content: vary the recognized SID values so distinct draws
# produce distinct Content_Hashes, without needing a valid checksum (the
# archiver copies bytes and never validates the GRT checksum).
_SID_VALUES = st.integers(min_value=0, max_value=5000)
_EXTS = st.sampled_from([".bak", ".dat"])


def _grt_body(vne: int, cht: int, update: int) -> str:
    """Build an arbitrary-but-well-formed GRT settings body."""
    return (
        "CHECKTYPE=STD\n"
        f"151={cht}\n"
        f"22={vne}\n"
        f"UPDATE={update}\n"
        "CHECKSIZE=1234\n"
        "CHECKSUM=ABCD\n"
    )


@settings(max_examples=150)
@given(
    link_id=_LINK_IDS,
    vne=_SID_VALUES,
    cht=_SID_VALUES,
    update=st.integers(min_value=0, max_value=100000),
    ext=_EXTS,
)
def test_archive_writes_only_under_resolved_link_dir(link_id, vne, cht, update, ext):
    """Archiving under Link_ID X writes new snapshots only under X's dir."""
    archive_root = Path(tempfile.mkdtemp(prefix="efis_iso_archive_"))
    mount = Path(tempfile.mkdtemp(prefix="efis_iso_mount_"))
    try:
        (mount / f"Settings{ext}").write_text(_grt_body(vne, cht, update))

        def fake_load_config():
            return {"archive_path": str(archive_root)}

        with patch.object(archiver, "load_config", fake_load_config):
            results = archiver.archive_efis_drive(
                str(mount), today="2026-01-01", link_id=link_id
            )

        # The archive succeeded and wrote exactly one settings snapshot.
        assert results["errors"] == []
        assert results["settings_copied"] == 1

        # The snapshot lives under THIS Link_ID's per-device directory.
        own_dir = device_settings_dir(archive_root, link_id)
        own_snaps = list(own_dir.glob("Settings-*"))
        assert len(own_snaps) == 1
        assert own_snaps[0].name == f"Settings-2026-01-01{ext}"

        # No OTHER link{M} directory (nor unresolved/) received a snapshot.
        settings_root = archive_root / "Settings"
        for other in settings_root.iterdir():
            if not other.is_dir() or other == own_dir:
                continue
            assert list(other.glob("Settings-*")) == [], (
                f"snapshot leaked into {other.name} when archiving link {link_id}"
            )
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)
        shutil.rmtree(mount, ignore_errors=True)
