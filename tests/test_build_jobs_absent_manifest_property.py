# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the absent/corrupt-card-manifest job in build_jobs.

manifest-only-chart-sync R2.1/R2.3 RETIRED the legacy size-only fallback. The
OLD contract (chart-sync-wiring-fix Property 3) was: an absent/corrupt card
manifest yields a Legacy_Job (``files_from is None``) that runs
``rsync --size-only --delete``. That path compared by byte size only and
silently skipped a same-size content change — the flight-safety gap this spec
closes — so it is gone.

NEW contract (design Section A, R2.3): when ``build_jobs`` builds a chart-family
job and the Card_Manifest (``EFIS_MANIFEST.json`` at the volume root) is ABSENT
or CORRUPT, ``Manifest_Store.load_card`` returns ``None`` and
``_chart_family_job`` builds a MANIFEST job — it diffs the local manifest
(building it from the SSD source if missing) against an EMPTY card family, i.e.
a full content write. The emitted job is therefore ``kind == "tree"`` with a
populated ``files_from`` list (NEVER ``None``) and a ``removed`` list (empty when
the card has nothing to prune). This holds for EVERY chart family and regardless
of whether a local manifest happens to exist.

This property asserts, across both chart families, both "no manifest file at
all" and "corrupt/unparseable manifest file" card states, and an arbitrary
presence of a local manifest:

  * (R2.1/R2.3) every chart-family job is a MANIFEST job -> ``files_from is not
    None`` (a list) AND ``removed is not None`` (a list) — never the retired
    legacy size-only job;
  * the job is ``kind == "tree"`` with the SAME payload roots, excludes, and
    markers as ``_chart_tree_job_skeleton`` (the roots/excludes/markers are
    unchanged from the old legacy builder); and
  * the nav job is present and unchanged (``kind == "files"``, the NAV DBs,
    ``files_from``/``removed`` None), because nav carries no manifest and no
    cycle gate (R3.4).

Each example isolates the local manifest dir (``manifest.LOCAL_MANIFEST_DIR``),
pins ``drive_updater.load_config`` to a throwaway temp local root and
``drive_updater._volume_name`` to ``None`` (so the gate's label cross-check
never shells out to ``diskutil``), and uses fresh temp dirs created inside the
test and torn down in a ``finally`` block so the run is Hypothesis-safe and
never touches the real app-support dir or a real volume. The monkeypatched
module globals are saved/restored per example for the same reason.

Validates: Requirements 2.1, 2.3
"""

import os
import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as m
from efis_data_manager.manifest import (
    CARD_MANIFEST_NAME,
    FamilyManifest,
    Manifest_Store,
)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# The two card states that both make load_card return None (treating the card as
# having no valid manifest): no manifest file at all, or a present-but-unparseable
# one. Varying this proves absent AND corrupt both yield a full-write manifest job.
_CARD_STATE = st.sampled_from(["absent", "corrupt"])

# Bodies for the corrupt-manifest case. Two classes, both of which
# Manifest_Store.load_card treats as "no valid manifest" (returns None):
#
#   1. Unparseable bytes -> json.load raises JSONDecodeError -> None.
#   2. Parseable JSON that IS an object but is not a valid manifest shape
#      (card_from_json raises KeyError/TypeError/ValueError) -> None.
#
# NOTE: parseable JSON that is a *scalar* or *list* (e.g. ``0``, ``"x"``,
# ``true``, ``[]``) is deliberately NOT generated here. Such a file makes
# card_from_json call ``raw.get(...)`` on a non-dict and raise AttributeError,
# which load_card does not catch today -- a production robustness gap against
# R3.3 / the "Never raises" load_card contract, out of scope for this test task
# (see the task report). This generator stays within the corrupt inputs the
# spec names ("corrupt/unparseable"), so the property it asserts is sound.
_CORRUPT_BODY = st.one_of(
    # Unparseable: non-JSON text that never begins a JSON value. Surrogates
    # (category "Cs", U+D800-U+DFFF) are excluded: the corrupt body is written
    # to the manifest path in TEXT mode, and a lone surrogate is not UTF-8
    # encodable (it would raise UnicodeEncodeError in the test harness, not
    # exercise load_card). Excluding them keeps the generated body a real,
    # writable "corrupt/unparseable manifest" input, which is all this property
    # needs.
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),
            blacklist_characters='{}[]"0123456789 \t\n\r',
        ),
        min_size=1,
        max_size=64,
    ).map(lambda s: "x" + s),
    st.just("{not: valid json"),
    st.just("\x00\x01\x02 not json"),
    # Parseable JSON OBJECT, invalid manifest shape: card_from_json raises
    # ValueError on int("banana") and load_card returns None.
    st.just('{"schema_version": "banana"}'),
)

# A 4-digit cycle string for the optional local manifest.
_CYCLE = st.integers(min_value=0, max_value=9999).map(lambda n: f"{n:04d}")


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(
    card_state=_CARD_STATE,
    corrupt_body=_CORRUPT_BODY,
    local_present=st.booleans(),
    local_cycle=_CYCLE,
)
def test_absent_manifest_yields_manifest_job(
    card_state, corrupt_body, local_present, local_cycle
):
    """An absent or corrupt card manifest yields a MANIFEST job for every chart
    family (full write, diff vs empty); nav is present and unchanged.

    For a mount whose card manifest is either missing entirely or
    corrupt/unparseable, ``build_jobs`` returns, for each requested chart family,
    a ``kind == "tree"`` job whose ``files_from`` is a populated list (NEVER
    ``None``) and whose ``removed`` is a list, with payload roots / excludes /
    markers matching ``_chart_tree_job_skeleton``. The presence of a local
    manifest is varied and must not change the shape of the job (the local
    manifest is built from the SSD source when absent). nav is always present and
    unchanged.

    Validates: Requirements 2.1, 2.3
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

        # Put the card into the chosen no-valid-manifest state. In "absent" we
        # write nothing (there is simply no EFIS_MANIFEST.json). In "corrupt" we
        # write unparseable / invalid bytes to the manifest path at the volume
        # root. Either way Manifest_Store.load_card returns None.
        card_path = os.path.join(mount_point, CARD_MANIFEST_NAME)
        if card_state == "corrupt":
            with open(card_path, "w") as f:
                f.write(corrupt_body)
        assert Manifest_Store.load_card(mount_point) is None

        # Optionally seed local family manifests. Their presence must NOT matter:
        # the absent/corrupt card manifest forces legacy before load_local is
        # even reached in _chart_family_job.
        if local_present:
            Manifest_Store.save_local(
                FamilyManifest(family="scanned", cycle=local_cycle)
            )
            Manifest_Store.save_local(
                FamilyManifest(family="plates", cycle=local_cycle)
            )

        # Request all three families (chart families + nav).
        jobs = du.build_jobs(mount_point, ["scanned", "plates", "nav"])
        jobs_by_name = {job.name: job for job in jobs}

        # Every chart family is present as a MANIFEST job (absent manifest never
        # omits a family — only skip-cycle does, which requires a valid card).
        for name in ("scanned", "plates"):
            assert name in jobs_by_name, f"{name} job missing for a chart family"
            job = jobs_by_name[name]

            # (R2.1/R2.3) MANIFEST job: files_from AND removed are LISTS (never
            # None). An absent/corrupt card manifest diffs the local manifest
            # against an empty card family -> a full content write, not the
            # retired size-only legacy job. The local image is empty here, so the
            # change set happens to be empty — but it is a list, not None.
            assert job.files_from is not None
            assert isinstance(job.files_from, list)
            assert job.removed is not None
            assert isinstance(job.removed, list)

            # A tree job whose payload roots / excludes / markers match the
            # shared skeleton exactly (the roots/excludes/markers are unchanged
            # from the retired legacy builder).
            expected = du._chart_tree_job_skeleton(name, mount_point, local_root)
            assert job.kind == "tree"
            assert job.payload_root_local == expected.payload_root_local
            assert job.payload_root_drive == expected.payload_root_drive
            assert job.excludes == expected.excludes
            assert job.marker_src == expected.marker_src
            assert job.marker_dst == expected.marker_dst

        # nav is always present and unchanged by the manifest wiring (R3.4).
        assert "nav" in jobs_by_name
        nav = jobs_by_name["nav"]
        assert nav.kind == "files"
        assert nav.files == list(du.NAV_FILES)
        assert nav.files_from is None
        assert nav.removed is None
    finally:
        m.LOCAL_MANIFEST_DIR = saved_local_dir
        du.load_config = saved_load_config
        du._volume_name = saved_volume_name
        shutil.rmtree(local_root, ignore_errors=True)
        shutil.rmtree(mount_point, ignore_errors=True)
        shutil.rmtree(manifests_dir, ignore_errors=True)
