# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the event-driven composer refresh hook (image-based-drive-write
Task 8, Requirements 9.1-9.4).

Constructing the full rumps ``EFISDataManagerApp`` is impractical in a headless
test (it builds a live macOS menu-bar item), so — mirroring
``test_prop_prepare_adopt_preserves_autosync`` — these tests bind the REAL
unbound methods (``_run_chart_check_auto``, ``_do_chart_download``,
``_run_nav_db_check``, ``_refresh_composed_image``) to a minimal stand-in
``self``. The decision logic under test (trigger a compose on new data; keep the
previous image on compose failure) is therefore the genuine production code.

``image_composer.compose`` is ALWAYS mocked: a real compose runs a slow
newfs_msdos + hdiutil round-trip and needs a 64 MiB+ image, which must never run
in a unit test.
"""

import os
from types import SimpleNamespace
from unittest import mock

import pytest

from efis_data_manager import app as app_module
from efis_data_manager import image_composer
from efis_data_manager.app import EFISDataManagerApp


# --------------------------------------------------------------------------- #
# Stand-in app
# --------------------------------------------------------------------------- #


def _make_standin():
    """Minimal stand-in exposing only what the methods under test touch.

    ``_trigger_composer_refresh`` is recorded (not run) so the chart/nav tests
    can assert whether a refresh was triggered, independently of the compose
    mechanics (which are exercised directly against ``_refresh_composed_image``).
    """
    standin = SimpleNamespace()
    standin._charts_running = False
    standin._nav_running = False
    standin._compose_running = False
    standin.config = {
        "composed_image_path": "/tmp/efis-test/EFIS_USB.img",
        "composed_image_size_bytes": 64 * 1024 * 1024,
        "usb_image_path": "/tmp/efis-test/usb",
    }
    standin.status_calls = []
    standin.notify_calls = []
    standin.refresh_calls = []

    standin._set_status = lambda msg: standin.status_calls.append(msg)
    standin._notify = lambda title, msg: standin.notify_calls.append((title, msg))
    # Record refresh triggers instead of launching a thread.
    standin._trigger_composer_refresh = lambda reason: standin.refresh_calls.append(
        reason
    )
    return standin


# --------------------------------------------------------------------------- #
# Req 9.1 — chart check new data triggers a refresh; no new data does not
# --------------------------------------------------------------------------- #


def test_chart_check_new_data_triggers_refresh():
    standin = _make_standin()
    results = {"downloaded": 3, "errors": []}

    with mock.patch(
        "efis_data_manager.currency.update_charts", return_value=results
    ):
        EFISDataManagerApp._run_chart_check_auto(standin)

    assert standin.refresh_calls == ["new charts"]


def test_chart_check_no_new_data_does_not_trigger_refresh():
    standin = _make_standin()
    results = {"downloaded": 0, "errors": []}

    with mock.patch(
        "efis_data_manager.currency.update_charts", return_value=results
    ):
        EFISDataManagerApp._run_chart_check_auto(standin)

    assert standin.refresh_calls == []


def test_manual_chart_download_success_triggers_refresh():
    standin = _make_standin()
    results = {"downloaded": 2, "errors": []}

    with mock.patch(
        "efis_data_manager.currency.download_charts", return_value=results
    ):
        EFISDataManagerApp._do_chart_download(standin, [("x", "y")])

    assert standin.refresh_calls == ["new charts"]


def test_manual_chart_download_nothing_downloaded_does_not_trigger_refresh():
    standin = _make_standin()
    results = {"downloaded": 0, "errors": []}

    with mock.patch(
        "efis_data_manager.currency.download_charts", return_value=results
    ):
        EFISDataManagerApp._do_chart_download(standin, [("x", "y")])

    assert standin.refresh_calls == []


# --------------------------------------------------------------------------- #
# Req 9.2 — nav check updated triggers a refresh; current does not
# --------------------------------------------------------------------------- #


def test_nav_check_updated_triggers_refresh():
    standin = _make_standin()
    result = {"status": "updated", "message": "nav 10/1"}

    with mock.patch(
        "efis_data_manager.currency.check_and_download_nav_db", return_value=result
    ):
        EFISDataManagerApp._run_nav_db_check(standin)

    assert standin.refresh_calls == ["new nav DB"]


def test_nav_check_current_does_not_trigger_refresh():
    standin = _make_standin()
    result = {"status": "current", "message": "up to date"}

    with mock.patch(
        "efis_data_manager.currency.check_and_download_nav_db", return_value=result
    ):
        EFISDataManagerApp._run_nav_db_check(standin)

    assert standin.refresh_calls == []


# --------------------------------------------------------------------------- #
# Req 9.4 — compose success promotes; compose failure keeps the previous image
# --------------------------------------------------------------------------- #


def test_refresh_success_promotes_image_atomically():
    standin = _make_standin()
    image_path = standin.config["composed_image_path"]
    tmp_path = f"{image_path}.new"

    fake_result = SimpleNamespace(image_path=tmp_path)

    with mock.patch.object(
        image_composer, "compose", return_value=fake_result
    ) as mock_compose, mock.patch.object(app_module.os, "replace") as mock_replace:
        EFISDataManagerApp._refresh_composed_image(standin, "new charts")

    # Compose ran against the TEMP path (never directly the current image).
    assert mock_compose.call_count == 1
    compose_args, compose_kwargs = mock_compose.call_args
    assert compose_args[0] == tmp_path
    # Atomic promote: temp -> current image path.
    mock_replace.assert_called_once_with(tmp_path, image_path)
    assert standin._compose_running is False


def test_refresh_failure_retains_previous_image():
    """Compose raises -> the current-image path is NOT clobbered (Req 9.4)."""
    standin = _make_standin()
    image_path = standin.config["composed_image_path"]
    tmp_path = f"{image_path}.new"

    removed = []

    with mock.patch.object(
        image_composer, "compose", side_effect=RuntimeError("newfs blew up")
    ), mock.patch.object(app_module.os, "replace") as mock_replace, mock.patch.object(
        app_module.os.path, "exists", return_value=True
    ), mock.patch.object(
        app_module.os, "remove", side_effect=lambda p: removed.append(p)
    ):
        # Must not raise — the error is handled, no crash.
        EFISDataManagerApp._refresh_composed_image(standin, "new charts")

    # The current image path was NEVER promoted/overwritten.
    mock_replace.assert_not_called()
    # The partial temp image was discarded.
    assert removed == [tmp_path]
    # A failure notification was surfaced and the running flag cleared.
    assert any("Fail" in title for title, _ in standin.notify_calls)
    assert standin._compose_running is False


def test_refresh_skips_when_already_running():
    """The running-flag guard coalesces overlapping new-data events."""
    standin = _make_standin()
    standin._compose_running = True

    with mock.patch.object(image_composer, "compose") as mock_compose:
        EFISDataManagerApp._refresh_composed_image(standin, "new nav DB")

    mock_compose.assert_not_called()
    # Flag left as-is (owned by the in-flight compose).
    assert standin._compose_running is True


# --------------------------------------------------------------------------- #
# Req 9.3 — the refresh is NOT driven by a separate polling timer
# --------------------------------------------------------------------------- #


def test_refresh_is_a_plain_method_not_a_rumps_timer():
    """The refresh must be driven off the existing checks, never its own timer.

    rumps.timer wraps a function in a ``rumps.Timer`` / ``timers`` registration;
    a plain method is a bare function. Assert the two refresh entry points are
    plain functions (not Timer objects), and that the only ``@rumps.timer``
    callbacks on the class remain the three pre-existing ones.
    """
    import inspect
    import rumps

    # The refresh entry points are plain functions, not rumps.Timer instances.
    assert inspect.isfunction(EFISDataManagerApp._refresh_composed_image.__wrapped__
                              if hasattr(EFISDataManagerApp._refresh_composed_image, "__wrapped__")
                              else EFISDataManagerApp._refresh_composed_image)
    assert not isinstance(EFISDataManagerApp._refresh_composed_image, rumps.Timer)
    assert not isinstance(EFISDataManagerApp._trigger_composer_refresh, rumps.Timer)

    # rumps.timer records registered timers at the module level. Capture the
    # count, then confirm defining the refresh methods added none beyond the
    # pre-existing chart (43200), nav (86400), and startup (30) timers. We
    # assert structurally: scan the class source for @rumps.timer decorators.
    source = inspect.getsource(EFISDataManagerApp)
    timer_decorators = source.count("@rumps.timer(")
    assert timer_decorators == 3, (
        f"expected exactly 3 pre-existing @rumps.timer callbacks, found "
        f"{timer_decorators} — a new timer was added for the refresh"
    )
    # And none of them wrap the composer refresh.
    assert "rumps.timer" not in inspect.getsource(
        EFISDataManagerApp._refresh_composed_image
    )
    assert "rumps.timer" not in inspect.getsource(
        EFISDataManagerApp._trigger_composer_refresh
    )


def test_trigger_launches_daemon_thread_targeting_refresh():
    """`_trigger_composer_refresh` runs the compose off the main thread."""
    standin = _make_standin()
    # Use the REAL trigger (not the stand-in's recording lambda) for this test.
    # The trigger references self._refresh_composed_image as the thread target,
    # so give the stand-in a sentinel to confirm it is the target.
    refresh_sentinel = object()
    standin._refresh_composed_image = refresh_sentinel
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **_):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    with mock.patch.object(app_module.threading, "Thread", _FakeThread):
        EFISDataManagerApp._trigger_composer_refresh(standin, "new charts")

    assert captured["daemon"] is True
    assert captured["args"] == ("new charts",)
    assert captured["target"] is refresh_sentinel
    assert captured["started"] is True
