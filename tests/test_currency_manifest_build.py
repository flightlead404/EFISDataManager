# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the download-time manifest build wiring in currency.py.

Covers Task 1.7 of the chart-manifest-sync spec: after a successful
extract_chart the affected family's SHA-256 content manifest is built on the SSD
and persisted locally (alongside chart_cycles.json), with the cycle harvested
from the SA download-URL key or the valid_dates prose. A manifest build failure
is logged at WARNING and does NOT fail download/extract.

Requirements: 5.1, 6.3
"""

import os

import pytest

from efis_data_manager import currency
from efis_data_manager import manifest as m
from efis_data_manager.manifest import Manifest_Store


@pytest.fixture(autouse=True)
def _local_dir(tmp_path, monkeypatch):
    """Redirect the local manifest dir into tmp_path so the real one is safe."""
    local = tmp_path / "manifests"
    monkeypatch.setattr(m, "LOCAL_MANIFEST_DIR", local)
    return local


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _usb_image_with_charts(tmp_path):
    """Build a small ChartData tree: scanned (SEC/LO) + a Plates subtree."""
    usb = tmp_path / "EFIS_USBImage"
    _write(str(usb / "ChartData" / "SEC" / "W085" / "a.SEC.0.png"), b"sec-a")
    _write(str(usb / "ChartData" / "LO" / "W085" / "b.LO.0.png"), b"lo-b")
    _write(str(usb / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    _write(str(usb / "ChartData" / "Plates" / "US" / "p1.png"), b"plate-1")
    _write(str(usb / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")
    return str(usb)


# ---------------------------------------------------------------------------
# Family classification
# ---------------------------------------------------------------------------


def test_family_classification():
    assert currency._family_for_description("IFR Low Altitude") == "scanned"
    assert currency._family_for_description("Sectional Charts") == "scanned"
    assert currency._family_for_description("IFR High Altitude") == "scanned"
    assert currency._family_for_description("Approach Plates") == "plates"
    assert currency._family_for_description("Airport Diagrams") == "plates"


# ---------------------------------------------------------------------------
# Scanned manifest excludes the Plates subtree, plates manifest is Plates-only
# ---------------------------------------------------------------------------


def test_scanned_manifest_excludes_plates_subtree(tmp_path):
    usb = _usb_image_with_charts(tmp_path)
    entry = {
        "description": "IFR Low Altitude",
        "valid_dates": "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
        "download_url": "https://seattleavionics.com/ChartData/MultiDiskImg2611.zip",
    }

    currency.build_and_store_manifest(entry, usb)

    man = Manifest_Store.load_local("scanned")
    assert man is not None
    assert man.family == "scanned"
    assert man.cycle == "2611"  # harvested from the URL key
    rels = set(man.entries)
    # Scanned tree present; marker is excluded; Plates subtree excluded.
    assert "SEC/W085/a.SEC.0.png" in rels
    assert "LO/W085/b.LO.0.png" in rels
    assert "ScannedCharts.sqlite" not in rels  # commit marker excluded
    assert not any(r.startswith("Plates/") for r in rels)


def test_plates_manifest_is_plates_only(tmp_path):
    usb = _usb_image_with_charts(tmp_path)
    entry = {
        "description": "Approach Plates",
        "valid_dates": "Wednesday, October 1, 2026 through Tuesday, October 28, 2026",
        "download_url": "https://seattleavionics.com/ChartData/Plates2612.zip",
    }

    currency.build_and_store_manifest(entry, usb)

    man = Manifest_Store.load_local("plates")
    assert man is not None
    assert man.family == "plates"
    assert man.cycle == "2612"
    rels = set(man.entries)
    # Rooted at ChartData/Plates, so keys are relative to Plates/.
    assert "US/p1.png" in rels
    assert "Plates.sqlite" not in rels  # plates commit marker excluded


# ---------------------------------------------------------------------------
# Cycle harvest: URL key preferred, valid_dates fallback, None when neither
# ---------------------------------------------------------------------------


def test_harvest_cycle_prefers_url_over_valid_dates():
    # URL carries 2611; valid_dates prose would derive 2609 — URL wins.
    cycle = currency._harvest_cycle(
        "https://seattleavionics.com/ChartData/MultiDiskImg2611.zip",
        "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
    )
    assert cycle == "2611"


def test_harvest_cycle_falls_back_to_valid_dates():
    cycle = currency._harvest_cycle(
        "https://seattleavionics.com/ChartData/getfile.aspx",
        "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
    )
    assert cycle == "2609"


def test_harvest_cycle_none_when_neither():
    assert currency._harvest_cycle("https://seattleavionics.com/x.zip", "") is None


def test_url_cycle_ignores_year_tokens():
    # A bare year (2026) is not a plausible cycle ordinal (CC=26) -> ignored.
    assert currency._cycle_from_url("https://x/ChartData/2026/foo.zip") is None


# ---------------------------------------------------------------------------
# Failure is swallowed (does NOT fail download/extract)
# ---------------------------------------------------------------------------


def test_build_failure_does_not_raise(tmp_path, monkeypatch, caplog):
    usb = _usb_image_with_charts(tmp_path)
    entry = {
        "description": "IFR Low Altitude",
        "valid_dates": "",
        "download_url": "https://x/MultiDiskImg2611.zip",
    }

    def _boom(*a, **k):
        raise OSError("simulated hash failure")

    monkeypatch.setattr(m.Manifest_Builder, "build", staticmethod(_boom))

    import logging

    with caplog.at_level(logging.WARNING):
        # Must NOT raise.
        currency.build_and_store_manifest(entry, usb)

    assert any("Manifest build failed" in r.message for r in caplog.records)
    # Nothing persisted.
    assert Manifest_Store.load_local("scanned") is None
