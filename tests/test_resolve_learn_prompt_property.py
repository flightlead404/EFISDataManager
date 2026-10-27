# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the PURE side of Property 9 (multi-device-settings).

Property 9 — "Learn binds the managed aircraft only on confirmation" — has two
sides. This file covers the PURE guarantee: when a drive is inserted while no
managed aircraft is established yet and a Mode_S can be derived, the pure
``resolve_drive`` classifier returns ``LEARN_PROMPT`` carrying that Mode_S and
NEVER itself binds — it does not write a real Mode_S key into ``config``. It
only CLASSIFIES the situation as LEARN_PROMPT; actually adopting the Mode_S is a
side effect the caller performs on user confirmation.

The persist-on-confirm / decline-on-reject behavior (the side effects:
confirming persists the managed aircraft via ``save_config`` and re-resolves;
declining leaves the aircraft map unlearned, takes no action, and leaves the
drive mounted) is covered by the app-level integration test (task 7.3), not
here.

Requirements: 3.2, 3.3
"""

import copy
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.device_resolution import (
    ResolutionStatus,
    resolve_drive,
)
from efis_data_manager.efis_settings import (
    SID_LINK_ID,
    SID_MODE_S,
    ParsedBackup,
)


def _parsed(mode_s: Optional[str] = None, link_id: Optional[str] = None) -> ParsedBackup:
    """Build a real ParsedBackup carrying only the requested identity SIDs.

    The ParsedBackup runs through ``extract_source_key`` unchanged inside
    ``resolve_drive``; omitting a SID derives that identity field as absent.
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


# A small hex set of plausible Mode_S (ICAO 24-bit) values.
_MODE_S = st.sampled_from(["A60670", "AABBCC", "C0FFEE", "123ABC", "DEADBE"])
# Link_ID over the valid position set plus "no Link_ID SID present".
_LINK_ID = st.sampled_from(["1", "2", "3", None])


# Feature: multi-device-settings, Property 9
@settings(max_examples=100)
@given(mode_s=_MODE_S, link_id=_LINK_ID)
def test_learn_prompt_is_pure_and_never_binds(mode_s, link_id):
    """For any insert with NO managed aircraft and a PRESENT derived Mode_S,
    resolve_drive classifies as LEARN_PROMPT carrying that Mode_S, and the pure
    call never mutates config (it does not bind a real Mode_S key).

    Validates: Requirements 3.2, 3.3
    """
    # Unlearned config: managed aircraft not yet established.
    config = {"aircraft": {}}
    before = copy.deepcopy(config)

    parsed = _parsed(mode_s=mode_s, link_id=link_id)
    outcome = resolve_drive(parsed, recorded_identity=None, config=config)

    # Classifies as LEARN_PROMPT carrying the derived Mode_S (R3.2).
    assert outcome.status is ResolutionStatus.LEARN_PROMPT
    assert outcome.mode_s == mode_s

    # Pure: resolve_drive is side-effect-free. It only CLASSIFIES and never
    # itself binds — the aircraft map still has no real Mode_S key, and the
    # whole config is byte-identical to the pre-call snapshot (R3.3).
    assert config == before
    assert config["aircraft"] == {}
    assert mode_s not in config["aircraft"]
