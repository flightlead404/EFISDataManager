# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure, UI-free notification history buffer for EFIS Data Manager.

This module deliberately imports nothing from ``rumps``/``AppKit`` so the
buffer logic is fully testable in a headless environment. ``app.py`` owns a
single :class:`NotificationHistory` instance and wires it into ``_notify`` and
the menu.

A :class:`NotificationHistory` is a bounded, insertion-ordered ring buffer
(oldest left, newest right) of :class:`NotificationEntry` items. Callers review
it most-recent-first via :meth:`NotificationHistory.get_recent`.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime

CAP = 50  # History_Cap

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class NotificationEntry:
    """A recorded notification.

    Attributes:
        title: The notice headline (rumps subtitle arg).
        message: The body text (rumps message arg).
        timestamp: Local-time capture, preformatted ``YYYY-MM-DD HH:MM:SS``.
    """

    title: str
    message: str
    timestamp: str


class NotificationHistory:
    """Bounded, most-recent-first notification history.

    Internal storage is a ``collections.deque(maxlen=cap)`` in insertion order
    (oldest at left, newest at right). Once the cap is exceeded, appending
    discards the oldest entry automatically.
    """

    def __init__(self, cap: int = CAP) -> None:
        self._cap = cap
        self._entries: "deque[NotificationEntry]" = deque(maxlen=cap)

    def add(self, title, message, when=None) -> None:
        """Append an entry for (title, message).

        ``when`` is an optional datetime (defaults to ``datetime.now()``, local
        time) used to build the ``YYYY-MM-DD HH:MM:SS`` timestamp. Non-string
        title/message are coerced with ``str(...)`` so a stray non-string
        argument cannot crash capture. The deque's ``maxlen`` discards the
        oldest entry once the cap is exceeded.
        """
        if when is None:
            when = datetime.now()
        timestamp = when.strftime(TIMESTAMP_FORMAT)
        self._entries.append(
            NotificationEntry(title=str(title), message=str(message), timestamp=timestamp)
        )

    def get_recent(self) -> "list[NotificationEntry]":
        """Return entries most-recent-first (newest at index 0)."""
        return list(reversed(self._entries))

    def to_serializable(self) -> "list[dict]":
        """Return a JSON-ready list of ``{title, message, timestamp}`` dicts.

        Most-recent-first order, at most ``cap`` items.
        """
        return [
            {"title": e.title, "message": e.message, "timestamp": e.timestamp}
            for e in self.get_recent()
        ]

    @classmethod
    def from_serializable(cls, data, cap: int = CAP) -> "NotificationHistory":
        """Build a history from a serialized list (most-recent-first).

        Tolerant: non-list input, or malformed items, yield a valid (possibly
        empty) history and NEVER raise. Receives newest-first data and
        re-appends in oldest-first order so the deque's ``maxlen`` keeps the
        newest ``cap`` entries when the input is over-length.
        """
        history = cls(cap=cap)
        if not isinstance(data, list):
            return history
        # data is newest-first; re-append oldest-first so maxlen keeps newest.
        for item in reversed(data):
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            message = item.get("message")
            timestamp = item.get("timestamp")
            if title is None or message is None or timestamp is None:
                continue
            history._entries.append(
                NotificationEntry(
                    title=str(title), message=str(message), timestamp=str(timestamp)
                )
            )
        return history


def format_entry_line(entry) -> str:
    """Return ``'YYYY-MM-DD HH:MM:SS — title — message'`` for review surfaces."""
    return f"{entry.timestamp} — {entry.title} — {entry.message}"
