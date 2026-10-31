# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the Prepare-time Spotlight marker + initial volume label.

Covers chart-manifest-sync task 6.4: ``prepare_drive`` writes an empty
``.metadata_never_index`` marker at the volume root (disabling Spotlight
indexing per design "Prepare: Spotlight disable + label") and sets an initial
``C<charts>P<plates>`` volume label via the relocated ``labels`` helpers. Both
steps are best-effort: a marker/label failure must NOT fail preparation.

``prepare_drive`` itself shells out to the destructive ``diskutil eraseDisk``,
so the marker/label behavior is factored into small helpers
(``_write_metadata_never_index`` / ``_apply_volume_label`` /
``_prepare_spotlight_marker_and_label``) exercised directly against a tmp_path
mount. A focused assertion — the marker file exists at the volume root — is
sufficient here (task 6.5 covers the broader prepare flow).
"""

import os

from efis_data_manager import drive_updater as du
from efis_data_manager.drive_updater import METADATA_NEVER_INDEX


def test_write_metadata_never_index_creates_empty_marker_at_root(tmp_path):
    """The marker is an empty file at the volume root; presence is what counts."""
    mount = tmp_path / "EFIS"
    mount.mkdir()

    assert du._write_metadata_never_index(str(mount)) is True

    marker = mount / METADATA_NEVER_INDEX
    assert marker.is_file()
    assert marker.read_bytes() == b""


def test_write_metadata_never_index_failure_is_swallowed(tmp_path):
    """A write failure returns False rather than raising (best-effort)."""
    missing = tmp_path / "does-not-exist"
    # Parent dir absent -> open() raises OSError, which the helper swallows.
    assert du._write_metadata_never_index(str(missing)) is False


def test_prepare_spotlight_marker_and_label_writes_marker(tmp_path, monkeypatch):
    """The combined Prepare step drops the marker and attempts a relabel.

    Label derivation is stubbed to a known value so the test does not depend on
    real cycle metadata; the assertion that matters for task 6.4 is that the
    marker lands at the volume root.
    """
    mount = tmp_path / "EFIS"
    mount.mkdir()

    # Stub the relocated label helpers so the step is deterministic.
    import efis_data_manager.labels as labels
    monkeypatch.setattr(labels, "_cycles_from_metadata", lambda: ("2611", "2612"))

    applied = {}

    def fake_apply(mount_point, label):
        applied["mount"] = mount_point
        applied["label"] = label
        return True

    monkeypatch.setattr(du, "_apply_volume_label", fake_apply)

    du._prepare_spotlight_marker_and_label(str(mount), lambda _msg: None)

    assert (mount / METADATA_NEVER_INDEX).is_file()
    # Initial label derives from current cycles via build_volume_label.
    assert applied["mount"] == str(mount)
    assert applied["label"] == "C2611P2612"


def test_prepare_spotlight_marker_and_label_survives_label_failure(tmp_path, monkeypatch):
    """A label-derivation failure must not stop the marker from being written."""
    mount = tmp_path / "EFIS"
    mount.mkdir()

    import efis_data_manager.labels as labels

    def boom():
        raise RuntimeError("no metadata")

    monkeypatch.setattr(labels, "_cycles_from_metadata", boom)

    # Should not raise despite the label failure.
    du._prepare_spotlight_marker_and_label(str(mount), lambda _msg: None)

    assert (mount / METADATA_NEVER_INDEX).is_file()


def test_apply_volume_label_runs_diskutil_rename(tmp_path, monkeypatch):
    """The label is applied via ``diskutil rename <mount> <label>``."""
    captured = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(du.subprocess, "run", fake_run)

    assert du._apply_volume_label("/Volumes/EFIS", "C2611P2612") is True
    assert captured["cmd"] == ["diskutil", "rename", "/Volumes/EFIS", "C2611P2612"]


def test_apply_volume_label_failure_is_swallowed(monkeypatch):
    """A diskutil failure returns False rather than raising."""
    class _P:
        returncode = 1
        stdout = ""
        stderr = "rename failed"

    monkeypatch.setattr(du.subprocess, "run", lambda *a, **k: _P())
    assert du._apply_volume_label("/Volumes/EFIS", "C2611P2612") is False

    # Empty label short-circuits to False without shelling out.
    assert du._apply_volume_label("/Volumes/EFIS", "") is False


# --------------------------------------------------------------------------- #
# chart-manifest-sync task 6.5 — prepare_drive-level integration + flush.
#
# Task 6.4's tests above exercise the marker/label HELPERS directly. Task 6.5
# asserts the clearly-missing integration piece: driving the full
# ``prepare_drive`` end-to-end (format -> remount -> marker -> populate) lands a
# real ``.metadata_never_index`` at the resolved volume root. It reuses the
# ``_prepare_to_populate`` stubbing pattern from ``test_update_drive.py`` so the
# destructive eraseDisk/Popen/remount steps are faked and only the marker write
# is real.
#
# The companion flush assertion (a successful sync performs ``os.sync`` and
# unmounts at the end) is already fully covered in
# ``test_app_write_routing.py`` (``test_successful_update_flushes_and_unmounts_
# at_end`` and ``test_flush_and_unmount_runs_os_sync_then_diskutil_unmount``).
# Task 6.5 adds a single focused, complementary confirmation that the flush
# helper does ``os.sync`` then unmount against a temp-dir stand-in for the
# mount, without duplicating those existing assertions verbatim.
# --------------------------------------------------------------------------- #

import plistlib
import subprocess
from types import SimpleNamespace

import efis_data_manager.labels as labels
from efis_data_manager.drive_updater import JobResult


def _drive_prepare_to_populate(monkeypatch, tmp_path):
    """Stub prepare_drive's destructive format/remount steps so it reaches the
    marker write + populate against a REAL temp dir standing in for the mount.

    Unlike test_update_drive's helper (which resolves to the literal
    "/Volumes/EFIS"), this routes the resolved ``mount_point`` to ``tmp_path``
    so the real ``_write_metadata_never_index`` lands its marker somewhere the
    test can read it back. The resolved mount is ``tmp_path/"EFIS"``.

    Returns the mount dir used as the freshly-"formatted" drive.
    """
    mount = tmp_path / "EFIS"
    mount.mkdir()

    class _FakeProc:
        returncode = 0
        stdout = plistlib.dumps({"DeviceIdentifier": "disk9"})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: _FakeProc())

    class _FakePopen:
        def __init__(self, *a, **k):
            self.returncode = 0
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
        def communicate(self):
            return ("", "")
        def terminate(self):
            pass
        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    # prepare_drive computes expected = os.path.join("/Volumes", label) and polls
    # os.path.isdir(expected). Route that expected path to our temp mount and
    # mark it present so mount_point resolves to the temp dir — every subsequent
    # join(mount_point, ...) (marker, GRTCHARTS, identity) then hits the real
    # temp dir.
    real_join = du.os.path.join

    def fake_join(a, *parts):
        if a == "/Volumes" and parts == ("EFIS",):
            return str(mount)
        return real_join(a, *parts)

    monkeypatch.setattr(du.os.path, "join", fake_join)

    real_isdir = du.os.path.isdir
    monkeypatch.setattr(
        du.os.path, "isdir",
        lambda p: True if p == str(mount) else real_isdir(p),
    )

    return mount


def test_prepare_drive_writes_metadata_never_index_at_volume_root(monkeypatch, tmp_path):
    """Driving prepare_drive end-to-end lands a real .metadata_never_index at
    the resolved volume root (task 6.5, Req 6.4)."""
    mount = _drive_prepare_to_populate(monkeypatch, tmp_path)

    # Deterministic label derivation and a clean populate so prepare succeeds.
    monkeypatch.setattr(labels, "_cycles_from_metadata", lambda: ("2611", "2612"))
    monkeypatch.setattr(du, "_apply_volume_label", lambda mp, label: True)
    monkeypatch.setattr(du, "_ensure_identity", lambda mp: None)
    monkeypatch.setattr(du, "_safe_update_provenance", lambda mp, **k: None)
    monkeypatch.setattr(du, "_current_data_cycle", lambda: None)

    def fake_update_drive(mount_point, families=None, progress_callback=None):
        return {
            "jobs": {
                "scanned": JobResult(name="scanned", status="updated", verified=True),
                "plates": JobResult(name="plates", status="updated", verified=True),
                "nav": JobResult(name="nav", status="updated", verified=True),
            },
            "errors": [],
            "aborted": False,
        }

    monkeypatch.setattr(du, "update_drive", fake_update_drive)

    out = du.prepare_drive("/Volumes/UNTITLED", label="EFIS")

    assert out["success"] is True
    # The marker is a real empty file at the resolved volume root — written by
    # the live _write_metadata_never_index inside the prepare flow, not a stub.
    marker = mount / METADATA_NEVER_INDEX
    assert marker.is_file()
    assert marker.read_bytes() == b""


def test_successful_sync_flush_syncs_then_unmounts_tempdir(monkeypatch, tmp_path):
    """Complementary flush confirmation: on a successful sync the flush helper
    performs os.sync then unmounts the card (task 6.5, Req 7.1).

    Uses a temp dir standing in for the mount; os.sync and the diskutil unmount
    are observed via mocks so no real device is touched. This is the focused
    end-of-sync confirmation that os.sync precedes the unmount — the broader
    _run_drive_update wiring is covered in test_app_write_routing.py.
    """
    mount = tmp_path / "EFIS"
    mount.mkdir()

    order = []

    def fake_sync():
        order.append("sync")

    def fake_run(cmd, *a, **k):
        order.append(("unmount", list(cmd)))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(du.os, "sync", fake_sync)
    monkeypatch.setattr(du.subprocess, "run", fake_run)

    assert du.flush_and_unmount(str(mount)) is True

    # os.sync ran first, THEN diskutil unmount of the (temp) mount point.
    assert order == [
        "sync",
        ("unmount", ["diskutil", "unmount", str(mount)]),
    ]
