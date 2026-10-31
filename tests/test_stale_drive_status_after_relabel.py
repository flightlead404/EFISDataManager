# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bug condition exploration test for the stale-drive-status-after-relabel bug.

Spec: .kiro/specs/stale-drive-status-after-relabel

Property 1 (Bug Condition): on an eject event the handler must reconcile the
connected status against ground truth. In the RELABEL case a managed drive is
still mounted (``find_mounted_managed_drive()`` returns a path), so after
``_on_efis_drive_ejected`` runs the connected status must read "Connected", the
overall status must NOT be forced to "Idle", and the "Drive removed safely"
removal notification must NOT fire.

isBugCondition(X):
    find_mounted_managed_drive(X.volumes_dir) != None
    AND X.menubar_drive_status == "Not connected"

This test encodes the EXPECTED (post-fix) behavior, so on the current UNFIXED
``_on_efis_drive_ejected`` — which unconditionally sets "Not connected", forces
"Idle", and fires the removal notification — it MUST FAIL. That failure confirms
the bug exists. The same test validates the fix once the handler reconciles to
ground truth (tasks 3.1/3.2).

The handler mutates rumps menu items off the main thread, so per the design
Testing Strategy > Validation Approach we build the app via
``EFISDataManagerApp.__new__`` and observe ``find_mounted_managed_drive``,
``_set_drive_status``, ``_set_status``, ``_notify``, and
``_reconcile_drive_status`` rather than driving a live AppKit UI.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4
"""

from unittest.mock import patch

import pytest

from efis_data_manager.app import EFISDataManagerApp


# Concrete relabel case from the reported defect: adopt relabels EFIS_MINI ->
# MINIAP1029. On the next poll the monitor reports an eject of the OLD name
# while the relabelled volume is still mounted and managed.
STALE_EJECTED_PATH = "/Volumes/EFIS_MINI"
REMAINING_MANAGED_PATH = "/Volumes/MINIAP1029"

REMOVAL_MESSAGE = "Drive removed safely."


class _Harness:
    """Observable test double for the eject handler's UI helpers.

    Tracks the resulting menu-bar connected status and records the overall
    status + notification calls so the test can assert on ground-truth
    reconciliation without a live rumps/AppKit menu.
    """

    def __init__(self):
        # menu-bar connected status as the handler leaves it
        self.menubar_drive_status = None
        self.drive_status_calls = []
        self.set_status_calls = []
        self.notify_calls = []
        self.reconcile_calls = 0

    def _set_drive_status(self, drive_text):
        self.drive_status_calls.append(drive_text)
        # Mirror _set_drive_status's own flag semantics: anything other than
        # "Not connected" is a connected state.
        self.menubar_drive_status = (
            "Not connected" if "Not connected" in drive_text else "Connected"
        )

    def _reconcile_drive_status(self):
        # The fixed handler's relabel branch calls this; it resolves to
        # "Connected: <mount>" because a managed drive is still mounted.
        self.reconcile_calls += 1
        self.menubar_drive_status = "Connected"

    def _set_status(self, status_text):
        self.set_status_calls.append(status_text)

    def _notify(self, title, message):
        self.notify_calls.append((title, message))

    @property
    def removal_notified(self):
        return any(REMOVAL_MESSAGE in msg for _title, msg in self.notify_calls)

    @property
    def idle_forced(self):
        return "Idle" in self.set_status_calls


def _build_app(harness):
    """App instance with the eject handler's UI helpers observed by ``harness``."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    app._set_drive_status = harness._set_drive_status
    app._reconcile_drive_status = harness._reconcile_drive_status
    app._set_status = harness._set_status
    app._notify = harness._notify
    return app


def _run_relabel_eject():
    """Drive the bug condition: a managed drive is still mounted when the
    stale-name eject is processed. Returns the observing harness."""
    harness = _Harness()
    app = _build_app(harness)

    # Ground truth after the eject event: the relabelled volume is still
    # mounted and managed (the bug condition). The handler must consult this.
    with patch(
        "efis_data_manager.usb_monitor.find_mounted_managed_drive",
        return_value=REMAINING_MANAGED_PATH,
    ), patch(
        "efis_data_manager.app.find_mounted_managed_drive",
        return_value=REMAINING_MANAGED_PATH,
        create=True,
    ):
        app._on_efis_drive_ejected(STALE_EJECTED_PATH)

    return harness


def test_relabel_eject_reconciles_to_connected():
    """Counterexample 1: on a relabel eject with a managed drive still mounted,
    the connected status must end "Connected". Unfixed code sets "Not connected".
    (Requirements 1.1, 1.2, 2.1, 2.2)"""
    harness = _run_relabel_eject()
    assert harness.menubar_drive_status == "Connected", (
        "relabel eject left status "
        f"{harness.menubar_drive_status!r}; _set_drive_status calls="
        f"{harness.drive_status_calls}"
    )


def test_relabel_eject_does_not_force_idle():
    """Counterexample 2: a relabel eject must NOT force the overall status to
    "Idle". Unfixed code calls _set_status("Idle"). (Requirements 1.3, 2.3)"""
    harness = _run_relabel_eject()
    assert not harness.idle_forced, (
        f"_set_status('Idle') was called on a relabel eject; calls="
        f"{harness.set_status_calls}"
    )


def test_relabel_eject_does_not_notify_removal():
    """Counterexample 3: a relabel eject must NOT emit the "Drive removed
    safely" notification. Unfixed code fires it. (Requirements 1.4, 2.4)"""
    harness = _run_relabel_eject()
    assert not harness.removal_notified, (
        f"removal notification fired on a relabel eject; notify calls="
        f"{harness.notify_calls}"
    )


def test_genuine_removal_still_notifies_baseline():
    """Edge/baseline (NOT the bug condition): when NO managed drive remains,
    the handler must set "Not connected", force "Idle", and emit the removal
    notification. This passes on unfixed code and defines the behavior the fix
    must preserve. (Requirements 2.2, baseline for 3.1/3.2)"""
    harness = _Harness()
    app = _build_app(harness)

    with patch(
        "efis_data_manager.usb_monitor.find_mounted_managed_drive",
        return_value=None,
    ), patch(
        "efis_data_manager.app.find_mounted_managed_drive",
        return_value=None,
        create=True,
    ):
        app._on_efis_drive_ejected(STALE_EJECTED_PATH)

    assert harness.menubar_drive_status == "Not connected"
    assert harness.idle_forced
    assert harness.removal_notified


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
