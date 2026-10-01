# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A reusable, non-modal, scrollable text window for the menu-bar app.

Replaces the previous ``rumps.alert``-with-a-long-body approach for list views
like "Recent Notifications" and "Recent Errors". That approach used a modal
``NSAlert`` which (a) blocked the whole app (menu items greyed out, Quit
unreachable), (b) had no scrollbar and was not resizable, and (c) grew taller
than the screen with enough content, pushing its only button off the bottom so
the window could not be dismissed at all.

This window is:
  - **titled + closable** — a real title bar with a close button;
  - **resizable** — the user can size it;
  - **scrollable** — long content scrolls inside an ``NSScrollView`` instead of
    growing the window off-screen;
  - **non-modal** — shown via ``makeKeyAndOrderFront_`` (NOT ``runModal``), so
    the menu bar and the rest of the app stay responsive.

Lifetime: Cocoa releases a window when it is closed by default, which would free
the ``NSWindow`` out from under the Python references and risk EXC_BAD_ACCESS on
a later touch. We set ``setReleasedWhenClosed_(False)`` and keep the open windows
in a module-level set so they live until the user closes them, mirroring the
pattern in ``settings_window.py``.
"""

import objc
from AppKit import (
    NSWindow, NSScrollView, NSTextView, NSApp,
    NSBackingStoreBuffered,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
    NSMakeRect, NSMakeSize, NSFont,
    NSViewWidthSizable, NSViewHeightSizable,
)
from Foundation import NSObject


# Keep references to open windows + their delegates so Cocoa does not deallocate
# them while visible (see module docstring).
_open_windows = set()


class _TextListWindowDelegate(NSObject):
    """Window delegate: drops the retained reference when the window closes."""

    def windowWillClose_(self, _notification):
        # Remove our strong reference so the window (and this delegate) can be
        # garbage-collected now that the user has dismissed it.
        _open_windows.discard(self)
        if getattr(self, "window", None) is not None:
            # Break the retain cycle; the window is closing anyway.
            self.window.setDelegate_(None)
            self.window = None


def show_text_list(title: str, body: str):
    """Show ``body`` in a non-modal, titled, resizable, scrollable window.

    Args:
        title: Window title (e.g. "Recent Notifications (7)").
        body: Full text to display; may be arbitrarily long (it scrolls).

    Returns:
        The delegate object (kept alive internally); callers may ignore it.
    """
    width, height = 560, 420

    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable
        | NSWindowStyleMaskMiniaturizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(320, 180, width, height),
        style,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_(title)
    window.setLevel_(3)  # float above the desktop like the other app windows
    # Do NOT let Cocoa free the window on close (see module docstring).
    window.setReleasedWhenClosed_(False)
    window.setMinSize_(NSMakeSize(360, 200))

    # Scroll view fills the content area and resizes with the window.
    content = window.contentView()
    bounds = content.bounds()
    scroll = NSScrollView.alloc().initWithFrame_(bounds)
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setAutohidesScrollers_(True)
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    scroll.setBorderType_(0)

    # Read-only text view inside the scroll view.
    text_view = NSTextView.alloc().initWithFrame_(bounds)
    text_view.setEditable_(False)
    text_view.setSelectable_(True)  # allow copy
    text_view.setRichText_(False)
    text_view.setFont_(NSFont.userFixedPitchFontOfSize_(12))
    text_view.setString_(body or "")
    text_view.setAutoresizingMask_(NSViewWidthSizable)
    text_view.textContainer().setWidthTracksTextView_(True)

    scroll.setDocumentView_(text_view)
    content.addSubview_(scroll)

    delegate = _TextListWindowDelegate.alloc().init()
    delegate.window = window
    window.setDelegate_(delegate)
    _open_windows.add(delegate)

    # Non-modal: show and focus WITHOUT taking over the event loop.
    window.makeKeyAndOrderFront_(None)
    NSApp.activateIgnoringOtherApps_(True)
    return delegate
