# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit test for IFR-high (HI) extraction routing in currency._extract_multidisk.

Covers Task 2.2 of the chart-manifest-sync spec: a MultiDiskImg-shaped zip with
.HI., .LO., and .SEC. members must extract so that HI members land under
ChartData/HI/W###/, LO under ChartData/LO/W###/, and SEC under
ChartData/SEC/W###/, with nothing dumped loose at the ChartData/ root.

Requirements: 10.1, 10.2
"""

import os
import zipfile

from efis_data_manager import currency


def _build_multidisk_zip(zip_path, members):
    """Write a MultiDiskImg-shaped zip containing the given member paths."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in members:
            # Content keyed by member so we can assert bytes survive routing.
            zf.writestr(member, member.encode("utf-8"))


def test_hi_lo_sec_routing(tmp_path):
    """HI routes to ChartData/HI/W###/, LO/SEC stay under their own dirs."""
    zip_path = os.path.join(tmp_path, "MultiDiskImg.zip")
    chartdata_dir = os.path.join(tmp_path, "ChartData")
    os.makedirs(chartdata_dir, exist_ok=True)

    members = [
        "2607/W085/W085.00xN31.00.HI.0.png",
        "2607/W085/W085.00xN31.00.LO.0.png",
        "2607/W085/W085.00xN31.00.SEC.0.png",
    ]
    _build_multidisk_zip(zip_path, members)

    n_files = currency._extract_multidisk(zip_path, None, chartdata_dir)

    assert n_files == 3

    hi_path = os.path.join(chartdata_dir, "HI", "W085", "W085.00xN31.00.HI.0.png")
    lo_path = os.path.join(chartdata_dir, "LO", "W085", "W085.00xN31.00.LO.0.png")
    sec_path = os.path.join(chartdata_dir, "SEC", "W085", "W085.00xN31.00.SEC.0.png")

    # HI lands in its dedicated subdir parallel to LO/SEC (Req 10.1, 10.2).
    assert os.path.isfile(hi_path)
    assert os.path.isfile(lo_path)
    assert os.path.isfile(sec_path)

    # Content survived the routing intact.
    with open(hi_path, "rb") as f:
        assert f.read() == members[0].encode("utf-8")

    # Nothing dumped loose at the ChartData/ root — only the HI/LO/SEC dirs.
    root_entries = set(os.listdir(chartdata_dir))
    assert root_entries == {"HI", "LO", "SEC"}, root_entries

    # No file sits directly at the ChartData/ root.
    for entry in root_entries:
        assert os.path.isdir(os.path.join(chartdata_dir, entry))


def test_hi_multiple_tiles(tmp_path):
    """HI files across multiple W### tiles each route under ChartData/HI/."""
    zip_path = os.path.join(tmp_path, "MultiDiskImg.zip")
    chartdata_dir = os.path.join(tmp_path, "ChartData")
    os.makedirs(chartdata_dir, exist_ok=True)

    members = [
        "2607/W085/W085.00xN31.00.HI.0.png",
        "2607/W090/W090.00xN35.00.HI.1.png",
    ]
    _build_multidisk_zip(zip_path, members)

    n_files = currency._extract_multidisk(zip_path, None, chartdata_dir)

    assert n_files == 2
    assert os.path.isfile(
        os.path.join(chartdata_dir, "HI", "W085", "W085.00xN31.00.HI.0.png")
    )
    assert os.path.isfile(
        os.path.join(chartdata_dir, "HI", "W090", "W090.00xN35.00.HI.1.png")
    )
    # Only the HI dir exists at the root.
    assert set(os.listdir(chartdata_dir)) == {"HI"}
