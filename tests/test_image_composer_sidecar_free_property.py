# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test: image population is sidecar-free (Property family B).

Feature: image-based-drive-write, Property: sidecar-free image

Exercises the independently-callable sidecar helpers in ``image_composer``
(``find_sidecars`` / ``strip_sidecars`` / ``assert_sidecar_free``) against
randomly-generated directory trees that are deliberately salted with
AppleDouble (``._*``) and ``.DS_Store`` entries. These helpers are factored out
of ``populate`` precisely so this property can run WITHOUT a real image or
device: it builds the tree under an ordinary temp directory, not a mounted
FAT32 image.

Properties asserted, for ANY generated salted tree:
  (a) after ``strip_sidecars(root)``, ``find_sidecars(root) == []`` and
      ``assert_sidecar_free(root)`` does not raise;
  (b) ``strip_sidecars`` returns a count equal to the number of sidecars that
      were present before the strip;
  (c) real (non-sidecar) files are PRESERVED by the strip — same relative paths
      and same byte content before and after;
  (d) if at least one sidecar is present and the tree is NOT stripped,
      ``assert_sidecar_free`` raises ``SidecarAssertionError``.

Validates: Requirements 3.1, 3.2, 3.3
"""

import os
import tempfile

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager import image_composer as ic


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#
# A tree is generated as a list of "entries". Each entry is a leaf file placed
# at a relative path built from directory segments plus a filename. The filename
# is tagged real-or-sidecar so classification is UNAMBIGUOUS:
#
#   * real segment/name   -> drawn from a restricted lowercase+digit alphabet,
#                            guaranteed NOT to start with "._" and NOT to equal
#                            ".DS_Store" (the alphabet has no "." so this holds).
#   * sidecar name        -> either "._<realname>" (AppleDouble) or ".DS_Store".
#
# Keeping real names in a dot-free alphabet means the real/sidecar partition the
# test relies on can never be confused by a generated name.

# Dot-free, separator-free alphabet: real path segments drawn from here can
# never collide with the sidecar classes ("._*" / ".DS_Store").
_REAL_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# A real path segment (directory name or the "real" part of a filename).
_real_segment = st.text(alphabet=_REAL_ALPHABET, min_size=1, max_size=8)

# Zero to three directory segments give nested-but-bounded trees.
_dir_path = st.lists(_real_segment, min_size=0, max_size=3)


@st.composite
def _entry(draw):
    """Draw one tree entry: (relative_segments, is_sidecar, content_bytes).

    ``relative_segments`` is the list of directory segments plus the final
    filename. ``is_sidecar`` marks whether the leaf is a ``._*`` / ``.DS_Store``
    entry (so the test knows what to expect post-strip). ``content_bytes`` is the
    file body (used to assert real files are byte-preserved).
    """
    dirs = draw(_dir_path)
    is_sidecar = draw(st.booleans())
    if is_sidecar:
        # Either an AppleDouble companion ("._<name>") or a ".DS_Store".
        kind = draw(st.sampled_from(["appledouble", "ds_store"]))
        if kind == "appledouble":
            base = draw(_real_segment)
            name = ic._APPLEDOUBLE_PREFIX + base
        else:
            name = ic._DS_STORE_NAME
    else:
        name = draw(_real_segment)
    content = draw(st.binary(min_size=0, max_size=16))
    return (dirs + [name], is_sidecar, content)


# A whole tree: a list of entries (possibly empty, bounded for speed).
_tree = st.lists(_entry(), min_size=0, max_size=12)


# ---------------------------------------------------------------------------
# Materialisation helpers
# ---------------------------------------------------------------------------


def _materialise(root, entries):
    """Write ``entries`` under ``root``; return a map of real relpath -> content.

    Collisions are possible (two generated entries landing on the same relative
    path, or a file name colliding with an earlier directory). Those are skipped
    rather than fought — the goal is an arbitrary VALID salted tree, and the
    returned real-file map reflects exactly what actually ended up on disk so the
    preservation check (property c) stays accurate. Returns the mapping of
    surviving REAL (non-sidecar) files to their on-disk content.
    """
    real_files = {}
    for segments, is_sidecar, content in entries:
        rel = os.path.join(*segments)
        abs_path = os.path.join(root, rel)
        parent = os.path.dirname(abs_path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Skip if something already occupies this path as a directory, or if
            # a parent segment is already a file (both make the write invalid).
            if os.path.isdir(abs_path):
                continue
            with open(abs_path, "wb") as fh:
                fh.write(content)
        except (NotADirectoryError, FileExistsError, IsADirectoryError, OSError):
            # A parent segment collided with an existing file (or similar); this
            # entry simply does not materialise. Keep going.
            continue
        if not is_sidecar:
            real_files[rel] = content
        else:
            # A sidecar write may have overwritten a real file recorded earlier
            # at the same relpath (rare, but keep the map truthful).
            real_files.pop(rel, None)
    return real_files


def _read_all_real(root):
    """Return a map of relpath -> content for every NON-sidecar file under root.

    Mirrors what ``_materialise`` tracks, read back from disk, so property (c)
    compares the real-file set/content before vs after the strip.
    """
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if ic._is_sidecar(name):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root)
            with open(abs_path, "rb") as fh:
                out[rel] = fh.read()
    return out


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(entries=_tree)
def test_strip_makes_tree_sidecar_free_and_preserves_real_files(entries):
    """strip_sidecars clears all sidecars, counts them, and preserves real files.

    Feature: image-based-drive-write, Property: sidecar-free image
    Validates: Requirements 3.1, 3.2, 3.3

    A fresh TemporaryDirectory is created INSIDE the test body (not via a
    function-scoped fixture) so Hypothesis drives many examples against isolated
    trees without the fixture-reuse pitfall.
    """
    with tempfile.TemporaryDirectory() as root:
        real_before = _materialise(root, entries)

        # Count sidecars present before the strip (property b reference value).
        sidecars_before = ic.find_sidecars(root)
        real_on_disk_before = _read_all_real(root)

        # Snapshot the real-file set/content as actually written.
        assert real_on_disk_before == real_before

        removed = ic.strip_sidecars(root)

        # (b) strip returns a count == number of sidecars that were present.
        assert removed == len(sidecars_before)

        # (a) the stripped tree is sidecar-free and the hard assert passes.
        assert ic.find_sidecars(root) == []
        ic.assert_sidecar_free(root)  # must not raise

        # (c) real files are preserved byte-for-byte across the strip.
        real_after = _read_all_real(root)
        assert real_after == real_before


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(entries=_tree)
def test_assert_raises_iff_sidecar_present_without_strip(entries):
    """assert_sidecar_free raises exactly when an unstripped tree has a sidecar.

    Feature: image-based-drive-write, Property: sidecar-free image
    Validates: Requirements 3.1, 3.2, 3.3

    (d) If at least one sidecar materialised and we do NOT strip, the hard assert
    raises SidecarAssertionError. If none materialised, it passes. This pins the
    "fail loud" guarantee that keeps a dirty image from ever being marked ready.
    """
    with tempfile.TemporaryDirectory() as root:
        _materialise(root, entries)
        has_sidecar = bool(ic.find_sidecars(root))

        if has_sidecar:
            raised = False
            try:
                ic.assert_sidecar_free(root)
            except ic.SidecarAssertionError:
                raised = True
            assert raised, "expected SidecarAssertionError on a tree with a sidecar"
        else:
            # Clean tree: the assert must pass.
            ic.assert_sidecar_free(root)
