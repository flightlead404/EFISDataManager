# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_and_download_nav_db result classification.

A transient incomplete page load / Sucuri challenge (no data table) must be
reported as a soft "blocked" (retry later), NOT a hard parse error — that was
the field false alarm ("nav db page could not be parsed"). A page that clearly
rendered a table but has no valid date is a genuine possible layout change and
stays an "error". A well-formed page yields "current"/"updated".
"""

import pytest

from efis_data_manager import currency as cur


@pytest.fixture(autouse=True)
def _no_browser_gate(monkeypatch, tmp_path):
    # Playwright browser check passes; metadata is isolated to a temp file.
    monkeypatch.setattr(cur, "_check_playwright_browser", lambda: None)
    monkeypatch.setattr(cur, "load_config",
                        lambda: {"usb_image_path": str(tmp_path / "image")})
    store = {"nav_db_valid_date": "9/3/2026"}
    monkeypatch.setattr(cur, "_load_grt_metadata", lambda: dict(store))
    monkeypatch.setattr(cur, "_save_grt_metadata", lambda m: store.update(m))


def _fetch(html):
    return lambda *a, **k: html


def test_no_table_is_soft_blocked(monkeypatch):
    # Incomplete/challenge page: no <table> at all.
    monkeypatch.setattr(cur, "_playwright_fetch_grt_page",
                        _fetch("<html><body>Just a moment...</body></html>"))
    result = cur.check_and_download_nav_db()
    assert result["status"] == "blocked"
    assert "parse" not in result["message"].lower()


def test_table_without_date_is_error(monkeypatch):
    # Page rendered a table but no date cell -> possible layout change.
    html = "<html><body><table><tr><td>Navigation Database</td></tr></table></body></html>"
    monkeypatch.setattr(cur, "_playwright_fetch_grt_page", _fetch(html))
    result = cur.check_and_download_nav_db()
    assert result["status"] == "error"


def test_good_page_reports_current(monkeypatch):
    html = (
        "<html><body><table>"
        "<tr><td>Nav DB desc</td><td>9/3/2026</td><td>8/31/2026</td></tr>"
        "</table></body></html>"
    )
    monkeypatch.setattr(cur, "_playwright_fetch_grt_page", _fetch(html))
    result = cur.check_and_download_nav_db()
    assert result["status"] == "current"
    assert "9/3/2026" in result["message"]
