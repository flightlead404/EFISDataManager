# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests: adopt persists the device binding into the identity file.

Bug: ``EFISDataManagerApp._do_adopt_drive`` resolved the Adopt_Device Link_ID on
the main thread but never wrote it into the drive's ``EFIS_DRIVE_ID.json``. For a
nav-only adopted drive (no settings file to re-derive a Link_ID from) that made
every re-insert resolve to DEVICE_UNDETERMINED, because
``device_resolution.resolve_drive`` falls back to the recorded ``device_link_id``
binding, which was absent.

Fix: ``_do_adopt_drive`` now takes ``device_link_id`` and, on a successful adopt,
calls ``update_identity_provenance(volume_path, device_link_id=...)`` — mirroring
the prepare/Start-clean path. The binding write is best-effort (a failure is
logged, the adopt still succeeds).

As with test_adopt_device_selection.py, the full rumps app is impractical
headless, so the REAL unbound ``_do_adopt_drive`` is bound to a minimal
SimpleNamespace stand-in; only the collaborators (adopt_drive, the UI/notify
helpers, provisioning guards) are stubs/recorders. The production logic under
test is genuine.
"""

from types import SimpleNamespace
from unittest import mock

from efis_data_manager.app import EFISDataManagerApp


LINK_ID = "2"
VOLUME = "/Volumes/MINIAP1029"


def _make_standin():
    """Stand-in with the real _do_adopt_drive bound and all side effects observed."""
    s = SimpleNamespace()
    s.notify_calls = []
    s.status_calls = []
    s.reconcile_calls = 0
    s.marked = []
    s.unmarked = []

    s._set_status = lambda t: s.status_calls.append(t)
    s._notify = lambda title, msg: s.notify_calls.append((title, msg))
    s._reconcile_drive_status = lambda: setattr(s, "reconcile_calls", s.reconcile_calls + 1)
    s._mark_provisioning = lambda vp: s.marked.append(vp)
    s._unmark_provisioning = lambda vp: s.unmarked.append(vp)
    s._archive_before_provision = lambda vp: True

    s._do_adopt_drive = lambda *a, **k: EFISDataManagerApp._do_adopt_drive(s, *a, **k)
    return s


def _run(standin, *, adopt_result, device_link_id=LINK_ID, binding_side_effect=None):
    """Invoke _do_adopt_drive with adopt_drive + update_identity_provenance stubbed.

    Returns the update_identity_provenance recorder so callers can assert on it.
    """
    with mock.patch(
        "efis_data_manager.drive_updater.adopt_drive",
        return_value=adopt_result,
    ), mock.patch(
        "efis_data_manager.drive_updater.update_identity_provenance",
        side_effect=binding_side_effect,
    ) as binding_mock:
        standin._do_adopt_drive(
            VOLUME,
            chart_capable=False,
            nav_label_prefix="MINIAP",
            content_action="nav_only",
            device_link_id=device_link_id,
        )
    return binding_mock


def test_successful_adopt_persists_device_link_id():
    """On a successful adopt the resolved Link_ID is written to the identity file."""
    s = _make_standin()
    binding = _run(s, adopt_result={"success": True, "message": "ok (nav: updated)"})

    binding.assert_called_once_with(VOLUME, device_link_id=LINK_ID)
    # Adopt still reports success and reconciles the UI.
    assert ("Drive Adopted", "ok (nav: updated)") in s.notify_calls
    assert s.reconcile_calls == 1


def test_binding_write_failure_does_not_fail_adopt():
    """A failed binding write is best-effort: the adopt still succeeds/notifies."""
    s = _make_standin()
    binding = _run(
        s,
        adopt_result={"success": True, "message": "ok"},
        binding_side_effect=OSError("read-only fs"),
    )

    binding.assert_called_once_with(VOLUME, device_link_id=LINK_ID)
    assert ("Drive Adopted", "ok") in s.notify_calls
    # Not the failure notification.
    assert all(title != "Adopt Drive Failed" for title, _ in s.notify_calls)


def test_no_binding_written_when_link_id_absent():
    """When no Link_ID was resolved, no binding write is attempted."""
    s = _make_standin()
    binding = _run(
        s,
        adopt_result={"success": True, "message": "ok"},
        device_link_id=None,
    )

    binding.assert_not_called()
    assert ("Drive Adopted", "ok") in s.notify_calls


def test_no_binding_written_when_adopt_fails():
    """A failed adopt must not write a device binding."""
    s = _make_standin()
    binding = _run(s, adopt_result={"success": False, "message": "boom"})

    binding.assert_not_called()
    assert any(title == "Adopt Drive Failed" for title, _ in s.notify_calls)
