# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for manifest/card convergence (task 1.6).

Feature: chart-manifest-sync, Property 2: Manifest/card convergence.

Design Property 2 — Manifest/card convergence: for any local Family_Manifest
``L`` and any prior card family state ``C``, diffing ``L`` against ``C`` with
``diff_family`` and then applying the resulting Change_Set to the card tree —
writing (adding/overwriting) every ``added`` + ``changed`` relpath and deleting
every ``removed`` relpath — leaves the card tree exactly equal to ``L``'s
content, and the updated card family entries equal ``L.entries``.

This is the correctness guarantee that makes the delta write safe: the sync
only ever transfers ``files_from`` (added + changed) and unlinks ``removed``, it
never touches ``unchanged`` files, yet the end state must be byte-for-byte the
local manifest. If convergence did not hold, a partial delta would leave the
card stale — the exact failure the feature exists to prevent (Req 4.1, 4.3,
4.4).

Modeling: a "card tree" is modeled as a ``dict`` relpath -> ``ManifestEntry``,
where the entry IS the content (its ``sha256``/``size`` identify the bytes).
Applying the diff is the pure analogue of what the real sync does against the
FSKit mount:

  * ``added`` + ``changed`` (``diff.files_from``): copy ``L.entries[rel]`` into
    the card tree (add new, overwrite existing) — this is the rsync
    ``--files-from`` transfer.
  * ``removed``: delete ``rel`` from the card tree — this is the explicit
    ``os.unlink`` of pruned files.
  * ``unchanged``: left untouched (never in ``files_from`` or ``removed``).

The generators deliberately build the card family ``C`` to overlap ``L`` three
ways so every diff bucket is exercised across examples: shared relpaths with the
SAME digest (unchanged), shared relpaths with a DIFFERENT digest (changed, incl.
same-size content change), local-only relpaths (added), and card-only relpaths
(removed).

Validates: Requirements 4.1, 4.3, 4.4
"""

from hypothesis import given, settings, strategies as st

from efis_data_manager import manifest as m
from efis_data_manager.manifest import (
    FamilyManifest,
    ManifestEntry,
    diff_family,
)


# A lowercase-hex SHA-256 digest: exactly 64 hex chars, matching the builder.
_SHA256 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

# Non-negative byte count.
_SIZE = st.integers(min_value=0, max_value=2**31)

# POSIX relpath keys: 1-3 filesystem-safe segments joined by "/". A leading "f"
# guarantees we never produce "." / ".." nor collide with an excluded basename.
# These are only dict keys here (no real file is written), so any distinct
# string works, but realistic keys exercise the true key space.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789" + "".join(("_", "-")),
    ),
    min_size=1,
    max_size=8,
).map(lambda s: "f" + s)

_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

_FAMILY = st.sampled_from(["scanned", "plates"])

# Cycle number: a 4-digit string, or None (first-sync / unknown).
_CYCLE = st.one_of(st.none(), st.text(alphabet="0123456789", min_size=4, max_size=4))


def _entry(relpath: str, sha: str, size: int) -> ManifestEntry:
    return ManifestEntry(relpath=relpath, sha256=sha, size=size)


@st.composite
def _local_and_card(draw):
    """Generate a local manifest ``L`` and a card family dict ``C`` that overlap.

    Produces four disjoint relpath groups so every diff bucket is reachable:

      * ``shared_same``  -> in both, identical digest  (diff: unchanged)
      * ``shared_diff``  -> in both, differing digest   (diff: changed)
      * ``local_only``   -> only in L                   (diff: added)
      * ``card_only``    -> only in C                   (diff: removed)

    ``L.entries`` is (shared_same + shared_diff + local_only); ``C`` is
    (shared_same + shared_diff + card_only). The digest for a ``shared_diff``
    relpath is forced to differ between L and C (a byte flip in the hex,
    including the same ``size`` so a same-size content change is covered).
    """
    # Draw a pool of distinct relpaths, then partition it into the four groups.
    pool = draw(st.lists(_RELPATH, min_size=0, max_size=12, unique=True))

    group_of = {}
    for rel in pool:
        group_of[rel] = draw(
            st.sampled_from(["shared_same", "shared_diff", "local_only", "card_only"])
        )

    local_entries = {}
    card_entries = {}

    for rel, group in group_of.items():
        size = draw(_SIZE)
        sha = draw(_SHA256)
        if group == "shared_same":
            e = _entry(rel, sha, size)
            local_entries[rel] = e
            card_entries[rel] = e
        elif group == "shared_diff":
            # Force a differing digest on the card side while keeping the SAME
            # size, so this also covers the same-size content-change case that
            # the whole feature exists to catch. Flipping one hex nibble is
            # guaranteed to change the digest.
            flipped = ("0" if sha[0] != "0" else "1") + sha[1:]
            local_entries[rel] = _entry(rel, sha, size)
            card_entries[rel] = _entry(rel, flipped, size)
        elif group == "local_only":
            local_entries[rel] = _entry(rel, sha, size)
        else:  # card_only
            card_entries[rel] = _entry(rel, sha, size)

    local = FamilyManifest(
        family=draw(_FAMILY),
        cycle=draw(_CYCLE),
        entries=local_entries,
        built="",
        schema_version=m.SCHEMA_VERSION,
    )
    return local, card_entries


def _apply_diff(card_tree, local, diff):
    """Apply a Change_Set to a modeled card tree, returning the new tree.

    Mirrors exactly what the real sync does against the FSKit mount, purely:

      * write (add/overwrite) every ``files_from`` relpath with the LOCAL
        content for that relpath (rsync ``--files-from`` transfer), and
      * delete every ``removed`` relpath (explicit ``os.unlink``).

    ``unchanged`` files are deliberately never touched.
    """
    result = dict(card_tree)
    for rel in diff.files_from:
        # The transferred content is the local manifest's entry for that relpath.
        result[rel] = local.entries[rel]
    for rel in diff.removed:
        result.pop(rel, None)
    return result


@settings(max_examples=100, deadline=None)
@given(data=_local_and_card())
def test_diff_then_apply_converges_to_local(data):
    """Applying ``diff_family``'s Change_Set converges the card tree to ``L``.

    For a generated local manifest ``L`` and prior card family ``C``:
    compute ``diff_family(L, C)``, apply it to the modeled card tree (write
    added+changed, delete removed), and assert the resulting tree equals
    ``L.entries`` — i.e. the card family converges byte-for-byte to the local
    manifest using ONLY the delta, never re-touching unchanged files.

    Validates: Requirements 4.1, 4.3, 4.4
    """
    local, card_family = data

    diff = diff_family(local, card_family)

    converged = _apply_diff(card_family, local, diff)

    # (Req 4.4) the card tree converges exactly to the local manifest content.
    assert converged == local.entries

    # (Req 4.4) the same statement framed as "updated card family entries equal
    # L.entries" — identical mapping, which is what task 1.6 asserts.
    assert converged == dict(local.entries)


@settings(max_examples=100, deadline=None)
@given(data=_local_and_card())
def test_change_set_buckets_are_correct_and_disjoint(data):
    """The Change_Set buckets partition the diff space correctly (Req 4.1).

    Guards the convergence test above by pinning what each bucket must contain,
    so a degenerate ``diff_family`` (e.g. one that classified everything as
    ``added``) could not vacuously satisfy convergence:

      * ``added``   == relpaths only in L
      * ``changed`` == shared relpaths whose SHA-256 differs
      * ``removed`` == relpaths only on the card
      * ``files_from`` == added + changed, disjoint from ``removed``

    Validates: Requirements 4.1, 4.3, 4.4
    """
    local, card_family = data
    local_entries = local.entries

    diff = diff_family(local, card_family)

    expected_added = {r for r in local_entries if r not in card_family}
    expected_changed = {
        r
        for r in local_entries
        if r in card_family and local_entries[r].sha256 != card_family[r].sha256
    }
    expected_removed = {r for r in card_family if r not in local_entries}

    assert set(diff.added) == expected_added
    assert set(diff.changed) == expected_changed
    assert set(diff.removed) == expected_removed

    # files_from is exactly added + changed, and never overlaps removed: a file
    # is never both transferred and pruned in the same delta.
    assert set(diff.files_from) == expected_added | expected_changed
    assert set(diff.files_from).isdisjoint(set(diff.removed))
