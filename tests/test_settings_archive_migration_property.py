# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the one-time flat-archive migration.

# Feature: settings-management, Property 10

Design Property 10 — Migration adoption is faithful and idempotent: for any set
of flat archive files directly under ``archive_root/Settings/``, each is adopted
to the Link_ID derived from its OWN parsed content (SID 387 present -> that
``link{N}``; underivable -> ``unresolved/``), with its file bytes, Archive_Date,
and same-date sequence preserved, no pre-existing destination snapshot ever
overwritten (a collision takes the next non-destructive sequence suffix), and a
SECOND migration run makes no further change.

Strategy — each example generates an arbitrary set of flat snapshots named
``<Family>-YYYY-MM-DD[-N].<ext>`` with varying byte content:

* derivable: content carries Link_ID (SID 387), optionally Mode_S (SID 1063) —
  ``extract_source_key`` yields a concrete ``link_id``, so it adopts under
  ``Settings/link{N}/``.
* underivable: content carries neither SID 387 nor SID 1063 — ``link_id`` is
  ``None``, so it adopts under ``Settings/unresolved/``.

Some destination names are pre-seeded with sentinel bytes to FORCE collisions,
letting us assert the migration never overwrites an existing snapshot.

Each example uses its own ``tempfile`` archive root to avoid the Hypothesis +
function-scoped-fixture reuse pitfall.

Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.6
"""

import shutil
import tempfile
from datetime import date
from pathlib import Path

from hypothesis import given, settings, strategies as st

from efis_data_manager.efis_settings import SID_LINK_ID, SID_MODE_S
from efis_data_manager.settings_archive import (
    _MIGRATION_SENTINEL,
    device_settings_dir,
    migrate_flat_archives,
)

_FAMILIES = st.sampled_from(["Settings", "WP", "Plan"])
_LINK_IDS = st.sampled_from(["1", "2", "3"])
_EXTS = st.sampled_from([".bak", ".dat"])
_DATES = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_SEQUENCES = st.integers(min_value=1, max_value=4)
# Varying "real" content lines beyond identity SIDs so distinct files have
# distinct bytes and the parser always finds at least one valid pair.
_PAYLOADS = st.integers(min_value=0, max_value=100_000)


def _flat_name(family: str, d: date, seq: int, ext: str) -> str:
    stamp = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if seq <= 1:
        return f"{family}-{stamp}{ext}"
    return f"{family}-{stamp}-{seq}{ext}"


def _file_content(link_id, payload: int, uniq: str) -> str:
    """Build settings-file bytes.

    ``link_id`` None -> no identity SIDs (underivable). Otherwise SID 387 is
    present (and SID 1063, exercising the full Source_Key), so ``link_id`` is
    derivable. A non-identity SID (151), a per-file-unique comment line, and an
    UPDATE= line make every flat file a valid AND byte-distinct settings file
    (two files that differ only by extension still get different bytes, so byte
    content uniquely identifies an adopted file).
    """
    lines = [f"# src={uniq}", f"151={payload}", "UPDATE=1"]
    if link_id is not None:
        lines.append(f"{SID_MODE_S}=A6{payload % 10000:04d}")
        lines.append(f"{SID_LINK_ID}={link_id}")
    return "\n".join(lines) + "\n"


# A single generated flat snapshot: (family, date, sequence, ext, link_id, payload).
# link_id None means underivable content.
_SNAPSHOT = st.tuples(
    _FAMILIES,
    _DATES,
    _SEQUENCES,
    _EXTS,
    st.one_of(st.none(), _LINK_IDS),
    _PAYLOADS,
)


@settings(max_examples=120, deadline=None)
@given(
    # A set of snapshots keyed by their flat NAME so no two generated files
    # share a filename directly under Settings/ (a real dir cannot hold two).
    snapshots=st.lists(_SNAPSHOT, min_size=0, max_size=10),
    # Pre-seed a fraction of destinations to force collisions.
    seed_collisions=st.booleans(),
)
def test_migration_is_faithful_and_idempotent(snapshots, seed_collisions):
    archive_root = Path(tempfile.mkdtemp(prefix="efis_migrate_"))
    try:
        settings_root = archive_root / "Settings"
        settings_root.mkdir(parents=True, exist_ok=True)

        # ---- lay down flat snapshots, de-duping by on-disk filename ----
        # (family, date, seq, ext) collapses to one filename; keep the LAST.
        flat_by_name: dict[str, tuple] = {}
        for family, d, seq, ext, link_id, payload in snapshots:
            name = _flat_name(family, d, seq, ext)
            flat_by_name[name] = (family, d, seq, ext, link_id, payload)

        # Record the exact bytes we wrote per flat file for byte-preservation
        # checks after the move.
        original_bytes: dict[str, bytes] = {}
        for name, (family, d, seq, ext, link_id, payload) in flat_by_name.items():
            content = _file_content(link_id, payload, uniq=name)
            (settings_root / name).write_text(content)
            original_bytes[name] = content.encode("ascii")

        # ---- optionally pre-seed destination collisions ----
        # For each flat file, drop a DIFFERENT-bytes file at its would-be primary
        # destination so the migration must take a sequence-suffixed name and
        # must not overwrite the pre-existing file.
        preseeded: dict[Path, bytes] = {}
        if seed_collisions:
            for name, (family, d, seq, ext, link_id, payload) in flat_by_name.items():
                dest_dir = device_settings_dir(archive_root, link_id)
                dest_dir.mkdir(parents=True, exist_ok=True)
                collide_path = dest_dir / name  # same name => forces collision
                sentinel = f"# preseeded {name}\n387=preseed\n".encode("ascii")
                collide_path.write_bytes(sentinel)
                preseeded[collide_path] = sentinel

        # ---- run the migration ----
        result = migrate_flat_archives(archive_root)

        # No flat snapshot should remain directly under Settings/ (all adopted).
        remaining = [
            p.name
            for p in settings_root.iterdir()
            if p.is_file() and p.name != _MIGRATION_SENTINEL
        ]
        assert remaining == [], f"flat snapshots left behind: {remaining}"

        # Sentinel written so reruns are no-ops (Req 11.6).
        assert (settings_root / _MIGRATION_SENTINEL).exists()

        # Counts: every derivable file adopted, every underivable -> unresolved.
        expected_adopted = sum(
            1 for v in flat_by_name.values() if v[4] is not None
        )
        expected_unresolved = sum(
            1 for v in flat_by_name.values() if v[4] is None
        )
        assert result.adopted == expected_adopted
        assert result.unresolved == expected_unresolved
        assert result.errors == []

        # Pre-seeded files must survive byte-for-byte (nothing overwritten, Req 11.5).
        for path, sentinel in preseeded.items():
            assert path.exists()
            assert path.read_bytes() == sentinel

        # ---- each snapshot adopted faithfully (Req 11.2/11.3/11.4/11.5) ----
        for name, (family, d, seq, ext, link_id, payload) in flat_by_name.items():
            dest_dir = device_settings_dir(archive_root, link_id)
            want_bytes = original_bytes[name]

            # Find the adopted file: the one in dest_dir whose bytes match the
            # original (and which is NOT a pre-seeded sentinel).
            matches = [
                p
                for p in dest_dir.iterdir()
                if p.is_file() and p.read_bytes() == want_bytes
            ]
            assert len(matches) == 1, (
                f"{name}: expected exactly one adopted copy in {dest_dir}, "
                f"found {len(matches)}"
            )
            adopted = matches[0]

            # Bytes preserved verbatim (Req 11.4).
            assert adopted.read_bytes() == want_bytes

            # Extension preserved.
            assert adopted.suffix == ext

            # Archive_Date preserved: the adopted name carries the same date.
            stamp = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
            assert stamp in adopted.name

            # Family preserved.
            assert adopted.name.startswith(f"{family}-")

            # Sequence preserved when no collision forced a new suffix. Without a
            # pre-seeded collision the adopted name equals the original flat name,
            # so the original same-date sequence is preserved exactly (Req 11.4).
            if not seed_collisions:
                assert adopted.name == name

        # Collision accounting: with pre-seeding every adopted file collided.
        if seed_collisions:
            assert result.collisions == len(flat_by_name)
        else:
            assert result.collisions == 0

        # ---- idempotency: a SECOND run changes nothing (Req 11.6) ----
        before = _snapshot_tree(settings_root)
        result2 = migrate_flat_archives(archive_root)
        after = _snapshot_tree(settings_root)

        assert result2.adopted == 0
        assert result2.unresolved == 0
        assert result2.collisions == 0
        assert result2.errors == []
        assert before == after, "second migration run mutated the archive tree"
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` (relpath -> bytes) for equality checks."""
    tree: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            tree[str(path.relative_to(root))] = path.read_bytes()
    return tree
