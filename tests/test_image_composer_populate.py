# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the sidecar-free populate method (task 1.1).

Covers the locked populate contract in image_composer:

  - populate(source_dir, dest_dir): rsync-copies a source tree into a
    destination (the mounted image root in production, an ordinary temp dir
    here), strips residual ._* / .DS_Store, then hard-asserts the finished
    tree contains none. No real device / hdiutil is involved.
  - strip_sidecars(root) / assert_sidecar_free(root): independently callable
    (task 1.5's property test exercises these against salted trees).
  - find_sidecars(root): the scan the strip + assert are built on.

The real system rsync (/usr/bin/rsync) is used for the end-to-end populate
test (native tool, no third-party dep); the strip/assert helpers are pure
filesystem logic exercised directly.

Requirements: 3.1, 3.2, 3.3
"""

import os

import pytest

from efis_data_manager import image_composer as ic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _touch(path: str, content: bytes = b"x") -> None:
    """Create a file (and any parent dirs) with some content."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


def _build_source_tree(root: str) -> None:
    """Build a representative source tree with real files AND salted sidecars."""
    _touch(os.path.join(root, "ChartData", "plate1.pdf"), b"plate-1")
    _touch(os.path.join(root, "ChartData", "sub", "plate2.pdf"), b"plate-2")
    _touch(os.path.join(root, "NAV.DB"), b"nav-db-bytes")
    # Salted AppleDouble + .DS_Store sidecars that must NOT survive populate.
    _touch(os.path.join(root, "ChartData", "._plate1.pdf"), b"appledouble")
    _touch(os.path.join(root, "ChartData", ".DS_Store"), b"finder")
    _touch(os.path.join(root, "ChartData", "sub", "._plate2.pdf"), b"appledouble")
    _touch(os.path.join(root, ".DS_Store"), b"finder-root")


# ---------------------------------------------------------------------------
# find_sidecars / _is_sidecar
# ---------------------------------------------------------------------------


def test_is_sidecar_classifies_names():
    assert ic._is_sidecar("._foo.pdf") is True
    assert ic._is_sidecar(".DS_Store") is True
    assert ic._is_sidecar("foo.pdf") is False
    assert ic._is_sidecar("NAV.DB") is False
    # A dotfile that is not .DS_Store and not ._* is NOT a sidecar.
    assert ic._is_sidecar(".gitignore") is False


def test_find_sidecars_locates_all(tmp_path):
    root = str(tmp_path)
    _build_source_tree(root)
    found = ic.find_sidecars(root)
    names = {os.path.basename(p) for p in found}
    assert names == {"._plate1.pdf", "._plate2.pdf", ".DS_Store"}
    # 4 sidecar entries total (two ._*, two .DS_Store).
    assert len(found) == 4


def test_find_sidecars_missing_root_is_empty(tmp_path):
    assert ic.find_sidecars(str(tmp_path / "does-not-exist")) == []


# ---------------------------------------------------------------------------
# strip_sidecars
# ---------------------------------------------------------------------------


def test_strip_sidecars_removes_and_counts(tmp_path):
    root = str(tmp_path)
    _build_source_tree(root)
    removed = ic.strip_sidecars(root)
    assert removed == 4
    assert ic.find_sidecars(root) == []
    # Real files are untouched.
    assert os.path.exists(os.path.join(root, "NAV.DB"))
    assert os.path.exists(os.path.join(root, "ChartData", "plate1.pdf"))
    assert os.path.exists(os.path.join(root, "ChartData", "sub", "plate2.pdf"))


def test_strip_sidecars_idempotent_on_clean_tree(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "ChartData", "plate1.pdf"))
    assert ic.strip_sidecars(root) == 0
    assert ic.strip_sidecars(root) == 0


# ---------------------------------------------------------------------------
# assert_sidecar_free
# ---------------------------------------------------------------------------


def test_assert_sidecar_free_passes_on_clean_tree(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "NAV.DB"))
    # Should not raise.
    ic.assert_sidecar_free(root)


def test_assert_sidecar_free_raises_when_sidecar_present(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "NAV.DB"))
    _touch(os.path.join(root, "._NAV.DB"))
    with pytest.raises(ic.SidecarAssertionError):
        ic.assert_sidecar_free(root)


# ---------------------------------------------------------------------------
# populate (end-to-end, real system rsync)
# ---------------------------------------------------------------------------


def test_populate_copies_real_files_and_is_sidecar_free(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _build_source_tree(str(src))

    ic.populate(str(src), str(dst))

    # Real files copied through.
    assert (dst / "NAV.DB").read_bytes() == b"nav-db-bytes"
    assert (dst / "ChartData" / "plate1.pdf").read_bytes() == b"plate-1"
    assert (dst / "ChartData" / "sub" / "plate2.pdf").read_bytes() == b"plate-2"
    # Finished tree has zero sidecars.
    assert ic.find_sidecars(str(dst)) == []


def test_populate_missing_source_raises(tmp_path):
    with pytest.raises(ic.PopulateError):
        ic.populate(str(tmp_path / "nope"), str(tmp_path / "dst"))


def test_populate_asserts_when_sidecar_survives(tmp_path, monkeypatch):
    """The final hard assert must fail the populate if a sidecar is present.

    rsync's own excludes already drop source sidecars, so to exercise the LAST
    line of defence we simulate the mount layer synthesising a ``._*`` companion
    AFTER the copy: replace strip_sidecars with a stub that, instead of
    removing sidecars, writes one into dest. assert_sidecar_free must then raise.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _build_source_tree(str(src))

    def _inject_sidecar(root):
        # Stand in for a post-copy mount-layer sidecar the strip failed to catch.
        _touch(os.path.join(root, "._injected"), b"appledouble")
        return 0

    monkeypatch.setattr(ic, "strip_sidecars", _inject_sidecar)
    with pytest.raises(ic.SidecarAssertionError):
        ic.populate(str(src), str(dst))


# ---------------------------------------------------------------------------
# _copy_file_into_image (nav files + commit markers)
# ---------------------------------------------------------------------------


def test_copy_file_into_image_copies_content_on_normal_dir(tmp_path):
    """Positive case: content is copied byte-for-byte into a normal dir."""
    src = tmp_path / "NAV.DB"
    src.write_bytes(b"nav-db-bytes")
    dst = tmp_path / "image" / "NAV.DB"

    ic._copy_file_into_image(str(src), str(dst))

    assert dst.is_file()
    assert dst.read_bytes() == b"nav-db-bytes"


def test_copy_file_into_image_survives_fat32_metadata_rejection(tmp_path, monkeypatch):
    """Regression: a FAT32-style metadata rejection must not abort the copy.

    On a real FAT32 image mount ``shutil.copy2`` -> ``copystat`` -> ``chflags``
    raises ``OSError: [Errno 22] Invalid argument`` because FAT32 does not
    support BSD file flags, which previously aborted compose on the first nav
    file. The headless suite missed this because plain temp dirs DO support
    file flags. Here we simulate the unsupported-metadata filesystem by making
    ``shutil.copystat`` raise EINVAL, then assert the content still lands and no
    error propagates.
    """
    import errno
    import shutil

    src = tmp_path / "NAV.DB"
    src.write_bytes(b"nav-db-bytes")
    dst = tmp_path / "image" / "NAV.DB"

    def _reject_metadata(*_a, **_k):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(shutil, "copystat", _reject_metadata)

    # Must NOT raise despite the metadata rejection.
    ic._copy_file_into_image(str(src), str(dst))

    # CONTENT is the hard requirement and must have landed intact.
    assert dst.is_file()
    assert dst.read_bytes() == b"nav-db-bytes"
