# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property tests for the aircraft-keyed config model (multi-device-settings).

Three Hypothesis properties over the pure helpers in
``efis_data_manager.config``:

  * Property 1 (migration idempotency): ``migrate_aircraft_config`` applied
    twice equals applying it once, for any config dict, and it never raises.
  * Property 2 (migration key preservation): migration leaves every non-
    ``aircraft`` key byte-identical and only adds / normalizes ``aircraft``.
  * Property 3 (label composition): ``device_label`` always contains
    ``Link {link_id}``; equals exactly ``Link {link_id}`` for an empty / all-
    whitespace friendly name; equals exactly ``{name} (Link {link_id})`` for a
    non-whitespace name.

Feature: multi-device-settings.
Validates: Requirements 1.6, 1.7, 5.3, 5.4, 6.2, 6.3
"""

import copy

from hypothesis import given, settings, strategies as st

from efis_data_manager.config import (
    PENDING_AIRCRAFT_KEY,
    device_label,
    migrate_aircraft_config,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A real Mode_S-like ICAO 24-bit hex key, e.g. "A60670".
_mode_s = st.text(alphabet="0123456789ABCDEF", min_size=6, max_size=6)

# Simple JSON-ish scalar values for arbitrary non-aircraft config keys.
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.text(max_size=12),
)

# An already-keyed aircraft map: real Mode_S keys and/or the :pending sentinel,
# each mapping to a small record dict.
_aircraft_record = st.fixed_dictionaries(
    {
        "device_count": st.integers(min_value=1, max_value=3),
        "devices": st.dictionaries(
            st.sampled_from(["1", "2", "3"]),
            st.fixed_dictionaries({"link_id": st.sampled_from(["1", "2", "3"])}),
            max_size=3,
        ),
    }
)
_keyed_aircraft = st.dictionaries(
    st.one_of(_mode_s, st.just(PENDING_AIRCRAFT_KEY)),
    _aircraft_record,
    max_size=2,
)

# The many shapes ``aircraft`` can take: absent (key omitted), a keyed map, or a
# non-dict (int/list/str/None). Represented as a draw of the value, with a
# separate flag for "key absent".
_aircraft_value = st.one_of(
    _keyed_aircraft,
    st.integers(),
    st.lists(st.integers(), max_size=3),
    st.text(max_size=8),
    st.none(),
)

# Arbitrary extra (non-aircraft) config keys, modelling empty {}, legacy flat
# shapes, and unrelated settings.
_other_keys = st.dictionaries(
    st.text(min_size=1, max_size=8).filter(lambda k: k != "aircraft"),
    _scalars,
    max_size=5,
)


@st.composite
def _configs(draw):
    """Build an arbitrary config dict, sometimes with an ``aircraft`` key."""
    config = dict(draw(_other_keys))
    if draw(st.booleans()):
        config["aircraft"] = draw(_aircraft_value)
    return config


# ---------------------------------------------------------------------------
# Property 1: Migration idempotency
# ---------------------------------------------------------------------------
# Feature: multi-device-settings, Property 1
@settings(max_examples=200)
@given(config=_configs())
def test_migrate_aircraft_config_idempotent(config):
    """migrate(migrate(c)) == migrate(c) and migration never raises.

    Validates: Requirements 6.2, 6.3
    """
    # migrate mutates in place, so give each call an independent deepcopy.
    once = migrate_aircraft_config(copy.deepcopy(config))
    twice = migrate_aircraft_config(copy.deepcopy(once))
    assert twice == once
    # And the result is always the normalized map shape.
    assert isinstance(once["aircraft"], dict)


# ---------------------------------------------------------------------------
# Property 2: Migration preserves existing keys
# ---------------------------------------------------------------------------
# Feature: multi-device-settings, Property 2
@settings(max_examples=200)
@given(config=_configs())
def test_migrate_aircraft_config_preserves_other_keys(config):
    """Only ``aircraft`` is added / normalized; all other keys are unchanged.

    Validates: Requirements 6.2, 6.3
    """
    original = copy.deepcopy(config)
    migrated = migrate_aircraft_config(copy.deepcopy(config))

    # Every non-aircraft key survives byte-identical (same value).
    for key, value in original.items():
        if key == "aircraft":
            continue
        assert key in migrated
        assert migrated[key] == value

    # Migration introduces no non-aircraft keys.
    for key in migrated:
        if key == "aircraft":
            continue
        assert key in original

    # The only key migration may add is ``aircraft``.
    assert "aircraft" in migrated
    added = set(migrated) - set(original)
    assert added <= {"aircraft"}

    # When ``aircraft`` was already a dict, it is left untouched (not normalized).
    if isinstance(original.get("aircraft"), dict):
        assert migrated["aircraft"] == original["aircraft"]


# ---------------------------------------------------------------------------
# Property 3: Label composition round-trips the Link_ID
# ---------------------------------------------------------------------------
# Friendly names: empty, whitespace-only, and arbitrary Unicode (incl. names
# that are non-whitespace once stripped and names that strip to empty).
_friendly_name = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r", max_size=5),  # all-whitespace
    st.text(max_size=20),  # arbitrary Unicode
)
# Link_IDs: the real "1"/"2"/"3" positions plus arbitrary text.
_link_id = st.one_of(st.sampled_from(["1", "2", "3"]), st.text(max_size=10))


# Feature: multi-device-settings, Property 3
@settings(max_examples=200)
@given(friendly_name=_friendly_name, link_id=_link_id)
def test_device_label_composition(friendly_name, link_id):
    """Label always carries ``Link {link_id}``; composes with the friendly name.

    Validates: Requirements 1.6, 1.7, 5.3, 5.4
    """
    device = {"link_id": link_id, "friendly_name": friendly_name}
    label = device_label(device)

    # Always contains the Link_ID substring.
    assert f"Link {link_id}" in label

    stripped = friendly_name.strip()
    if stripped:
        # A non-whitespace name composes exactly as "{name} (Link {link_id})".
        # The helper trims surrounding whitespace before composing, so the
        # composed form uses the stripped name.
        assert label == f"{stripped} (Link {link_id})"
    else:
        # Empty / all-whitespace name -> exactly "Link {link_id}".
        assert label == f"Link {link_id}"
