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
(currency check, ``update_drive``) are recorders/stubs. The write runs DIRECTLY
against the FSKit ``/Volumes`` mount (chart-manifest-sync Stage 3), so the gate
is observed via the ``families`` ``update_drive`` is asked to sync.
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
    standin.config = {}
    standin.status_calls = []
    standin.notify_calls = []
    standin._set_status = lambda m: standin.status_calls.append(m)
    standin._notify = lambda t, m: standin.notify_calls.append((t, m))
    # The watchdog wrapper just runs the operation with a never-aborted
    # predicate (no real MountWatchdog in a headless test).
    standin._with_active_watchdog = lambda mount, run: run(lambda: False)
    standin._usb_monitor = None
    # Bind the real methods under test.
    standin._device_chart_capable = lambda mp: EFISDataManagerApp._device_chart_capable(standin, mp)
    standin._find_drive_settings = lambda mp: standin._parsed
    standin._parsed = None
    return standin


def _run(standin, stale_families):
    """Drive _run_drive_update with a crafted currency result.

    Returns the list of family-sets handed to ``update_drive`` (empty if the
    no-op path was taken and no write happened). The write runs directly against
    the FSKit mount, so the gate decision is observed via what ``update_drive``
    is asked to sync.
    """
    stale_items = sorted(stale_families)
    currency = {
        "is_current": len(stale_families) == 0,
        "families": {
            name: {"current": name not in stale_families}
            for name in ("scanned", "plates", "nav")
        },
        "stale_items": stale_items,
    }
    update_families = []

    def _fake_update_drive(mount_point, families=None, progress_callback=None,
                           is_aborted=None, chart_capable=None):
        update_families.append(list(families))
        jobs = {f: SimpleNamespace(files_updated=1) for f in families}
        return {"jobs": jobs, "errors": [], "aborted": False}

    with mock.patch.object(app_module, "load_config", return_value=_config_two_devices()), \
         mock.patch("efis_data_manager.drive_updater.read_identity", return_value=None), \
         mock.patch("efis_data_manager.drive_updater.resolve_sync_state_key", return_value=None), \
         mock.patch("efis_data_manager.drive_updater.check_drive_currency", return_value=currency), \
         mock.patch("efis_data_manager.drive_updater.update_drive", side_effect=_fake_update_drive), \
         mock.patch("efis_data_manager.drive_updater.verify_drive"), \
         mock.patch("efis_data_manager.drive_updater._terminal_status_from_jobs",
                    return_value="Drive current"):
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_X")
    standin.update_families = update_families
    return update_families


def test_non_chart_capable_device_drops_chart_families():
    """Mini (Link 2, not chart-capable): scanned+plates dropped, nav still synced."""
    standin = _make_standin()
    standin._parsed = _parsed("2")  # resolves to the non-chart-capable Mini
    update_families = _run(standin, stale_families=["scanned", "plates", "nav"])

    # update_drive ran exactly once (nav still needs writing).
    assert len(update_families) == 1
    stale_arg = update_families[0]
    assert "nav" in stale_arg
    assert not (set(stale_arg) & CHART_FAMILIES), f"chart families leaked: {stale_arg}"


def test_non_chart_capable_device_noop_when_only_charts_stale():
    """Mini: if ONLY charts are stale (nav current), there is no write at all."""
    standin = _make_standin()
    standin._parsed = _parsed("2")
    update_families = _run(standin, stale_families=["scanned", "plates"])  # nav current

    # Everything stale was a chart family -> dropped -> nothing left -> no-op.
    assert update_families == []
    assert any("up to date" in t.lower() or "current" in t.lower()
               for t, _ in standin.notify_calls)


def test_chart_capable_device_keeps_chart_families():
    """HXr (Link 1, chart-capable): chart families are NOT dropped."""
    standin = _make_standin()
    standin._parsed = _parsed("1")  # resolves to the chart-capable HXr
    update_families = _run(standin, stale_families=["scanned", "plates", "nav"])

    assert len(update_families) == 1
    stale_arg = update_families[0]
    assert set(stale_arg) >= {"scanned", "plates", "nav"}


def test_unresolved_device_fail_safe_allows_charts():
    """No settings file / undetermined device -> fail-safe True -> charts allowed."""
    standin = _make_standin()
    standin._parsed = None  # no settings -> DEVICE_UNDETERMINED -> allow charts
    update_families = _run(standin, stale_families=["scanned", "plates", "nav"])

    assert len(update_families) == 1
    stale_arg = update_families[0]
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


# ---------------------------------------------------------------------------
# Property 8: Chart-capability gate (preserved)
#
# Feature: chart-manifest-sync, Property 8: Chart-capability gate
#
# For ANY device positively resolved as not chart-capable, no scanned/plates
# payload is written (nav only); any resolution failure/indeterminate outcome
# fails safe to chart-capable (charts allowed). This generates an arbitrary
# resolution outcome and arbitrary stale/pending family sets and asserts the
# gate decision observed via what ``update_drive`` is asked to sync.
# Validates: Requirements 9.1, 9.2
# ---------------------------------------------------------------------------

from hypothesis import given, settings, strategies as st  # noqa: E402


ALL_FAMILIES = ("scanned", "plates", "nav")

# The three resolution-outcome categories the gate distinguishes. Each is
# realized through the REAL resolution path (device_resolution.resolve_drive)
# by what settings/Link_ID the drive presents:
#   "not_capable"  -> Link 2 (chart_capable=False) resolves RESOLVED, not capable
#   "capable"      -> Link 1 (chart_capable=True)  resolves RESOLVED, capable
#   "unresolved"   -> no settings file -> undetermined -> fail-safe allows charts
RESOLUTION_OUTCOMES = ("not_capable", "capable", "unresolved")


def _parsed_for_outcome(outcome):
    """Map a resolution-outcome label to the drive's parsed settings."""
    if outcome == "not_capable":
        return _parsed("2")   # Link 2 -> Mini A/P, chart_capable=False
    if outcome == "capable":
        return _parsed("1")   # Link 1 -> HXr, chart_capable=True
    return None               # no settings -> DEVICE_UNDETERMINED -> fail-safe


def _run_with_pending(standin, stale_families, pending_families_set):
    """Drive _run_drive_update with crafted currency AND interrupted-sync state.

    Extends the module ``_run`` helper to also exercise the pending/interrupted
    path, so the gate is observed against BOTH the stale set and the pending
    set. Returns the list of family-sets handed to ``update_drive`` (the write
    the gate ultimately permits).
    """
    stale_items = sorted(stale_families)
    currency = {
        "is_current": len(stale_families) == 0,
        "families": {
            name: {"current": name not in stale_families}
            for name in ALL_FAMILIES
        },
        "stale_items": stale_items,
    }
    # Both write channels the gate feeds: update_drive (stale sync) AND
    # verify_drive (interrupted-family repair). A chart family reaches the card
    # through EITHER channel, so the gate must prune both; we capture both.
    write_channels = []

    def _fake_update_drive(mount_point, families=None, progress_callback=None,
                           is_aborted=None, chart_capable=None):
        write_channels.append(list(families))
        jobs = {f: SimpleNamespace(files_updated=1) for f in families}
        return {"jobs": jobs, "errors": [], "aborted": False}

    def _fake_verify_drive(mount_point, families=None, repair=False,
                           progress_callback=None, is_aborted=None):
        # Interrupted families repaired clean so control proceeds to the
        # stale-sync step (the family list the gate already pruned). repair
        # touches these families on the card, so it counts as a write channel.
        write_channels.append(list(families))
        return {"clean": True, "families": {}}

    with mock.patch.object(app_module, "load_config", return_value=_config_two_devices()), \
         mock.patch("efis_data_manager.drive_updater.read_identity", return_value=None), \
         mock.patch("efis_data_manager.drive_updater.resolve_sync_state_key", return_value="drive-x"), \
         mock.patch("efis_data_manager.drive_updater.pending_families",
                    return_value=sorted(pending_families_set)), \
         mock.patch("efis_data_manager.drive_updater.check_drive_currency", return_value=currency), \
         mock.patch("efis_data_manager.drive_updater.update_drive", side_effect=_fake_update_drive), \
         mock.patch("efis_data_manager.drive_updater.verify_drive", side_effect=_fake_verify_drive), \
         mock.patch("efis_data_manager.drive_updater._terminal_status_from_jobs",
                    return_value="Drive current"):
        EFISDataManagerApp._run_drive_update(standin, "/Volumes/EFIS_X")
    return write_channels


@settings(max_examples=150)
@given(
    outcome=st.sampled_from(RESOLUTION_OUTCOMES),
    stale=st.sets(st.sampled_from(ALL_FAMILIES)),
    pending=st.sets(st.sampled_from(ALL_FAMILIES)),
)
def test_prop_chart_capability_gate(outcome, stale, pending):
    """Property 8: the gate withholds charts iff positively not-chart-capable.

    Feature: chart-manifest-sync, Property 8: Chart-capability gate
    Validates: Requirements 9.1, 9.2
    """
    standin = _make_standin()
    standin._parsed = _parsed_for_outcome(outcome)

    write_channels = _run_with_pending(standin, stale, pending)
    # Every family that reached the card, across BOTH write channels (stale
    # sync via update_drive + interrupted-family repair via verify_drive).
    all_written = set().union(*write_channels) if write_channels else set()

    # Everything the currency/interrupted state asked to touch this run.
    requested = stale | pending

    if outcome == "not_capable":
        # Req 9.1: a device positively resolved as not chart-capable receives
        # NO scanned/plates payload through any channel, regardless of what was
        # stale/pending.
        assert not (all_written & CHART_FAMILIES), (
            f"chart payload leaked to non-chart-capable device: {all_written}"
        )
        # nav is never gated: it is written exactly when it was requested.
        assert ("nav" in all_written) == ("nav" in requested)
        # The gate strips only chart families; nothing else is touched.
        assert all_written == (requested - CHART_FAMILIES), (
            f"gate wrote the wrong set: written={all_written} "
            f"expected={requested - CHART_FAMILIES}"
        )
    else:
        # Req 9.2: an unresolved/indeterminate outcome fails safe to
        # chart-capable, and a positively chart-capable device is unaffected.
        # Both leave the gate a no-op: every requested family — charts
        # included — reaches the card.
        assert all_written == requested, (
            f"gate altered the write for a chart-capable/fail-safe device: "
            f"written={all_written} requested={requested}"
        )
