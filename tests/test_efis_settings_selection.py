# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property-based tests for per-threshold import selection (Requirement 16).

Covers the fresh all-selected initialization (Property 33), the apply frame
property over the selected subset (Property 34), Vne unit atomicity
(Property 35), the caution<redline guard over the selected subset (Property 36),
and deselect-all no-op (Property 37).

Requirements: 16.1-16.9
"""

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.efis_settings import (
    VNE_DISABLED_SENTINEL,
    Import_Workflow,
    ParsedBackup,
    content_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parsed(sids: dict, update_value=1, path="Settings-2026-09-12.dat") -> ParsedBackup:
    str_sids = {str(k): str(v) for k, v in sids.items()}
    return ParsedBackup(
        path=path,
        sids=str_sids,
        update_value=update_value,
        checksize="10",
        checksum="ABCD",
        line_count=len(str_sids),
        valid_pairs=len(str_sids),
    )


def _primary_sids(extra=None) -> dict:
    sids = {"1063": "A60670", "387": "1"}
    if extra:
        sids.update(extra)
    return sids


def _config():
    # A config with no prior marker; first-for-source, no bound source.
    return {"analysis_thresholds": {}, "num_cylinders": 4, "settings_import": {}}


# A rich backup that exercises ordinary rows, a tier row, num_cylinders, and Vne.
def _rich_parsed(vne=200, convert=1, rpm=2700, num_cyl=6, cht=400):
    return _parsed(_primary_sids(extra={
        "120": 25, "118": 100,      # oil pressure (ordinary)
        "123": rpm,                 # rpm_redline (ordinary)
        "151": cht,                 # cht (tier choice)
        "22": vne, "889": convert,  # vne unit (two rows)
        "1055": num_cyl,            # num_cylinders
    }))


# ===========================================================================
# Property 33: Preview initializes fresh with every row selected
# Feature: efis-settings-import, Property 33: Preview initializes fresh with
#   every row selected
# Validates: Requirements 16.1, 16.2, 16.8
# ===========================================================================
@settings(max_examples=100)
@given(
    vne=st.integers(100, 300),
    convert=st.sampled_from([0, 1]),
    rpm=st.integers(2000, 3000),
    num_cyl=st.sampled_from([4, 6]),
)
def test_property33_fresh_all_selected(vne, convert, rpm, num_cyl):
    parsed = _rich_parsed(vne=vne, convert=convert, rpm=rpm, num_cyl=num_cyl)
    config = _config()

    preview = Import_Workflow.build_preview(parsed, config, selected_keys=None)
    # Every row (including num_cylinders and both Vne rows) is selected.
    assert preview.changes
    assert all(c.selected for c in preview.changes)
    assert any(c.key == "num_cylinders" and c.selected for c in preview.changes)
    vne_rows = [c for c in preview.changes if c.selection_key == "vne"]
    assert len(vne_rows) == 2 and all(c.selected for c in vne_rows)

    # Rebuilding yields all-selected again — nothing carried over.
    preview2 = Import_Workflow.build_preview(parsed, config, selected_keys=None)
    assert all(c.selected for c in preview2.changes)


# ===========================================================================
# Property 34: Apply writes exactly the selected subset (frame property)
# Feature: efis-settings-import, Property 34: Apply writes exactly the selected
#   subset (frame property)
# Validates: Requirements 16.5
# ===========================================================================
@settings(max_examples=150)
@given(pick=st.lists(st.booleans(), min_size=0, max_size=12))
def test_property34_apply_writes_selected_subset(pick):
    parsed = _rich_parsed()
    config = _config()
    # Build with all selected to enumerate the available selection keys.
    full = Import_Workflow.build_preview(parsed, config, selected_keys=None)
    all_sel_keys = []
    for c in full.changes:
        if c.selection_key not in all_sel_keys:
            all_sel_keys.append(c.selection_key)

    # Choose a subset S from the available selection keys.
    S = {k for k, keep in zip(all_sel_keys, pick) if keep}

    preview = Import_Workflow.build_preview(parsed, config, selected_keys=S)
    # Guard may block if a selected caution >= redline; skip the frame check for
    # blocked previews (apply would be a no-op) — but with only redline tier
    # defaults there is no caution/redline pairing, so can_apply holds.
    if not preview.can_apply:
        return

    before = copy.deepcopy(config)
    result = Import_Workflow.apply(
        preview, True, True, copy.deepcopy(config),
        save=lambda c: None, selected_keys=S,
    )

    if not S:
        # Empty set => no-op (also covered by Property 37).
        assert result.applied is False
        return

    assert result.applied
    out_thr = result.config.get("analysis_thresholds", {})
    cur_thr = before.get("analysis_thresholds", {})

    for c in preview.changes:
        if c.key == "num_cylinders":
            if c.selection_key in S:
                assert result.config["num_cylinders"] == c.proposed
            else:
                assert result.config["num_cylinders"] == before["num_cylinders"]
            continue
        if c.selection_key in S:
            assert out_thr.get(c.key) == c.proposed
        else:
            # Unselected threshold stays byte-identical to its current value.
            assert out_thr.get(c.key) == cur_thr.get(c.key)


# ===========================================================================
# Property 35: Vne unit is atomic
# Feature: efis-settings-import, Property 35: Vne unit is atomic
# Validates: Requirements 16.3, 16.4
# ===========================================================================
@settings(max_examples=150)
@given(
    vne=st.integers(100, 300),
    convert=st.sampled_from([0, 1]),
    include_vne=st.booleans(),
)
def test_property35_vne_unit_atomic(vne, convert, include_vne):
    parsed = _rich_parsed(vne=vne, convert=convert)
    config = _config()

    # Selecting "vne" writes BOTH keys preserving SID-889 exclusivity.
    S = {"vne"} if include_vne else set()
    # Always include a benign ordinary key so the empty-set no-op path isn't hit
    # when include_vne is False.
    S = S | {"rpm_redline"}

    preview = Import_Workflow.build_preview(parsed, config, selected_keys=S)
    result = Import_Workflow.apply(
        preview, True, True, copy.deepcopy(config),
        save=lambda c: None, selected_keys=S,
    )
    assert result.applied
    thr = result.config["analysis_thresholds"]

    if include_vne:
        # Both vne_* keys written, exactly one below the sentinel.
        assert "vne_tas_redline" in thr and "vne_ias_redline" in thr
        active = [k for k in ("vne_tas_redline", "vne_ias_redline")
                  if thr[k] < VNE_DISABLED_SENTINEL]
        assert len(active) == 1
        other = ("vne_ias_redline" if active[0] == "vne_tas_redline"
                 else "vne_tas_redline")
        assert thr[other] == VNE_DISABLED_SENTINEL
    else:
        # Neither vne_* key written when the Vne unit is not selected.
        assert "vne_tas_redline" not in thr
        assert "vne_ias_redline" not in thr


def test_property35_no_selected_keys_writes_exactly_one_active_vne():
    # There is NO selected_keys set that causes apply to write exactly ONE
    # vne_* key: they always move together. Enumerate a few sets that mention a
    # single vne_* key by its threshold key (not "vne") — those are never the
    # selection_key, so they never write a lone vne_* key.
    parsed = _rich_parsed(vne=180, convert=1)
    config = _config()
    for S in ({"vne_tas_redline"}, {"vne_ias_redline"}, {"vne_tas_redline", "rpm_redline"}):
        preview = Import_Workflow.build_preview(parsed, config, selected_keys=S)
        result = Import_Workflow.apply(
            preview, True, True, copy.deepcopy(config),
            save=lambda c: None, selected_keys=S,
        )
        thr = result.config.get("analysis_thresholds", {}) if result.applied else {}
        # A lone vne threshold key is not a selection_key ("vne" is), so neither
        # vne_* key is ever written this way.
        assert "vne_tas_redline" not in thr
        assert "vne_ias_redline" not in thr


# ===========================================================================
# Property 36: Guard evaluates over the selected subset
# Feature: efis-settings-import, Property 36: Guard evaluates over the selected
#   subset
# Validates: Requirements 16.6
# ===========================================================================
@settings(max_examples=200)
@given(
    cht=st.integers(300, 500),
    tier=st.sampled_from(["caution", "redline"]),
    select_cht=st.booleans(),
)
def test_property36_guard_over_selected_subset(cht, tier, select_cht):
    # Current config has a cht_caution and cht_redline so a selected single-limit
    # tier choice can conflict with the OTHER tier's current value.
    config = {
        "analysis_thresholds": {"cht_caution": 380, "cht_redline": 420},
        "num_cylinders": 4,
        "settings_import": {},
    }
    # Only map CHT (single tier) so the guard is exercised in isolation.
    parsed = _parsed(_primary_sids(extra={"151": cht}))
    tier_selections = {"cht": tier}

    chosen_key = "cht_caution" if tier == "caution" else "cht_redline"
    S = {chosen_key} if select_cht else set()
    # keep the set non-empty with a benign key when not selecting cht
    S = S | {"rpm_redline"} if not select_cht else S

    preview = Import_Workflow.build_preview(
        parsed, config, tier_selections=tier_selections, selected_keys=S,
    )

    # Compute expectation over the selected subset.
    cur = {"cht_caution": 380, "cht_redline": 420}
    expect_conflict = False
    if select_cht:
        if tier == "caution":
            caution_val = round(cht)
            redline_val = cur["cht_redline"]  # redline not selected -> current
        else:
            redline_val = round(cht)
            caution_val = cur["cht_caution"]  # caution not selected -> current
        expect_conflict = caution_val >= redline_val

    assert preview.can_apply == (not expect_conflict)


# ===========================================================================
# Property 37: Deselect-all is a no-op
# Feature: efis-settings-import, Property 37: Deselect-all is a no-op
# Validates: Requirements 16.7
# ===========================================================================
@settings(max_examples=100)
@given(vne=st.integers(100, 300), rpm=st.integers(2000, 3000))
def test_property37_deselect_all_noop(vne, rpm):
    parsed = _rich_parsed(vne=vne, rpm=rpm)
    config = _config()
    preview = Import_Workflow.build_preview(parsed, config, selected_keys=set())

    before = copy.deepcopy(config)
    result = Import_Workflow.apply(
        preview, True, True, config, save=lambda c: None, selected_keys=set(),
    )
    assert result.applied is False
    assert "nothing selected" in result.reason.lower()
    # Config byte-identical to the input.
    assert result.config == before
