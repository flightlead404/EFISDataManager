# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for the manifest builder's content signal (task 1.2).

Feature: chart-manifest-sync, Property 1: Content-change detection.

Design Property 1 — Content-change detection (closes the ``--size-only`` gap):
for any Chart_File whose content changes between cycles — INCLUDING a change
that preserves the exact byte size — the manifest built from the new tree
records a SHA-256 digest that differs from the old manifest's, so the file is
detected as changed and would be transferred.

This is the core correctness property of the whole feature: the retired
``rsync --size-only`` path compared two files equal whenever their byte sizes
matched, silently skipping a same-size content revision. The manifest builder
hashes content, so a same-size edit MUST produce a different ``sha256``.

The test therefore deliberately generates a mutation set that INCLUDES
same-byte-size content changes (a byte flipped in place, which preserves
length), builds a ``FamilyManifest`` before and after the mutation with
``Manifest_Builder.build``, and asserts that:

  (a) every mutated relpath has a differing ``sha256`` across the two
      manifests; and
  (b) every mutated relpath would appear in the ``changed`` set — the set of
      relpaths present in both manifests whose ``sha256`` differs (this mirrors
      exactly what ``Manifest_Diff.diff_family`` will compute in task 1.5:
      compare by ``relpath -> sha256``).

To avoid the Hypothesis + function-scoped-fixture reuse pitfall (a single
``tmp_path`` shared across all generated examples), each example materialises
its tree inside a fresh ``tempfile.mkdtemp`` that is removed afterward.

Validates: Requirements 1.1, 1.2, 1.3, 1.4
"""

import os
import shutil
import tempfile

from hypothesis import assume, given, settings, strategies as st

from efis_data_manager.manifest import Manifest_Builder


# Relative-path segments: FAT32 is the target filesystem (the EFIS cards) and is
# case-INSENSITIVE, so the generator alphabet is restricted to a SINGLE case of
# FAT32-legal ASCII (lowercase letters, digits, and "_"/"-"). This keeps every
# generated name a legal FAT32 long-name component AND unambiguous under
# case-folding, so two distinct Python strings can never resolve to the same
# file on a case-insensitive host (the macOS/APFS collision that an unrestricted
# Unicode alphabet produced, e.g. "fŽ" vs "fž"). A leading "f" keeps names clear
# of excluded basenames (".DS_Store", "._*") and of "." / "..".
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789_-",
    ),
    min_size=1,
    max_size=8,
).map(lambda s: "f" + s)

# A relative path of 1-3 segments (so the tree has some depth). The last
# segment is the file name; earlier segments are directories.
_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

# File contents. min_size=1 so there is always at least one byte to flip for a
# same-size mutation.
_CONTENT = st.binary(min_size=1, max_size=256)


def _has_prefix_collision(relpaths):
    """True if any relpath is a strict directory-prefix of another.

    ``a/b`` and ``a/b/c`` cannot coexist on a real filesystem (``a/b`` would be
    both a file and a directory), so such a generated set is discarded.
    """
    parts = [tuple(r.split("/")) for r in relpaths]
    for i, p in enumerate(parts):
        for j, q in enumerate(parts):
            if i != j and len(p) < len(q) and q[: len(p)] == p:
                return True
    return False


def _build_tree(root, files):
    """Materialise ``files`` ({relpath: bytes}) under ``root`` on disk."""
    for rel, data in files.items():
        abspath = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(abspath) or root, exist_ok=True)
        with open(abspath, "wb") as handle:
            handle.write(data)


def _changed_relpaths(before, after):
    """relpaths present in both manifests whose sha256 differs.

    Mirrors what ``Manifest_Diff.diff_family`` (task 1.5) will compute: compare
    by ``relpath -> sha256``. Defined here so Property 1 does not depend on the
    not-yet-implemented diff.
    """
    changed = set()
    for rel, entry in after.entries.items():
        prior = before.entries.get(rel)
        if prior is not None and prior.sha256 != entry.sha256:
            changed.add(rel)
    return changed


@settings(max_examples=100, deadline=None)
@given(
    files=st.dictionaries(_RELPATH, _CONTENT, min_size=1, max_size=8),
    data=st.data(),
)
def test_same_size_content_change_is_detected(files, data):
    """A content change — including a same-byte-size one — shows a new sha256.

    Builds a family tree, selects a non-empty subset of its files to mutate,
    and for each mutation chooses between a same-size byte flip (the critical
    ``--size-only`` gap case) and an arbitrary content replacement. Then builds
    the before/after manifests and asserts every mutated relpath has a differing
    ``sha256`` and appears in the ``changed`` set.

    Validates: Requirements 1.1, 1.2, 1.3, 1.4
    """
    relpaths = sorted(files)
    # Discard generated sets where one relpath is a directory-prefix of another
    # (cannot coexist as real files/dirs).
    assume(not _has_prefix_collision(relpaths))

    # Pick a non-empty subset of files to mutate.
    to_mutate = data.draw(
        st.lists(st.sampled_from(relpaths), min_size=1, unique=True).map(set)
    )

    mutated_files = dict(files)
    for rel in to_mutate:
        original = files[rel]
        # Force a same-size change for at least part of the set: flip the first
        # byte in place (length preserved — the exact --size-only gap). This is
        # the discriminating case the property exists to prove.
        same_size = bytearray(original)
        same_size[0] ^= 0xFF  # guaranteed to differ from the original byte
        same_size = bytes(same_size)

        # Hypothesis also gets to choose an arbitrary replacement that differs
        # from the original, so the set mixes same-size and size-changing edits.
        arbitrary = data.draw(_CONTENT.filter(lambda c, o=original: c != o))

        use_same_size = data.draw(st.booleans())
        mutated_files[rel] = same_size if use_same_size else arbitrary

    before_root = tempfile.mkdtemp(prefix="efis_manifest_before_")
    after_root = tempfile.mkdtemp(prefix="efis_manifest_after_")
    try:
        _build_tree(before_root, files)
        _build_tree(after_root, mutated_files)

        before = Manifest_Builder.build("scanned", before_root, cycle="2611")
        after = Manifest_Builder.build("scanned", after_root, cycle="2612")

        changed = _changed_relpaths(before, after)

        for rel in to_mutate:
            # (a) The digest actually changed.
            assert before.entries[rel].sha256 != after.entries[rel].sha256, (
                f"sha256 did not change for mutated file {rel!r}"
            )
            # (b) It would appear in the diff's `changed` set.
            assert rel in changed, (
                f"mutated file {rel!r} was not detected as changed"
            )

        # Unmutated files must keep an identical digest (no false positives).
        for rel in set(files) - to_mutate:
            assert before.entries[rel].sha256 == after.entries[rel].sha256
            assert rel not in changed
    finally:
        shutil.rmtree(before_root, ignore_errors=True)
        shutil.rmtree(after_root, ignore_errors=True)
