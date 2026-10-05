# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test: composer contents, current-source, drive-independence.

Feature: image-based-drive-write, Property 5: composition is drive-independent

This is Task 1.4 (optional property family A). It generalises the two
drive-independence unit tests in ``test_image_composer_compose.py`` into a
Hypothesis property over randomly-shaped fixture source trees, asserting the
three facets of design Property 5:

  (1) CONTENTS. For any fixture source tree (varied ChartData files/dirs, nav
      present-or-absent, identity passed-or-None), the finished image contains
      EXACTLY the sourced charts + nav + identity + the markers that were present
      in the source, and is sidecar-free. ``families_included`` reflects exactly
      what was populated.
  (2) DRIVE-INDEPENDENCE (the core property). ``compose`` touches NO ``/dev/disk``
      USB node and NO ``/Volumes`` USB mount, and still SUCCEEDS with no drive
      attached. We prove this by monkeypatching every USB-drive-touching entry
      point — ``drive_updater.resolve_drive_id`` / ``drive_updater.msdos_mount``
      and the underlying ``mount_swap.msdos_mount`` / ``mount_swap.run_privileged``
      — to RAISE, and asserting compose still returns a ``ComposeResult``. We also
      assert the compose ``mountpoint`` is never a ``/Volumes/...`` path.
  (3) CURRENT-SOURCE. The identity on the finished image is the PASSED-IN identity
      (when given) or a freshly-minted one (when ``None``) — NEVER read off a
      drive. nav / charts / markers come only from the local ``usb_image_path``.

  **Validates: Requirements 1.2, 1.3, 1.5, 1.6, 4.1.**

Why the split (stubbed @ 100 + a few real round-trips)
------------------------------------------------------
``compose`` runs a REAL ``newfs_msdos`` + ``hdiutil`` round-trip, and FAT32 on
macOS has a ~64 MiB minimum format size; a real round-trip takes on the order of
a second-plus and leaves an attached image to clean up. Running that 100x under
Hypothesis is far too slow and risks leaking hdiutil attachments.

The property VALUE here is in compose's SOURCING + ordering + drive-independence
logic — none of which needs a real FAT32 format to exercise. So:

  * The main property (``test_compose_is_drive_independent_and_correct``) runs at
    100 Hypothesis examples with ``create_image`` / ``attach_for_population``
    STUBBED to use a plain temp dir as the "mounted" image root (the same
    technique ``test_compose_writes_markers_last`` uses). The forbidden USB entry
    points are monkeypatched to RAISE, so compose completing is the
    drive-independence proof. ``read_identity`` / ``write_identity`` run for real
    against the LOCAL fake root only.
  * A SMALL handful of REAL round-trips (``test_compose_real_roundtrip_anchor``,
    ~3 examples, skipif-gated on native tools) anchors the stubbed property to the
    genuine ``newfs_msdos`` + ``hdiutil`` path, verifying the finished FAT32 image
    actually carries the generated contents (modulo the macOS ``.fseventsd/``
    mount journal) and is sidecar-free.
"""

import os
import subprocess

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager import drive_updater
from efis_data_manager import image_composer as ic
from efis_data_manager import mount_swap


# 64 MiB is the smallest size newfs_msdos formats cleanly as FAT32 on macOS
# (mirrors the sibling compose/lifecycle tests). Only the real-round-trip anchor
# pays this cost; the stubbed property uses a nominal value.
_TINY_FAT32_BYTES = 64 * 1024 * 1024

_NATIVE_TOOLS_PRESENT = os.path.exists(ic.NEWFS_MSDOS_BIN) and os.path.exists(
    ic.HDIUTIL_BIN
)

requires_native_tools = pytest.mark.skipif(
    not _NATIVE_TOOLS_PRESENT,
    reason="native newfs_msdos / hdiutil not present",
)


# ---------------------------------------------------------------------------
# Fixture-source-tree strategy
# ---------------------------------------------------------------------------
#
# A "source tree spec" is a plain dict Hypothesis draws, then materialised on
# disk by _materialise_source_tree. Keeping the spec separate from the on-disk
# build lets each example rebuild under its own tmp dir and keeps the generators
# intelligent about the input space compose actually sources from:
#   * ChartData : a varied set of relative file paths (nested dirs, assorted
#                 names) always under ChartData/, always non-empty (compose
#                 requires the ChartData dir to exist).
#   * nav       : NAV.DB / NAV-proc.DB each independently present-or-absent.
#   * markers   : SCANNED_MARKER / PLATES_MARKER each independently
#                 present-or-absent (both live under ChartData/).
#   * identity  : passed-in dict vs None (mint) chosen per example.

# Path-segment names kept to a safe, FAT32-friendly, rsync-friendly alphabet so
# the generator exercises structure (nesting, counts) rather than hunting for
# filename-escaping bugs that are out of scope for this property.
_SEG_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

_segment = st.text(alphabet=_SEG_ALPHABET, min_size=1, max_size=10).filter(
    # Never generate a name that looks like a sidecar — those are the subject of
    # Task 1.5's property, and here we are asserting EXACT contents, so a
    # generated "._x" would be (correctly) stripped and break the exact match.
    lambda s: not s.startswith("._") and s != ".DS_Store"
)

# A ChartData-relative file path: 1-3 segments, last is the file name. Excludes
# the two marker relative paths (added separately) so counts stay exact.
_MARKER_RELS_UNDER_CHARTDATA = frozenset(
    os.path.relpath(m, "ChartData")
    for m in (drive_updater.SCANNED_MARKER, drive_updater.PLATES_MARKER)
)


def _is_prefix_path(a, b):
    """True iff path ``a`` is a strict directory-prefix of path ``b``.

    e.g. "0" is a prefix of "0/0". Such a pair is an invalid source tree: one
    name cannot be both a file and a directory, so we reject these in the
    generator rather than generate trees compose could never see on disk.
    """
    return b != a and b.startswith(a + "/")


@st.composite
def _chart_rel_paths(draw):
    """Draw a non-empty set of distinct, non-colliding ChartData-relative files.

    Each path is a 1-3 segment relative file path. We drop (a) collisions with a
    marker relative path (markers are added via their own flags so counts stay
    exact) and (b) any path that is a directory-prefix of, or has as a prefix,
    another kept path — such a name cannot be both a file and a dir on disk.
    """
    paths = draw(
        st.lists(
            st.lists(_segment, min_size=1, max_size=3).map(lambda parts: "/".join(parts)),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    candidates = sorted(
        {p for p in paths if p not in _MARKER_RELS_UNDER_CHARTDATA}
    )
    kept = []
    for p in candidates:
        if any(_is_prefix_path(p, k) or _is_prefix_path(k, p) for k in kept):
            continue
        kept.append(p)
    # Guarantee at least one chart file so ChartData/ is always non-empty.
    if not kept:
        kept = ["enroute.bin"]
    return sorted(kept)


@st.composite
def _source_tree_spec(draw):
    """Draw a full source-tree spec dict for one compose example."""
    return {
        "charts": draw(_chart_rel_paths()),
        "nav_db": draw(st.booleans()),
        "nav_proc_db": draw(st.booleans()),
        "scanned_marker": draw(st.booleans()),
        "plates_marker": draw(st.booleans()),
        "identity_passed": draw(st.booleans()),
    }


def _materialise_source_tree(root, spec):
    """Write the spec onto disk under ``root``; return a description of expectations.

    Returns a dict of the exact relative paths (relative to the image root) the
    finished image must contain, so the property can assert EXACT contents.
    """
    chartdata = os.path.join(root, "ChartData")
    os.makedirs(chartdata, exist_ok=True)

    expected_rel = set()

    # Chart files (bulk-copied via populate, markers excluded).
    for rel in spec["charts"]:
        dst = os.path.join(chartdata, rel)
        os.makedirs(os.path.dirname(dst) or chartdata, exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(("chart:" + rel).encode("utf-8"))
        expected_rel.add(os.path.join("ChartData", rel))

    # Nav files, each independently present.
    if spec["nav_db"]:
        with open(os.path.join(root, "NAV.DB"), "wb") as fh:
            fh.write(b"nav-db-current")
        expected_rel.add("NAV.DB")
    if spec["nav_proc_db"]:
        with open(os.path.join(root, "NAV-proc.DB"), "wb") as fh:
            fh.write(b"nav-proc-db-current")
        expected_rel.add("NAV-proc.DB")

    # Commit markers, each independently present (both under ChartData/).
    if spec["scanned_marker"]:
        p = os.path.join(root, drive_updater.SCANNED_MARKER)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"scanned-marker")
        expected_rel.add(drive_updater.SCANNED_MARKER)
    if spec["plates_marker"]:
        p = os.path.join(root, drive_updater.PLATES_MARKER)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"plates-marker")
        expected_rel.add(drive_updater.PLATES_MARKER)

    # Identity is always on the finished image (passed-in or minted).
    expected_rel.add(drive_updater.IDENTITY_FILENAME)

    return expected_rel


def _relpaths_under(root):
    """Return the set of file relative paths under ``root`` (posix-style)."""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.add(rel)
    return out


def _passed_identity():
    """A known target-drive identity dict (the drive's-own-identity path)."""
    return {
        "schema_version": drive_updater.IDENTITY_SCHEMA_VERSION,
        "id": "known-target-drive-id",
        "kind": drive_updater.IDENTITY_KIND,
    }


def _install_forbidden_usb_mocks(monkeypatch):
    """Make every USB-drive-touching entry point RAISE.

    compose must complete without any of these: ``resolve_drive_id`` (drive-id
    resolution via diskutil + mount), ``msdos_mount`` (the FSKit mount swap — the
    only thing that mounts a /Volumes USB volume), and the privileged shim
    ``run_privileged`` that mount-swap routes through. Patched on BOTH the
    ``drive_updater`` namespace (where compose's deferred import binds them) and
    the ``mount_swap`` source module, so no alias escapes the guard.
    """

    def _forbidden(*_a, **_k):
        raise AssertionError("compose touched a USB drive node/mount")

    monkeypatch.setattr(drive_updater, "resolve_drive_id", _forbidden)
    monkeypatch.setattr(drive_updater, "msdos_mount", _forbidden)
    monkeypatch.setattr(mount_swap, "msdos_mount", _forbidden)
    monkeypatch.setattr(mount_swap, "run_privileged", _forbidden)


# ---------------------------------------------------------------------------
# Property family A (stubbed @ 100) — drive-independent + correct + current-source
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(spec=_source_tree_spec())
def test_compose_is_drive_independent_and_correct(spec, tmp_path, monkeypatch):
    """Property 5: compose is drive-independent, correct, and current-sourced.

    For any generated source tree, with ALL USB-drive access mocked to raise:
      (2) compose SUCCEEDS (returns a ComposeResult) — the drive-independence
          proof, since any drive touch would have raised;
      (1) the finished image contains EXACTLY the sourced charts + nav +
          identity + present markers, and is sidecar-free; families_included
          reflects exactly what was populated;
      (3) the identity on the image is the PASSED-IN one (when given) or a
          freshly-minted one (when None), read only off the LOCAL image root; and
          the compose mountpoint is never a /Volumes USB path.
    """
    # Unique per-example paths so Hypothesis's reuse of the fixture dir is safe.
    ex = tmp_path / os.urandom(6).hex()
    source = str(ex / "usb_image")
    os.makedirs(source, exist_ok=True)
    image_path = str(ex / "EFIS_USB.img")
    fake_root = str(ex / "image_root")
    os.makedirs(fake_root, exist_ok=True)
    mountpoint = str(ex / "mnt")

    expected_rel = _materialise_source_tree(source, spec)

    # (2) Forbid every USB-drive touch: compose completing proves independence.
    _install_forbidden_usb_mocks(monkeypatch)

    # Stub the slow FAT32 round-trip: no real image, a plain dir as mount root.
    # This exercises compose's SOURCING + ordering + drive-independence logic at
    # full example count without paying the newfs_msdos + hdiutil cost.
    monkeypatch.setattr(ic, "create_image", lambda _p, _n: None)

    import contextlib

    @contextlib.contextmanager
    def fake_attach(_image_path, _mp):
        yield fake_root

    monkeypatch.setattr(ic, "attach_for_population", fake_attach)

    # finalize_result would hash a non-existent image here; stub it to report the
    # families and a byte size, leaving the real sourcing/ordering under test.
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

    identity = _passed_identity() if spec["identity_passed"] else None

    # The compose mountpoint must never be a USB /Volumes mount (drive-independence).
    assert not mountpoint.startswith("/Volumes/")

    result = ic.compose(
        image_path,
        _TINY_FAT32_BYTES,
        usb_image_path=source,
        identity=identity,
        mountpoint=mountpoint,
    )

    # (2) compose returned without any forbidden USB touch firing.
    assert isinstance(result, ic.ComposeResult)

    # (1) EXACT contents: the image carries exactly the sourced files + identity.
    actual_rel = _relpaths_under(fake_root)
    assert actual_rel == expected_rel, (
        f"contents mismatch\n  spec={spec}\n  expected={sorted(expected_rel)}\n"
        f"  actual={sorted(actual_rel)}"
    )

    # (1) sidecar-free.
    assert ic.find_sidecars(fake_root) == []

    # (1) families_included reflects exactly what was populated. Charts and
    # identity are always present; nav only when a nav file landed; marker only
    # when a marker landed.
    expected_families = ["charts"]
    if spec["nav_db"] or spec["nav_proc_db"]:
        expected_families.append("nav")
    expected_families.append("identity")
    if spec["scanned_marker"] or spec["plates_marker"]:
        expected_families.append("marker")
    assert result.families_included == expected_families

    # (3) current-source identity: the image's identity is the passed-in one or
    # a freshly minted one — read ONLY off the local image root, never a drive.
    on_image = drive_updater.read_identity(fake_root)
    assert on_image is not None
    assert on_image.get("kind") == drive_updater.IDENTITY_KIND
    if identity is not None:
        assert on_image.get("id") == "known-target-drive-id"
    else:
        # A minted identity carries a generated id distinct from the passed one.
        assert on_image.get("id")
        assert on_image.get("id") != "known-target-drive-id"


# ---------------------------------------------------------------------------
# Real round-trip anchor (~3 examples, skipif-gated) — ties the stubbed property
# to the genuine newfs_msdos + hdiutil FAT32 path.
# ---------------------------------------------------------------------------


def _attached_images_for(image_path):
    """Count hdiutil-attached entries whose backing image path matches."""
    try:
        out = (
            subprocess.run(
                [ic.HDIUTIL_BIN, "info"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            or ""
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if image_path in line)


@requires_native_tools
@settings(
    max_examples=3,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
@given(spec=_source_tree_spec())
def test_compose_real_roundtrip_anchor(spec, tmp_path, monkeypatch):
    """A few REAL FAT32 round-trips anchoring the stubbed property.

    Runs the genuine newfs_msdos + hdiutil compose on a 64 MiB image for a
    handful of generated source trees (device access still mocked to raise), then
    re-attaches the finished image and asserts it carries EXACTLY the sourced
    contents and is sidecar-free. Cleans up the attachment and image file so no
    hdiutil attachment leaks.
    """
    ex = tmp_path / os.urandom(6).hex()
    source = str(ex / "usb_image")
    os.makedirs(source, exist_ok=True)
    image_path = str(ex / "EFIS_USB.img")
    mountpoint = str(ex / "mnt")

    expected_rel = _materialise_source_tree(source, spec)

    _install_forbidden_usb_mocks(monkeypatch)
    assert not mountpoint.startswith("/Volumes/")

    identity = _passed_identity() if spec["identity_passed"] else None

    try:
        result = ic.compose(
            image_path,
            _TINY_FAT32_BYTES,
            usb_image_path=source,
            identity=identity,
            mountpoint=mountpoint,
        )
        assert isinstance(result, ic.ComposeResult)
        assert result.byte_size == _TINY_FAT32_BYTES

        # Re-attach and verify the finished FAT32 image carries the sourced
        # contents and no sidecars. The exact-equality check belongs to the
        # stubbed property (plain dir, no mount artifacts); a REAL macOS FAT32
        # mount synthesises a ``.fseventsd/`` journal dir of its own that is NOT
        # sourced content and NOT a sidecar, so here we assert (a) every sourced
        # file is present and (b) the only extra entries are macOS mount
        # artifacts, never a ``._*`` / ``.DS_Store`` sidecar.
        with ic.attach_for_population(image_path, mountpoint) as root:
            actual_rel = _relpaths_under(root)
            missing = expected_rel - actual_rel
            assert not missing, (
                f"real round-trip missing sourced content\n  spec={spec}\n"
                f"  missing={sorted(missing)}\n  actual={sorted(actual_rel)}"
            )
            extra = actual_rel - expected_rel
            assert all(e.startswith(".fseventsd/") for e in extra), (
                f"unexpected extra entries on real image: {sorted(extra)}"
            )
            assert ic.find_sidecars(root) == []
            on_image = drive_updater.read_identity(root)
            assert on_image is not None
            if identity is not None:
                assert on_image.get("id") == "known-target-drive-id"
            else:
                assert on_image.get("id")

        assert _attached_images_for(image_path) == 0
        assert not os.path.ismount(mountpoint)
    finally:
        if os.path.ismount(mountpoint):
            subprocess.run(
                [ic.HDIUTIL_BIN, "detach", mountpoint, "-force"],
                capture_output=True,
            )
        if os.path.exists(image_path):
            os.remove(image_path)
