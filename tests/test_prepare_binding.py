# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example tests for the Prepare-Drive device binding (multi-device-settings).

Covers task 8.2: the main-thread device picker
``EFISDataManagerApp._choose_prepare_device_link_id`` and the binding write in
``EFISDataManagerApp._do_prepare_drive`` (which records ``device_link_id`` into
the drive's identity file after a successful ``prepare_drive``).

Everything that touches a real drive, the filesystem, rumps prompts, or the UI
is mocked, so these tests never run ``diskutil`` or touch a USB/dev node.

The app instance is built via ``EFISDataManagerApp.__new__`` to avoid the full
rumps.App initialisation; only the attributes the methods under test touch are
set (stubbed status/notify/provisioning helpers).

Requirements: 4.2, 4.3, 4.4, 6.4, 6.5
"""

from unittest.mock import MagicMock, patch

import pytest

from efis_data_manager.app import EFISDataManagerApp


# --------------------------------------------------------------------------- #
# Config fixtures mirroring the aircraft-keyed model (config.py).
# --------------------------------------------------------------------------- #

def _single_device_config():
    """A learned single-device install: one real Mode_S, one device at Link 1."""
    return {
        "aircraft": {
            "A60670": {
                "device_count": 1,
                "devices": {
                    "1": {"link_id": "1", "friendly_name": "Pilot", "chart_capable": True},
                },
            }
        }
    }


def _empty_config():
    """Nothing learned yet: empty aircraft map (device_count defaults to 1)."""
    return {"aircraft": {}}


def _multi_device_config():
    """A learned two-device install: devices at Link 1 and Link 2."""
    return {
        "aircraft": {
            "A60670": {
                "device_count": 2,
                "devices": {
                    "1": {"link_id": "1", "friendly_name": "Pilot", "chart_capable": True},
                    "2": {"link_id": "2", "friendly_name": "Copilot", "chart_capable": False},
                },
            }
        }
    }


# --------------------------------------------------------------------------- #
# A) _choose_prepare_device_link_id
# --------------------------------------------------------------------------- #

def test_choose_single_configured_device_no_prompt():
    """Single configured device: bind to its link_id, no rumps prompt (R4.4)."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    with patch("efis_data_manager.app.load_config", return_value=_single_device_config()), \
         patch("efis_data_manager.app.rumps.alert") as m_alert, \
         patch("efis_data_manager.app.rumps.Window") as m_window:
        link_id = app._choose_prepare_device_link_id()

    assert link_id == "1"
    m_alert.assert_not_called()
    m_window.assert_not_called()


def test_choose_empty_config_defaults_to_one_no_prompt():
    """Nothing learned: default to '1' without prompting (R6.5)."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    with patch("efis_data_manager.app.load_config", return_value=_empty_config()), \
         patch("efis_data_manager.app.rumps.alert") as m_alert, \
         patch("efis_data_manager.app.rumps.Window") as m_window:
        link_id = app._choose_prepare_device_link_id()

    assert link_id == "1"
    m_alert.assert_not_called()
    m_window.assert_not_called()


def test_choose_multi_device_selects_second():
    """Two devices: alert returning -1 (other) picks the second device (R4.2)."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    # rumps.alert: ok=1 -> first device, other=-1 -> second device, cancel=0.
    with patch("efis_data_manager.app.load_config", return_value=_multi_device_config()), \
         patch("efis_data_manager.app.rumps.alert", return_value=-1) as m_alert:
        link_id = app._choose_prepare_device_link_id()

    assert link_id == "2"
    m_alert.assert_called_once()


def test_choose_multi_device_selects_first():
    """Two devices: alert returning 1 (ok) picks the first device (R4.2)."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    with patch("efis_data_manager.app.load_config", return_value=_multi_device_config()), \
         patch("efis_data_manager.app.rumps.alert", return_value=1) as m_alert:
        link_id = app._choose_prepare_device_link_id()

    assert link_id == "1"
    m_alert.assert_called_once()


def test_choose_multi_device_cancel_returns_none():
    """Two devices: alert returning 0 (cancel) returns None (abort prepare)."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    with patch("efis_data_manager.app.load_config", return_value=_multi_device_config()), \
         patch("efis_data_manager.app.rumps.alert", return_value=0) as m_alert:
        link_id = app._choose_prepare_device_link_id()

    assert link_id is None
    m_alert.assert_called_once()


# --------------------------------------------------------------------------- #
# B) _do_prepare_drive binding write
# --------------------------------------------------------------------------- #

def _make_app():
    """App instance with the side-effecting helpers _do_prepare_drive touches
    stubbed to no-ops so nothing real runs."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    app._usb_monitor = MagicMock()
    app._notify = MagicMock()
    app._set_status = MagicMock()
    app._reconcile_drive_status = MagicMock()
    app._mark_provisioning = MagicMock()
    app._unmark_provisioning = MagicMock()
    app._archive_before_provision = MagicMock(return_value=True)
    return app


def test_do_prepare_binds_chosen_link_id():
    """A multi-device choice (Link 2) writes device_link_id='2' to the post-
    format mount via update_identity_provenance (R4.3, R6.4)."""
    app = _make_app()
    with patch(
        "efis_data_manager.drive_updater.prepare_drive",
        return_value={"success": True, "message": "ok"},
    ) as m_prepare, patch(
        "efis_data_manager.drive_updater.update_identity_provenance"
    ) as m_update:
        app._do_prepare_drive(
            volume_path="/Volumes/EFIS_X", label="EFIS_X", device_link_id="2"
        )

    m_prepare.assert_called_once()
    m_update.assert_called_once_with("/Volumes/EFIS_X", device_link_id="2")
    # No real USB/dev node touched; status reconciled, no failure notice.
    app._notify.assert_any_call("Drive Prepared", "ok")


def test_do_prepare_single_device_binds_link_one():
    """Single-device default (Link 1) writes device_link_id='1' (R4.4)."""
    app = _make_app()
    with patch(
        "efis_data_manager.drive_updater.prepare_drive",
        return_value={"success": True, "message": "ok"},
    ), patch(
        "efis_data_manager.drive_updater.update_identity_provenance"
    ) as m_update:
        app._do_prepare_drive(
            volume_path="/Volumes/EFIS", label="EFIS", device_link_id="1"
        )

    m_update.assert_called_once_with("/Volumes/EFIS", device_link_id="1")


def test_do_prepare_none_link_id_skips_binding():
    """device_link_id=None: the implementation guards `is not None`, so no
    binding is written (R4.3 guard)."""
    app = _make_app()
    with patch(
        "efis_data_manager.drive_updater.prepare_drive",
        return_value={"success": True, "message": "ok"},
    ), patch(
        "efis_data_manager.drive_updater.update_identity_provenance"
    ) as m_update:
        app._do_prepare_drive(
            volume_path="/Volumes/EFIS", label="EFIS", device_link_id=None
        )

    m_update.assert_not_called()


def test_do_prepare_failure_skips_binding():
    """A failed prepare does not write a binding and surfaces the failure."""
    app = _make_app()
    with patch(
        "efis_data_manager.drive_updater.prepare_drive",
        return_value={"success": False, "message": "erase failed"},
    ), patch(
        "efis_data_manager.drive_updater.update_identity_provenance"
    ) as m_update:
        app._do_prepare_drive(
            volume_path="/Volumes/EFIS", label="EFIS", device_link_id="2"
        )

    m_update.assert_not_called()
    app._notify.assert_any_call("Prepare Drive Failed", "erase failed")
