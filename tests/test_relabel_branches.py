# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the nav-aware post-sync relabel branches (chart-sync-wiring-fix
task 11.3).

Exercises ``drive_updater._relabel_from_card_manifest`` directly against a
``tmp_path`` mount, with ``_rename_volume`` patched to capture the label the
relabel step computes. Two branches, chosen by the ``chart_capable`` boolean in
the IDENTITY file (never the aircraft config):

  * chart-capable (True, or field absent -> fail-safe True): mirror the card
    manifest cycles into ``C<charts>P<plates>`` (R9.2), degrading to the single
    present component when only one family synced.
  * non-chart-capable (False): build ``<PREFIX><MMDD>`` from the identity's
    ``nav_label_prefix`` + the GRT nav valid date (``_current_data_cycle()``),
    without reading the aircraft config (R9.3, R9.7).

Requirements: 9.2, 9.3, 9.7.
"""

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest


@pytest.fixture(autouse=True)
def _stub_volume_uuid(monkeypatch):
    """Identity writes capture a VolumeUUID cross-check; a tmp_path dir has no
    diskutil-reported UUID, so stub it to None (matches the production
    best-effort read returning None for non-volume paths)."""
    monkeypatch.setattr(du, "_volume_uuid", lambda mount_point: None)


def _capture_rename(monkeypatch):
    """Patch ``_rename_volume`` to record every (mount, label) call and return
    True (as a successful diskutil rename would)."""
    calls = []
    monkeypatch.setattr(
        du,
        "_rename_volume",
        lambda mount_point, label: calls.append((mount_point, label)) or True,
    )
    return calls


def _write_identity(mount, *, chart_capable, nav_label_prefix=""):
    du.write_identity(
        str(mount),
        du._new_identity(
            str(mount),
            chart_capable=chart_capable,
            nav_label_prefix=nav_label_prefix,
        ),
    )


def _write_identity_without_chart_capable(mount):
    """Write an identity dict that lacks the ``chart_capable`` field, simulating
    a drive provisioned before the schema change -> readers fail-safe to True
    (R9.7)."""
    identity = du._new_identity(str(mount), chart_capable=True)
    identity.pop("chart_capable", None)
    du.write_identity(str(mount), identity)


def _save_card(mount, *, charts_cycle=None, plates_cycle=None):
    card = manifest.CardManifest(
        charts_cycle=charts_cycle,
        plates_cycle=plates_cycle,
    )
    manifest.Manifest_Store.save_card(str(mount), card)


# --------------------------------------------------------------------------- #
# Chart-capable branch -> C<charts>P<plates> (R9.2)
# --------------------------------------------------------------------------- #


def test_chart_capable_both_cycles_labels_c_charts_p_plates(monkeypatch, tmp_path):
    """Chart-capable identity + card manifest with both cycles -> relabel to
    C<charts>P<plates> (R9.2)."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity(mount, chart_capable=True)
    _save_card(mount, charts_cycle="2611", plates_cycle="2612")

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    assert calls == [(str(mount), "C2611P2612")]


def test_chart_capable_missing_field_fail_safe_true(monkeypatch, tmp_path):
    """An identity missing the chart_capable field fails safe to chart-capable,
    so the chart label is still produced (R9.7)."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity_without_chart_capable(mount)
    _save_card(mount, charts_cycle="2611", plates_cycle="2612")

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    assert calls == [(str(mount), "C2611P2612")]


def test_chart_capable_single_cycle_degrades(monkeypatch, tmp_path):
    """Only one chart family synced (only charts_cycle recorded) -> the label
    degrades to the single present component C<charts> (R9.2)."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity(mount, chart_capable=True)
    _save_card(mount, charts_cycle="2611", plates_cycle=None)

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    assert calls == [(str(mount), "C2611")]


def test_chart_capable_no_cycles_does_not_relabel(monkeypatch, tmp_path):
    """Chart-capable but the card manifest has no cycles -> skip the relabel;
    must NOT overwrite with a date fallback (the post-sync relabel only mirrors
    known cycles)."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity(mount, chart_capable=True)
    _save_card(mount, charts_cycle=None, plates_cycle=None)

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    assert calls == []


# --------------------------------------------------------------------------- #
# Non-chart-capable branch -> <PREFIX><MMDD> (R9.3, R9.7)
# --------------------------------------------------------------------------- #


def test_non_chart_capable_labels_prefix_mmdd(monkeypatch, tmp_path):
    """Non-chart-capable identity (chart_capable=False, nav_label_prefix set) +
    a GRT nav valid date -> relabel to <PREFIX><MMDD> (R9.3). The MMDD comes
    from _current_data_cycle(), which is monkeypatched to a fixed date."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity(mount, chart_capable=False, nav_label_prefix="MINIAP")
    # A non-chart-capable (nav-only) run leaves no chart cycles on the card.
    monkeypatch.setattr(du, "_current_data_cycle", lambda: "10/29/2026")

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    assert calls == [(str(mount), "MINIAP1029")]


def test_non_chart_capable_does_not_read_aircraft_config(monkeypatch, tmp_path):
    """R9.7: the non-chart-capable relabel branch builds the label from the
    identity's nav_label_prefix + _current_data_cycle ONLY, and never reads the
    aircraft config. We spy on both config-reading paths and assert neither is
    called within _relabel_from_card_manifest, while the label still builds."""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity(mount, chart_capable=False, nav_label_prefix="MINIAP")
    monkeypatch.setattr(du, "_current_data_cycle", lambda: "10/29/2026")

    config_reads = []

    def _spy_load_config(*args, **kwargs):
        config_reads.append("load_config")
        return {}

    monkeypatch.setattr(du, "load_config", _spy_load_config)

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    # The label still built purely from identity + nav date ...
    assert calls == [(str(mount), "MINIAP1029")]
    # ... and no aircraft-config read happened at relabel time (R9.7).
    assert config_reads == []


def test_non_chart_capable_empty_label_does_not_relabel(monkeypatch, tmp_path):
    """Non-chart-capable with no prefix and no parseable date would produce an
    empty label -> skip the relabel rather than renaming to nothing.

    (A blank prefix normally falls back to EFISNAV; here we force a genuinely
    empty label by stubbing the nav label builder, exercising the function's
    empty-label guard.)"""
    mount = tmp_path / "EFIS"
    mount.mkdir()
    _write_identity(mount, chart_capable=False, nav_label_prefix="")
    monkeypatch.setattr(du, "_current_data_cycle", lambda: None)

    from efis_data_manager import labels

    monkeypatch.setattr(labels, "build_nav_volume_label", lambda prefix, date: "")

    calls = _capture_rename(monkeypatch)

    du._relabel_from_card_manifest(str(mount))

    assert calls == []
