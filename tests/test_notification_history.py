# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the pure notification_history buffer.

Imports ONLY notification_history (no rumps/AppKit) so these run headless.
Property tests use Hypothesis (max_examples >= 100). A static text-scan test
of app.py asserts notification posting is centralized (Req 1.3).
"""

from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager.notification_history import (
    CAP,
    NotificationHistory,
    format_entry_line,
)


# --------------------------------------------------------------------------
# Property-based tests (one per design property)
# --------------------------------------------------------------------------


# Feature: notifications-history, Property 1: Add preserves title and message
# without truncation.
@given(title=st.text(), message=st.text())
@settings(max_examples=200)
def test_property1_add_preserves_title_and_message(title, message):
    h = NotificationHistory()
    h.add(title, message)
    recent = h.get_recent()
    assert len(recent) == 1
    assert recent[0].title == title
    assert recent[0].message == message


# Feature: notifications-history, Property 2: Bounded cap.
@given(n=st.integers(min_value=0, max_value=200))
@settings(max_examples=200)
def test_property2_bounded_cap(n):
    h = NotificationHistory()
    for i in range(n):
        h.add(f"title-{i}", f"message-{i}")
    assert len(h.get_recent()) == min(n, CAP)
    assert len(h.get_recent()) <= CAP


# Feature: notifications-history, Property 3: FIFO eviction retains the newest
# 50 in order.
@given(n=st.integers(min_value=CAP + 1, max_value=300))
@settings(max_examples=200)
def test_property3_fifo_eviction_keeps_newest(n):
    h = NotificationHistory()
    for i in range(n):
        h.add(f"title-{i}", f"message-{i}")
    recent = h.get_recent()
    assert len(recent) == CAP
    # Retained entries are the last CAP inserted, most-recent-first.
    expected_titles = [f"title-{i}" for i in range(n - 1, n - 1 - CAP, -1)]
    assert [e.title for e in recent] == expected_titles
    # The first n - CAP inserted are absent.
    retained_titles = {e.title for e in recent}
    for i in range(n - CAP):
        assert f"title-{i}" not in retained_titles


# Feature: notifications-history, Property 4: Most-recent-first ordering.
@given(
    items=st.lists(
        st.tuples(st.text(), st.text()), min_size=0, max_size=CAP
    )
)
@settings(max_examples=200)
def test_property4_most_recent_first_ordering(items):
    h = NotificationHistory()
    for title, message in items:
        h.add(title, message)
    recent = h.get_recent()
    # get_recent == reverse insertion order.
    expected = [(t, m) for (t, m) in reversed(items)]
    assert [(e.title, e.message) for e in recent] == expected


# Feature: notifications-history, Property 5: Serialize/deserialize round-trip
# preserves entries, order, and cap.
@given(
    items=st.lists(
        st.tuples(st.text(), st.text()), min_size=0, max_size=120
    )
)
@settings(max_examples=200)
def test_property5_serialize_roundtrip(items):
    h = NotificationHistory()
    for title, message in items:
        h.add(title, message)
    restored = NotificationHistory.from_serializable(h.to_serializable())
    original = h.get_recent()
    round_tripped = restored.get_recent()
    assert len(round_tripped) <= CAP
    assert [(e.title, e.message, e.timestamp) for e in round_tripped] == [
        (e.title, e.message, e.timestamp) for e in original
    ]


# Feature: notifications-history, Property 6: Robust load never raises.
_malformed_item = st.one_of(
    st.none(),
    st.integers(),
    st.text(),
    st.booleans(),
    st.lists(st.integers()),
    st.dictionaries(st.text(), st.text()),  # dicts likely missing required keys
)


@given(
    data=st.one_of(
        st.none(),
        st.text(),
        st.integers(),
        st.booleans(),
        st.dictionaries(st.text(), st.text()),
        st.lists(_malformed_item),
    )
)
@settings(max_examples=300)
def test_property6_robust_load_never_raises(data):
    # Must never raise, regardless of input shape.
    h = NotificationHistory.from_serializable(data)
    assert isinstance(h, NotificationHistory)
    # Non-list (or list of malformed items) yields a valid history.
    assert len(h.get_recent()) <= CAP


# --------------------------------------------------------------------------
# Example / unit tests (Task 1.8)
# --------------------------------------------------------------------------


def test_format_entry_line_contains_fields_separated():
    h = NotificationHistory()
    h.add("Charts Current", "All chart data sets are up to date.",
          when=datetime(2026, 9, 9, 14, 32, 7))
    entry = h.get_recent()[0]
    line = format_entry_line(entry)
    assert "2026-09-09 14:32:07" in line
    assert "Charts Current" in line
    assert "All chart data sets are up to date." in line
    # Separated by " — ".
    parts = line.split(" — ")
    assert parts == [
        "2026-09-09 14:32:07",
        "Charts Current",
        "All chart data sets are up to date.",
    ]


def test_add_with_known_datetime_yields_exact_timestamp():
    h = NotificationHistory()
    h.add("t", "m", when=datetime(2026, 1, 2, 3, 4, 5))
    assert h.get_recent()[0].timestamp == "2026-01-02 03:04:05"


def test_info_style_notice_retained_no_severity_filter():
    # An INFO-style notice (e.g. "Charts Current") must be retained; the buffer
    # applies no severity filter (unlike RecentErrorHandler).
    h = NotificationHistory()
    h.add("Charts Current", "All 4 chart data sets are up to date.")
    recent = h.get_recent()
    assert len(recent) == 1
    assert recent[0].title == "Charts Current"


def test_from_serializable_over_length_keeps_newest_cap():
    # 60 newest-first items -> keep newest 50, still newest-first.
    data = [
        {"title": f"t{i}", "message": f"m{i}", "timestamp": "2026-01-01 00:00:00"}
        for i in range(60)
    ]
    h = NotificationHistory.from_serializable(data)
    recent = h.get_recent()
    assert len(recent) == CAP
    # Input newest-first: index 0 is newest; the newest CAP are t0..t49.
    assert [e.title for e in recent] == [f"t{i}" for i in range(CAP)]


# --------------------------------------------------------------------------
# Static call-site verification (Task 4.2, Req 1.3)
# --------------------------------------------------------------------------


def test_notification_posting_centralized_in_app():
    # app.py imports rumps/AppKit at module top and can NOT be imported in the
    # headless test env, so we scan its source text instead. All posting must
    # route through _notify, leaving exactly ONE rumps.notification( call.
    import os

    app_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "efis_data_manager",
        "app.py",
    )
    with open(app_path, "r") as f:
        source = f.read()
    assert source.count("rumps.notification(") == 1
