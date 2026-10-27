# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the chart-capability sync gate in ``_run_drive_update``.

A device the user declared NOT chart-capable (e.g. a Mini A/P at Link 2) must
never receive chart/plate payloads — only ``nav`` is synced to it. A
chart-capable device (e.g. an HXr at Link 1) is unaffected. The gate is
fail-safe: if the device cannot be positively resolved as non-chart-capable,
charts are allowed (so a resolution hiccup never silently strips charts from a
chart-capable display).

These bind the REAL unbound ``_run_drive_update`` / ``_device_chart_capable`` to
a minimal ``SimpleNamespace`` stand-in (mirroring test_app_write_routing.py), so
the gating logic under test is genuine production code; only the collaborators
(currency check, _perform_write) are recorders/stubs.
"""

from types import SimpleNamespace
from unittest import mock

from efis_data_manager import app as app_module
from efis_data_manager.app import EFISDataManagerApp, CHART_FAMILIES
from efis_data_manager.efis_settings import ParsedBackup, SID_MODE_S, SID_LINK_ID


MODE_S = "A60670"


def _config_two_devices():
    """Managed aircraft with HXr (Link 1, chart-capable) + Mini (Link 2, not)."""
    return {
        "aircraft": {
            MODE_S: {
                "device_count": 2,
                "devices": {
                    "1": {"link_id": "1", "friendly_name": "HXr", "chart_capable": True},
                    "2": {"link_id": "2", "friendly_name": "Mini A/P", "chart_capable": False},
                },
                "analysis_data_source": "1",
            }
        }
    }


def _parsed(link_id):
    return ParsedBackup(
        path="/fake/Settings.bak",
        sids={SID_MODE_S: MODE_S, SID_LINK_ID: link_id},
        update_value=1,
        line_count=2,
        valid_pairs=2,
    )


def _make_standin():
    standin = SimpleNamespace()
    standin.config = {"image_write_enabled": False}
    standin.status_calls = []
    standin.notify_calls = []
    standin.perform_write_calls = []
    standin._set_status = lambda m: standin.status_calls.append(m)
    standin._notify = lambda t, m: standin.notify_calls.append((t, m))
    standin._perform_write = lambda *a, **k: standin.perform_write_calls.append((a, k))
    # Bind the real methods under test.
    standin._device_chart_capable = lambda mp: EFISDataManagerApp._device_chart_capable(standin, mp)
    standin._find_drive_settings = lambda mp: standin._parsed
    standin._parsed = None
    return standin


def _run(standin, stale_families):
    """Drive _run_drive_update with a crafted currency result (all stale)."""
    currency = {
        "is_current": len(stale_families) == 0,
        "families": {
            name: {"current": name not in stale_families}
            for name in ("scanned", "plates", "nav")
        },
    }
    with mock.patch.object(app_module, "load_config", return_value=_config_two_devices()), \
         mock.patch("efis_data_manager.drive_updater.read_identity", return_value=None), \
         mock.patch("efis_data_manager.drive_updater.resolve_drive_id", return_value=None), \
         mock.patch("efis_data_manager.drive_updater.check_drive_currency", return_value=currency):
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_X")
    return currency


def test_non_chart_capable_device_drops_chart_families():
    """Mini (Link 2, not chart-capable): scanned+plates dropped, nav still synced."""
    standin = _make_standin()
    standin._parsed = _parsed("2")  # resolves to the non-chart-capable Mini
    _run(standin, stale_families=["scanned", "plates", "nav"])

    # Write dispatcher ran exactly once (nav still needs writing).
    assert len(standin.perform_write_calls) == 1
    (args, _kwargs) = standin.perform_write_calls[0]
    # _perform_write signature: (mount_point, pending, stale, currency, on_progress)
    stale_arg = args[2]
    assert "nav" in stale_arg
    assert not (set(stale_arg) & CHART_FAMILIES), f"chart families leaked: {stale_arg}"


def test_non_chart_capable_device_noop_when_only_charts_stale():
    """Mini: if ONLY charts are stale (nav current), there is no write at all."""
    standin = _make_standin()
    standin._parsed = _parsed("2")
    _run(standin, stale_families=["scanned", "plates"])  # nav current

    # Everything stale was a chart family -> dropped -> nothing left -> no-op.
    assert standin.perform_write_calls == []
    assert any("up to date" in t.lower() or "current" in t.lower()
               for t, _ in standin.notify_calls)


def test_chart_capable_device_keeps_chart_families():
    """HXr (Link 1, chart-capable): chart families are NOT dropped."""
    standin = _make_standin()
    standin._parsed = _parsed("1")  # resolves to the chart-capable HXr
    _run(standin, stale_families=["scanned", "plates", "nav"])

    assert len(standin.perform_write_calls) == 1
    stale_arg = standin.perform_write_calls[0][0][2]
    assert set(stale_arg) >= {"scanned", "plates", "nav"}


def test_unresolved_device_fail_safe_allows_charts():
    """No settings file / undetermined device -> fail-safe True -> charts allowed."""
    standin = _make_standin()
    standin._parsed = None  # no settings -> DEVICE_UNDETERMINED -> allow charts
    _run(standin, stale_families=["scanned", "plates", "nav"])

    assert len(standin.perform_write_calls) == 1
    stale_arg = standin.perform_write_calls[0][0][2]
    assert "scanned" in stale_arg and "plates" in stale_arg


def test_device_chart_capable_helper_direct():
    """Direct check of the helper's resolution outcomes."""
    standin = _make_standin()
    with mock.patch.object(app_module, "load_config", return_value=_config_two_devices()), \
         mock.patch("efis_data_manager.drive_updater.read_identity", return_value=None):
        standin._parsed = _parsed("2")
        assert standin._device_chart_capable("/Volumes/EFIS_X") is False
        standin._parsed = _parsed("1")
        assert standin._device_chart_capable("/Volumes/EFIS_X") is True
        standin._parsed = None
        assert standin._device_chart_capable("/Volumes/EFIS_X") is True
