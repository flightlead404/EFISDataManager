# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression: the sync-state key is the static identity id, independent of the
card manifest and the drive label/mount path.

Bug: run_sync_job keyed .sync_state.json via resolve_drive_id, which returned
None (-> mount-path fallback) whenever the card manifest did not load. Because
the drive label (hence mount path) changes every chart/plate cycle and the card
manifest does not exist until the first successful sync, the SAME physical drive
got interrupted-sync entries recorded under different keys at different times, so
a completed family could never be cleared and the drive wedged in a permanent
"resuming interrupted sync" loop.

Fix: resolve_sync_state_key returns the identity file's static ``id`` whenever a
valid identity exists, WITHOUT consulting the card manifest and WITHOUT waiting
for mount readiness (it only reads the identity file). These tests pin that.
"""

import json
import os

from efis_data_manager import drive_updater as du
from efis_data_manager.drive_updater import IDENTITY_FILENAME, IDENTITY_KIND


STATIC_ID = "234f0c5d-bab3-4ecf-86b7-87c9582d2cc4"


def _write_identity(root, **extra):
    data = {
        "schema_version": 2,
        "id": STATIC_ID,
        "kind": IDENTITY_KIND,
        "label": "EFIS_1",
        "volume_uuid": None,
    }
    data.update(extra)
    with open(os.path.join(root, IDENTITY_FILENAME), "w") as f:
        json.dump(data, f)


def test_key_is_identity_id_when_card_manifest_absent(tmp_path):
    """The key is the static id even with NO card manifest (the bug case)."""
    root = str(tmp_path)
    _write_identity(root)
    assert du.resolve_sync_state_key(root) == STATIC_ID


def test_key_is_stable_across_label_change(tmp_path):
    """Changing the label (cycle relabel) does not change the key."""
    root = str(tmp_path)
    _write_identity(root, label="C2609P2610")
    k1 = du.resolve_sync_state_key(root)
    _write_identity(root, label="C2610P2611")  # new cycle label
    k2 = du.resolve_sync_state_key(root)
    assert k1 == k2 == STATIC_ID


def test_no_identity_fails_safe_none(tmp_path):
    """No identity file -> None (fail-safe; no other drive's state applied)."""
    assert du.resolve_sync_state_key(str(tmp_path)) is None


def test_identity_without_id_fails_safe_none(tmp_path):
    """Identity present but missing an id -> None (fail-safe)."""
    root = str(tmp_path)
    data = {"schema_version": 2, "kind": IDENTITY_KIND, "label": "EFIS_1"}
    with open(os.path.join(root, IDENTITY_FILENAME), "w") as f:
        json.dump(data, f)
    assert du.resolve_sync_state_key(root) is None


def test_wrong_kind_fails_safe_none(tmp_path):
    """An identity file of a foreign kind -> None (not one of ours)."""
    root = str(tmp_path)
    _write_identity(root, kind="some-other-kind")
    assert du.resolve_sync_state_key(root) is None


def test_begin_then_complete_clears_entry_across_manifest_boundary(tmp_path, monkeypatch):
    """End to end: begin_family keyed by the static id, then complete_family
    keyed by the SAME static id, clears the entry — even though the card
    manifest state changed in between (the scenario that previously orphaned
    the entry)."""
    root = str(tmp_path)
    _write_identity(root)

    # Point the durable sync-state file at a temp location.
    state_path = tmp_path / ".sync_state.json"
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_path))

    key_before = du.resolve_sync_state_key(root)   # no card manifest yet
    du.begin_family(key_before, "scanned", mount=root)
    assert du.pending_families(key_before) == ["scanned"]

    # ... sync writes the card manifest here; key must be unchanged ...
    key_after = du.resolve_sync_state_key(root)
    assert key_after == key_before

    du.complete_family(key_after, "scanned")
    assert du.pending_families(key_after) == []
