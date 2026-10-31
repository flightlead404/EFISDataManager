# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the write-on-sync path in ``_run_drive_update`` (chart-manifest-sync
Stage 3, task 4.1; Requirements 4.5, 9.1, 9.2).

chart-manifest-sync **supersedes** chart-sync-stall-fix and the image-dd
experiment: the ``_perform_write`` / ``_write_via_image`` / ``_write_via_mount_swap``
dispatch and the ``msdos_mount`` swap are gone. ``_run_drive_update`` now runs the
sync DIRECTLY against the FSKit ``/Volumes`` mount. These tests exercise that
collapsed path:

  * currency current + nothing pending -> the idempotent no-op path, NO write;
  * a stale family -> ``update_drive`` runs directly against the real
    ``mount_point`` (never a private work-mount, ``msdos_mount`` is never used);
  * an interrupted ``pending`` family -> ``verify_drive(repair=True)`` runs first,
    against the real ``mount_point``;
  * the update path never invokes the archiver (Req 12.2, preserved).

Constructing the full rumps ``EFISDataManagerApp`` is impractical in a headless
test (it builds a live macOS menu-bar item), so — mirroring
``test_app_composer_refresh`` / ``test_app_image_notification`` — these tests
bind the REAL unbound ``_run_drive_update`` to a minimal stand-in ``self``. The
sync-engine entry points (``update_drive`` / ``verify_drive`` /
``check_drive_currency`` / ``resolve_drive_id`` / ``pending_families`` /
``_terminal_status_from_jobs``) are ALL mocked; nothing real is written.
"""

from types import SimpleNamespace
from unittest import mock

from efis_data_manager import app as app_module
from efis_data_manager.app import EFISDataManagerApp


# --------------------------------------------------------------------------- #
# Stand-in app
# --------------------------------------------------------------------------- #


def _make_standin():
    """Minimal stand-in for the full `_run_drive_update` path.

    The watchdog wrapper just runs the operation with a never-aborted predicate
    (no real MountWatchdog in a headless test). The chart-capability gate
    returns the fail-safe default (charts allowed); the gate itself is covered
    by its own tests.
    """
    standin = SimpleNamespace()
    standin.status_calls = []
    standin.notify_calls = []

    standin._set_status = lambda msg: standin.status_calls.append(msg)
    standin._notify = lambda title, msg: standin.notify_calls.append((title, msg))

    # The watchdog wrapper runs `run(is_aborted)` and records the path it
    # monitored, so a test can assert the watchdog watched the REAL mount_point
    # (not a swapped-out private work-mount).
    standin.watchdog_mounts = []

    def _wad(mount, run):
        standin.watchdog_mounts.append(mount)
        return run(lambda: False)

    standin._with_active_watchdog = _wad
    standin._device_chart_capable = lambda mount_point: True
    standin._usb_monitor = None

    # Record flush+unmount calls so a test can assert the "flush = done" step
    # (chart-manifest-sync task 6.2, Req 7.1) ran at the end of a successful
    # auto-sync. The real method delegates to drive_updater.flush_and_unmount;
    # the stand-in records the mount it was asked to flush.
    standin.flush_unmount_calls = []
    standin._flush_and_unmount = lambda mp: standin.flush_unmount_calls.append(mp)
    return standin


# --------------------------------------------------------------------------- #
# Req 9.1/9.2 + idempotent no-op path — currency current -> NO write
# --------------------------------------------------------------------------- #


def test_currency_current_performs_no_write():
    """No pending + nothing stale -> report current, NO write, no engine call."""
    standin = _make_standin()

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
        "efis_data_manager.drive_updater.update_drive"
    ) as mock_update, mock.patch(
        "efis_data_manager.drive_updater.verify_drive"
    ) as mock_verify:
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_3")

    # No write work ran, and the watchdog was never engaged.
    mock_update.assert_not_called()
    mock_verify.assert_not_called()
    assert standin.watchdog_mounts == []
    assert "Drive current" in standin.status_calls


# --------------------------------------------------------------------------- #
# Req 4.5 — a stale family writes DIRECTLY to the FSKit mount (no swap)
# --------------------------------------------------------------------------- #


def test_stale_family_syncs_directly_to_fskit_mount():
    """A stale family runs update_drive against the real mount_point itself."""
    standin = _make_standin()
    mount_point = "/Volumes/EFIS_3"

    currency = {
        "is_current": False,
        "families": {"charts": {"current": False}},
        "stale_items": ["scanned charts"],
    }
    jobs = {"charts": SimpleNamespace(files_updated=5)}
    update_result = {"jobs": jobs, "errors": [], "aborted": False}

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families", return_value=[]
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch(
        "efis_data_manager.drive_updater.update_drive",
        return_value=update_result,
    ) as mock_update, mock.patch(
        "efis_data_manager.drive_updater.verify_drive"
    ) as mock_verify, mock.patch(
        "efis_data_manager.drive_updater._terminal_status_from_jobs",
        return_value="Drive current",
    ):
        EFISDataManagerApp._run_drive_update(standin, mount_point)

    # update_drive ran exactly once, against the REAL FSKit mount (not a
    # private work-mount), for the stale family only.
    mock_update.assert_called_once()
    assert mock_update.call_args[0][0] == mount_point
    assert mock_update.call_args[1]["families"] == ["charts"]
    # No verify (nothing was pending) and the watchdog watched the real mount.
    mock_verify.assert_not_called()
    assert standin.watchdog_mounts == [mount_point]
    assert "Drive current" in standin.status_calls


# --------------------------------------------------------------------------- #
# Req 4.5 — an interrupted family verifies+repairs first, on the real mount
# --------------------------------------------------------------------------- #


def test_pending_family_verifies_before_update():
    """An interrupted (pending) family runs verify_drive(repair=True) first,
    against the real mount_point, then update_drive for stale families."""
    standin = _make_standin()
    mount_point = "/Volumes/EFIS_3"

    currency = {
        "is_current": False,
        "families": {"charts": {"current": False}},
        "stale_items": ["scanned charts"],
    }
    jobs = {"charts": SimpleNamespace(files_updated=5)}
    update_result = {"jobs": jobs, "errors": [], "aborted": False}

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families",
        return_value=["charts"],
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch(
        "efis_data_manager.drive_updater.update_drive",
        return_value=update_result,
    ) as mock_update, mock.patch(
        "efis_data_manager.drive_updater.verify_drive",
        return_value={"clean": True, "families": {}},
    ) as mock_verify, mock.patch(
        "efis_data_manager.drive_updater._terminal_status_from_jobs",
        return_value="Drive current",
    ):
        EFISDataManagerApp._run_drive_update(standin, mount_point)

    # verify_drive ran first, with repair=True, against the real mount_point.
    mock_verify.assert_called_once()
    assert mock_verify.call_args[0][0] == mount_point
    assert mock_verify.call_args[1]["families"] == ["charts"]
    assert mock_verify.call_args[1]["repair"] is True
    # Then update_drive ran for the stale family against the same real mount.
    mock_update.assert_called_once()
    assert mock_update.call_args[0][0] == mount_point
    # Both operations watched the real FSKit mount_point.
    assert standin.watchdog_mounts == [mount_point, mount_point]


def test_pending_verify_not_clean_stops_before_update():
    """If verify+repair leaves discrepancies, no update_drive is attempted and
    the drive is not declared current."""
    standin = _make_standin()
    mount_point = "/Volumes/EFIS_3"

    currency = {
        "is_current": False,
        "families": {"charts": {"current": False}},
        "stale_items": ["scanned charts"],
    }

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families",
        return_value=["charts"],
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch(
        "efis_data_manager.drive_updater.update_drive"
    ) as mock_update, mock.patch(
        "efis_data_manager.drive_updater.verify_drive",
        return_value={"clean": False, "families": {"charts": ["x"]}},
    ):
        EFISDataManagerApp._run_drive_update(standin, mount_point)

    # verify was not clean -> update_drive never ran.
    mock_update.assert_not_called()
    assert "Drive verify incomplete" in standin.status_calls
    assert "Drive current" not in standin.status_calls


# --------------------------------------------------------------------------- #
# Req 12.2 — archive-on-insert is independent of _run_drive_update
# --------------------------------------------------------------------------- #


def test_run_drive_update_does_not_invoke_archive():
    """`_run_drive_update` never calls the archive path (Req 12.2).

    The archive/FDL-on-insert path (`_run_archive`) runs BEFORE
    `_run_drive_update` and is untouched. Driving the currency-current no-op
    path calls nothing from the archiver.
    """
    standin = _make_standin()
    standin.archive_calls = []
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

    assert standin.archive_calls == []
    mock_archive.assert_not_called()


# --------------------------------------------------------------------------- #
# Req 7.1 / Property 6 — flush = done: os.sync + unmount at the end of a
# successful auto-sync, and NOT on an aborted/errored run.
# --------------------------------------------------------------------------- #


def test_successful_update_flushes_and_unmounts_at_end():
    """A clean stale-family sync flushes + unmounts the card as its last step.

    The flush/unmount is the top-level operation's "done" step: once the
    families, commit markers, card manifest, and relabel (inside update_drive)
    are written, _run_drive_update makes the card durable and releases the mount
    so a subsequent insert reads exactly what was written (Req 7.1 / Property 6).
    """
    standin = _make_standin()
    mount_point = "/Volumes/EFIS_3"

    currency = {
        "is_current": False,
        "families": {"charts": {"current": False}},
        "stale_items": ["scanned charts"],
    }
    jobs = {"charts": SimpleNamespace(files_updated=5)}
    update_result = {"jobs": jobs, "errors": [], "aborted": False}

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families", return_value=[]
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch(
        "efis_data_manager.drive_updater.update_drive",
        return_value=update_result,
    ), mock.patch(
        "efis_data_manager.drive_updater.verify_drive"
    ), mock.patch(
        "efis_data_manager.drive_updater._terminal_status_from_jobs",
        return_value="Drive current",
    ):
        EFISDataManagerApp._run_drive_update(standin, mount_point)

    # Flush+unmount ran exactly once, against the real mount, on the clean path.
    assert standin.flush_unmount_calls == [mount_point]


def test_aborted_update_does_not_flush_and_unmount():
    """An aborted/errored run leaves the card mounted (pending family, stale
    manifest) — the flush/unmount only runs on the clean "done" path."""
    standin = _make_standin()
    mount_point = "/Volumes/EFIS_3"

    currency = {
        "is_current": False,
        "families": {"charts": {"current": False}},
        "stale_items": ["scanned charts"],
    }
    jobs = {"charts": SimpleNamespace(files_updated=0)}
    # Aborted run (drive removed mid-update): the engine reports aborted=True.
    update_result = {"jobs": jobs, "errors": [], "aborted": True}

    with mock.patch(
        "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
    ), mock.patch(
        "efis_data_manager.drive_updater.pending_families", return_value=[]
    ), mock.patch(
        "efis_data_manager.drive_updater.check_drive_currency",
        return_value=currency,
    ), mock.patch(
        "efis_data_manager.drive_updater.update_drive",
        return_value=update_result,
    ), mock.patch(
        "efis_data_manager.drive_updater.verify_drive"
    ), mock.patch(
        "efis_data_manager.drive_updater._terminal_status_from_jobs",
        return_value="Drive removed during update",
    ):
        EFISDataManagerApp._run_drive_update(standin, mount_point)

    assert standin.flush_unmount_calls == []


# --------------------------------------------------------------------------- #
# Req 7.1 — the flush helper itself: os.sync THEN diskutil unmount
# --------------------------------------------------------------------------- #


def test_flush_and_unmount_runs_os_sync_then_diskutil_unmount():
    """`flush_and_unmount` flushes the buffer cache then unmounts (not ejects)."""
    from efis_data_manager import drive_updater

    mount_point = "/Volumes/EFIS_3"
    with mock.patch("efis_data_manager.drive_updater.os.sync") as mock_sync, \
            mock.patch(
                "efis_data_manager.drive_updater.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stderr=b""),
            ) as mock_run:
        ok = drive_updater.flush_and_unmount(mount_point)

    assert ok is True
    mock_sync.assert_called_once_with()
    # diskutil UNMOUNT (not eject) the mount point.
    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["diskutil", "unmount", mount_point]


def test_flush_and_unmount_best_effort_on_unmount_failure():
    """A failed unmount is logged but never raises; os.sync still ran so the
    bytes are durable (the unmount only releases the mount)."""
    from efis_data_manager import drive_updater

    with mock.patch("efis_data_manager.drive_updater.os.sync") as mock_sync, \
            mock.patch(
                "efis_data_manager.drive_updater.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stderr=b"busy"),
            ):
        ok = drive_updater.flush_and_unmount("/Volumes/EFIS_3")

    # os.sync still ran (bytes durable); the unmount failure is reported as
    # False but did NOT raise.
    mock_sync.assert_called_once_with()
    assert ok is False
