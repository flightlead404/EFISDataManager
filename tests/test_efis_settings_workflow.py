# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property-based tests for the Import_Workflow (efis_settings.py).

Covers the mapper-side Vne activeness (Property 14), preview / apply gating
(Properties 16-19), source-keyed regression-proof detection (Properties 23-27),
and the primary-display import gate (Properties 28-32).

The primary/source-keyed detection properties (23-32) exercise the pure
``_decide`` helper directly so no archive filesystem setup is needed, exactly as
the design describes; ``detect_new_backup`` is the thin archive-I/O wrapper
around it.

Requirements: 5.5, 5.6, 8.2, 9.2-9.6, 10.3, 13.3, 14, 15
"""

import copy

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from efis_data_manager import analysis
from efis_data_manager.efis_settings import (
    BOUND_IMPORT_SOURCE_KEY,
    VNE_DISABLED_SENTINEL,
    Import_Workflow,
    ParsedBackup,
    Settings_Mapper,
    _decide,
    content_hash,
    extract_source_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parsed(sids: dict, update_value=1, path="Settings.dat") -> ParsedBackup:
    """Build a ParsedBackup from a SID->value dict (values stringified)."""
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


def _primary_sids(mode_s="A60670", link_id="1", extra=None) -> dict:
    """Identity SIDs for a Primary_Display, plus optional extra SIDs."""
    sids = {"1063": mode_s, "387": link_id}
    if extra:
        sids.update(extra)
    return sids


def _marker(update_value, chash="hashval", imported_at="2026-01-01T00:00:00"):
    return {
        "last_update_value": update_value,
        "content_hash": chash,
        "backup_name": "Settings-2026-01-01.dat",
        "imported_at": imported_at,
    }


# ===========================================================================
# Property 14 (mapper side): exactly one Vne key active after mapping
# Feature: efis-settings-import, Property 14: Active airspeed limit alerts iff
#   its airspeed reaches it
# Validates: Requirements 5.5, 5.6
# ===========================================================================
@settings(max_examples=200)
@given(vne=st.integers(1, 400), convert=st.sampled_from([0, 1]))
def test_property14_exactly_one_vne_active(vne, convert):
    parsed = _parsed({"22": vne, "889": convert})
    mapped = Settings_Mapper.map(parsed)
    by_key = {mv.threshold_key: mv.value for mv in mapped.values}

    # Both keys are emitted; exactly one is active (below the sentinel).
    assert "vne_tas_redline" in by_key
    assert "vne_ias_redline" in by_key
    active = [
        k for k in ("vne_tas_redline", "vne_ias_redline")
        if by_key[k] < VNE_DISABLED_SENTINEL
    ]
    assert len(active) == 1
    if convert == 1:
        assert by_key["vne_tas_redline"] == round(vne)
        assert by_key["vne_ias_redline"] == VNE_DISABLED_SENTINEL
    else:
        assert by_key["vne_ias_redline"] == round(vne)
        assert by_key["vne_tas_redline"] == VNE_DISABLED_SENTINEL


# ===========================================================================
# Property 16: Preview completeness
# Feature: efis-settings-import, Property 16: Preview completeness
# Validates: Requirements 9.2, 13.3
# ===========================================================================
@settings(max_examples=150)
@given(
    oil_low=st.integers(20, 80),
    oil_high=st.integers(90, 120),
    rpm=st.integers(2000, 3000),
    num_cyl=st.sampled_from([4, 6]),
    vne=st.integers(100, 300),
)
def test_property16_preview_completeness(oil_low, oil_high, rpm, num_cyl, vne):
    parsed = _parsed(_primary_sids(extra={
        "120": oil_low,
        "118": oil_high,
        "123": rpm,
        "1055": num_cyl,
        "22": vne,
        "889": 1,
    }))
    config = {"analysis_thresholds": {}, "num_cylinders": 4, "settings_import": {}}
    preview = Import_Workflow.build_preview(parsed, config, tier_selections={})

    mapped = Settings_Mapper.map(parsed)
    # Expected mapped keys: every direct value key + num_cylinders (tier choices
    # here are none since we passed no 151/144/121). Each change carries current
    # + proposed (proposed always set; current may legitimately be None).
    expected_keys = {mv.threshold_key for mv in mapped.values}
    expected_keys.add("num_cylinders")

    change_keys = [c.key for c in preview.changes]
    # Exactly one ThresholdChange per mapped key (no duplicates).
    assert sorted(change_keys) == sorted(expected_keys)
    assert len(change_keys) == len(set(change_keys))
    for c in preview.changes:
        assert c.proposed is not None
        # current attribute is always present (may be None if key unset).
        assert hasattr(c, "current")


# ===========================================================================
# Property 17: Caution-below-redline guard
# Feature: efis-settings-import, Property 17: Caution-below-redline guard
# Validates: Requirements 8.2
# ===========================================================================
@settings(max_examples=200)
@given(
    cht_limit=st.integers(300, 500),
    tier=st.sampled_from(["caution", "redline"]),
    cur_caution=st.integers(300, 460),
    cur_redline=st.integers(300, 460),
)
def test_property17_caution_below_redline_guard(cht_limit, tier, cur_caution, cur_redline):
    parsed = _parsed(_primary_sids(extra={"151": cht_limit}))
    config = {
        "analysis_thresholds": {
            "cht_caution": cur_caution,
            "cht_redline": cur_redline,
        },
        "num_cylinders": 4,
        "settings_import": {},
    }
    preview = Import_Workflow.build_preview(
        parsed, config, tier_selections={"cht": tier}
    )

    # Resolve proposed caution / redline for CHT after applying the tier choice.
    if tier == "caution":
        caution_val = cht_limit
        redline_val = cur_redline
    else:
        caution_val = cur_caution
        redline_val = cht_limit

    expected_ok = caution_val < redline_val
    assert preview.can_apply == expected_ok
    if not expected_ok:
        assert any("caution" in w.lower() for w in preview.warnings)


# ===========================================================================
# Property 18: Apply is gated on double-confirmation and no-op otherwise
# Feature: efis-settings-import, Property 18: Apply is gated on double-
#   confirmation and no-op otherwise
# Validates: Requirements 9.4, 9.6, 10.3
# ===========================================================================
@settings(max_examples=200)
@given(
    confirm1=st.booleans(),
    confirm2=st.booleans(),
    can_apply=st.booleans(),
    rpm=st.integers(2000, 3000),
)
def test_property18_apply_double_confirm_gate(confirm1, confirm2, can_apply, rpm):
    parsed = _parsed(_primary_sids(extra={"123": rpm}), update_value=50)
    config = {"analysis_thresholds": {}, "num_cylinders": 4, "settings_import": {}}
    preview = Import_Workflow.build_preview(parsed, config, tier_selections={})
    # Force the can_apply flag under test.
    preview.can_apply = can_apply

    saved = {}

    def fake_save(cfg):
        saved["cfg"] = copy.deepcopy(cfg)

    before = copy.deepcopy(config)
    result = Import_Workflow.apply(preview, confirm1, confirm2, config, save=fake_save)

    should_apply = confirm1 and confirm2 and can_apply
    assert result.applied == should_apply
    if should_apply:
        assert "cfg" in saved
    else:
        # No-op: config byte-identical, and no save call happened.
        assert result.config == before
        assert "cfg" not in saved


# ===========================================================================
# Property 19: Apply updates only mapped keys (frame property)
# Feature: efis-settings-import, Property 19: Apply updates only mapped keys
# Validates: Requirements 9.5
# ===========================================================================
@settings(max_examples=150)
@given(
    rpm=st.integers(2000, 3000),
    oil_high=st.integers(90, 120),
    num_cyl=st.sampled_from([4, 6]),
    extra_keys=st.dictionaries(
        st.text(min_size=1, max_size=6).filter(
            lambda s: s not in ("analysis_thresholds", "num_cylinders", "settings_import")
        ),
        st.integers(-100, 100),
        max_size=4,
    ),
)
def test_property19_apply_frame_property(rpm, oil_high, num_cyl, extra_keys):
    parsed = _parsed(_primary_sids(extra={
        "123": rpm, "118": oil_high, "1055": num_cyl,
    }), update_value=77)
    config = {
        "analysis_thresholds": {"cht_caution": 380},
        "num_cylinders": 4,
        "settings_import": {},
    }
    config.update(extra_keys)

    preview = Import_Workflow.build_preview(parsed, config, tier_selections={})
    assert preview.can_apply

    before = copy.deepcopy(config)
    result = Import_Workflow.apply(preview, True, True, config, save=lambda c: None)
    assert result.applied

    new = result.config
    # Every mapped threshold key equals its proposed value.
    for change in preview.changes:
        if change.key == "num_cylinders":
            assert new["num_cylinders"] == change.proposed
        else:
            assert new["analysis_thresholds"][change.key] == change.proposed

    # Non-mapped, unrelated top-level keys are unchanged.
    mapped_thr = {c.key for c in preview.changes if c.key != "num_cylinders"}
    for k, v in before.items():
        if k in ("analysis_thresholds", "num_cylinders", "settings_import"):
            continue
        assert new[k] == v
    # Pre-existing thresholds not touched by the import are preserved.
    for k, v in before["analysis_thresholds"].items():
        if k not in mapped_thr:
            assert new["analysis_thresholds"][k] == v


# ===========================================================================
# Property 23: Source-keyed detection — offered iff first-for-source or content
#   changed (refined by Req 17.5 to Content_Hash, not UPDATE= magnitude)
# Feature: efis-settings-import, Property 23: Source-keyed detection — offered
#   iff first-for-source or content changed
# Validates: Requirements 14.1, 14.3, 14.4, 17.5
# ===========================================================================
@settings(max_examples=200)
@given(
    # UPDATE= is arbitrary and must NOT drive the offer (Req 17.2/17.5); the
    # offer is decided by Content_Hash difference. We vary a real content SID so
    # the candidate's hash matches or differs from the marker independently.
    marker_update=st.integers(0, 500),
    cand_update=st.integers(0, 500),
    has_marker=st.booleans(),
    content_changed=st.booleans(),
)
def test_property23_offered_iff_first_or_content_changed(
    marker_update, cand_update, has_marker, content_changed
):
    source_key = "A60670:1"
    # Candidate content: a CHT max (SID 151) that may match or differ.
    cand_cht = 401 if content_changed else 400
    parsed = _parsed(_primary_sids(extra={"151": cand_cht}), update_value=cand_update)
    cand_hash = content_hash(parsed)
    # Marker's content hash = the hash of the unchanged (151=400) content.
    marker_hash = content_hash(_parsed(_primary_sids(extra={"151": 400})))

    settings_import = {}
    if has_marker:
        settings_import[source_key] = _marker(marker_update, chash=marker_hash)
        # Bind so the bound-source gate passes (same source).
        settings_import[BOUND_IMPORT_SOURCE_KEY] = source_key
    config = {"settings_import": settings_import}

    info = _decide(parsed, config)

    if not has_marker:
        # First-for-source is always offered, regardless of UPDATE= (Req 14.3).
        assert info.available is True
        assert info.is_first_for_source is True
    else:
        # Offered iff content differs — independent of UPDATE= magnitude.
        assert info.available == (cand_hash != marker_hash)


# ===========================================================================
# Property 24: Regression-proof — not offered when content is unchanged for its
#   source (refined by Req 17.5: identical Content_Hash => nothing new, even if
#   the archived date is newer or UPDATE= differs)
# Feature: efis-settings-import, Property 24: Regression-proof — not offered
#   when content is unchanged for its source
# Validates: Requirements 14.5, 17.5
# ===========================================================================
@settings(max_examples=200)
@given(
    marker_update=st.integers(1, 500),
    cand_update=st.integers(0, 500),
    other_update=st.integers(0, 500),
)
def test_property24_unchanged_content_suppressed(marker_update, cand_update, other_update):
    # Candidate whose Content_Hash matches its own source's marker => suppressed,
    # regardless of UPDATE= magnitude (higher, equal, or lower) and regardless of
    # any other source's marker.
    source_key = "A60670:1"
    other_key = "A60670:2"
    parsed = _parsed(_primary_sids(link_id="1", extra={"151": 400}),
                     update_value=cand_update)
    same_hash = content_hash(parsed)
    settings_import = {
        source_key: _marker(marker_update, chash=same_hash),
        other_key: _marker(other_update, chash="otherhash"),
        BOUND_IMPORT_SOURCE_KEY: source_key,
    }
    config = {"settings_import": settings_import}

    info = _decide(parsed, config)
    # Identical content hash to its own marker => no offer, no regression.
    assert info.available is False


# ===========================================================================
# Property 25: Different source flagged + apply still double-confirm gated
# Feature: efis-settings-import, Property 25: Different source is flagged and
#   never silently overwrites
# Validates: Requirements 14.6, 14.7, 14.9
# ===========================================================================
@settings(max_examples=100)
@given(cand_update=st.integers(50, 500), rpm=st.integers(2000, 3000))
def test_property25_different_source_flagged_and_gated(cand_update, rpm):
    # Prior import from a different source; candidate is a first-for-source
    # (its own key has no marker) but flagged different. No bound source yet so
    # it stays eligible (binding happens on first import).
    prior_key = "B00000:1"
    settings_import = {prior_key: _marker(10)}
    config = {"analysis_thresholds": {}, "num_cylinders": 4,
              "settings_import": settings_import}

    parsed = _parsed(_primary_sids(mode_s="A60670", extra={"123": rpm}),
                     update_value=cand_update)
    info = _decide(parsed, config)
    assert info.available is True
    assert info.is_different_source is True

    preview = Import_Workflow.build_preview(parsed, config, tier_selections={})
    assert preview.is_different_source is True
    assert preview.source_note

    # Apply is still no-op without both confirmations (no silent overwrite).
    before = copy.deepcopy(config)
    noop = Import_Workflow.apply(preview, True, False, config, save=lambda c: None)
    assert noop.applied is False
    assert noop.config == before


# ===========================================================================
# Property 26: Undetermined source content-hash fallback
# Feature: efis-settings-import, Property 26: Undetermined source falls back to
#   content-hash and never regresses
# Validates: Requirements 14.8, 17.5
# ===========================================================================
# The offer/regression decision is driven by Content_Hash difference (Req 17.5;
# UPDATE= is not globally monotonic). The undetermined path additionally retains
# the Req 14.8 "UPDATE greater" guard so ambiguous identity never regresses; the
# assertions below exercise the Content_Hash driver against a prior marker.
# NOTE on the undetermined-source path and the primary gate:
#   ``extract_source_key`` returns ``None`` (undetermined) only when NEITHER
#   Mode_S (1063) nor Link_ID (387) is readable. But the primary gate (0a)
#   requires SID 387 == "1", so any candidate that reaches the R14.8 branch in
#   the full ``detect_new_backup`` pipeline would already have failed 0a. The
#   undetermined branch in ``_decide`` therefore encodes the pure R14.8 rule
#   that predates the R15 gate; we test that pure branch by calling the module
#   helpers directly (below), and separately assert that composition with the
#   primary gate suppresses a non-primary undetermined candidate.
from efis_data_manager import efis_settings as _es


@settings(max_examples=200)
@given(
    prior_update=st.integers(0, 500),
    cand_update=st.integers(0, 500),
    same_content=st.booleans(),
)
def test_property26_undetermined_fallback_math(prior_update, cand_update, same_content):
    # Exercise the pure R14.8 branch of _decide: source_key undetermined, decide
    # against the most-recent prior marker via Content_Hash + UPDATE. To reach
    # the branch past the device-agnostic primary gate we make the candidate
    # primary-eligible (link_id treated as "1") while its Source_Key is None, by
    # patching extract_source_key for this candidate only.
    parsed = _parsed({"387": "1", "151": 400 if same_content else 401},
                     update_value=cand_update)
    prior_hash = content_hash(_parsed({"387": "1", "151": 400}))
    cand_hash = content_hash(parsed)
    settings_import = {"PRIOR:1": _marker(prior_update, chash=prior_hash)}
    config = {"settings_import": settings_import}

    real_extract = _es.extract_source_key

    def fake_extract(p):
        ident = real_extract(p)
        # Keep link_id (so the primary gate passes) but force undetermined key.
        return _es.SourceIdentity(source_key=None, mode_s=None,
                                  link_id=ident.link_id, flight_id=None)

    _es.extract_source_key = fake_extract
    try:
        info = _decide(parsed, config)
    finally:
        _es.extract_source_key = real_extract

    content_differs = cand_hash != prior_hash
    update_greater = cand_update > prior_update
    expected = content_differs and update_greater
    assert info.available == expected
    # Never regresses on ambiguous identity.
    if not expected:
        assert info.available is False


def test_property26_undetermined_composed_with_primary_gate():
    # A genuinely undetermined candidate (no identity SIDs at all) fails the
    # primary gate and is never offered — composition with R15 (R14.8 + R15.2).
    parsed = _parsed({"151": 400}, update_value=999)  # no 387 -> not primary
    config = {"settings_import": {":legacy": _marker(1, chash="prior")}}
    info = _decide(parsed, config)
    assert info.available is False
    assert info.reason == "not the primary display"


# ===========================================================================
# Property 27: Successful apply refreshes only the candidate source's marker
# Feature: efis-settings-import, Property 27: Successful apply refreshes only
#   the candidate source's marker
# Validates: Requirements 14.2
# ===========================================================================
@settings(max_examples=100)
@given(cand_update=st.integers(50, 500), other_update=st.integers(0, 40))
def test_property27_apply_refreshes_only_candidate_marker(cand_update, other_update):
    cand_key = "A60670:1"
    other_key = "A60670:2"
    other_marker = _marker(other_update, chash="otherhash", imported_at="2025-06-06T00:00:00")
    settings_import = {
        cand_key: _marker(1, chash="oldhash", imported_at="2025-01-01T00:00:00"),
        other_key: other_marker,
        BOUND_IMPORT_SOURCE_KEY: cand_key,
    }
    config = {"analysis_thresholds": {}, "num_cylinders": 4,
              "settings_import": copy.deepcopy(settings_import)}

    parsed = _parsed(_primary_sids(extra={"123": 2700}), update_value=cand_update)
    preview = Import_Workflow.build_preview(parsed, config, tier_selections={})
    assert preview.can_apply
    result = Import_Workflow.apply(preview, True, True, config, save=lambda c: None)
    assert result.applied

    new_si = result.config["settings_import"]
    # Candidate marker refreshed.
    assert new_si[cand_key]["last_update_value"] == cand_update
    assert new_si[cand_key]["content_hash"] == content_hash(parsed)
    assert new_si[cand_key]["imported_at"] is not None
    # Other source's marker untouched.
    assert new_si[other_key] == other_marker


# ===========================================================================
# Property 28: Primary gate — offered only if SID 387 == 1
# Feature: efis-settings-import, Property 28: Primary gate — import offered only
#   for the Primary_Display
# Validates: Requirements 15.1, 15.2
# ===========================================================================
@settings(max_examples=200)
@given(
    link_id=st.integers(0, 254),
    cand_update=st.integers(0, 500),
)
def test_property28_primary_gate(link_id, cand_update):
    # No marker for this source and no bound source: first-for-source would be
    # offered iff the primary gate passes.
    parsed = _parsed(_primary_sids(link_id=str(link_id)), update_value=cand_update)
    config = {"settings_import": {}}
    info = _decide(parsed, config)
    if link_id == 1:
        assert info.available is True
    else:
        assert info.available is False
        assert info.reason == "not the primary display"


# ===========================================================================
# Property 29: Primary gate is device-agnostic
# Feature: efis-settings-import, Property 29: Primary gate is device-agnostic
# Validates: Requirements 15.1
# ===========================================================================
@settings(max_examples=200)
@given(
    model_sid=st.integers(1, 9999).map(str),
    model_val=st.integers(0, 1000),
    cand_update=st.integers(0, 500),
    has_model=st.booleans(),
)
def test_property29_device_agnostic(model_sid, model_val, cand_update, has_model):
    # Hold Link_ID / Source_Key / markers fixed; toggle a non-identity model SID.
    assume(model_sid not in ("1063", "387", "1062", "22", "889", "345"))
    base = _primary_sids()  # A60670:1, primary
    config = {"settings_import": {}}

    without = _decide(_parsed(base, update_value=cand_update), config)

    extra = dict(base)
    if has_model:
        extra[model_sid] = str(model_val)
    with_model = _decide(_parsed(extra, update_value=cand_update), config)

    assert without.available == with_model.available


# ===========================================================================
# Property 30: Bound-source binding and exclusivity
# Feature: efis-settings-import, Property 30: Bound-source binding and
#   exclusivity
# Validates: Requirements 15.3, 15.4, 15.5
# ===========================================================================
@settings(max_examples=100)
@given(
    first_update=st.integers(10, 500),
    other_update=st.integers(10, 500),
)
def test_property30_bound_source_binding_exclusivity(first_update, other_update):
    # First successful import from primary k binds bound_import_source = k.
    k = "A60670:1"
    config = {"analysis_thresholds": {}, "num_cylinders": 4, "settings_import": {}}
    parsed1 = _parsed(_primary_sids(mode_s="A60670", extra={"123": 2700}),
                      update_value=first_update)
    preview1 = Import_Workflow.build_preview(parsed1, config, tier_selections={})
    res1 = Import_Workflow.apply(preview1, True, True, config, save=lambda c: None)
    assert res1.applied
    assert res1.config["settings_import"][BOUND_IMPORT_SOURCE_KEY] == k

    # A different primary source is now not offered (bound-source mismatch).
    bound_config = res1.config
    other_parsed = _parsed(
        _primary_sids(mode_s="C00000", extra={"123": 2700}),
        update_value=other_update,
    )
    info = _decide(other_parsed, bound_config)
    assert info.available is False
    assert info.is_bound_source_mismatch is True

    # And apply for that mismatched candidate is a byte-identical no-op.
    preview2 = Import_Workflow.build_preview(other_parsed, bound_config, tier_selections={})
    before = copy.deepcopy(bound_config)
    res2 = Import_Workflow.apply(preview2, True, True, bound_config, save=lambda c: None)
    assert res2.applied is False
    assert res2.config == before


# ===========================================================================
# Property 31: No primary present ⇒ no offer, bound stays unset
# Feature: efis-settings-import, Property 31: No primary present ⇒ no offer and
#   no non-primary source
# Validates: Requirements 15.6, 15.7
# ===========================================================================
@settings(max_examples=200)
@given(
    link_ids=st.lists(
        st.integers(0, 254).filter(lambda x: x != 1),
        min_size=1, max_size=4,
    ),
    cand_update=st.integers(0, 500),
)
def test_property31_no_primary_no_offer(link_ids, cand_update):
    config = {"settings_import": {}}
    # Every candidate is non-primary => each individually suppressed.
    for lid in link_ids:
        parsed = _parsed(_primary_sids(link_id=str(lid)), update_value=cand_update)
        info = _decide(parsed, config)
        assert info.available is False
    # bound_import_source is never set by detection.
    assert BOUND_IMPORT_SOURCE_KEY not in config["settings_import"]


# ===========================================================================
# Property 32: Primary gate composes with Requirement 14 detection
# Feature: efis-settings-import, Property 32: Primary gate composes with
#   Requirement 14 detection
# Validates: Requirements 15.8
# ===========================================================================
@settings(max_examples=200)
@given(
    marker_update=st.integers(0, 200),
    cand_update=st.integers(201, 500),  # strictly newer than the marker
    link_id=st.integers(0, 254).filter(lambda x: x != 1),
)
def test_property32_nonprimary_newer_still_suppressed(marker_update, cand_update, link_id):
    # A non-primary candidate whose UPDATE exceeds its source's marker must NOT
    # be offered — the primary gate is evaluated before the R14 newer rule.
    source_key = f"A60670:{link_id}"
    settings_import = {source_key: _marker(marker_update)}
    config = {"settings_import": settings_import}
    parsed = _parsed(_primary_sids(link_id=str(link_id)), update_value=cand_update)
    info = _decide(parsed, config)
    assert info.available is False
    assert info.reason == "not the primary display"



# ===========================================================================
# Property 39: Content-hash decides whether an import is offered
# Feature: efis-settings-import, Property 39: Content-hash decides whether an
#   import is offered
# Validates: Requirements 17.5
# ===========================================================================
@settings(max_examples=200)
@given(
    # UPDATE= and the archived-date differences must NOT matter (Req 17.2/17.5);
    # only the Content_Hash difference vs. the source's marker decides the offer.
    cand_update=st.integers(0, 500),
    marker_update=st.integers(0, 500),
    content_changed=st.booleans(),
)
def test_property39_content_hash_decides_offer(cand_update, marker_update, content_changed):
    source_key = "A60670:1"
    # The candidate is the latest-dated backup for its source; vary a real
    # content SID so its Content_Hash matches or differs from the marker's.
    cand_cht = 401 if content_changed else 400
    parsed = _parsed(_primary_sids(extra={"151": cand_cht}), update_value=cand_update)
    cand_hash = content_hash(parsed)
    marker_hash = content_hash(_parsed(_primary_sids(extra={"151": 400})))

    settings_import = {
        source_key: _marker(marker_update, chash=marker_hash),
        BOUND_IMPORT_SOURCE_KEY: source_key,
    }
    config = {"settings_import": settings_import}

    info = _decide(parsed, config)
    # Offered iff Content_Hash differs; identical hash => nothing new to import,
    # independent of Archive_Date and UPDATE= magnitude.
    assert info.available == (cand_hash != marker_hash)
    if cand_hash == marker_hash:
        assert info.available is False
