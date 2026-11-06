# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the build_jobs manifest-diff wiring (task 6.2).

Feature: chart-sync-wiring-fix, Property 1: build_jobs populates the manifest
diff.

``drive_updater.build_jobs`` (design Section B, R2.1 / R2.6) must, for a chart
family with a valid card manifest present and a local manifest whose cycle has
ROLLED relative to the card (so ``decide_streams`` returns ``"diff"``, not
``"skip-cycle"``), populate the returned ``SyncJob``'s ``files_from`` with the
added-plus-changed relpaths and ``removed`` with the removed relpaths, computed
by ``diff_family`` against that family's card entries.

Asserting equality against ``diff_family(local, card.families[name].entries)``
proves BOTH that the diff is populated (R2.1) AND that ``decide_streams`` and
``diff_family`` are reached as real production callers through ``build_jobs``
(R2.6): if ``decide_streams`` were not wired, the family would never resolve to
``"diff"``; if ``diff_family`` were not wired, ``files_from`` / ``removed``
would stay at their legacy ``None``.

Each example uses fresh temp dirs (isolated local manifest dir + a temp card
mount) cleaned up in a ``finally`` block so the run is Hypothesis-safe and never
touches the real app-support dir or a real volume. ``load_config`` is patched so
``usb_image_path`` points at a throwaway local root, and ``_volume_name`` is
patched to ``None`` so the gate's label cross-check never shells out to
``diskutil``.

Validates: Requirements 2.1, 2.6
"""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Store,
    ManifestEntry,
)


# --- generators -------------------------------------------------------------

# A lowercase-hex SHA-256 digest: exactly 64 hex chars, matching the builder.
_SHA256 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

# Non-negative byte count.
_SIZE = st.integers(min_value=0, max_value=2**31)

# POSIX relpath keys: 1-3 filesystem-safe segments joined by "/". A leading "f"
# guarantees we never produce "." / ".." nor a leading dot.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789" + "".join(("_", "-")),
    ),
    min_size=1,
    max_size=8,
).map(lambda s: "f" + s)

_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

# A 4-digit cycle number (e.g. "2611").
_CYCLE = st.text(alphabet="0123456789", min_size=4, max_size=4)


@st.composite
def _entries(draw):
    """Generate a relpath -> ManifestEntry map (a modeled family tree)."""
    pool = draw(st.lists(_RELPATH, min_size=0, max_size=8, unique=True))
    return {
        rel: ManifestEntry(relpath=rel, sha256=draw(_SHA256), size=draw(_SIZE))
        for rel in pool
    }


def _rolled(cycle: str) -> str:
    """Return a 4-digit cycle guaranteed to differ from ``cycle``.

    A rolled cycle makes ``decide_streams`` choose ``"diff"`` (never
    ``"skip-cycle"``) for the family, which is the precondition for the diff
    being wired into the job.
    """
    first = "0" if cycle[0] != "0" else "1"
    return first + cycle[1:]


# --- the property -----------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    family=st.sampled_from(["scanned", "plates"]),
    local_entries=_entries(),
    card_entries=_entries(),
    card_cycle=_CYCLE,
)
def test_build_jobs_populates_manifest_diff(
    family, local_entries, card_entries, card_cycle
):
    """build_jobs sets files_from/removed from the family's manifest diff.

    For a chart family with a valid card manifest whose family cycle has rolled
    (so the gate diffs, not skips) and a saved local manifest, the job returned
    by ``build_jobs`` carries exactly the ``diff_family`` result:
    ``files_from == list(diff.files_from)`` and ``removed == diff.removed``. This
    proves ``decide_streams`` and ``diff_family`` are both reached through
    ``build_jobs`` (R2.6) and the diff is populated (R2.1).

    Validates: Requirements 2.1, 2.6
    """
    local_manifest_dir = tempfile.mkdtemp(prefix="efis_manifests_")
    card_mount = tempfile.mkdtemp(prefix="efis_card_")
    local_root = tempfile.mkdtemp(prefix="efis_local_")
    try:
        # Local cycle is the card cycle ROLLED, so decide_streams returns
        # "diff" for this family and never "skip-cycle".
        local_cycle = _rolled(card_cycle)

        local_fam = FamilyManifest(
            family=family,
            cycle=local_cycle,
            entries=dict(local_entries),
            built="2026-10-24T18:03:11-07:00",
        )

        # The card family records a DIFFERENT cycle and arbitrary entries.
        card_fam = FamilyManifest(
            family=family,
            cycle=card_cycle,
            entries=dict(card_entries),
            built="2026-10-24T18:40:52-07:00",
        )
        card = CardManifest(
            charts_cycle=(card_cycle if family == "scanned" else None),
            plates_cycle=(card_cycle if family == "plates" else None),
            families={family: card_fam},
        )

        with mock.patch.object(mf, "LOCAL_MANIFEST_DIR", Path(local_manifest_dir)):
            # Persist the local + card manifests exactly where build_jobs reads
            # them (save_local -> LOCAL_MANIFEST_DIR, save_card -> volume root).
            Manifest_Store.save_local(local_fam)
            Manifest_Store.save_card(card_mount, card)

            # The expected diff — the exact computation build_jobs must perform.
            expected = mf.diff_family(local_fam, card_fam.entries)

            with mock.patch.object(
                du, "load_config", lambda: {"usb_image_path": local_root}
            ), mock.patch.object(du, "_volume_name", lambda mp: None):
                jobs = du.build_jobs(card_mount, [family])

        # The gate diffed (did not skip), so exactly one job comes back.
        assert len(jobs) == 1
        job = jobs[0]
        assert job.name == family
        assert job.kind == "tree"

        # R2.1 / R2.6: files_from == added+changed, removed == removed, both
        # from diff_family — proving decide_streams AND diff_family are wired.
        assert job.files_from == list(expected.files_from)
        assert job.removed == list(expected.removed)

        # Sanity: the diff relpaths partition into added/changed vs removed as
        # diff_family defines them, so the job is not a legacy (None) job.
        assert job.files_from is not None
        assert job.removed is not None
    finally:
        shutil.rmtree(local_manifest_dir, ignore_errors=True)
        shutil.rmtree(card_mount, ignore_errors=True)
        shutil.rmtree(local_root, ignore_errors=True)
