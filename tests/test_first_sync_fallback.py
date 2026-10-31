# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for first-sync and corrupt-manifest fallback (task 3.8).

These tests cover the *fallback integration* between the on-card manifest
reader and the per-stream cycle gate — the path a fresh or repaired card takes:

  * First sync (no ``EFIS_MANIFEST.json`` on the card): ``load_card`` returns
    ``None`` and ``decide_streams`` diffs every enabled chart family against an
    empty card manifest (a full write), while nav evaluates normally (Req 6.1).

  * Corrupt / unparseable card manifest: ``load_card`` returns ``None`` (treated
    as absent) and the gate produces the same full-write diffs; the malformed
    case is logged at WARNING (Req 6.2).

The two source-level behaviours are exercised end-to-end here rather than in
isolation (``load_card`` is unit-tested in ``test_manifest_store.py`` and
``decide_streams`` in ``test_decide_streams.py``). The point of this file is the
seam: a real bad/absent file on disk feeding the gate.

Requirements: 6.1, 6.2
"""

import json
import logging
import os

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as m
from efis_data_manager.manifest import Manifest_Store


def _decisions_by_family(decisions):
    return {d.family: d for d in decisions}


def _gate_from_card_on_disk(mount_point):
    """Read the card manifest from disk and run the gate, as a real insert does."""
    card = Manifest_Store.load_card(mount_point)
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")
    return card, _decisions_by_family(du.decide_streams(mount_point, local, card))


@pytest.fixture(autouse=True)
def _no_label(monkeypatch):
    """A fresh/repaired card has no C<charts>P<plates> label to cross-check."""
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)


# --- first sync: no card manifest file at all (Req 6.1) ---------------------


def test_first_sync_absent_manifest_full_write(tmp_path):
    mount = str(tmp_path)
    # Nothing on the card: no EFIS_MANIFEST.json present.
    assert not os.path.exists(os.path.join(mount, m.CARD_MANIFEST_NAME))

    card, by_family = _gate_from_card_on_disk(mount)

    # Treated as absent -> both chart families get a full-write diff.
    assert card is None
    assert by_family["scanned"].action == "diff"
    assert by_family["scanned"].card_cycle is None
    assert by_family["plates"].action == "diff"
    assert by_family["plates"].card_cycle is None
    # Local cycles still flow through as the target of the full write.
    assert by_family["scanned"].local_cycle == "2611"
    assert by_family["plates"].local_cycle == "2612"
    # nav is evaluated normally, never gated by a cycle.
    assert by_family["nav"].action == "evaluate"


def test_first_sync_full_write_covers_every_enabled_family(tmp_path):
    """Every enabled chart family is written, and only nav is ungated."""
    _card, by_family = _gate_from_card_on_disk(str(tmp_path))

    chart_families = {"scanned", "plates"}
    assert chart_families.issubset(by_family.keys())
    assert all(by_family[f].action == "diff" for f in chart_families)
    assert by_family["nav"].action == "evaluate"


# --- corrupt / unparseable card manifest (Req 6.2) --------------------------


def test_corrupt_json_manifest_treated_as_absent_full_write(tmp_path):
    mount = str(tmp_path)
    # Truncated / non-JSON bytes at the manifest path.
    with open(os.path.join(mount, m.CARD_MANIFEST_NAME), "w") as f:
        f.write("{ this is not valid json")

    card, by_family = _gate_from_card_on_disk(mount)

    assert card is None  # unparseable -> absent
    assert by_family["scanned"].action == "diff"
    assert by_family["plates"].action == "diff"
    assert by_family["nav"].action == "evaluate"


def test_malformed_schema_manifest_treated_as_absent_and_warns(tmp_path, caplog):
    mount = str(tmp_path)
    # Valid JSON, but a family entry is missing its required "family" key, so
    # card_from_json raises and load_card logs the malformed condition.
    with open(os.path.join(mount, m.CARD_MANIFEST_NAME), "w") as f:
        json.dump({"families": {"scanned": {"entries": {}}}}, f)

    with caplog.at_level(logging.WARNING, logger=m.logger.name):
        card = Manifest_Store.load_card(mount)

    assert card is None
    assert any(
        "malformed" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    ), "corrupt card manifest should be logged at WARNING (Req 6.2)"

    # And the gate still forces a full write from that absent result.
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")
    by_family = _decisions_by_family(du.decide_streams(mount, local, card))
    assert by_family["scanned"].action == "diff"
    assert by_family["plates"].action == "diff"
    assert by_family["nav"].action == "evaluate"


def test_empty_file_manifest_treated_as_absent_full_write(tmp_path):
    mount = str(tmp_path)
    # A zero-byte manifest (e.g. a write interrupted before any bytes landed)
    # is unparseable JSON -> absent -> full write.
    open(os.path.join(mount, m.CARD_MANIFEST_NAME), "w").close()

    card, by_family = _gate_from_card_on_disk(mount)

    assert card is None
    assert by_family["scanned"].action == "diff"
    assert by_family["plates"].action == "diff"
    assert by_family["nav"].action == "evaluate"
