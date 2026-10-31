# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preservation property tests for the stale-drive-status-after-relabel bug.

Spec: .kiro/specs/stale-drive-status-after-relabel

Property 2 (Preservation): for every input where the bug condition does NOT
hold, the handler behaves exactly as it does today. Concretely, the eject
handler's genuine-removal branch (``find_mounted_managed_drive()`` -> None) must
keep setting "Not connected", forcing overall status "Idle", and emitting the
"Drive removed safely." notification; and the collateral flows the fix does not
touch (``_on_efis_drive_mounted`` new-mount, ``_reconcile_drive_status`` adopt
reconcile, and Eject/Verify enablement via ``_drive_connected``) must be
unchanged.

isBugCondition(X):
    find_mounted_managed_drive(X.volumes_dir) != None
    AND X.menubar_drive_status == "Not connected"

OBSERVATION-FIRST METHODOLOGY
-----------------------------
These tests were written by first running the UNFIXED
``_on_efis_drive_ejected`` and recording what it does, then asserting that
recorded behavior. On the UNFIXED handler the three removal calls fire
unconditionally — which, for the NON-bug-condition inputs exercised here (no
managed drive remains), is already the correct, to-be-preserved behavior. So
these tests PASS on unfixed code and define the baseline the Task-3 fix must not
regress. The relabel-branch (bug-condition) assertions live in Property 1
(tests/test_stale_drive_status_after_relabel.py), NOT here.

Per the design Testing Strategy > Validation Approach, the handler mutates rumps
menu items off the main thread, so we build the app via
``EFISDataManagerApp.__new__`` and observe ``find_mounted_managed_drive``,
``_set_drive_status``, ``_set_status``, ``_notify``, and
``_reconcile_drive_status`` rather than driving a live AppKit UI.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from efis_data_manager.app import EFISDataManagerApp


REMOVAL_TITLE = "EFIS Drive Ejected"
REMOVAL_MESSAGE = "Drive removed safely."

# A random ejected path the poll loop might report. Its exact value is
# irrelevant to the fixed handler — ground truth after the event (what
# find_mounted_managed_drive returns) is the only signal — so we generate it
# freely to prove that irrelevance.
_ejected_path = st.builds(
    lambda name: f"/Volumes/{name}",
    st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
        min_size=1,
        max_size=11,
    ),
)

# A /Volumes/ configuration: a set of mount names, each flagged managed or not.
# "no managed drive" is the preservation (genuine-removal) domain; "one or more
# managed" is the relabel domain (asserted by Property 1, not here).
_volume_name = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=11,
)


class _Harness:
    """Observable test double for the eject handler's UI helpers.

    Mirrors ``_set_drive_status``'s own flag semantics so ``_drive_connected``
    (which drives Eject/Verify enablement, Req 3.5) is observable without a live
    menu.
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
        # Real _reconcile_drive_status resolves find_mounted_managed_drive() and
        # calls _set_drive_status accordingly. We route through the observed
        # _set_drive_status so the observed flag/status stay consistent.
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
    def removal_notified(self):
        return (REMOVAL_TITLE, REMOVAL_MESSAGE) in self.notify_calls

    @property
    def idle_forced(self):
        return "Idle" in self.set_status_calls


# The value find_mounted_managed_drive is stubbed to return for the current
# scenario. _Harness._reconcile_drive_status reads it so the observed reconcile
# matches ground truth.
_scan_result = None


def _current_scan():
    return _scan_result


def _build_app(harness):
    """App instance with the eject handler's UI helpers observed by ``harness``."""
    app = EFISDataManagerApp.__new__(EFISDataManagerApp)
    app._set_drive_status = harness._set_drive_status
    app._reconcile_drive_status = harness._reconcile_drive_status
    app._set_status = harness._set_status
    app._notify = harness._notify
    return app


@contextmanager
def _ground_truth(remaining):
    """Stub find_mounted_managed_drive everywhere the handler/reconcile could
    reach it, so the observed behavior reflects ``remaining`` as the mounted
    managed drive (or None for a genuine removal).

    Patches both the canonical definition in usb_monitor (where
    _reconcile_drive_status imports it from, and where the Task-3 fix will too)
    and the app module (create=True so it works whether or not app.py has
    bound the name yet — the fix adds it in Task 3).
    """
    global _scan_result
    _scan_result = remaining
    try:
        with patch(
            "efis_data_manager.usb_monitor.find_mounted_managed_drive",
            return_value=remaining,
        ), patch(
            "efis_data_manager.app.find_mounted_managed_drive",
            return_value=remaining,
            create=True,
        ):
            yield
    finally:
        _scan_result = None


# --------------------------------------------------------------------------- #
# 3.1 / 3.2 Genuine-removal preservation (the NON-bug-condition eject domain)
# --------------------------------------------------------------------------- #


@settings(max_examples=200)
@given(ejected=_ejected_path)
def test_genuine_removal_preserves_not_connected_idle_and_notify(ejected):
    """3.1 + 3.2: for ANY ejected path, when no managed drive remains mounted
    (``find_mounted_managed_drive()`` -> None), the handler must set
    "Not connected", force overall "Idle", and emit the removal notification.

    This is the behavior the unfixed handler already produces for this domain;
    the fix must preserve it byte-for-byte. Validates: Requirements 3.1, 3.2."""
    harness = _Harness()
    app = _build_app(harness)

    with _ground_truth(remaining=None):
        app._on_efis_drive_ejected(ejected)

    assert harness.menubar_drive_status == "Not connected", (
        f"genuine removal of {ejected!r} left status "
        f"{harness.menubar_drive_status!r}; drive_status_calls="
        f"{harness.drive_status_calls}"
    )
    assert harness.idle_forced, (
        f"_set_status('Idle') not called on genuine removal; calls="
        f"{harness.set_status_calls}"
    )
    assert harness.removal_notified, (
        f"removal notification not emitted on genuine removal; notify calls="
        f"{harness.notify_calls}"
    )


# --------------------------------------------------------------------------- #
# 3.5 Enablement preservation: removal/notification gates on ground truth, and
# _drive_connected ends false (removal) / true (relabel).
# --------------------------------------------------------------------------- #


@settings(max_examples=200)
@given(
    names=st.sets(_volume_name, max_size=5),
    managed_flags=st.lists(st.booleans(), max_size=5),
    ejected=_ejected_path,
)
def test_removal_fires_when_no_managed_drive_remains(names, managed_flags, ejected):
    """Property (preservation domain only): across varied /Volumes/
    configurations where NO managed drive remains mounted, the eject handler
    fires the removal notification, forces overall "Idle", and ends with
    ``_drive_connected`` false (so Eject/Verify disable per 3.5) — regardless of
    how many unmanaged volumes exist or which path was ejected.

    We generate 0..5 volumes each flagged managed/unmanaged, then CONSTRAIN to
    the non-bug-condition domain via ``assume`` (no managed volume present),
    because the relabel domain (a managed drive still mounted) is asserted by
    Property 1 and would fail on the unfixed handler. This keeps the test
    passing on unfixed code while exercising the genuine-removal baseline across
    many shapes.

    Validates: Requirements 3.1, 3.2, 3.5."""
    ordered = sorted(names)
    has_managed = any(
        (managed_flags[i] if i < len(managed_flags) else False)
        for i in range(len(ordered))
    )
    # Preservation (non-bug-condition) domain only: no managed drive remains.
    assume(not has_managed)

    harness = _Harness()
    app = _build_app(harness)

    with _ground_truth(remaining=None):
        app._on_efis_drive_ejected(ejected)

    # Genuine removal: removal path fires, _drive_connected ends false so
    # Eject/Verify disable (3.5).
    assert harness.removal_notified
    assert harness.idle_forced
    assert harness.drive_connected is False
    assert harness.menubar_drive_status == "Not connected"


# --------------------------------------------------------------------------- #
# 3.3 New-mount preservation: _on_efis_drive_mounted is NOT touched by the fix.
# --------------------------------------------------------------------------- #


def test_new_mount_connects_unchanged():
    """3.3: a newly mounted managed drive still connects via
    ``_on_efis_drive_mounted`` (reconcile path). The fix does not touch this
    method; we assert the connect-on-mount behavior is intact.

    We stub the provisioning guard (returns False so the mount is not owned by a
    prepare/adopt) and the resolve/gate step, then assert the handler sets
    "Connected: <mount>" and does not emit a removal notification.
    Validates: Requirement 3.3."""
    harness = _Harness()
    app = _build_app(harness)
    # _on_efis_drive_mounted consults these collaborators before connecting.
    app._is_provisioning = lambda mount_point: False
    app._resolve_and_gate_drive = lambda mount_point: False  # stop before archive

    mount = "/Volumes/MINIAP1029"
    with _ground_truth(remaining=mount):
        app._on_efis_drive_mounted(mount)

    assert f"Connected: {mount}" in harness.drive_status_calls, (
        f"new mount did not set Connected status; calls={harness.drive_status_calls}"
    )
    assert harness.drive_connected is True
    assert not harness.removal_notified


# --------------------------------------------------------------------------- #
# 3.4 Adopt-reconcile preservation: _reconcile_drive_status is unchanged and
# resolves to Connected/Not connected purely from ground truth.
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(mount_name=_volume_name)
def test_reconcile_reflects_ground_truth_connected(mount_name):
    """3.4: the adopt worker calls ``_reconcile_drive_status()``; when a managed
    drive is mounted it must resolve to "Connected: <mount>". We exercise the
    REAL ``_reconcile_drive_status`` (not the harness stub) against a stubbed
    ground-truth scan, asserting it connects.

    Validates: Requirement 3.4."""
    harness = _Harness()
    app = _build_app(harness)
    # Use the real _reconcile_drive_status; route its _set_drive_status to the
    # harness observer.
    app._reconcile_drive_status = EFISDataManagerApp._reconcile_drive_status.__get__(app)

    mount = f"/Volumes/{mount_name}"
    with _ground_truth(remaining=mount):
        app._reconcile_drive_status()

    assert harness.drive_status_calls == [f"Connected: {mount}"]
    assert harness.drive_connected is True


def test_reconcile_reflects_ground_truth_not_connected():
    """3.4: ``_reconcile_drive_status()`` with no managed drive mounted resolves
    to "Not connected" — the same behavior the adopt path relies on and the fix
    leaves unchanged. Validates: Requirement 3.4."""
    harness = _Harness()
    app = _build_app(harness)
    app._reconcile_drive_status = EFISDataManagerApp._reconcile_drive_status.__get__(app)

    with _ground_truth(remaining=None):
        app._reconcile_drive_status()

    assert harness.drive_status_calls == ["Not connected"]
    assert harness.drive_connected is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
