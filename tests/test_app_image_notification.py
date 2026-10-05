# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the persistent "new image ready" menu item + menu-bar badge
(image-based-drive-write Task 9, Requirements 10.1, 10.2, 10.3, 10.5).

Constructing the full rumps ``EFISDataManagerApp`` is impractical in a headless
test (it builds a live macOS menu-bar item), so — mirroring
``test_app_composer_refresh`` — these tests bind the REAL unbound methods
(``_mark_image_ready``, ``_clear_image_ready``, ``_on_prepare_image_drive``,
``_update_title``) to a minimal stand-in ``self`` with a FAKE dict-like menu
that captures each item's title + callback, plus a captured ``self.title`` for
the menu-bar badge. The state-transition logic under test is therefore the
genuine production code.

AppKit's ``NSOperationQueue`` is stubbed so the main-thread block executes
INLINE (synchronously), the same marshalling seam every menu/title mutation in
``app.py`` uses. ``currency`` metadata lookups (for the cycle labels) are
mocked so no on-disk state is read.
"""

import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from efis_data_manager import app as app_module
from efis_data_manager.app import (
    EFISDataManagerApp,
    IMAGE_STATE_CURRENT_TITLE,
    IMAGE_READY_BADGE_GLYPH,
)


# --------------------------------------------------------------------------- #
# Fake menu + inline NSOperationQueue
# --------------------------------------------------------------------------- #


class _FakeMenuItem:
    """Captures a menu item's displayed title and wired callback."""

    def __init__(self, title):
        self.title = title
        self.callback = None

    def set_callback(self, cb):
        self.callback = cb


class _FakeMenu(dict):
    """Dict-like stand-in for rumps' menu.

    rumps keys items by their ORIGINAL title; retitling an item does NOT re-key
    it. This fake reproduces that: ``__getitem__`` by the original key works,
    but once ``item.title`` changes, indexing by the NEW title raises
    ``KeyError`` — forcing the production code's scan-by-value lookup
    (``_image_state_item``) to be exercised for real.
    """

    def values(self):  # noqa: D401 - dict override
        return list(super().values())


@pytest.fixture(autouse=True)
def _inline_nsoperationqueue():
    """Make ``from AppKit import NSOperationQueue`` run blocks synchronously."""

    class _FakeQueue:
        def addOperationWithBlock_(self, block):
            block()  # run inline so assertions see the mutation immediately

    class _FakeNSOperationQueue:
        @staticmethod
        def mainQueue():
            return _FakeQueue()

    fake_appkit = SimpleNamespace(NSOperationQueue=_FakeNSOperationQueue)
    with mock.patch.dict(sys.modules, {"AppKit": fake_appkit}):
        yield


def _make_standin():
    """Minimal stand-in exposing only what the methods under test touch."""
    standin = SimpleNamespace()
    standin._image_ready = False
    standin._image_ready_label = None
    standin.title = ""  # the menu-bar badge glyph
    standin._drive_connected = False

    menu = _FakeMenu()
    menu["Status: Idle"] = _FakeMenuItem("Status: Idle")
    menu[IMAGE_STATE_CURRENT_TITLE] = _FakeMenuItem(IMAGE_STATE_CURRENT_TITLE)
    standin.menu = menu

    standin.status_calls = []
    standin.notify_calls = []
    standin._set_status = lambda msg: standin.status_calls.append(msg)
    standin._notify = lambda title, msg: standin.notify_calls.append((title, msg))

    # Bind the real helpers the methods under test call on self.
    standin._update_title = EFISDataManagerApp._update_title.__get__(standin)
    standin._image_state_item = EFISDataManagerApp._image_state_item.__get__(
        standin
    )
    standin._describe_image_cycles = (
        EFISDataManagerApp._describe_image_cycles.__get__(standin)
    )
    standin._mark_image_ready = EFISDataManagerApp._mark_image_ready.__get__(
        standin
    )
    standin._clear_image_ready = EFISDataManagerApp._clear_image_ready.__get__(
        standin
    )
    standin._on_prepare_image_drive = (
        EFISDataManagerApp._on_prepare_image_drive.__get__(standin)
    )
    return standin


def _mock_cycles(nav_valid="8/6/2026", plates_valid="2610"):
    """Patch currency metadata so cycle labels are deterministic."""
    grt_meta = {"nav_db_valid_date": nav_valid} if nav_valid else {}
    cycle_meta = (
        {"http://x/plates.zip": {"description": "Approach Plates", "valid_dates": plates_valid}}
        if plates_valid
        else {}
    )
    return mock.patch.multiple(
        "efis_data_manager.currency",
        _load_grt_metadata=mock.Mock(return_value=grt_meta),
        load_cycle_metadata=mock.Mock(return_value=cycle_meta),
    )


def _image_item(standin):
    return standin._image_state_item()


# --------------------------------------------------------------------------- #
# Req 10.1 — the "current" state: current text, no image badge
# --------------------------------------------------------------------------- #


def test_current_state_shows_current_text_and_no_badge():
    standin = _make_standin()

    # In the default (no pending image) state, the persistent item reads as
    # current and the title carries no image badge.
    item = _image_item(standin)
    assert item.title == IMAGE_STATE_CURRENT_TITLE
    assert standin._image_ready is False

    # _update_title in the idle/no-drive state leaves no image badge.
    standin._update_title("Idle")
    assert standin.title == ""
    assert standin.title != IMAGE_READY_BADGE_GLYPH


# --------------------------------------------------------------------------- #
# Req 10.2, 10.3 — fresh image sets the menu text (naming nav + plates) + badge
# --------------------------------------------------------------------------- #


def test_mark_image_ready_sets_menu_text_and_badge():
    standin = _make_standin()

    with _mock_cycles(nav_valid="8/6/2026", plates_valid="2610"):
        standin._mark_image_ready("new charts")

    # State flag flipped.
    assert standin._image_ready is True

    # Menu item retitled to name BOTH the nav cycle and the chart (plates) cycle
    # (Req 10.2). Year trimmed from the nav date for a compact label.
    item = _image_item(standin)
    assert item.title == "New image ready (nav 8/6 + plates 2610)"

    # The item is now actionable: its callback begins the prepare flow (Req 10.4).
    assert item.callback == standin._on_prepare_image_drive

    # Menu-bar title badged with the distinct fresh-image glyph (Req 10.3).
    assert standin.title == IMAGE_READY_BADGE_GLYPH


def test_mark_image_ready_falls_back_to_reason_when_no_cycle_metadata():
    """With no cycle metadata, the label degrades to the trigger reason."""
    standin = _make_standin()

    with _mock_cycles(nav_valid=None, plates_valid=None):
        standin._mark_image_ready("new nav DB")

    item = _image_item(standin)
    assert item.title == "New image ready (new nav DB)"
    assert standin._image_ready is True
    assert standin.title == IMAGE_READY_BADGE_GLYPH


# --------------------------------------------------------------------------- #
# Req 10.5 — acting on the new image resets BOTH the menu item and the badge
# --------------------------------------------------------------------------- #


def test_acting_on_image_resets_menu_item_and_badge():
    standin = _make_standin()

    # Drive into the ready state first.
    with _mock_cycles():
        standin._mark_image_ready("new charts")
    assert standin._image_ready is True
    assert standin.title == IMAGE_READY_BADGE_GLYPH

    # User clicks the ready item -> the wired action runs (Task 10). With no
    # managed drive connected, no write is launched, but the ready-state UI is
    # still reset immediately (Req 10.5). Patch /Volumes discovery to empty so
    # the no-drive branch runs deterministically and no thread is spawned.
    with mock.patch.object(app_module.os, "listdir", return_value=[]):
        standin._on_prepare_image_drive(None)

    # Ready state cleared.
    assert standin._image_ready is False
    assert standin._image_ready_label is None

    # Menu item returned to the "current" text with no callback (Req 10.5).
    item = _image_item(standin)
    assert item.title == IMAGE_STATE_CURRENT_TITLE
    assert item.callback is None

    # Menu-bar badge cleared (back to the non-image idle state, Req 10.5).
    assert standin.title == ""
    assert standin.title != IMAGE_READY_BADGE_GLYPH

    # With no drive connected the action posts a "No Drive" hint and reset the
    # UI (Task 10 wires the real write when a drive IS present).
    assert any("No Drive" in t for t, _ in standin.notify_calls)


def test_prepare_with_connected_drive_launches_image_write(monkeypatch):
    """Clicking "ready" with a managed drive connected resets the UI AND kicks
    off the image write on a daemon thread (Task 10, Req 10.4/10.5)."""
    standin = _make_standin()
    # Record the background write target instead of running it.
    prepare_calls = []
    standin._run_prepare_image_write = lambda mp: prepare_calls.append(mp)

    with _mock_cycles():
        standin._mark_image_ready("new charts")

    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **_):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True
            # Run inline so the recorded call is observable in the test.
            self._run()

        def _run(self):
            captured["target"](*captured["args"])

    with mock.patch.object(
        app_module.os, "listdir", return_value=["EFIS_3"]
    ), mock.patch.object(
        app_module.os.path, "isdir", return_value=True
    ), mock.patch(
        "efis_data_manager.usb_monitor.is_managed_drive", return_value=True
    ), mock.patch.object(
        app_module.threading, "Thread", _FakeThread
    ):
        standin._on_prepare_image_drive(None)

    # UI reset immediately (Req 10.5).
    assert standin._image_ready is False
    item = _image_item(standin)
    assert item.title == IMAGE_STATE_CURRENT_TITLE
    # The write was launched on a daemon thread targeting the connected drive.
    assert captured["daemon"] is True
    assert captured["args"] == ("/Volumes/EFIS_3",)
    assert prepare_calls == ["/Volumes/EFIS_3"]


def test_clear_image_ready_directly_resets_state():
    """`_clear_image_ready` alone resets menu text + badge (Req 10.5)."""
    standin = _make_standin()
    with _mock_cycles():
        standin._mark_image_ready("new charts")

    standin._clear_image_ready()

    item = _image_item(standin)
    assert item.title == IMAGE_STATE_CURRENT_TITLE
    assert item.callback is None
    assert standin._image_ready is False
    assert standin.title == ""


# --------------------------------------------------------------------------- #
# Badge precedence — active work (↻) outranks the image badge (⬇)
# --------------------------------------------------------------------------- #


def test_active_work_glyph_outranks_image_badge():
    standin = _make_standin()
    standin._image_ready = True  # image pending...

    # ...but active work is in progress -> ↻ wins (precedence 1 over 2).
    standin._update_title("Composing USB image...")
    assert standin.title == "\u21BB"  # ↻
    assert standin.title != IMAGE_READY_BADGE_GLYPH


def test_image_badge_outranks_connected_idle_dot():
    standin = _make_standin()
    standin._image_ready = True
    standin._drive_connected = True  # connected-idle would be ●...

    # ...but a pending image outranks the steady dot (precedence 2 over 3).
    standin._update_title("Idle")
    assert standin.title == IMAGE_READY_BADGE_GLYPH
