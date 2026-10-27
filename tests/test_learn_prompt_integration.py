# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the app.py learn-prompt persist/decline flow
(multi-device-settings Task 7.3, Requirements 3.2, 3.3, 5.5, 5.6).

Scope: the I/O-side wrappers added in Task 7.1 around the pure
``device_resolution.resolve_drive`` state machine —
``_resolve_and_gate_drive`` / ``_handle_learn_prompt`` /
``_establish_managed_aircraft`` / ``_proceed_resolved`` /
``_handle_terminal_outcome`` on ``EFISDataManagerApp``.

Instantiation / mocking approach
--------------------------------
Constructing the full rumps ``EFISDataManagerApp`` is impractical headless (it
builds a live macOS menu-bar item), so — mirroring the project's existing app
tests (``test_app_write_routing`` / ``test_app_composer_refresh``) — these tests
bind the REAL unbound methods to a minimal ``SimpleNamespace`` stand-in. The
decision logic under test is therefore genuine production code; only the side
effects are stubbed:

* ``_run_alert_on_main_thread`` is replaced with a recorder that returns the
  desired modal result (1 = OK/confirm, 0 = Cancel/decline). This cleanly avoids
  all AppKit / main-thread marshalling.
* ``app.load_config`` / ``app.save_config`` are patched so no real config file is
  touched; ``save_config`` captures what would be persisted. (``_resolve_and_
  gate_drive`` reads via the module-level ``load_config``; ``_handle_learn_
  prompt`` persists via the module-level ``save_config``.)
* ``drive_updater.read_identity`` is patched to return ``None`` (no recorded
  binding) — it is imported inside ``_resolve_and_gate_drive`` from that module.
* ``_find_drive_settings`` is stubbed to return a crafted real ``ParsedBackup``
  (built from the identity SIDs) so ``extract_source_key`` runs for real.
* ``_notify`` / ``_set_status`` / ``_set_drive_status`` are no-op recorders.

No real drive, menubar, config file, or AppKit anything is touched.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from efis_data_manager import app as app_module
from efis_data_manager.app import EFISDataManagerApp
from efis_data_manager.device_resolution import ResolutionStatus
from efis_data_manager.efis_settings import (
    ParsedBackup,
    SID_LINK_ID,
    SID_MODE_S,
)
from efis_data_manager.config import PENDING_AIRCRAFT_KEY


REAL_MODE_S = "A60670"
MOUNT = "/Volumes/EFIS_TEST"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _parsed(mode_s=REAL_MODE_S, link_id="1"):
    """Build a real ParsedBackup carrying the given identity SIDs.

    extract_source_key reads SID_MODE_S (1063) and SID_LINK_ID (387), so a real
    ParsedBackup with those pairs exercises the production identity derivation.
    """
    sids = {}
    if mode_s is not None:
        sids[SID_MODE_S] = mode_s
    if link_id is not None:
        sids[SID_LINK_ID] = link_id
    return ParsedBackup(
        path="/Volumes/EFIS_TEST/Settings.bak",
        sids=sids,
        update_value=1,
        line_count=len(sids),
        valid_pairs=len(sids),
    )


def _make_standin(parsed):
    """Minimal stand-in exposing only what the resolution methods touch.

    The resolution / learn-prompt / terminal methods are bound at call time from
    the real class, so the logic under test is production code. Collaborators are
    recorders or stubs.
    """
    standin = SimpleNamespace()

    # Recorders.
    standin.alert_calls = []
    standin.notify_calls = []
    standin.status_calls = []
    standin.drive_status_calls = []

    standin._notify = lambda title, msg: standin.notify_calls.append((title, msg))
    standin._set_status = lambda msg: standin.status_calls.append(msg)
    standin._set_drive_status = lambda msg: standin.drive_status_calls.append(msg)

    # Settings lookup returns the crafted ParsedBackup (or None).
    standin._find_drive_settings = lambda mount_point: parsed

    # The alert result is configured per-test via standin._alert_result.
    standin._alert_result = 0

    def _alert(title, message):
        standin.alert_calls.append((title, message))
        return standin._alert_result

    standin._run_alert_on_main_thread = _alert

    # Bind the real production methods for the decision logic under test.
    standin._resolve_and_gate_drive = (
        lambda mount_point: EFISDataManagerApp._resolve_and_gate_drive(
            standin, mount_point
        )
    )
    standin._handle_learn_prompt = (
        lambda *a, **k: EFISDataManagerApp._handle_learn_prompt(standin, *a, **k)
    )
    standin._establish_managed_aircraft = (
        lambda *a, **k: EFISDataManagerApp._establish_managed_aircraft(
            standin, *a, **k
        )
    )
    standin._proceed_resolved = (
        lambda outcome: EFISDataManagerApp._proceed_resolved(standin, outcome)
    )
    standin._handle_terminal_outcome = (
        lambda *a, **k: EFISDataManagerApp._handle_terminal_outcome(
            standin, *a, **k
        )
    )
    return standin


class _ConfigStore:
    """Captures the config seen by the patched load_config / save_config.

    load_config returns the live dict (so the method mutates the same object the
    test inspects); save_config records each persisted snapshot's aircraft map.
    """

    def __init__(self, config):
        self.config = config
        self.saved = []

    def load(self):
        return self.config

    def save(self, config):
        # Mirror real save_config (persist the passed config); record it.
        self.config = config
        self.saved.append(config)


# --------------------------------------------------------------------------- #
# 1. LEARN confirm path (R3.2) — adopt Mode_S, persist, re-resolve, proceed.
# --------------------------------------------------------------------------- #


def test_learn_confirm_persists_aircraft_and_proceeds():
    """Confirming a LEARN_PROMPT persists the managed aircraft under the real
    Mode_S via save_config, re-resolves, and returns True (proceed to archive).

    Single-device learn: _establish_managed_aircraft creates a device at
    Link_ID "1", and the settings Link_ID is "1", so the re-resolve is RESOLVED.
    """
    parsed = _parsed(mode_s=REAL_MODE_S, link_id="1")
    standin = _make_standin(parsed)
    standin._alert_result = 1  # confirm / OK

    store = _ConfigStore({"aircraft": {}})  # empty aircraft map: nothing learned

    with mock.patch.object(app_module, "load_config", store.load), \
        mock.patch.object(app_module, "save_config", store.save), \
        mock.patch(
            "efis_data_manager.drive_updater.read_identity", return_value=None
        ):
        result = standin._resolve_and_gate_drive(MOUNT)

    # Proceed to archive.
    assert result is True
    # The modal prompt was shown exactly once.
    assert len(standin.alert_calls) == 1
    # Managed aircraft persisted under the REAL Mode_S (not the pending key).
    assert store.saved, "save_config should have been called on confirm"
    aircraft = store.config["aircraft"]
    assert REAL_MODE_S in aircraft
    assert PENDING_AIRCRAFT_KEY not in aircraft
    # The minimal learned record has a device at Link_ID "1".
    assert aircraft[REAL_MODE_S]["devices"]["1"]["link_id"] == "1"


# --------------------------------------------------------------------------- #
# 2. LEARN decline path (R3.3) — no Mode_S written, no action, notify.
# --------------------------------------------------------------------------- #


def test_learn_decline_writes_no_aircraft_and_leaves_mounted():
    """Declining a LEARN_PROMPT writes no real Mode_S key, returns False (leave
    mounted, no archive), posts a notification, and never calls save_config."""
    parsed = _parsed(mode_s=REAL_MODE_S, link_id="1")
    standin = _make_standin(parsed)
    standin._alert_result = 0  # cancel / decline

    store = _ConfigStore({"aircraft": {}})

    with mock.patch.object(app_module, "load_config", store.load), \
        mock.patch.object(app_module, "save_config", store.save), \
        mock.patch(
            "efis_data_manager.drive_updater.read_identity", return_value=None
        ):
        result = standin._resolve_and_gate_drive(MOUNT)

    # Do not proceed; drive left mounted.
    assert result is False
    # The prompt was shown.
    assert len(standin.alert_calls) == 1
    # No aircraft adopted: no save_config, no real Mode_S key.
    assert store.saved == []
    assert REAL_MODE_S not in store.config["aircraft"]
    assert store.config["aircraft"] == {}
    # A decline notification was posted (R3.3 "take no action", user informed).
    assert len(standin.notify_calls) == 1


# --------------------------------------------------------------------------- #
# 3. Terminal no-action outcomes (R5.5, R5.6) — return False, no archive.
# --------------------------------------------------------------------------- #


def test_aircraft_unrecognized_returns_false_and_notifies():
    """A drive from a DIFFERENT aircraft (Mode_S present but != managed) yields
    AIRCRAFT_UNRECOGNIZED: _resolve_and_gate_drive returns False, no prompt is
    shown, no archive is started, and _handle_terminal_outcome notified."""
    # Settings report a different Mode_S than the already-managed aircraft.
    parsed = _parsed(mode_s="DIFFERENT", link_id="1")
    standin = _make_standin(parsed)

    # Managed aircraft already established as REAL_MODE_S -> UNRECOGNIZED.
    store = _ConfigStore(
        {
            "aircraft": {
                REAL_MODE_S: {
                    "device_count": 1,
                    "devices": {
                        "1": {
                            "link_id": "1",
                            "friendly_name": "",
                            "chart_capable": True,
                        }
                    },
                    "analysis_data_source": "1",
                }
            }
        }
    )

    with mock.patch.object(app_module, "load_config", store.load), \
        mock.patch.object(app_module, "save_config", store.save), \
        mock.patch(
            "efis_data_manager.drive_updater.read_identity", return_value=None
        ):
        result = standin._resolve_and_gate_drive(MOUNT)

    assert result is False
    # No learn prompt for a terminal outcome.
    assert standin.alert_calls == []
    # No config persisted.
    assert store.saved == []
    # Terminal outcome notified the user (unrecognized aircraft).
    assert len(standin.notify_calls) == 1
    assert standin.notify_calls[0][0] == "Unrecognized aircraft"


def test_device_undetermined_returns_false_and_notifies():
    """No settings file and no usable recorded binding yields
    DEVICE_UNDETERMINED: returns False, no prompt, no archive, notifies."""
    standin = _make_standin(parsed=None)  # no readable settings file

    # Managed aircraft exists, but there is no settings Link_ID and no recorded
    # device_link_id (read_identity -> None), so the device is undetermined.
    store = _ConfigStore(
        {
            "aircraft": {
                REAL_MODE_S: {
                    "device_count": 1,
                    "devices": {
                        "1": {
                            "link_id": "1",
                            "friendly_name": "",
                            "chart_capable": True,
                        }
                    },
                    "analysis_data_source": "1",
                }
            }
        }
    )

    with mock.patch.object(app_module, "load_config", store.load), \
        mock.patch.object(app_module, "save_config", store.save), \
        mock.patch(
            "efis_data_manager.drive_updater.read_identity", return_value=None
        ):
        result = standin._resolve_and_gate_drive(MOUNT)

    assert result is False
    assert standin.alert_calls == []
    assert store.saved == []
    assert len(standin.notify_calls) == 1
    assert standin.notify_calls[0][0] == "Device undetermined"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
