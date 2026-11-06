# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example/integration tests for the settings-management DM wiring in ``app.py``
(settings-management task 6.4; Requirements 1.1, 1.10, 6.1, 7.2, 7.4).

Covers the pieces task 6.1/6.2/6.3 wired into the menu-bar app:

* ``_run_archive`` threads the resolved Link_ID into ``archive_efis_drive`` so a
  resolved insert archives Settings into the per-device ``Settings/link{N}/``
  location (R1.1); exercised end-to-end against a real archiver + temp dirs.
* ``_run_archive`` asks for confirmation BEFORE archiving when a resolved
  drive's Settings diverge from the latest archive; "Skip settings" archives
  with ``archive_settings=False`` (R7.2, R7.4, R7.6).
* An unresolved drive (terminal resolution outcome) takes NO archive action —
  ``_resolve_and_gate_drive`` returns ``None`` and no archive runs (R1.10).
* ``_run_populate_settings`` reports each ``populate_latest`` outcome
  (written / skipped-equal / skipped-none / failed) via ``_notify`` (R6.1).

Migration-failure-not-blocking-startup (R11.6) is already covered by
``test_app_startup_migration.py`` and is deliberately NOT duplicated here.

Constructing the full rumps ``EFISDataManagerApp`` is impractical headless (it
builds a live macOS menu-bar item), so — mirroring ``test_app_write_routing`` /
``test_app_startup_migration`` / ``test_learn_prompt_integration`` — these tests
bind the REAL unbound methods to a minimal ``SimpleNamespace`` stand-in. The
decision/wiring logic under test is genuine production code; only the side
effects (``_notify``, ``_set_status``, threads, the resolution oracle, and the
settings_writer predicates) are stubbed or mocked.
"""

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from efis_data_manager.app import EFISDataManagerApp
from efis_data_manager.device_resolution import (
    ResolutionOutcome,
    ResolutionStatus,
)
from efis_data_manager.settings_archive import device_settings_dir
from efis_data_manager.settings_writer import DivergenceResult, WriteResult


# --------------------------------------------------------------------------- #
# Stand-in app
# --------------------------------------------------------------------------- #


def _make_standin(archive_path="/tmp/efis-archive"):
    """Minimal stand-in exposing only what the settings-wiring methods touch."""
    standin = SimpleNamespace()
    standin.config = {"archive_path": archive_path}

    standin.notify_calls = []
    standin.status_calls = []
    standin.drive_status_calls = []

    standin._notify = lambda title, msg: standin.notify_calls.append((title, msg))
    standin._set_status = lambda msg: standin.status_calls.append(msg)
    standin._set_drive_status = lambda msg: standin.drive_status_calls.append(msg)
    return standin


def _notify_titles(standin):
    return [title for title, _msg in standin.notify_calls]


# --------------------------------------------------------------------------- #
# R1.1 — a resolved insert archives Settings into Settings/link{N}/
# --------------------------------------------------------------------------- #


def _grt_body(vne: int, update: int) -> str:
    """A well-formed-enough GRT settings body (archiver copies bytes verbatim)."""
    return (
        "CHECKTYPE=STD\n"
        f"22={vne}\n"
        f"UPDATE={update}\n"
        "CHECKSIZE=1234\n"
        "CHECKSUM=ABCD\n"
    )


def test_resolved_insert_archives_into_per_device_link_dir():
    """`_run_archive` with a resolved Link_ID routes Settings into link{N}/.

    Drives the REAL archiver against temp dirs so the assertion is on the actual
    on-disk per-device location, not just the call signature. The divergence
    check, alert refresh, and chart sync are stubbed so this isolates the
    archive step (R1.1).
    """
    archive_root = Path(tempfile.mkdtemp(prefix="efis_wire_archive_"))
    mount = Path(tempfile.mkdtemp(prefix="efis_wire_mount_"))
    try:
        (mount / "Settings.dat").write_text(_grt_body(vne=200, update=5))

        standin = _make_standin(archive_path=str(archive_root))
        # Isolate the archive step: no divergence check, no alert refresh, and
        # capture (not run) the follow-on chart-sync thread.
        standin._confirm_archive_settings = lambda mp, lid: True
        standin._refresh_alerts = lambda: None
        standin._run_drive_update = lambda mp: None

        def fake_load_config():
            return {"archive_path": str(archive_root)}

        with mock.patch(
            "efis_data_manager.archiver.load_config", fake_load_config
        ), mock.patch("efis_data_manager.app.threading.Thread") as mock_thread:
            # Make the follow-on drive-update thread a no-op so the test does
            # not spawn a real background thread.
            mock_thread.return_value = SimpleNamespace(start=lambda: None)
            EFISDataManagerApp._run_archive(standin, str(mount), link_id="2")

        link_dir = device_settings_dir(archive_root, "2")
        snaps = list(link_dir.glob("Settings-*"))
        assert len(snaps) == 1, f"expected one snapshot under {link_dir}"
        assert snaps[0].suffix == ".dat"

        # Nothing leaked into the unresolved bucket or another link dir.
        settings_root = archive_root / "Settings"
        for other in settings_root.iterdir():
            if not other.is_dir() or other == link_dir:
                continue
            assert list(other.glob("Settings-*")) == []
    finally:
        shutil.rmtree(archive_root, ignore_errors=True)
        shutil.rmtree(mount, ignore_errors=True)


def test_run_archive_passes_resolved_link_id_to_archiver():
    """`_run_archive` forwards the resolved Link_ID to archive_efis_drive (R1.1).

    A lighter-weight companion to the end-to-end test: with the archiver mocked,
    assert the Link_ID is threaded through the `link_id=` kwarg verbatim.
    """
    standin = _make_standin()
    standin._confirm_archive_settings = lambda mp, lid: True
    standin._refresh_alerts = lambda: None
    standin._run_drive_update = lambda mp: None

    archive_result = {
        "fdl_moved": 0, "demo_moved": 0, "snap_moved": 0,
        "logbook_copied": 0, "settings_copied": 1,
        "skipped": 0, "cleaned": [], "errors": [],
    }

    with mock.patch(
        "efis_data_manager.archiver.archive_efis_drive",
        return_value=archive_result,
    ) as mock_archive, mock.patch(
        "efis_data_manager.app.threading.Thread",
        return_value=SimpleNamespace(start=lambda: None),
    ):
        EFISDataManagerApp._run_archive(standin, "/Volumes/EFIS_3", link_id="1")

    mock_archive.assert_called_once()
    assert mock_archive.call_args[1]["link_id"] == "1"


# --------------------------------------------------------------------------- #
# R7 — divergence confirmation BEFORE archiving
# --------------------------------------------------------------------------- #

_ARCHIVE_OK = {
    "fdl_moved": 0, "demo_moved": 0, "snap_moved": 0,
    "logbook_copied": 0, "settings_copied": 0,
    "skipped": 0, "cleaned": [], "errors": [],
}


def _prompt_standin(alert_result=1):
    """Stand-in with the REAL prompt helpers bound and a recording alert."""
    standin = _make_standin()
    standin.config = {"archive_path": "/tmp/efis-archive"}
    standin._refresh_alerts = lambda: None
    standin._run_drive_update = lambda mp: None
    standin.alert_calls = []

    def _alert(title, message, **kw):
        standin.alert_calls.append((title, message, kw))
        return alert_result

    standin._run_alert_on_main_thread = _alert
    standin._device_display_label = (
        lambda lid: EFISDataManagerApp._device_display_label(standin, lid)
    )
    standin._confirm_archive_settings = (
        lambda mp, lid: EFISDataManagerApp._confirm_archive_settings(
            standin, mp, lid
        )
    )
    return standin


def _run_with(standin, diverges_result=None, diverges_exc=None, link_id="1"):
    """Run _run_archive with mocked diverges/archiver; return (archive, diverges)."""
    kwargs = (
        {"side_effect": diverges_exc}
        if diverges_exc is not None
        else {"return_value": diverges_result}
    )
    with mock.patch(
        "efis_data_manager.settings_writer.diverges", **kwargs
    ) as mock_div, mock.patch(
        "efis_data_manager.app.configured_device",
        return_value={"link_id": "1", "friendly_name": "PFD"},
    ), mock.patch(
        "efis_data_manager.app.device_label", return_value="PFD (Link 1)"
    ), mock.patch(
        "efis_data_manager.archiver.archive_efis_drive",
        return_value=dict(_ARCHIVE_OK),
    ) as mock_archive, mock.patch(
        "efis_data_manager.app.threading.Thread",
        return_value=SimpleNamespace(start=lambda: None),
    ):
        EFISDataManagerApp._run_archive(standin, "/Volumes/EFIS_3", link_id=link_id)
    return mock_archive, mock_div


def test_divergence_prompts_with_buttons_and_message_matched():
    import datetime

    standin = _prompt_standin(alert_result=1)
    res = DivergenceResult(
        diverged=True,
        latest_archive_date=datetime.date(2026, 10, 29),
        matched_archive_date=datetime.date(2026, 9, 1),
        link_id="1",
    )
    _run_with(standin, res)

    assert len(standin.alert_calls) == 1
    _title, msg, kw = standin.alert_calls[0]
    assert kw == {"ok": "Archive settings", "cancel": "Skip settings"}
    assert "PFD (Link 1)" in msg
    assert "2026-10-29" in msg
    assert "This card matches the version archived 2026-09-01." in msg
    assert "unchanged" in msg and "Flight data is still archived" in msg


def test_divergence_prompt_says_new_content_when_no_match():
    import datetime

    standin = _prompt_standin(alert_result=1)
    res = DivergenceResult(
        diverged=True, latest_archive_date=datetime.date(2026, 10, 29), link_id="1"
    )
    _run_with(standin, res)
    msg = standin.alert_calls[0][1]
    assert "This content has not been archived before." in msg
    assert "matches the version" not in msg


def test_skip_passes_archive_settings_false():
    import datetime

    standin = _prompt_standin(alert_result=0)
    res = DivergenceResult(
        diverged=True, latest_archive_date=datetime.date(2026, 10, 29), link_id="1"
    )
    mock_archive, _ = _run_with(standin, res)
    mock_archive.assert_called_once()
    assert mock_archive.call_args[1]["archive_settings"] is False
    assert mock_archive.call_args[1]["link_id"] == "1"


def test_archive_choice_passes_archive_settings_true():
    import datetime

    standin = _prompt_standin(alert_result=1)
    res = DivergenceResult(
        diverged=True, latest_archive_date=datetime.date(2026, 10, 29), link_id="1"
    )
    mock_archive, _ = _run_with(standin, res)
    assert mock_archive.call_args[1]["archive_settings"] is True


def test_no_prompt_when_not_diverged():
    standin = _prompt_standin()
    mock_archive, mock_div = _run_with(standin, DivergenceResult(diverged=False))
    mock_div.assert_called_once()
    assert standin.alert_calls == []
    assert mock_archive.call_args[1]["archive_settings"] is True


def test_no_check_or_prompt_without_link_id():
    standin = _prompt_standin()
    mock_archive, mock_div = _run_with(
        standin, DivergenceResult(diverged=True), link_id=None
    )
    mock_div.assert_not_called()
    assert standin.alert_calls == []
    assert mock_archive.call_args[1]["archive_settings"] is True


def test_diverges_failure_does_not_block_archive():
    standin = _prompt_standin()
    mock_archive, _ = _run_with(
        standin, diverges_exc=RuntimeError("archive volume offline")
    )
    mock_archive.assert_called_once()
    assert mock_archive.call_args[1]["archive_settings"] is True
    assert standin.alert_calls == []


def test_no_post_archive_settings_drift_notification():
    import datetime

    standin = _prompt_standin(alert_result=1)
    res = DivergenceResult(
        diverged=True, latest_archive_date=datetime.date(2026, 10, 29), link_id="1"
    )
    _run_with(standin, res)
    assert "Settings drift" not in _notify_titles(standin)
    assert not hasattr(EFISDataManagerApp, "_check_settings_divergence")


# --------------------------------------------------------------------------- #
# R1.10 — an unresolved drive takes no archive action
# --------------------------------------------------------------------------- #


def test_unresolved_drive_takes_no_archive_action():
    """A terminal (non-RESOLVED) outcome gates to None: no archive, drive left.

    `_resolve_and_gate_drive` returns None for a terminal outcome, which the
    mount hook uses to skip the archive entirely (R1.10). Here we drive the
    gate directly: a DEVICE_NOT_CONFIGURED outcome yields None and only the
    advisory notify/status side effects, never an archive.
    """
    standin = _make_standin()
    standin._find_drive_settings = lambda mount_point: None
    # Bind the REAL terminal-outcome handler so the advisory notify/status side
    # effects under test are genuine production code.
    standin._handle_terminal_outcome = (
        lambda outcome, mp: EFISDataManagerApp._handle_terminal_outcome(
            standin, outcome, mp
        )
    )

    outcome = ResolutionOutcome(
        status=ResolutionStatus.DEVICE_NOT_CONFIGURED,
        message="Link_ID matches no configured device.",
    )

    with mock.patch(
        "efis_data_manager.app.load_config", return_value={"archive_path": "/x"}
    ), mock.patch(
        "efis_data_manager.drive_updater.read_identity", return_value=None
    ), mock.patch(
        "efis_data_manager.app.device_resolution.resolve_drive",
        return_value=outcome,
    ), mock.patch(
        "efis_data_manager.archiver.archive_efis_drive"
    ) as mock_archive:
        link_id = EFISDataManagerApp._resolve_and_gate_drive(
            standin, "/Volumes/EFIS_3"
        )

    # No device resolved -> no archive gating value, and the archiver was never
    # invoked for this drive.
    assert link_id is None
    mock_archive.assert_not_called()
    # The drive is left mounted with an advisory notification (terminal path).
    assert "Device not configured" in _notify_titles(standin)


# --------------------------------------------------------------------------- #
# R6.1 — populate action reports each outcome via _notify
# --------------------------------------------------------------------------- #


def test_populate_reports_written_outcome():
    """A "written" populate posts the "Settings Populated" notification."""
    standin = _make_standin()
    result = WriteResult(status="written", written_path="/Volumes/EFIS_3/Settings.dat")

    with mock.patch(
        "efis_data_manager.settings_writer.populate_latest", return_value=result
    ):
        EFISDataManagerApp._run_populate_settings(
            standin, "/Volumes/EFIS_3", "1", "PFD (Link 1)"
        )

    assert "Settings Populated" in _notify_titles(standin)


def test_populate_reports_already_current_outcome():
    """A "skipped-equal" populate posts the "Settings Already Current" notice."""
    standin = _make_standin()
    result = WriteResult(status="skipped-equal")

    with mock.patch(
        "efis_data_manager.settings_writer.populate_latest", return_value=result
    ):
        EFISDataManagerApp._run_populate_settings(
            standin, "/Volumes/EFIS_3", "1", "PFD (Link 1)"
        )

    assert "Settings Already Current" in _notify_titles(standin)


def test_populate_reports_no_archive_outcome():
    """A "skipped-none" populate posts the "No Archived Settings" notice."""
    standin = _make_standin()
    result = WriteResult(status="skipped-none")

    with mock.patch(
        "efis_data_manager.settings_writer.populate_latest", return_value=result
    ):
        EFISDataManagerApp._run_populate_settings(
            standin, "/Volumes/EFIS_3", "1", "PFD (Link 1)"
        )

    assert "No Archived Settings" in _notify_titles(standin)


def test_populate_reports_failed_outcome():
    """A "failed" populate posts the "Populate Settings Failed" notice."""
    standin = _make_standin()
    result = WriteResult(status="failed", error="byte verify mismatch")

    with mock.patch(
        "efis_data_manager.settings_writer.populate_latest", return_value=result
    ):
        EFISDataManagerApp._run_populate_settings(
            standin, "/Volumes/EFIS_3", "1", "PFD (Link 1)"
        )

    assert "Populate Settings Failed" in _notify_titles(standin)


def test_populate_reports_each_outcome_distinctly():
    """Each populate status maps to a distinct, correct notification title.

    One table-style check that the four terminal `populate_latest` statuses
    each surface their own notification (R6.1), so no two outcomes collapse to
    the same user-facing message.
    """
    cases = {
        "written": "Settings Populated",
        "skipped-equal": "Settings Already Current",
        "skipped-none": "No Archived Settings",
        "failed": "Populate Settings Failed",
    }
    for status, expected_title in cases.items():
        standin = _make_standin()
        result = WriteResult(
            status=status,
            error="boom" if status == "failed" else None,
        )
        with mock.patch(
            "efis_data_manager.settings_writer.populate_latest",
            return_value=result,
        ):
            EFISDataManagerApp._run_populate_settings(
                standin, "/Volumes/EFIS_3", "1", "PFD (Link 1)"
            )
        titles = _notify_titles(standin)
        assert expected_title in titles, (
            f"status {status!r} did not post {expected_title!r}; got {titles}"
        )
