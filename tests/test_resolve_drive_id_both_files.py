# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the both-files recognition gate in resolve_drive_id.

Task 8.1 added a card-manifest AND check to ``drive_updater.resolve_drive_id``:
a drive is recognized (its identity ``id`` returned) ONLY when BOTH the
Identity_File (``EFIS_DRIVE_ID.json``, valid identity with ``kind ==
"efis-chart-drive"`` and an ``id``) AND a valid Card_Manifest
(``EFIS_MANIFEST.json``) are present at the volume root. If either is
absent/corrupt, ``resolve_drive_id`` returns ``None`` so the app routes the
drive to the explicit Prepare / Adopt flow.

These tests assert the gate directly (R3.1-R3.4):

  - identity + manifest both valid  -> returns the identity id   (R3.1)
  - valid identity, manifest ABSENT -> None                      (R3.3)
  - valid identity, manifest CORRUPT -> None                     (R3.3)
  - identity absent / corrupt (regardless of manifest) -> None   (R3.2)

No physical USB is required: the mount root is ``tmp_path`` and diskutil /
mount-readiness are monkeypatched so the test is fast and offline. Identity is
written via ``drive_updater.write_identity`` and the card manifest via
``manifest.Manifest_Store.save_card``.

Requirements: 3.1, 3.2, 3.3, 3.4
"""

import json
import os

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as mf


@pytest.fixture
def mount(tmp_path):
    """A ready, writable, listable mount root that passes wait_for_mount_ready."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _fast_mount_ready(monkeypatch):
    """resolve_drive_id calls wait_for_mount_ready first; short-circuit it.

    tmp_path is a real directory but not an OS mount point, so the real
    wait_for_mount_ready would spin the full budget and return False. Treat the
    temp mount as ready for these tests.
    """
    monkeypatch.setattr(du, "wait_for_mount_ready", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _no_diskutil(monkeypatch):
    """Default: diskutil reports nothing so _volume_uuid never shells out."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mp: None)
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


def _write_valid_identity(mount):
    """Write a valid identity (kind == efis-chart-drive, has id) and return it."""
    data = du._new_identity(mount)
    du.write_identity(mount, data)
    return data


def _write_valid_card_manifest(mount):
    """Save a valid (empty) Card_Manifest at the volume root."""
    mf.Manifest_Store.save_card(mount, mf.CardManifest())


def _manifest_path(mount):
    return os.path.join(mount, mf.CARD_MANIFEST_NAME)


# --- R3.1: both files present -> returns the identity id --------------------


def test_identity_and_manifest_present_returns_id(mount):
    data = _write_valid_identity(mount)
    _write_valid_card_manifest(mount)

    # Sanity: both provisioning files are physically present.
    assert os.path.exists(os.path.join(mount, du.IDENTITY_FILENAME))
    assert os.path.exists(_manifest_path(mount))

    assert du.resolve_drive_id(mount) == data["id"]


def test_identity_and_populated_manifest_present_returns_id(mount):
    """A non-empty card manifest (with a family + cycle) is still valid."""
    data = _write_valid_identity(mount)
    fam = mf.FamilyManifest(family="scanned", cycle="2611")
    card = mf.CardManifest(
        charts_cycle="2611", plates_cycle=None, families={"scanned": fam}
    )
    mf.Manifest_Store.save_card(mount, card)

    assert du.resolve_drive_id(mount) == data["id"]


# --- R3.3: valid identity but manifest absent/corrupt -> None ---------------


def test_identity_present_manifest_absent_returns_none(mount):
    _write_valid_identity(mount)
    # No card manifest written.
    assert not os.path.exists(_manifest_path(mount))

    assert du.resolve_drive_id(mount) is None


def test_identity_present_manifest_corrupt_returns_none(mount):
    _write_valid_identity(mount)
    # Unparseable JSON at the card-manifest path -> load_card returns None.
    with open(_manifest_path(mount), "w") as f:
        f.write("{ not valid json ]")

    assert du.resolve_drive_id(mount) is None


# --- R3.2: identity absent/corrupt (regardless of manifest) -> None ---------


def test_identity_absent_with_valid_manifest_returns_none(mount):
    # A valid manifest alone is not enough: identity is required too.
    _write_valid_card_manifest(mount)
    assert not os.path.exists(os.path.join(mount, du.IDENTITY_FILENAME))

    assert du.resolve_drive_id(mount) is None


def test_identity_corrupt_with_valid_manifest_returns_none(mount):
    _write_valid_card_manifest(mount)
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        f.write("garbage{")

    assert du.resolve_drive_id(mount) is None


def test_identity_wrong_kind_with_valid_manifest_returns_none(mount):
    # Right file, wrong kind -> not one of ours.
    _write_valid_card_manifest(mount)
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        json.dump(
            {"schema_version": 2, "id": "abc", "kind": "something-else"}, f
        )

    assert du.resolve_drive_id(mount) is None


def test_identity_missing_id_with_valid_manifest_returns_none(mount):
    # Correct kind but no id field -> cannot resolve an id.
    _write_valid_card_manifest(mount)
    with open(os.path.join(mount, du.IDENTITY_FILENAME), "w") as f:
        json.dump({"schema_version": 2, "kind": du.IDENTITY_KIND}, f)

    assert du.resolve_drive_id(mount) is None


def test_neither_file_present_returns_none(mount):
    # Empty mount: neither identity nor manifest.
    assert du.resolve_drive_id(mount) is None
