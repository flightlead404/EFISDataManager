# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the per-stream cycle gate (chart-manifest-sync task 3.1).

Covers ``drive_updater.decide_streams`` / ``StreamDecision`` / ``LocalCycles``:
  - charts + plates skip-cycle when local cycle == card cycle, diff otherwise
  - nav always "evaluate" (no cycle gate) regardless of either cycle
  - no card manifest -> every chart family diffs against an empty manifest
  - the C<charts>P<plates> label cross-check logs a WARNING on disagreement,
    and the manifest value (not the label) drives the decision

Requirements: 3.1, 3.2, 3.5, 3.6, 6.1
"""

import logging

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager.manifest import CardManifest


def _decisions_by_family(decisions):
    return {d.family: d for d in decisions}


# --- label parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("C2611P2612", ("2611", "2612")),
        ("C2611", ("2611", None)),
        ("P2612", (None, "2612")),
        ("EFIS261024", (None, None)),  # fallback stamp -> no cycles
        ("MY VOLUME", (None, None)),  # user rename -> no cycles
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_label_cycles(label, expected):
    assert du._parse_label_cycles(label) == expected


# --- both chart families gated independently --------------------------------


def test_charts_skip_plates_diff(monkeypatch):
    # charts cycle unchanged -> skip-cycle; plates rolled -> diff.
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2611P2612")
    card = CardManifest(charts_cycle="2611", plates_cycle="2610")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    by_family = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, card))

    assert by_family["scanned"].action == "skip-cycle"
    assert by_family["plates"].action == "diff"
    assert by_family["nav"].action == "evaluate"


def test_both_charts_and_plates_skip_when_unchanged(monkeypatch):
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2611P2612")
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    by_family = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, card))

    assert by_family["scanned"].action == "skip-cycle"
    assert by_family["plates"].action == "skip-cycle"
    # nav is never gated.
    assert by_family["nav"].action == "evaluate"


def test_both_diff_when_rolled(monkeypatch):
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2609P2610")
    card = CardManifest(charts_cycle="2609", plates_cycle="2610")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    by_family = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, card))

    assert by_family["scanned"].action == "diff"
    assert by_family["plates"].action == "diff"
    assert by_family["scanned"].card_cycle == "2609"
    assert by_family["scanned"].local_cycle == "2611"


# --- nav always evaluated ----------------------------------------------------


def test_nav_always_evaluate_regardless_of_cycles(monkeypatch):
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2611P2612")
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    nav = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, card))["nav"]

    assert nav.action == "evaluate"
    assert nav.local_cycle is None
    assert nav.card_cycle is None


# --- no card manifest -> full write of each chart family (Req 6.1) ----------


def test_no_card_manifest_forces_full_diff(monkeypatch):
    monkeypatch.setattr(du, "_volume_name", lambda mp: None)
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    by_family = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, None))

    assert by_family["scanned"].action == "diff"
    assert by_family["scanned"].card_cycle is None
    assert by_family["plates"].action == "diff"
    assert by_family["plates"].card_cycle is None
    # nav still evaluates normally.
    assert by_family["nav"].action == "evaluate"


def test_missing_local_cycle_is_not_a_skip(monkeypatch):
    # A missing local cycle must never read as "equal" and skip a family.
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2611P2612")
    card = CardManifest(charts_cycle=None, plates_cycle=None)
    local = du.LocalCycles(charts_cycle=None, plates_cycle=None)

    by_family = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, card))

    assert by_family["scanned"].action == "diff"
    assert by_family["plates"].action == "diff"


# --- label cross-check: manifest wins, disagreement warns -------------------


def test_label_disagreement_logs_warning_manifest_wins(monkeypatch, caplog):
    # Label says charts=2609 but the authoritative manifest says 2611.
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2609P2612")
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        by_family = _decisions_by_family(
            du.decide_streams("/Volumes/CARD", local, card)
        )

    # Manifest (2611 == 2611) wins -> skip, not swayed by the stale label.
    assert by_family["scanned"].action == "skip-cycle"
    assert any("disagrees" in r.message for r in caplog.records)


def test_label_agreement_no_warning(monkeypatch, caplog):
    monkeypatch.setattr(du, "_volume_name", lambda mp: "C2611P2612")
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    with caplog.at_level(logging.WARNING, logger=du.logger.name):
        du.decide_streams("/Volumes/CARD", local, card)

    assert not any("disagrees" in r.message for r in caplog.records)


def test_label_read_failure_is_non_fatal(monkeypatch):
    def _boom(mp):
        raise RuntimeError("diskutil hiccup")

    monkeypatch.setattr(du, "_volume_name", _boom)
    card = CardManifest(charts_cycle="2611", plates_cycle="2612")
    local = du.LocalCycles(charts_cycle="2611", plates_cycle="2612")

    # Must not raise; gate still decides from the manifest.
    by_family = _decisions_by_family(du.decide_streams("/Volumes/CARD", local, card))
    assert by_family["scanned"].action == "skip-cycle"
