# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for identity-gated mount auto-action and the adoption hint.

Task 19 / Req 10.4-10.5: auto-action (archive + sync via ``on_efis_mount``)
fires ONLY for managed drives (valid identity file). An unmanaged drive
triggers NO automatic action; if it looks like a previously-used GRT chart
drive it surfaces a one-shot adoption hint via the optional
``on_adoption_candidate`` callback.

These tests never import ``app.py`` (which pulls in ``rumps``). They drive the
``USBMonitor``'s classification and polling structure directly, pointing the
scan at a fake ``/Volumes`` via monkeypatching.
"""

import json
import os

from efis_data_manager import usb_monitor as um


def _write_identity(mount, kind=um.IDENTITY_KIND):
    with open(os.path.join(mount, um.IDENTITY_FILENAME), "w") as f:
        json.dump({"schema_version": 1, "id": "abc", "kind": kind}, f)


def _managed_vol(base, name="EFIS_1"):
    mp = base / name
    mp.mkdir()
    _write_identity(str(mp))
    return mp


def _candidate_vol(base, name="GRTCHARTS_STICK", marker="GRTCHARTS"):
    mp = base / name
    (mp / marker).mkdir(parents=True)
    return mp


def _blank_vol(base, name="UNTITLED"):
    mp = base / name
    mp.mkdir()
    return mp


def _point_at(monkeypatch, volumes_dir):
    """Redirect the monitor's /Volumes scan at a temp directory."""
    monkeypatch.setattr(um, "VOLUMES_DIR", str(volumes_dir))


# --- classify_volume (pure classification) ----------------------------------


def test_classify_managed(tmp_path):
    mp = _managed_vol(tmp_path)
    assert um.classify_volume(str(mp)) == "managed"


def test_classify_candidate_grtcharts(tmp_path):
    mp = _candidate_vol(tmp_path)
    assert um.classify_volume(str(mp)) == "candidate"


def test_classify_candidate_chartdata(tmp_path):
    mp = _candidate_vol(tmp_path, name="OLD", marker="ChartData")
    assert um.classify_volume(str(mp)) == "candidate"


def test_classify_blank_is_ignore(tmp_path):
    mp = _blank_vol(tmp_path)
    assert um.classify_volume(str(mp)) == "ignore"


def test_managed_drive_is_never_a_candidate(tmp_path):
    # A managed drive that ALSO has a GRTCHARTS/ folder classifies as managed,
    # never candidate.
    mp = _managed_vol(tmp_path, name="EFIS_2")
    (mp / "GRTCHARTS").mkdir()
    assert um.classify_volume(str(mp)) == "managed"
    assert um.is_adoption_candidate(str(mp)) is False


# --- _is_scannable_volume (system-volume exclusions) ------------------------


def test_system_volumes_excluded():
    assert um._is_scannable_volume("Macintosh HD") is False
    assert um._is_scannable_volume("com.apple.TimeMachine.localsnapshots") is False
    assert um._is_scannable_volume("Backups of Mabel") is False
    assert um._is_scannable_volume("EFIS_1") is True


def test_scan_skips_system_volume_even_with_markers(tmp_path, monkeypatch):
    # A "Macintosh HD" entry that happens to have a ChartData/ folder must be
    # ignored — never treated as an adoption candidate.
    (tmp_path / "Macintosh HD" / "ChartData").mkdir(parents=True)
    _point_at(monkeypatch, tmp_path)
    managed, candidates = um._scan_volumes()
    assert managed == set()
    assert candidates == set()


# --- _scan_volumes (single pass classification) -----------------------------


def test_scan_partitions_managed_and_candidates(tmp_path, monkeypatch):
    m = _managed_vol(tmp_path, name="EFIS_1")
    c = _candidate_vol(tmp_path, name="WINDRIVE")
    _blank_vol(tmp_path, name="BLANK")
    _point_at(monkeypatch, tmp_path)

    managed, candidates = um._scan_volumes()
    assert managed == {str(m)}
    assert candidates == {str(c)}


# --- USBMonitor: auto-action gating -----------------------------------------


def test_on_efis_mount_fires_only_for_managed(tmp_path, monkeypatch):
    """on_efis_mount fires for a managed drive; on_adoption_candidate for a
    candidate; neither for a blank volume."""
    _managed_vol(tmp_path, name="EFIS_1")
    _candidate_vol(tmp_path, name="WINDRIVE")
    _blank_vol(tmp_path, name="BLANK")
    _point_at(monkeypatch, tmp_path)

    mounted = []
    candidates = []
    mon = um.USBMonitor(
        on_efis_mount=mounted.append,
        on_efis_unmount=lambda mp: None,
        on_adoption_candidate=candidates.append,
    )
    # start() scans already-mounted volumes but we don't want the poll thread.
    monkeypatch.setattr(mon, "_poll_loop", lambda: None)
    mon.start()

    assert mounted == [str(tmp_path / "EFIS_1")]
    assert candidates == [str(tmp_path / "WINDRIVE")]
    # The blank volume triggered no action at all.
    assert str(tmp_path / "BLANK") not in mounted
    assert str(tmp_path / "BLANK") not in candidates
    mon.stop()


def test_candidate_hint_optional_backward_compatible(tmp_path, monkeypatch):
    """Omitting on_adoption_candidate keeps old two-arg behavior; a candidate
    simply produces no hint and no error."""
    _candidate_vol(tmp_path, name="WINDRIVE")
    _point_at(monkeypatch, tmp_path)

    mounted = []
    mon = um.USBMonitor(
        on_efis_mount=mounted.append,
        on_efis_unmount=lambda mp: None,
    )
    monkeypatch.setattr(mon, "_poll_loop", lambda: None)
    mon.start()
    assert mounted == []
    mon.stop()


# --- USBMonitor._poll_loop step behavior ------------------------------------


def _poll_once(mon):
    """Run exactly one poll iteration without the sleep/thread machinery."""
    current, candidates = um._scan_volumes()

    new_mounts = current - mon._known_efis_mounts
    for mp in new_mounts:
        mon.on_efis_mount(mp)
    ejected = mon._known_efis_mounts - current
    for mp in ejected:
        mon.on_efis_unmount(mp)
    new_candidates = candidates - mon._known_candidates
    for mp in new_candidates:
        mon._notify_candidate(mp)

    mon._known_efis_mounts = current
    mon._known_candidates = candidates


def test_candidate_hint_fires_once_then_stops(tmp_path, monkeypatch):
    """A candidate that stays plugged in fires the hint once, not every poll."""
    _point_at(monkeypatch, tmp_path)
    candidates = []
    mon = um.USBMonitor(
        on_efis_mount=lambda mp: None,
        on_efis_unmount=lambda mp: None,
        on_adoption_candidate=candidates.append,
    )

    c = _candidate_vol(tmp_path, name="WINDRIVE")
    _poll_once(mon)
    assert candidates == [str(c)]

    # Second and third polls with the drive still present: no repeat.
    _poll_once(mon)
    _poll_once(mon)
    assert candidates == [str(c)]


def test_candidate_hint_refires_after_reappear(tmp_path, monkeypatch, ):
    """A candidate that disappears and reappears fires the hint again."""
    _point_at(monkeypatch, tmp_path)
    candidates = []
    mon = um.USBMonitor(
        on_efis_mount=lambda mp: None,
        on_efis_unmount=lambda mp: None,
        on_adoption_candidate=candidates.append,
    )

    c = _candidate_vol(tmp_path, name="WINDRIVE")
    _poll_once(mon)
    assert len(candidates) == 1

    # Remove the volume (unplug) and poll: no new hint.
    for child in list(c.iterdir()):
        child.rmdir()
    c.rmdir()
    _poll_once(mon)
    assert len(candidates) == 1

    # Reappears -> hint fires again.
    _candidate_vol(tmp_path, name="WINDRIVE")
    _poll_once(mon)
    assert len(candidates) == 2


def test_managed_drive_never_reported_as_candidate(tmp_path, monkeypatch):
    """A managed drive fires on_efis_mount but never on_adoption_candidate."""
    _point_at(monkeypatch, tmp_path)
    mounted = []
    candidates = []
    mon = um.USBMonitor(
        on_efis_mount=mounted.append,
        on_efis_unmount=lambda mp: None,
        on_adoption_candidate=candidates.append,
    )

    m = _managed_vol(tmp_path, name="EFIS_1")
    _poll_once(mon)
    assert mounted == [str(m)]
    assert candidates == []
