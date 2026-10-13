# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the top-level compose orchestration (task 1.3).

compose() creates a FAT32 image sized to the slice, populates it from the
CURRENT authoritative sources under usb_image_path (ChartData + NAV.DB /
NAV-proc.DB + a drive-independent identity), and writes the per-family commit
markers LAST. These tests cover:

  - A real round-trip (newfs_msdos + hdiutil) on a tiny image with a fixture
    chart/nav source tree: the finished image contains the charts, nav,
    identity, and markers, and is sidecar-free. Marked so it skips when the
    native tools are absent.
  - Drive-independence (design Property 5 / Req 1.6): compose reads only the
    local source and writes only the local image. The round-trip itself touches
    no /dev/disk USB node and no /Volumes USB mount, and the identity is passed
    in (never read off a drive) or minted.
  - A device-free structural test (populate / attach / finalize mocked) proving
    the markers are the LAST content written and that a missing chart source
    raises ComposeError.

Requirements: 1.2, 1.3, 1.4, 1.5, 4.1
"""

import os
import subprocess

import pytest

from efis_data_manager import drive_updater
from efis_data_manager import image_composer as ic
from efis_data_manager import write_verifier


# 64 MiB is the smallest size newfs_msdos formats cleanly as FAT32 on macOS
# (see test_image_composer_lifecycle); reuse it for the tiny round-trip.
_TINY_FAT32_BYTES = 64 * 1024 * 1024

_NATIVE_TOOLS_PRESENT = os.path.exists(ic.NEWFS_MSDOS_BIN) and os.path.exists(
    ic.HDIUTIL_BIN
)

requires_native_tools = pytest.mark.skipif(
    not _NATIVE_TOOLS_PRESENT,
    reason="native newfs_msdos / hdiutil not present",
)


def _attached_images_for(image_path: str) -> int:
    """Count hdiutil-attached entries whose backing image path matches."""
    try:
        out = subprocess.run(
            [ic.HDIUTIL_BIN, "info"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if image_path in line)


def _make_source_tree(root: str) -> str:
    """Build a tiny but representative usb_image_path source tree under ``root``.

    Lays down ChartData with a plate + a scanned tile, the two standalone nav
    files, and the two commit markers at their drive_updater-defined relative
    paths, so compose has every current source to pull from.
    """
    chartdata = os.path.join(root, "ChartData")
    os.makedirs(os.path.join(chartdata, "Plates"), exist_ok=True)
    os.makedirs(os.path.join(chartdata, "SEC"), exist_ok=True)

    with open(os.path.join(chartdata, "SEC", "tile.png"), "wb") as fh:
        fh.write(b"sectional-tile-bytes")
    with open(os.path.join(chartdata, "Plates", "plate.pdf"), "wb") as fh:
        fh.write(b"approach-plate-bytes")

    # Standalone nav files (current nav source the data manager downloads here).
    with open(os.path.join(root, "NAV.DB"), "wb") as fh:
        fh.write(b"nav-db-current")
    with open(os.path.join(root, "NAV-proc.DB"), "wb") as fh:
        fh.write(b"nav-proc-db-current")

    # The two commit markers at their authoritative relative paths.
    scanned = os.path.join(root, drive_updater.SCANNED_MARKER)
    plates = os.path.join(root, drive_updater.PLATES_MARKER)
    os.makedirs(os.path.dirname(scanned), exist_ok=True)
    os.makedirs(os.path.dirname(plates), exist_ok=True)
    with open(scanned, "wb") as fh:
        fh.write(b"scanned-marker")
    with open(plates, "wb") as fh:
        fh.write(b"plates-marker")

    return root


# ---------------------------------------------------------------------------
# Real round-trip — compose builds the whole image from current sources
# ---------------------------------------------------------------------------


@requires_native_tools
def test_compose_populates_charts_nav_identity_markers(tmp_path):
    """compose builds a complete image from current sources, markers included.

    The finished image contains the ChartData tree, both nav files, the
    identity file, and both commit markers; it is sidecar-free; and nothing is
    left attached. The identity is passed in (a known target drive's identity),
    exercising the drive's-own-identity path (Req 1.4) with no drive present.
    """
    source = _make_source_tree(str(tmp_path / "usb_image"))
    image_path = str(tmp_path / "EFIS_USB.img")
    mountpoint = str(tmp_path / "mnt")

    passed_identity = {
        "schema_version": drive_updater.IDENTITY_SCHEMA_VERSION,
        "id": "known-target-drive-id",
        "kind": drive_updater.IDENTITY_KIND,
    }

    try:
        result = ic.compose(
            image_path,
            _TINY_FAT32_BYTES,
            usb_image_path=source,
            identity=passed_identity,
            mountpoint=mountpoint,
        )

        # Hash the finished image NOW, before the re-attach below — mounting the
        # image writes FAT metadata back into the file and changes its bytes, so
        # the result hash must be checked against the as-composed image.
        composed_hash = write_verifier.hash_image(image_path)

        # Re-attach to inspect the finished image contents.
        with ic.attach_for_population(image_path, mountpoint) as root:
            # Charts landed.
            assert os.path.isfile(os.path.join(root, "ChartData", "SEC", "tile.png"))
            assert os.path.isfile(
                os.path.join(root, "ChartData", "Plates", "plate.pdf")
            )
            # Nav landed from the current nav source.
            assert os.path.isfile(os.path.join(root, "NAV.DB"))
            assert os.path.isfile(os.path.join(root, "NAV-proc.DB"))
            # Identity landed and is the PASSED-IN one, not read off any drive.
            on_image = drive_updater.read_identity(root)
            assert on_image is not None
            assert on_image.get("id") == "known-target-drive-id"
            # Both commit markers are present (written last).
            assert os.path.isfile(os.path.join(root, drive_updater.SCANNED_MARKER))
            assert os.path.isfile(os.path.join(root, drive_updater.PLATES_MARKER))
            # Sidecar-free.
            assert ic.find_sidecars(root) == []

        assert _attached_images_for(image_path) == 0
        assert not os.path.ismount(mountpoint)

        # ComposeResult shape.
        assert isinstance(result, ic.ComposeResult)
        assert result.byte_size == _TINY_FAT32_BYTES
        assert result.image_sha256 == composed_hash
        assert result.families_included == ["charts", "nav", "identity", "marker"]
        assert result.composed_at
    finally:
        if os.path.ismount(mountpoint):
            subprocess.run(
                [ic.HDIUTIL_BIN, "detach", mountpoint, "-force"],
                capture_output=True,
            )
        if os.path.exists(image_path):
            os.remove(image_path)


@requires_native_tools
def test_compose_mints_identity_when_none_and_is_drive_independent(tmp_path, monkeypatch):
    """compose mints a fresh identity when none is given, with NO drive touched.

    Drive-independence (design Property 5 / Req 1.6): any access to a /dev/disk
    USB node or a /Volumes USB mount is made to raise; compose must still
    complete. The local hdiutil image attach is NOT such an access (it is a
    local image mount), so it is left working. The identity is NOT read off a
    drive — read_identity is only ever called against the local image root.
    """
    source = _make_source_tree(str(tmp_path / "usb_image"))
    image_path = str(tmp_path / "EFIS_USB.img")
    mountpoint = str(tmp_path / "mnt")

    # Guard: fail loudly if compose ever reaches for a USB drive — the mount
    # swap or drive-id resolution are the operations that touch a /dev/disk USB
    # node or a /Volumes USB mount. (diskutil info on the LOCAL image mount, used
    # when minting an identity, is NOT drive access and is left working.)
    def _forbidden(*_a, **_k):
        raise AssertionError("compose touched a USB drive node/mount")

    monkeypatch.setattr(drive_updater, "resolve_drive_id", _forbidden)
    monkeypatch.setattr(drive_updater, "msdos_mount", _forbidden)

    # The compose mountpoint must be a LOCAL path, never a USB /Volumes mount.
    assert not mountpoint.startswith("/Volumes/")

    try:
        result = ic.compose(
            image_path,
            _TINY_FAT32_BYTES,
            usb_image_path=source,
            identity=None,  # mint a fresh identity for a drive being prepared
            mountpoint=mountpoint,
        )

        with ic.attach_for_population(image_path, mountpoint) as root:
            minted = drive_updater.read_identity(root)
            assert minted is not None
            # A freshly minted identity carries a uuid id and our kind.
            assert minted.get("kind") == drive_updater.IDENTITY_KIND
            assert minted.get("id")

        assert result.families_included == ["charts", "nav", "identity", "marker"]
        assert _attached_images_for(image_path) == 0
    finally:
        if os.path.ismount(mountpoint):
            subprocess.run(
                [ic.HDIUTIL_BIN, "detach", mountpoint, "-force"],
                capture_output=True,
            )
        if os.path.exists(image_path):
            os.remove(image_path)


@requires_native_tools
def test_compose_minted_identity_label_is_volume_label_not_temp_mount(
    tmp_path, monkeypatch
):
    """The minted identity's ``label`` is the C/P volume label, not the temp dir.

    Regression for the identity-label leak: compose with ``identity=None`` and
    its OWN temp mountpoint (mountpoint=None -> efis_compose_mnt_XXXX) must mint
    an identity whose ``label`` is the computed FAT volume label
    (``C<charts>P<plates>``), never the staging mountpoint basename
    (``efis_compose_mnt_...``) that previously leaked in.
    """
    source = _make_source_tree(str(tmp_path / "usb_image"))
    image_path = str(tmp_path / "EFIS_USB.img")
    reattach_mnt = str(tmp_path / "inspect_mnt")

    # Pin the cycle metadata so the expected label is deterministic.
    fake_meta = {
        "charts": {
            "description": "VFR Sectionals",
            "valid_dates": "Thursday, September 3, 2026 through Wednesday, October 28, 2026",
        },
        "plates": {
            "description": "IFR Approach Plates and Airport Diagrams",
            "valid_dates": "Thursday, October 1, 2026 through Wednesday, October 28, 2026",
        },
    }
    from efis_data_manager import currency

    monkeypatch.setattr(currency, "load_cycle_metadata", lambda: fake_meta)
    expected_label = "C2609P2610"

    try:
        # mountpoint=None so compose uses its own tempfile.mkdtemp staging dir —
        # exactly the path that previously leaked into the identity label.
        ic.compose(
            image_path,
            _TINY_FAT32_BYTES,
            usb_image_path=source,
            identity=None,
        )

        with ic.attach_for_population(image_path, reattach_mnt) as root:
            minted = drive_updater.read_identity(root)
            assert minted is not None
            assert minted.get("label") == expected_label
            assert not str(minted.get("label", "")).startswith("efis_compose_mnt_")
    finally:
        if os.path.ismount(reattach_mnt):
            subprocess.run(
                [ic.HDIUTIL_BIN, "detach", reattach_mnt, "-force"],
                capture_output=True,
            )
        if os.path.exists(image_path):
            os.remove(image_path)


# ---------------------------------------------------------------------------
# Device-free structural tests — ordering + error path (no newfs/hdiutil)
# ---------------------------------------------------------------------------


def test_compose_writes_markers_last(tmp_path, monkeypatch):
    """The commit markers are the LAST content written into the image (Req 4.1).

    create_image / attach are stubbed so no device is needed; a plain temp dir
    stands in for the mounted image root. We record the order in which paths are
    written and assert both markers land AFTER the bulk ChartData copy, the nav
    files, and the identity file.
    """
    source = _make_source_tree(str(tmp_path / "usb_image"))
    image_path = str(tmp_path / "EFIS_USB.img")
    fake_root = str(tmp_path / "image_root")
    os.makedirs(fake_root, exist_ok=True)

    writes = []

    # Stub the lifecycle: no real image, a plain dir as the "mounted" root.
    # Accept the volume_label kwarg compose now threads through.
    monkeypatch.setattr(
        ic, "create_image",
        lambda p, n, volume_label=None: writes.append(("create", p)),
    )

    import contextlib

    @contextlib.contextmanager
    def fake_attach(_image_path, _mountpoint):
        yield fake_root

    monkeypatch.setattr(ic, "attach_for_population", fake_attach)

    # Record populate (bulk ChartData) and single-file copies in order.
    real_populate = ic.populate

    def recording_populate(src, dst, extra_excludes=None):
        writes.append(("populate", "ChartData"))
        real_populate(src, dst, extra_excludes=extra_excludes)

    monkeypatch.setattr(ic, "populate", recording_populate)

    real_copy = ic._copy_file_into_image

    def recording_copy(src_file, dst_file):
        writes.append(("copy", os.path.relpath(dst_file, fake_root)))
        real_copy(src_file, dst_file)

    monkeypatch.setattr(ic, "_copy_file_into_image", recording_copy)

    # finalize_result can't hash a non-existent image here; stub it.
    monkeypatch.setattr(
        ic,
        "finalize_result",
        lambda p, families_included=None: ic.ComposeResult(
            image_path=p,
            image_sha256="stub",
            byte_size=_TINY_FAT32_BYTES,
            families_included=list(families_included or []),
            composed_at="now",
        ),
    )

    result = ic.compose(
        image_path,
        _TINY_FAT32_BYTES,
        usb_image_path=source,
        identity={"id": "x", "kind": drive_updater.IDENTITY_KIND},
        mountpoint=str(tmp_path / "mnt"),
    )

    # The two marker copies must be the final two recorded write events.
    marker_rels = {drive_updater.SCANNED_MARKER, drive_updater.PLATES_MARKER}
    last_two = [rel for (kind, rel) in writes[-2:]]
    assert set(last_two) == marker_rels, f"markers not written last: {writes}"

    # Everything else (ChartData populate, nav copies, identity write) precedes
    # the markers. Identity is written via write_identity (not _copy), so assert
    # the marker events come strictly after the last non-marker write event.
    marker_indices = [
        i for i, (kind, rel) in enumerate(writes) if rel in marker_rels
    ]
    non_marker_indices = [
        i for i, (kind, rel) in enumerate(writes) if rel not in marker_rels
    ]
    assert min(marker_indices) > max(non_marker_indices)

    assert result.families_included == ["charts", "nav", "identity", "marker"]


def test_compose_raises_when_chart_source_missing(tmp_path):
    """A missing ChartData source makes compose fail fast with ComposeError."""
    empty = str(tmp_path / "empty_usb_image")
    os.makedirs(empty, exist_ok=True)  # exists, but no ChartData subdir
    with pytest.raises(ic.ComposeError, match="chart source not found"):
        ic.compose(
            str(tmp_path / "img.img"),
            _TINY_FAT32_BYTES,
            usb_image_path=empty,
        )
