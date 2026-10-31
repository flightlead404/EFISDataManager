# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the adopt-relabel flow.

Spec: .kiro/specs/stale-drive-status-after-relabel

Where the Property 1 (bug condition) and Property 2 (preservation) tests
exercise a single ``_on_efis_drive_ejected`` call in isolation, these tests
SEQUENCE multiple events to reproduce the real end-to-end flows the bugfix
targets:

  * Adopt-relabel: the adopt worker reconciles to "Connected" via
    ``_reconcile_drive_status()`` against the relabelled volume, then the next
    poll delivers the stale-name eject of the OLD path while the relabelled
    volume is still mounted. The menu bar must stay "Connected" and Eject/Verify
    must stay enabled (Requirements 2.1-2.4, 3.5).
  * Physical removal after adopt: adopt reconciles to "Connected", then the
    only managed drive is physically removed and the eject fires with nothing
    remaining. The menu bar must read "Not connected", overall status "Idle",
    and the removal notification fires exactly once (Requirements 3.1, 3.2).
  * Relabel then actual removal: a relabel eject is silent (drive still
    mounted), then a later eject with no drive remaining notifies. The first
    eject must emit no removal notification and the second must emit exactly one
    (Requirements 2.4, 3.1, 3.2).

Per the design Testing Strategy > Validation Approach, the eject/reconcile
handlers mutate rumps menu items off the main thread, so we build the app via
``EFISDataManagerApp.__new__`` and observe ``find_mounted_managed_drive``,
``_set_drive_status``, ``_set_status``, and ``_notify`` rather than driving a
live AppKit UI. Ground truth is modelled with a mutable scan value so a single
test can step the ``/Volumes/`` state across adopt -> relabel -> removal, the
way the real poll loop observes it.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from efis_data_manager.app import EFISDataManagerApp


# Concrete relabel case from the reported defect: adopt relabels EFIS_MINI ->
# MINIAP1029. On the next poll the monitor reports an eject of the OLD name
# while the relabelled volume is still mounted and managed.
OLD_NAME_PATH = "/Volumes/EFIS_MINI"
RELABELLED_PATH = "/Volumes/MINIAP1029"

REMOVAL_TITLE = "EFIS Drive Ejected"
REMOVAL_MESSAGE = "Drive removed safely."


# Mutable ground-truth value the stubbed find_mounted_managed_drive returns.
# Stepping this between events models the /Volumes/ state the real poll loop
# observes as the flow progresses (adopt -> relabel eject -> removal).
_scan_result = None


def _current_scan():
    return _scan_result


class _Harness:
    """Observable test double for the eject/reconcile UI helpers.

    Mirrors ``_set_drive_status``'s own flag semantics so ``_drive_connected``
    (which drives Eject/Verify enablement, Req 3.5) is observable without a live
    menu. ``_reconcile_drive_status`` routes through the observed
    ``_set_drive_status`` against the current ground-truth scan, matching the
    real helper.
    """

    def __init__(self):
        self.menubar_drive_status = None
        self.drive_connected = None
        self.drive_status_calls = []
        self.set_status_calls = []
        self.notify_calls = []
        self.reconcile_calls = 0

    def _set_drive_status(self, drive_text):
        self.drive_status_calls.append(drive_text)
        connected = "Not connected" not in drive_text
        self.drive_connected = connected
        self.menubar_drive_status = "Connected" if connected else "Not connected"

    def _reconcile_drive_status(self):
        self.reconcile_calls += 1
        mount = _current_scan()
        if mount is not None:
            self._set_drive_status(f"Connected: {mount}")
        else:
            self._set_drive_status("Not connected")

    def _set_status(self, status_text):
        self.set_status_calls.append(status_text)

    def _notify(self, title, message):
        self.notify_calls.append((title, message))

    @property
    def removal_notifications(self):
        return [
            (t, m) for (t, m) in self.notify_calls
            if t == REMOVAL_TITLE and m == REMOVAL_MESSAGE
        ]

    @property
    def removal_notified(self):
        return bool(self.removal_notifications)

    @property
    def idle_forced(self):
        return "Idle" in self.set_status_calls

    @property
    def eject_verify_enabled(self):
        """Eject/Verify enablement tracks ``_drive_connected`` (Req 3.5)."""
        return self.drive_connected


def _build_app(harness):
    """App instance with the eject/reconcile UI helpers observed by ``harness``."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    app._set_drive_status = harness._set_drive_status
    app._reconcile_drive_status = harness._reconcile_drive_status
    app._set_status = harness._set_status
    app._notify = harness._notify
    return app


@contextmanager
def _ground_truth():
    """Stub find_mounted_managed_drive everywhere the handler/reconcile reach it,
    returning the current ``_scan_result`` so a test can STEP the mounted state
    between sequenced events. Reset to None on exit.

    Patches both the canonical definition in usb_monitor (where the real
    handler and ``_reconcile_drive_status`` import it from) and the app module.
    """
    global _scan_result
    _scan_result = None
    try:
        with patch(
            "efis_data_manager.usb_monitor.find_mounted_managed_drive",
            side_effect=_current_scan,
        ), patch(
            "efis_data_manager.app.find_mounted_managed_drive",
            side_effect=_current_scan,
            create=True,
        ):
            yield
    finally:
        _scan_result = None


def _set_mounted(mount_point):
    """Model the /Volumes/ state the next scan will observe."""
    global _scan_result
    _scan_result = mount_point


# --------------------------------------------------------------------------- #
# Flow 1: adopt-relabel — status stays Connected, Eject/Verify stay enabled.
# --------------------------------------------------------------------------- #


def test_adopt_relabel_flow_keeps_connected_and_actions_enabled():
    """Adopt relabels EFIS_MINI -> MINIAP1029 and reconciles to "Connected";
    the next poll ejects the OLD name while the relabelled volume is still
    mounted. The menu bar must stay "Connected" and Eject/Verify must stay
    enabled (``_drive_connected`` true).

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 3.4, 3.5."""
    harness = _Harness()
    app = _build_app(harness)

    with _ground_truth():
        # Step 1 — adopt completes: the relabelled volume is mounted and the
        # adopt worker reconciles the drive status (Req 3.4).
        _set_mounted(RELABELLED_PATH)
        app._reconcile_drive_status()
        assert harness.menubar_drive_status == "Connected"
        assert harness.eject_verify_enabled is True

        # Step 2 — next poll delivers the stale-name eject of the OLD path
        # while the relabelled volume is still mounted (the bug condition).
        app._on_efis_drive_ejected(OLD_NAME_PATH)

    # Menu bar still reads "Connected" (2.1), no Idle override (2.3), no removal
    # notification (2.4), and Eject/Verify stay enabled (3.5).
    assert harness.menubar_drive_status == "Connected", (
        f"stale-name eject left status {harness.menubar_drive_status!r}; "
        f"drive_status_calls={harness.drive_status_calls}"
    )
    assert harness.eject_verify_enabled is True
    assert not harness.idle_forced, (
        f"_set_status('Idle') fired on a relabel eject; calls={harness.set_status_calls}"
    )
    assert not harness.removal_notified, (
        f"removal notification fired on a relabel eject; notify={harness.notify_calls}"
    )
    # The eject reconciled once more against ground truth (adopt + eject).
    assert harness.reconcile_calls == 2


# --------------------------------------------------------------------------- #
# Flow 2: physical removal after adopt — Not connected, Idle, one notification.
# --------------------------------------------------------------------------- #


def test_physical_removal_after_adopt_notifies_once_and_idles():
    """Adopt reconciles to "Connected", then the only managed drive is
    physically removed and the eject fires with nothing remaining. The menu bar
    must read "Not connected", overall status "Idle", and the removal
    notification fires exactly once.

    Validates: Requirements 3.1, 3.2, 3.4, 3.5."""
    harness = _Harness()
    app = _build_app(harness)

    with _ground_truth():
        # Step 1 — adopt completes and reconciles to Connected.
        _set_mounted(RELABELLED_PATH)
        app._reconcile_drive_status()
        assert harness.menubar_drive_status == "Connected"
        assert harness.eject_verify_enabled is True

        # Step 2 — physical removal: no managed drive remains, then eject fires.
        _set_mounted(None)
        app._on_efis_drive_ejected(RELABELLED_PATH)

    assert harness.menubar_drive_status == "Not connected", (
        f"removal left status {harness.menubar_drive_status!r}; "
        f"drive_status_calls={harness.drive_status_calls}"
    )
    assert harness.idle_forced, (
        f"_set_status('Idle') not called on genuine removal; calls={harness.set_status_calls}"
    )
    assert len(harness.removal_notifications) == 1, (
        f"expected exactly one removal notification; notify={harness.notify_calls}"
    )
    # Eject/Verify disable once the drive is gone (3.5).
    assert harness.eject_verify_enabled is False


# --------------------------------------------------------------------------- #
# Flow 3: relabel then actual removal — first eject silent, second notifies.
# --------------------------------------------------------------------------- #


def test_relabel_then_removal_first_silent_second_notifies():
    """A relabel eject (drive still mounted) must be silent, then a later eject
    with no drive remaining must notify exactly once. Sequences both ejects on
    one app instance to prove the first does not consume/suppress the second.

    Validates: Requirements 2.4, 3.1, 3.2."""
    harness = _Harness()
    app = _build_app(harness)

    with _ground_truth():
        # Step 1 — adopt reconciles to Connected against the relabelled volume.
        _set_mounted(RELABELLED_PATH)
        app._reconcile_drive_status()

        # Step 2 — relabel stale-name eject: relabelled volume still mounted.
        app._on_efis_drive_ejected(OLD_NAME_PATH)
        # First eject is silent — no removal notification, no Idle override.
        assert harness.removal_notifications == [], (
            f"first (relabel) eject notified removal; notify={harness.notify_calls}"
        )
        assert not harness.idle_forced
        assert harness.menubar_drive_status == "Connected"

        # Step 3 — actual removal: drive unplugged, nothing remains, eject fires.
        _set_mounted(None)
        app._on_efis_drive_ejected(RELABELLED_PATH)

    # Second eject notifies exactly once and blanks/idles the UI.
    assert len(harness.removal_notifications) == 1, (
        f"expected exactly one removal notification after actual removal; "
        f"notify={harness.notify_calls}"
    )
    assert harness.idle_forced
    assert harness.menubar_drive_status == "Not connected"
    assert harness.eject_verify_enabled is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
