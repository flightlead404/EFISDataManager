# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for skip-cycle job omission in build_jobs (task 6.3).

Feature: chart-sync-wiring-fix, Property 2: skip-cycle omits the transfer job.

Design Section B — when ``decide_streams`` returns ``skip-cycle`` for a chart
family (the locally downloaded cycle equals the Card_Manifest's recorded cycle
for that family, which implies equal content because a cycle is only stamped
after a verified full-convergence sync), ``build_jobs`` MUST NOT emit a transfer
job for that family: ``_chart_family_job`` returns ``None`` and ``build_jobs``
filters it out. The nav family is never cycle-gated and is always present.

Property 2 asserts, across arbitrary 4-digit cycle strings and every
match/no-match combination of the two chart families:

  * (R2.2) a chart family whose local cycle EQUALS the card's recorded cycle for
    that family is ABSENT from ``build_jobs``' output (skip-cycle omission);
  * the contrast case — a chart family whose local cycle does NOT match the
    card's recorded cycle is PRESENT (so the test cannot pass vacuously by
    always omitting); and
  * (R2.5) the nav job is ALWAYS present regardless of either cycle, because nav
    carries no manifest and no cycle gate.

scanned is gated on ``charts_cycle``; plates is gated on ``plates_cycle``. Each
example isolates the local manifest dir (``manifest.LOCAL_MANIFEST_DIR``), pins
``drive_updater.load_config`` to a temp local root and ``drive_updater._volume_name``
to ``None`` (so the label cross-check never interferes), seeds a card manifest via
``Manifest_Store.save_card`` and both local family manifests via
``Manifest_Store.save_local``, then calls ``build_jobs`` and inspects which family
names survive. Fresh temp dirs per example, cleaned in a ``finally`` block, with
the monkeypatched module globals saved/restored per example (Hypothesis flags a
reused function-scoped fixture across generated examples).

Validates: Requirements 2.2
"""

import os
import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as m
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Store,
)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# An arbitrary 4-digit cycle string, e.g. "2611". Cycles are compared as
# strings by decide_streams, so a zero-padded 4-digit string is faithful to the
# real cycle format (YYMM).
_CYCLE = st.integers(min_value=0, max_value=9999).map(lambda n: f"{n:04d}")

# Whether each chart family's local cycle MATCHES the card's recorded cycle for
# that family (-> skip-cycle, job omitted) or NOT (-> diff, job present).
_MATCH = st.booleans()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _non_matching_cycle(cycle: str) -> str:
    """Return a 4-digit cycle string guaranteed to differ from ``cycle``."""
    other = (int(cycle) + 1) % 10000
    return f"{other:04d}"


def _job_names(mount_point: str) -> set:
    """Family names emitted by build_jobs for all three requested families."""
    jobs = du.build_jobs(mount_point, ["scanned", "plates", "nav"])
    return {job.name for job in jobs}


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(
    local_charts=_CYCLE,
    local_plates=_CYCLE,
    scanned_matches=_MATCH,
    plates_matches=_MATCH,
)
def test_skip_cycle_omits_the_transfer_job(
    local_charts, local_plates, scanned_matches, plates_matches
):
    """A chart family whose local cycle == the card cycle is omitted; a family
    whose cycle differs is present; nav is always present.

    Varying which family matches (and asserting the contrast case) ensures the
    property cannot pass vacuously by always omitting or always keeping a job.

    Validates: Requirements 2.2
    """
    # Fresh, isolated temp roots per example.
    local_root = tempfile.mkdtemp(prefix="efis_local_")
    mount_point = tempfile.mkdtemp(prefix="efis_card_")
    manifests_dir = tempfile.mkdtemp(prefix="efis_manifests_")

    # Save and restore the monkeypatched module globals per example (a reused
    # function-scoped fixture would be set up only once across all examples).
    saved_local_dir = m.LOCAL_MANIFEST_DIR
    saved_load_config = du.load_config
    saved_volume_name = du._volume_name

    try:
        m.LOCAL_MANIFEST_DIR = Path(manifests_dir)
        du.load_config = lambda: {"usb_image_path": local_root}
        du._volume_name = lambda mp: None  # no label cross-check interference

        # The card's recorded cycle per family: equal to local (-> skip) when
        # this family "matches", otherwise a guaranteed-different cycle.
        card_charts = (
            local_charts if scanned_matches else _non_matching_cycle(local_charts)
        )
        card_plates = (
            local_plates if plates_matches else _non_matching_cycle(local_plates)
        )

        # Seed the card manifest. Per-family entry maps can be empty; the
        # skip-cycle decision is driven solely by the top-level cycle values.
        card = CardManifest(
            charts_cycle=card_charts,
            plates_cycle=card_plates,
            families={
                "scanned": FamilyManifest(family="scanned", cycle=card_charts),
                "plates": FamilyManifest(family="plates", cycle=card_plates),
            },
        )
        Manifest_Store.save_card(mount_point, card)

        # Save BOTH local family manifests. A missing local manifest would make
        # _chart_family_job fall back to a legacy job (present) rather than
        # reaching the skip-cycle decision, so both must exist for the gate to
        # engage. scanned's cycle is the charts cycle; plates' is the plates
        # cycle (how _chart_family_job assembles LocalCycles).
        Manifest_Store.save_local(
            FamilyManifest(family="scanned", cycle=local_charts)
        )
        Manifest_Store.save_local(
            FamilyManifest(family="plates", cycle=local_plates)
        )

        names = _job_names(mount_point)

        # (R2.5) nav is never cycle-gated — always present.
        assert "nav" in names

        # (R2.2) scanned: matching charts cycle -> skip-cycle -> ABSENT;
        # non-matching -> diff -> PRESENT (contrast case).
        if scanned_matches:
            assert "scanned" not in names
        else:
            assert "scanned" in names

        # (R2.2) plates: matching plates cycle -> skip-cycle -> ABSENT;
        # non-matching -> diff -> PRESENT (contrast case).
        if plates_matches:
            assert "plates" not in names
        else:
            assert "plates" in names
    finally:
        m.LOCAL_MANIFEST_DIR = saved_local_dir
        du.load_config = saved_load_config
        du._volume_name = saved_volume_name
        shutil.rmtree(local_root, ignore_errors=True)
        shutil.rmtree(mount_point, ignore_errors=True)
        shutil.rmtree(manifests_dir, ignore_errors=True)
