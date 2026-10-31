# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for prepare recognition-immediately (task 10.2, R4.1/R4.3/R4.4).

Task 10.1 made ``prepare_drive`` write an empty ``CardManifest()`` before its
first populate ``update_drive``. These tests verify the two user-visible
consequences:

1. **Recognition-immediately (R4.1, R4.3).** After ``prepare_drive`` runs
   end-to-end (with the destructive format/remount stubbed exactly as
   ``tests/test_update_drive.py``'s ``_prepare_to_populate`` helper does, and
   ``update_drive`` stubbed to a clean no-op), BOTH ``EFIS_DRIVE_ID.json`` and
   ``EFIS_MANIFEST.json`` exist at the resolved mount root, and
   ``resolve_drive_id`` returns the written identity id — the both-files
   recognition gate (R3) is satisfied the moment prepare finishes, before any
   sync completes.

2. **Diff-against-empty (R4.4).** With the empty card manifest present and a
   local manifest for a chart family, ``build_jobs`` produces a diff job whose
   ``files_from`` is every local relpath and whose ``removed`` is empty — a
   blank card manifest means every local file is added and nothing is pruned.

Both tests use fresh temp dirs cleaned up per test; no real volume or diskutil
is touched. Placed in a NEW file to avoid collisions with
``test_prepare_identity.py`` / ``test_prepare_spotlight_marker.py``.

Requirements: 4.1, 4.3, 4.4
"""

import os
import plistlib
import subprocess

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf
from efis_data_manager.drive_updater import JobResult
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    Manifest_Store,
    ManifestEntry,
)


# ---------------------------------------------------------------------------
# Test 1: recognition-immediately — end-to-end prepare_drive
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_diskutil(monkeypatch):
    """No real diskutil in tests; identity captures no VolumeUUID/name, and the
    resolve_drive_id VolumeUUID cross-check never shells out."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


def _prepare_to_populate(monkeypatch, tmp_path):
    """Stub prepare_drive's destructive format/remount so it reaches the
    identity + initial-manifest write and its ``update_drive`` populate.

    Mirrors ``tests/test_update_drive.py::_prepare_to_populate``. Returns the
    temp mount dir that stands in for the freshly-"formatted" drive at
    ``/Volumes/EFIS``.
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

    # prepare_drive polls for "/Volumes/EFIS"; redirect isdir to our temp mount.
    real_isdir = du.os.path.isdir
    monkeypatch.setattr(
        du.os.path, "isdir",
        lambda p: True if p == "/Volumes/EFIS" else real_isdir(p),
    )

    # Redirect the mount-root writes (GRTCHARTS, identity, manifest) at
    # "/Volumes/EFIS" into our temp mount. makedirs is the only mount-root
    # directory op; identity/manifest writes go through os.path.join on the
    # mount_point string, so we also make the mount_point resolve to `mount`.
    real_makedirs = du.os.makedirs

    def fake_makedirs(path, *a, **k):
        if path == du.os.path.join("/Volumes/EFIS", "GRTCHARTS"):
            return real_makedirs(str(mount / "GRTCHARTS"), *a, **k)
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(du.os, "makedirs", fake_makedirs)

    return mount


def test_prepare_recognized_immediately(monkeypatch, tmp_path):
    """After prepare, both provisioning files exist and resolve_drive_id
    returns the written id (R4.1, R4.3).

    The destructive format/remount is stubbed and ``update_drive`` is a clean
    no-op, so the only writes under test are prepare's own identity + initial
    empty card-manifest writes. ``wait_for_mount_ready`` is short-circuited
    because the temp dir is not an OS mount point.
    """
    mount = _prepare_to_populate(monkeypatch, tmp_path)

    # The remount discovery lands on "/Volumes/EFIS"; make every mount-root
    # write operate on our temp `mount` by resolving that path to it. The
    # identity/manifest writers build paths via os.path.join(mount_point, ...),
    # and prepare_drive sets mount_point = "/Volumes/EFIS". Redirect the two
    # root writers to the temp dir.
    real_save_card = Manifest_Store.save_card
    monkeypatch.setattr(
        Manifest_Store, "save_card",
        staticmethod(lambda mp, card: real_save_card(str(mount), card)),
    )
    real_ensure_identity = du._ensure_identity
    captured = {}

    def fake_ensure_identity(mount_point):
        captured["id"] = real_ensure_identity(str(mount))
        return captured["id"]

    monkeypatch.setattr(du, "_ensure_identity", fake_ensure_identity)

    # provenance stamp + current-cycle read must not touch a real GRT dir.
    monkeypatch.setattr(du, "_safe_update_provenance", lambda *a, **k: None)
    monkeypatch.setattr(du, "_current_data_cycle", lambda: None)
    monkeypatch.setattr(du, "_prepare_spotlight_marker_and_label", lambda *a, **k: None)

    # Clean no-op populate: prepare still writes identity + manifest itself.
    monkeypatch.setattr(
        du, "update_drive",
        lambda *a, **k: {
            "jobs": {"scanned": JobResult(name="scanned", status="updated", verified=True)},
            "errors": [],
            "aborted": False,
        },
    )

    out = du.prepare_drive("/Volumes/UNTITLED")
    assert out["success"] is True

    # BOTH provisioning files present at the resolved mount root.
    identity_path = os.path.join(str(mount), du.IDENTITY_FILENAME)
    manifest_path = os.path.join(str(mount), mf.CARD_MANIFEST_NAME)
    assert os.path.exists(identity_path), "EFIS_DRIVE_ID.json missing after prepare"
    assert os.path.exists(manifest_path), "EFIS_MANIFEST.json missing after prepare"

    # The initial card manifest is the empty-shape one (null cycles, no families).
    card = Manifest_Store.load_card(str(mount))
    assert card is not None
    assert card.charts_cycle is None
    assert card.plates_cycle is None
    assert card.families == {}

    # resolve_drive_id returns the written id: the both-files gate is satisfied.
    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)
    assert captured["id"] is not None
    assert du.resolve_drive_id(str(mount)) == captured["id"]


# ---------------------------------------------------------------------------
# Test 2: diff-against-empty — build_jobs over a blank card manifest
# ---------------------------------------------------------------------------


def test_diff_against_empty_card_is_copy_all(monkeypatch, tmp_path):
    """A blank card manifest => every local file added, nothing pruned (R4.4).

    With an empty ``CardManifest()`` saved to the card and a local manifest
    carrying several entries, ``build_jobs`` must produce a chart job whose
    ``files_from`` is exactly the local relpaths (all added) and whose
    ``removed`` is ``[]``.
    """
    local_manifest_dir = tmp_path / "manifests"
    card_mount = tmp_path / "card"
    card_mount.mkdir()
    local_root = tmp_path / "image"

    family = "scanned"
    local_entries = {
        rel: ManifestEntry(relpath=rel, sha256="a" * 64, size=100)
        for rel in ["SEC/one.png", "SEC/two.png", "LO/three.png"]
    }
    local_fam = FamilyManifest(
        family=family,
        cycle="2611",  # non-null so decide_streams diffs (never skip-cycle)
        entries=local_entries,
        built="2026-10-24T18:03:11-07:00",
    )

    monkeypatch.setattr(mf, "LOCAL_MANIFEST_DIR", local_manifest_dir)
    # Empty card manifest: null cycles, no families => diff against {} per family.
    Manifest_Store.save_card(str(card_mount), CardManifest())
    Manifest_Store.save_local(local_fam)

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local_root)})
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)

    jobs = du.build_jobs(str(card_mount), [family])

    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == family
    assert job.kind == "tree"

    # Diff against an empty card family: every local relpath is added, in the
    # local manifest's iteration order; nothing removed.
    expected = mf.diff_family(local_fam, {})
    assert job.files_from == list(expected.files_from)
    assert job.files_from == list(local_entries)  # all local files added
    assert job.removed == []
