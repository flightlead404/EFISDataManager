# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property and example tests for the drive-insert resolution state machine.

Covers the pure, flight-critical ``resolve_drive`` classifier in
``device_resolution.py`` for the multi-device-settings foundation. The property
tests (Hypothesis, >=100 iterations each) exercise the flight-critical
invariants over the combinatorial input space (Mode_S present/absent/matching,
settings Link_ID configured/not, settings present/absent, recorded binding
present/absent/matching):

  - Property 4: settings-file Link_ID is authoritative over any recorded binding
  - Property 5: recorded binding is used only when no settings Link_ID
  - Property 6: aircraft verification gates device resolution
  - Property 7: an unconfigured Link_ID never resolves to a device
  - Property 8: resolution is deterministic

The example tests encode the 8-row resolution matrix from design.md "Testing
Strategy" as explicit one-assertion-per-row cases.

Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.4, 3.5, 3.6, 4.1, 4.5, 4.6
"""

from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.config import PENDING_AIRCRAFT_KEY
from efis_data_manager.device_resolution import (
    ResolutionStatus,
    resolve_drive,
)
from efis_data_manager.efis_settings import (
    SID_LINK_ID,
    SID_MODE_S,
    ParsedBackup,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parsed(mode_s: Optional[str] = None, link_id: Optional[str] = None) -> ParsedBackup:
    """Build a real ParsedBackup carrying only the requested identity SIDs.

    A ``None`` (or empty/whitespace) value for a SID omits it, so
    ``extract_source_key`` derives that identity field as absent. The resulting
    ParsedBackup runs through ``extract_source_key`` unchanged.
    """
    sids: dict[str, str] = {}
    if mode_s is not None:
        sids[SID_MODE_S] = mode_s
    if link_id is not None:
        sids[SID_LINK_ID] = link_id
    return ParsedBackup(
        path="/fake/Settings_Backup.dat",
        sids=sids,
        update_value=None,
        line_count=len(sids),
        valid_pairs=len(sids),
    )


def _device(link_id: str, friendly_name: str = "", chart_capable: bool = True) -> dict:
    return {
        "link_id": link_id,
        "friendly_name": friendly_name,
        "chart_capable": chart_capable,
    }


def _config(
    mode_s: str,
    configured_link_ids,
    device_count: int = 1,
    analysis_data_source: Optional[str] = None,
) -> dict:
    """Build a config with one managed aircraft and the given configured devices.

    ``configured_link_ids`` is an iterable of Link_ID strings that each become a
    configured device under the managed aircraft keyed by ``mode_s``.
    """
    link_ids = list(configured_link_ids)
    devices = {lid: _device(lid) for lid in link_ids}
    record = {
        "device_count": device_count,
        "devices": devices,
        "analysis_data_source": analysis_data_source
        if analysis_data_source is not None
        else (link_ids[0] if link_ids else "1"),
    }
    return {"aircraft": {mode_s: record}}


def _no_managed_config() -> dict:
    """A config with no managed aircraft learned yet (empty aircraft map)."""
    return {"aircraft": {}}


def _identity(device_link_id: Optional[str]) -> Optional[dict]:
    """Build a recorded identity dict carrying a device_link_id (or None)."""
    if device_link_id is None:
        return None
    return {"device_link_id": device_link_id}


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Mode_S-like values (ICAO 24-bit hex strings). Kept to a small set so managed
# vs non-managed collisions / matches are both exercised frequently.
_mode_s_strategy = st.sampled_from(["A60670", "B11111", "C22222", "D33333"])

# Link_ID values per the 1..3 domain, plus occasional out-of-domain strings.
_link_id_strategy = st.sampled_from(["1", "2", "3"])


# ---------------------------------------------------------------------------
# Property 4: Settings-file Link_ID is authoritative
# ---------------------------------------------------------------------------

# Feature: multi-device-settings, Property 4
@settings(max_examples=200)
@given(
    settings_link=_link_id_strategy,
    other_links=st.sets(_link_id_strategy, max_size=3),
    recorded=st.one_of(st.none(), _link_id_strategy),
)
def test_property4_settings_link_id_authoritative(settings_link, other_links, recorded):
    """A configured settings Link_ID resolves to the settings device regardless
    of any recorded binding; a differing recorded binding is flagged but does
    not change the resolved device. (Validates 2.2, 4.1, 4.6.)"""
    managed = "A60670"
    # The managed aircraft must have the settings Link_ID configured.
    configured = set(other_links) | {settings_link}
    config = _config(managed, configured, device_count=len(configured))

    parsed = _parsed(mode_s=managed, link_id=settings_link)
    outcome = resolve_drive(parsed, _identity(recorded), config)

    assert outcome.status is ResolutionStatus.RESOLVED
    # Resolved to the SETTINGS Link_ID device, never the recorded one.
    assert outcome.link_id == settings_link
    assert outcome.device is not None
    assert outcome.device["link_id"] == settings_link

    if recorded is not None and recorded != settings_link:
        assert outcome.binding_mismatch is True
        assert outcome.recorded_link_id == recorded
    else:
        assert outcome.binding_mismatch is False


# ---------------------------------------------------------------------------
# Property 5: Recorded binding used only when no settings Link_ID
# ---------------------------------------------------------------------------

# Feature: multi-device-settings, Property 5
@settings(max_examples=200)
@given(
    has_settings=st.booleans(),
    configured_links=st.sets(_link_id_strategy, min_size=1, max_size=3),
    recorded=st.one_of(st.none(), _link_id_strategy, st.sampled_from(["9"])),
)
def test_property5_recorded_binding_fallback(has_settings, configured_links, recorded):
    """With no readable settings file (parsed=None) OR settings present but no
    Link_ID SID, resolution uses the recorded device_link_id when configured,
    and returns DEVICE_UNDETERMINED when neither a settings Link_ID nor a
    configured recorded binding is available. (Validates 2.5, 2.6, 4.5.)"""
    managed = "A60670"
    config = _config(managed, configured_links, device_count=len(configured_links))

    # Either no settings file, or a settings file that carries no Link_ID SID.
    if has_settings:
        parsed = _parsed(mode_s=managed, link_id=None)
    else:
        parsed = None

    outcome = resolve_drive(parsed, _identity(recorded), config)

    recorded_configured = recorded is not None and recorded in configured_links
    if recorded_configured:
        assert outcome.status is ResolutionStatus.RESOLVED
        assert outcome.link_id == recorded
        assert outcome.device is not None
        assert outcome.device["link_id"] == recorded
    else:
        assert outcome.status is ResolutionStatus.DEVICE_UNDETERMINED
        assert outcome.device is None


# ---------------------------------------------------------------------------
# Property 6: Aircraft verification gates device resolution
# ---------------------------------------------------------------------------

# Feature: multi-device-settings, Property 6
@settings(max_examples=300)
@given(
    settings_mode_s=st.one_of(st.none(), _mode_s_strategy),
    settings_link=st.one_of(st.none(), _link_id_strategy),
    managed_learned=st.booleans(),
    managed_mode_s=_mode_s_strategy,
    configured_links=st.sets(_link_id_strategy, min_size=1, max_size=3),
)
def test_property6_aircraft_gates_resolution(
    settings_mode_s, settings_link, managed_learned, managed_mode_s, configured_links
):
    """For a populated drive, RESOLVED or LEARN_PROMPT is reachable ONLY when
    derived Mode_S is present AND (no managed aircraft yet, OR Mode_S matches
    the managed aircraft). Absent Mode_S -> AIRCRAFT_UNDETERMINED; present but
    differing -> AIRCRAFT_UNRECOGNIZED; a non-matching/undetermined aircraft
    never yields RESOLVED. (Validates 3.1, 3.4, 3.5, 3.6.)"""
    if managed_learned:
        config = _config(
            managed_mode_s, configured_links, device_count=len(configured_links)
        )
    else:
        config = _no_managed_config()

    # Populated drive: a settings file is present (parsed is not None).
    parsed = _parsed(mode_s=settings_mode_s, link_id=settings_link)
    outcome = resolve_drive(parsed, None, config)

    if settings_mode_s is None:
        # Absent Mode_S dominates.
        assert outcome.status is ResolutionStatus.AIRCRAFT_UNDETERMINED
    elif not managed_learned:
        # No managed aircraft yet, Mode_S present -> learn prompt.
        assert outcome.status is ResolutionStatus.LEARN_PROMPT
    elif settings_mode_s != managed_mode_s:
        # Present but differing -> unrecognized, never resolved.
        assert outcome.status is ResolutionStatus.AIRCRAFT_UNRECOGNIZED

    # Universal invariant: RESOLVED / LEARN_PROMPT imply a usable aircraft.
    if outcome.status in (ResolutionStatus.RESOLVED, ResolutionStatus.LEARN_PROMPT):
        assert settings_mode_s is not None
        if managed_learned:
            assert settings_mode_s == managed_mode_s

    # Never RESOLVED on a non-matching or undetermined aircraft.
    if outcome.status is ResolutionStatus.RESOLVED:
        assert settings_mode_s is not None
        assert (not managed_learned) is False  # must have managed to resolve by settings
        assert settings_mode_s == managed_mode_s


# ---------------------------------------------------------------------------
# Property 7: Unconfigured Link_ID never resolves
# ---------------------------------------------------------------------------

# Feature: multi-device-settings, Property 7
@settings(max_examples=200)
@given(
    settings_link=_link_id_strategy,
    configured_links=st.sets(_link_id_strategy, max_size=3),
    recorded=st.one_of(st.none(), _link_id_strategy),
)
def test_property7_unconfigured_link_never_resolves(
    settings_link, configured_links, recorded
):
    """For a populated drive of the managed aircraft whose settings Link_ID is
    not among the configured devices, resolution returns DEVICE_NOT_CONFIGURED
    and device is None. (Validates 2.3, 2.4.)"""
    managed = "A60670"
    # Ensure the settings Link_ID is NOT configured.
    configured = set(configured_links) - {settings_link}
    # Keep the aircraft managed even if no devices configured.
    config = _config(managed, configured, device_count=max(1, len(configured)))

    parsed = _parsed(mode_s=managed, link_id=settings_link)
    outcome = resolve_drive(parsed, _identity(recorded), config)

    assert outcome.status is ResolutionStatus.DEVICE_NOT_CONFIGURED
    assert outcome.device is None


# ---------------------------------------------------------------------------
# Property 8: Resolution is deterministic
# ---------------------------------------------------------------------------

# Feature: multi-device-settings, Property 8
@settings(max_examples=300)
@given(
    has_settings=st.booleans(),
    settings_mode_s=st.one_of(st.none(), _mode_s_strategy),
    settings_link=st.one_of(st.none(), _link_id_strategy),
    managed_learned=st.booleans(),
    managed_mode_s=_mode_s_strategy,
    configured_links=st.sets(_link_id_strategy, max_size=3),
    recorded=st.one_of(st.none(), _link_id_strategy),
)
def test_property8_resolution_deterministic(
    has_settings,
    settings_mode_s,
    settings_link,
    managed_learned,
    managed_mode_s,
    configured_links,
    recorded,
):
    """Two calls to resolve_drive on fixed inputs return the same status and the
    same resolved device. (Validates 2.3, 3.4, 4.1.)"""
    if managed_learned:
        config = _config(
            managed_mode_s, configured_links, device_count=max(1, len(configured_links))
        )
    else:
        config = _no_managed_config()

    parsed = (
        _parsed(mode_s=settings_mode_s, link_id=settings_link) if has_settings else None
    )
    identity = _identity(recorded)

    first = resolve_drive(parsed, identity, config)
    second = resolve_drive(parsed, identity, config)

    assert first.status == second.status
    assert first.device == second.device
    assert first.link_id == second.link_id


# ---------------------------------------------------------------------------
# Example tests: the 8-row resolution matrix (design.md "Testing Strategy")
# ---------------------------------------------------------------------------

def test_matrix_row1_present_managed_configured_no_recorded_resolves():
    """Row 1: present / A60670 / managed A60670 / link 1 configured / no
    recorded -> RESOLVED. (Validates 2.3.)"""
    config = _config("A60670", ["1"], device_count=1)
    outcome = resolve_drive(_parsed(mode_s="A60670", link_id="1"), None, config)
    assert outcome.status is ResolutionStatus.RESOLVED
    assert outcome.device["link_id"] == "1"
    assert outcome.binding_mismatch is False


def test_matrix_row2_recorded_differs_resolves_with_mismatch():
    """Row 2: same as row 1 but recorded device_link_id "2" differs ->
    RESOLVED + binding_mismatch. (Validates 4.6.)"""
    config = _config("A60670", ["1", "2"], device_count=2)
    outcome = resolve_drive(
        _parsed(mode_s="A60670", link_id="1"), _identity("2"), config
    )
    assert outcome.status is ResolutionStatus.RESOLVED
    assert outcome.device["link_id"] == "1"
    assert outcome.binding_mismatch is True
    assert outcome.recorded_link_id == "2"


def test_matrix_row3_unconfigured_link_not_configured():
    """Row 3: link 3 not configured -> DEVICE_NOT_CONFIGURED. (Validates 2.4.)"""
    config = _config("A60670", ["1", "2"], device_count=2)
    outcome = resolve_drive(_parsed(mode_s="A60670", link_id="3"), None, config)
    assert outcome.status is ResolutionStatus.DEVICE_NOT_CONFIGURED
    assert outcome.device is None


def test_matrix_row4_no_managed_learn_prompt():
    """Row 4: present / A60670 / managed none -> LEARN_PROMPT. (Validates 3.2.)"""
    config = _no_managed_config()
    outcome = resolve_drive(_parsed(mode_s="A60670", link_id="1"), None, config)
    assert outcome.status is ResolutionStatus.LEARN_PROMPT
    assert outcome.mode_s == "A60670"


def test_matrix_row5_differing_mode_s_unrecognized():
    """Row 5: present / B11111 / managed A60670 -> AIRCRAFT_UNRECOGNIZED.
    (Validates 3.5.)"""
    config = _config("A60670", ["1"], device_count=1)
    outcome = resolve_drive(_parsed(mode_s="B11111", link_id="1"), None, config)
    assert outcome.status is ResolutionStatus.AIRCRAFT_UNRECOGNIZED


def test_matrix_row6_absent_mode_s_undetermined():
    """Row 6: present / no-mode_s / managed A60670 -> AIRCRAFT_UNDETERMINED.
    (Validates 3.6.)"""
    config = _config("A60670", ["1"], device_count=1)
    outcome = resolve_drive(_parsed(mode_s=None, link_id="1"), None, config)
    assert outcome.status is ResolutionStatus.AIRCRAFT_UNDETERMINED


def test_matrix_row7_absent_settings_recorded_configured_resolves():
    """Row 7: absent settings / managed A60670 / recorded device_link_id "2"
    configured -> RESOLVED via recorded. (Validates 2.5, 4.5.)"""
    config = _config("A60670", ["1", "2"], device_count=2)
    outcome = resolve_drive(None, _identity("2"), config)
    assert outcome.status is ResolutionStatus.RESOLVED
    assert outcome.device["link_id"] == "2"
    assert outcome.link_id == "2"


def test_matrix_row8_absent_settings_no_recorded_undetermined():
    """Row 8: absent settings / no configured recorded binding ->
    DEVICE_UNDETERMINED. (Validates 2.6.)"""
    config = _config("A60670", ["1"], device_count=1)
    # No recorded binding at all.
    outcome = resolve_drive(None, None, config)
    assert outcome.status is ResolutionStatus.DEVICE_UNDETERMINED
    assert outcome.device is None
