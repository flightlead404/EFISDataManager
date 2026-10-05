# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the write-on-sync routing + preservation (image-based-drive-write
Task 10, Requirements 11.1, 11.2, 11.3, 12.1, 12.2, 12.3).

Constructing the full rumps ``EFISDataManagerApp`` is impractical in a headless
test (it builds a live macOS menu-bar item), so — mirroring
``test_app_composer_refresh`` / ``test_app_image_notification`` — these tests
bind the REAL unbound methods (``_run_drive_update``, ``_perform_write``,
``_write_via_image``, ``_write_via_mount_swap``, ``_resolve_write_target``) to a
minimal stand-in ``self``. The routing logic under test is therefore the genuine
production code.

NOTHING real is written: ``image_writer.write``, ``image_composer.compose``,
``mount_swap.msdos_mount``, ``drive_updater.update_drive`` / ``verify_drive`` /
``check_drive_currency``, and the diskutil-backed resolvers
(``resolve_device_node`` / ``_volume_uuid`` / ``_volume_name``) are ALL mocked.
No real ``dd`` runs here — the real dd is Task 12, gated behind the Task 6 safety
checkpoint, and ``image_write_enabled`` defaults to False anyway.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from efis_data_manager import app as app_module
from efis_data_manager import config as config_module
from efis_data_manager import image_writer
from efis_data_manager.app import EFISDataManagerApp
from efis_data_manager.mount_swap import MountSwapError


# --------------------------------------------------------------------------- #
# Stand-in app
# --------------------------------------------------------------------------- #


def _make_standin(image_write_enabled=False):
    """Minimal stand-in exposing only what the write-routing methods touch.

    ``_perform_write`` is given its real implementation (bound at call time via
    the class), so the dispatch decision is genuine production code. The two
    branch methods are recorded by default so a dispatch test can assert which
    one ran, then selectively replaced with the real implementation in the
    branch-behaviour tests.
    """
    standin = SimpleNamespace()
    standin.config = {
        "composed_image_path": "/tmp/efis-test/EFIS_USB.img",
        "composed_image_size_bytes": 64 * 1024 * 1024,
        "usb_image_path": "/tmp/efis-test/usb",
        "image_write_enabled": image_write_enabled,
    }
    standin.status_calls = []
    standin.notify_calls = []
    standin.image_branch_calls = []
    standin.swap_branch_calls = []

    standin._set_status = lambda msg: standin.status_calls.append(msg)
    standin._notify = lambda title, msg: standin.notify_calls.append((title, msg))

    # By default record which branch is chosen (dispatch tests). The branch
    # tests override these with the real bound methods.
    standin._write_via_image = lambda *a, **k: (
        standin.image_branch_calls.append((a, k)) or True
    )
    standin._write_via_mount_swap = lambda *a, **k: (
        standin.swap_branch_calls.append((a, k)) or None
    )

    # The watchdog wrapper just runs the operation with a never-aborted
    # predicate (no real MountWatchdog in a headless test).
    standin._with_active_watchdog = lambda mount, run: run(lambda: False)
    standin._usb_monitor = None
    return standin


def _perform_write(standin, mount_point="/Volumes/EFIS_3",
                   pending=None, stale=None, currency=None):
    """Invoke the real `_perform_write` against the stand-in."""
    EFISDataManagerApp._perform_write(
        standin,
        mount_point,
        pending or [],
        stale or ["charts"],
        currency or {"stale_items": ["scanned charts"]},
        lambda msg: standin.status_calls.append(msg),
    )


# --------------------------------------------------------------------------- #
# Req 11.1 — image-write enabled -> image path used, msdos_mount NOT used
# --------------------------------------------------------------------------- #


def test_enabled_routes_to_image_path_not_mount_swap():
    standin = _make_standin(image_write_enabled=True)

    with mock.patch.object(app_module, "msdos_mount") as mock_mount:
        _perform_write(standin)

    # Image branch ran; swap branch did not.
    assert len(standin.image_branch_calls) == 1
    assert standin.swap_branch_calls == []
    # And the msdos_mount swap was never entered for the write (Req 11.1).
    mock_mount.assert_not_called()


def test_enabled_image_write_calls_image_writer_write_only():
    """End-to-end through the REAL image branch: image_writer.write is called
    and msdos_mount is never used for the write (Req 11.1)."""
    standin = _make_standin(image_write_enabled=True)
    # Use the real image branch + resolver so image_writer.write is reached.
    standin._write_via_image = lambda *a, **k: EFISDataManagerApp._write_via_image(
        standin, *a, **k
    )
    standin._resolve_write_target = (
        lambda mp: EFISDataManagerApp._resolve_write_target(standin, mp)
    )

    complete = image_writer.WriteResult(
        device_node="/dev/disk4s1",
        bytes_written=standin.config["composed_image_size_bytes"],
        verified=True,
        status="complete",
        error=None,
    )

    with mock.patch.object(
        image_writer, "write", return_value=complete
    ) as mock_write, mock.patch.object(
        app_module, "msdos_mount"
    ) as mock_mount, mock.patch(
        "efis_data_manager.mount_swap.resolve_device_node",
        return_value="/dev/disk4s1",
    ), mock.patch(
        "efis_data_manager.drive_updater._volume_uuid", return_value="UUID-1234"
    ), mock.patch(
        "efis_data_manager.drive_updater._volume_name", return_value="EFIS_3"
    ):
        _perform_write(standin)

    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    # Guard expectations are forwarded, confirmed=True (flow authorization).
    assert kwargs["expected_identity_id"] == "UUID-1234"
    assert kwargs["expected_label"] == "EFIS_3"
    assert kwargs["expected_size_bytes"] == standin.config["composed_image_size_bytes"]
    assert kwargs["confirmed"] is True
    # The swap was never used for the write (Req 11.1).
    mock_mount.assert_not_called()


# --------------------------------------------------------------------------- #
# Req 11.2 — fallback flag (image_write_enabled False) -> msdos_mount
# --------------------------------------------------------------------------- #


def test_disabled_routes_to_mount_swap():
    standin = _make_standin(image_write_enabled=False)

    _perform_write(standin)

    # Swap branch ran; image branch did not (Req 11.2).
    assert len(standin.swap_branch_calls) == 1
    assert standin.image_branch_calls == []


def test_default_config_image_write_disabled():
    """The shipped default keeps the proven path (image-write opt-in)."""
    assert config_module.DEFAULT_CONFIG["image_write_enabled"] is False


def test_disabled_uses_msdos_mount_for_the_write():
    """End-to-end through the REAL swap branch: msdos_mount IS used (Req 11.2)."""
    standin = _make_standin(image_write_enabled=False)
    standin._write_via_mount_swap = (
        lambda *a, **k: EFISDataManagerApp._write_via_mount_swap(standin, *a, **k)
    )

    work_mount = "/private/var/efis_work"
    cm = mock.MagicMock()
    cm.__enter__.return_value = work_mount
    cm.__exit__.return_value = False

    jobs = {"charts": SimpleNamespace(files_updated=5)}
    update_result = {"jobs": jobs, "errors": [], "aborted": False}

    with mock.patch.object(
        app_module, "msdos_mount", return_value=cm
    ) as mock_mount, mock.patch(
        "efis_data_manager.drive_updater.update_drive", return_value=update_result
    ) as mock_update, mock.patch(
        "efis_data_manager.drive_updater.verify_drive"
    ), mock.patch(
        "efis_data_manager.drive_updater._terminal_status_from_jobs",
        return_value="Drive current",
    ), mock.patch.object(
        image_writer, "write"
    ) as mock_image_write:
        _perform_write(standin)

    # The swap was entered and update_drive ran against the work mount.
    mock_mount.assert_called_once()
    mock_update.assert_called_once()
    assert mock_update.call_args[0][0] == work_mount
    # The image writer was NOT touched on the disabled/fallback path.
    mock_image_write.assert_not_called()
    assert "Drive current" in standin.status_calls


# --------------------------------------------------------------------------- #
# Req 11.3 — image-write failure + fallback enabled -> msdos_mount
# --------------------------------------------------------------------------- #


def test_image_write_failure_falls_back_to_mount_swap():
    """status != "complete" from image-write -> swap branch runs (Req 11.3)."""
    standin = _make_standin(image_write_enabled=True)
    # Image branch reports failure (returns False); swap branch recorded.
    standin._write_via_image = lambda *a, **k: (
        standin.image_branch_calls.append((a, k)) or False
    )

    _perform_write(standin)

    # Image branch attempted first, then the swap fallback ran (Req 11.3).
    assert len(standin.image_branch_calls) == 1
    assert len(standin.swap_branch_calls) == 1


def test_image_write_incomplete_result_triggers_fallback_end_to_end():
    """A real `_write_via_image` returning an "incomplete" WriteResult makes
    `_perform_write` fall back to the swap path (Req 11.3)."""
    standin = _make_standin(image_write_enabled=True)
    standin._write_via_image = lambda *a, **k: EFISDataManagerApp._write_via_image(
        standin, *a, **k
    )
    standin._resolve_write_target = (
        lambda mp: EFISDataManagerApp._resolve_write_target(standin, mp)
    )
    # The swap fallback is recorded (not run) so we isolate the routing.

    incomplete = image_writer.WriteResult(
        device_node="/dev/disk4s1",
        bytes_written=0,
        verified=False,
        status="incomplete",
        error="raw readback hash did not match composed image; write incomplete",
    )

    with mock.patch.object(
        image_writer, "write", return_value=incomplete
    ), mock.patch(
        "efis_data_manager.mount_swap.resolve_device_node",
        return_value="/dev/disk4s1",
    ), mock.patch(
        "efis_data_manager.drive_updater._volume_uuid", return_value="UUID-1234"
    ), mock.patch(
        "efis_data_manager.drive_updater._volume_name", return_value="EFIS_3"
    ):
        _perform_write(standin)

    # image-write ran and reported incomplete; swap fallback was invoked.
    assert len(standin.swap_branch_calls) == 1


def test_image_write_unresolved_device_node_falls_back():
    """If the device node can't be resolved, image-write fails -> fallback."""
    standin = _make_standin(image_write_enabled=True)
    standin._write_via_image = lambda *a, **k: EFISDataManagerApp._write_via_image(
        standin, *a, **k
    )
    standin._resolve_write_target = (
        lambda mp: EFISDataManagerApp._resolve_write_target(standin, mp)
    )

    with mock.patch.object(image_writer, "write") as mock_write, mock.patch(
        "efis_data_manager.mount_swap.resolve_device_node", return_value=None
    ), mock.patch(
        "efis_data_manager.drive_updater._volume_uuid", return_value=None
    ), mock.patch(
        "efis_data_manager.drive_updater._volume_name", return_value=None
    ):
        _perform_write(standin)

    # No device node -> image_writer.write never called, swap fallback ran.
    mock_write.assert_not_called()
    assert len(standin.swap_branch_calls) == 1


# --------------------------------------------------------------------------- #
# Req 12.1, 12.3 — currency current -> NO write of either kind
# --------------------------------------------------------------------------- #


def _make_run_update_standin(image_write_enabled=False):
    """Stand-in for the full `_run_drive_update` currency-decision test.

    Records whether `_perform_write` is reached at all, so the no-op path can be
    asserted (the currency decision in steps 1-2 is unchanged by Task 10).
    """
    standin = _make_standin(image_write_enabled=image_write_enabled)
    standin.perform_write_calls = []
    standin._perform_write = lambda *a, **k: standin.perform_write_calls.append(
        (a, k)
    )
    return standin


def test_currency_current_performs_no_write():
    """No pending + is_current -> report current, NO write (Req 12.1, 12.3)."""
    standin = _make_run_update_standin()

    currency = {
        "is_current": True,
        "families": {"charts": {"current": True}},
        "stale_items": [],
    }

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families", return_value=[]
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch.object(
        app_module, "msdos_mount"
    ) as mock_mount, mock.patch.object(
        image_writer, "write"
    ) as mock_image_write:
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_3")

    # The write dispatcher was never reached (Req 12.3) ...
    assert standin.perform_write_calls == []
    # ... and neither write mechanism was touched (Req 12.1).
    mock_mount.assert_not_called()
    mock_image_write.assert_not_called()
    # The drive was reported current.
    assert "Drive current" in standin.status_calls


def test_write_needed_reaches_dispatcher():
    """Stale family present -> `_perform_write` IS reached (write needed)."""
    standin = _make_run_update_standin()

    currency = {
        "is_current": False,
        "families": {"charts": {"current": False}},
        "stale_items": ["scanned charts"],
    }

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families", return_value=[]
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ):
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_3")

    # The currency decision found work; the dispatcher ran exactly once.
    assert len(standin.perform_write_calls) == 1


# --------------------------------------------------------------------------- #
# Req 12.2 — archive-on-insert is independent of _run_drive_update
# --------------------------------------------------------------------------- #


def test_run_drive_update_does_not_invoke_archive():
    """`_run_drive_update` never calls the archive path (Req 12.2).

    The archive/FDL-on-insert path (`_run_archive`) runs BEFORE
    `_run_drive_update` and is untouched by Task 10. A light assertion: driving
    the currency-current no-op path imports/calls nothing from the archiver, and
    `_run_archive` is a distinct method the update path does not invoke.
    """
    standin = _make_run_update_standin()
    standin.archive_calls = []
    # If `_run_drive_update` ever called `_run_archive`, this would record it.
    standin._run_archive = lambda mp: standin.archive_calls.append(mp)

    currency = {
        "is_current": True,
        "families": {"charts": {"current": True}},
        "stale_items": [],
    }

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families", return_value=[]
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch(
        "efis_data_manager.archiver.archive_efis_drive"
    ) as mock_archive:
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_3")

    # The update path neither called _run_archive nor the archiver directly.
    assert standin.archive_calls == []
    mock_archive.assert_not_called()
