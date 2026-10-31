# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression: a concurrent 'Idle' write cannot clobber an active drive sync's
status line.

Bug: _run_drive_update set "Updating drive..." then a fast concurrent chart
check ended with _set_status("Idle"), stomping the status so the menu bar read
idle (filled ● glyph) while rsync was still running. Fix: a _drive_sync_running
guard in _set_status drops an "Idle" write while a sync is in progress; the
sync's own non-"Idle" terminal statuses still pass.

Spec: sync-status-overwritten-by-chart-check.

As elsewhere, the real _set_status marshals onto the AppKit main queue, so we
bind the real method to a stand-in and observe the status text via a stubbed
menu + _update_title, with NSOperationQueue patched to run the block inline.
"""

import sys
from types import SimpleNamespace
from unittest import mock

from efis_data_manager.app import EFISDataManagerApp


class _FakeQueue:
    def addOperationWithBlock_(self, block):
        block()  # run synchronously


class _FakeAppKit:
    class NSOperationQueue:
        @staticmethod
        def mainQueue():
            return _FakeQueue()


def _standin():
    s = SimpleNamespace()
    s._drive_sync_running = False
    s._status_text = None
    # menu is a dict-like keyed by the current "Status: X" item; _set_status
    # only needs __getitem__ returning an object with a .title setter.
    class _Item:
        title = "Status: Idle"
    s.menu = {"Status: Idle": _Item()}
    s._update_title = lambda text: setattr(s, "_status_text", text)
    s._set_status = lambda text: EFISDataManagerApp._set_status(s, text)
    return s


def _run_set_status(standin, text):
    with mock.patch.dict(sys.modules, {"AppKit": _FakeAppKit}):
        standin._set_status(text)


def test_idle_ignored_while_sync_running():
    s = _standin()
    s._drive_sync_running = True
    _run_set_status(s, "Updating drive...")
    assert s._status_text == "Updating drive..."
    # Concurrent chart check tries to go Idle — must be ignored.
    _run_set_status(s, "Idle")
    assert s._status_text == "Updating drive..."


def test_sync_terminal_status_passes_while_flag_set():
    """The sync's own terminal status is not 'Idle', so it is never blocked."""
    s = _standin()
    s._drive_sync_running = True
    _run_set_status(s, "Drive current")
    assert s._status_text == "Drive current"


def test_idle_allowed_when_no_sync_running():
    s = _standin()
    s._drive_sync_running = False
    _run_set_status(s, "Idle")
    assert s._status_text == "Idle"
