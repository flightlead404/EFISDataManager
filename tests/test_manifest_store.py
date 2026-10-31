# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for Manifest_Store (local + card manifest storage).

Covers task 1.3:
  - save_local / load_local round-trip (one JSON per family on the SSD).
  - load_local returns None for a missing or corrupt local manifest.
  - save_card atomic write + load_card round-trip at the card volume root.
  - load_card returns None for a missing / corrupt / unparseable card manifest
    (first-sync / recovery fallback — Req 6.1, 6.2).
  - A card pull mid-write never leaves a half manifest: the destination either
    holds the previous good manifest or the new complete one (Req 5.5).

The mount root is ``tmp_path``; the local manifest dir is redirected into
``tmp_path`` so the real app-support dir is never touched.

Requirements: 5.3, 5.4, 5.5, 6.1, 6.2
"""

import json
import os

import pytest

from efis_data_manager import manifest as m
from efis_data_manager.manifest import (
    CardManifest,
    FamilyManifest,
    ManifestEntry,
    Manifest_Store,
)


@pytest.fixture(autouse=True)
def _local_dir(tmp_path, monkeypatch):
    """Redirect the local manifest dir into tmp_path for every test."""
    local = tmp_path / "manifests"
    monkeypatch.setattr(m, "LOCAL_MANIFEST_DIR", local)
    return local


def _sample_family(family="scanned", cycle="2611"):
    return FamilyManifest(
        family=family,
        cycle=cycle,
        entries={
            "SEC/W085/W085.00xN31.00.SEC.0.png": ManifestEntry(
                "SEC/W085/W085.00xN31.00.SEC.0.png", "a" * 64, 148213
            ),
            "LO/W085/W085.00xN31.00.LO.0.png": ManifestEntry(
                "LO/W085/W085.00xN31.00.LO.0.png", "c" * 64, 93011
            ),
        },
        built="2026-10-24T18:03:11-07:00",
        schema_version=1,
    )


def _sample_card():
    return CardManifest(
        charts_cycle="2611",
        plates_cycle="2612",
        written="2026-10-24T18:40:52-07:00",
        families={
            "scanned": _sample_family("scanned", "2611"),
            "plates": _sample_family("plates", "2612"),
        },
    )


# --- local manifest ---------------------------------------------------------


def test_save_local_then_load_local_round_trips():
    manifest = _sample_family()
    Manifest_Store.save_local(manifest)
    loaded = Manifest_Store.load_local("scanned")
    assert loaded == manifest


def test_save_local_keyed_by_family(_local_dir):
    Manifest_Store.save_local(_sample_family("scanned", "2611"))
    Manifest_Store.save_local(_sample_family("plates", "2612"))
    assert (_local_dir / "scanned.json").exists()
    assert (_local_dir / "plates.json").exists()
    assert Manifest_Store.load_local("scanned").cycle == "2611"
    assert Manifest_Store.load_local("plates").cycle == "2612"


def test_load_local_missing_returns_none():
    assert Manifest_Store.load_local("scanned") is None


def test_load_local_corrupt_returns_none(_local_dir):
    _local_dir.mkdir(parents=True, exist_ok=True)
    (_local_dir / "scanned.json").write_text("{not valid json")
    assert Manifest_Store.load_local("scanned") is None


def test_load_local_malformed_schema_returns_none(_local_dir):
    _local_dir.mkdir(parents=True, exist_ok=True)
    # Valid JSON but missing the required "family" key.
    (_local_dir / "scanned.json").write_text(json.dumps({"entries": {}}))
    assert Manifest_Store.load_local("scanned") is None


# --- card manifest ----------------------------------------------------------


def test_save_card_then_load_card_round_trips(tmp_path):
    mount = str(tmp_path)
    card = _sample_card()
    Manifest_Store.save_card(mount, card)
    loaded = Manifest_Store.load_card(mount)
    assert loaded == card


def test_save_card_writes_at_volume_root(tmp_path):
    mount = str(tmp_path)
    Manifest_Store.save_card(mount, _sample_card())
    assert os.path.exists(os.path.join(mount, m.CARD_MANIFEST_NAME))


def test_load_card_missing_returns_none(tmp_path):
    assert Manifest_Store.load_card(str(tmp_path)) is None


def test_load_card_corrupt_returns_none(tmp_path):
    path = os.path.join(str(tmp_path), m.CARD_MANIFEST_NAME)
    with open(path, "w") as f:
        f.write("}{ broken")
    assert Manifest_Store.load_card(str(tmp_path)) is None


def test_load_card_malformed_schema_returns_none(tmp_path):
    path = os.path.join(str(tmp_path), m.CARD_MANIFEST_NAME)
    # Valid JSON, but a family entry is missing its required "family" key.
    with open(path, "w") as f:
        json.dump({"families": {"scanned": {"entries": {}}}}, f)
    assert Manifest_Store.load_card(str(tmp_path)) is None


def test_save_card_no_temp_residue_on_success(tmp_path):
    Manifest_Store.save_card(str(tmp_path), _sample_card())
    leftovers = [n for n in os.listdir(str(tmp_path)) if n.startswith(".efis_manifest.")]
    assert leftovers == []


def test_save_card_atomic_leaves_prior_intact_on_failure(tmp_path, monkeypatch):
    """A failure mid-write leaves the previous good manifest intact (Req 5.5)."""
    mount = str(tmp_path)
    first = _sample_card()
    Manifest_Store.save_card(mount, first)

    # Force os.replace to fail so the "new" write never lands.
    def boom(*a, **k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(m.os, "replace", boom)

    second = CardManifest(charts_cycle="9999", plates_cycle="9999")
    with pytest.raises(OSError):
        Manifest_Store.save_card(mount, second)

    # The destination still holds the first, complete manifest.
    loaded = Manifest_Store.load_card(mount)
    assert loaded == first
    # And no temp file was orphaned.
    leftovers = [n for n in os.listdir(mount) if n.startswith(".efis_manifest.")]
    assert leftovers == []


def test_save_card_fsyncs_and_syncs(tmp_path, monkeypatch):
    """The card write flushes to the device (fsync on the file + os.sync)."""
    fsync_calls = []
    sync_calls = []
    monkeypatch.setattr(m.os, "fsync", lambda fd: fsync_calls.append(fd))
    monkeypatch.setattr(m.os, "sync", lambda: sync_calls.append(True))
    Manifest_Store.save_card(str(tmp_path), _sample_card())
    assert fsync_calls, "expected os.fsync on the temp file before replace"
    assert sync_calls, "expected a best-effort os.sync after replace"
