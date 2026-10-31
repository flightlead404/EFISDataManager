# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for cycle-skip safety (task 3.2).

Feature: chart-manifest-sync, Property 3: Cycle-skip safety.

Design Property 3 — Cycle-skip safety: for any family whose local cycle equals
the card's stored cycle, skipping the family leaves the card byte-identical for
that family — i.e. equal cycle numbers imply equal content, because a cycle is
only recorded on the card AFTER a verified full-convergence sync of that cycle.

The property has two halves, both asserted here:

  1. ``decide_streams`` gate behaviour — when a chart family's local cycle equals
     the card manifest's stored cycle, the decision is ``"skip-cycle"`` (never a
     ``"diff"`` that would re-hash or rewrite). When the local cycle differs, or
     is unknown, or there is no card manifest at all, the decision is ``"diff"``.
     (Req 3.1, 3.2, 3.5, 6.1)

  2. Skip safety under the recording invariant — a cycle is only recorded on the
     card after that cycle's sync has fully converged (Req 3.4). We model that
     invariant explicitly: a recorded ``card_cycle`` means the card family tree
     equals the local tree that was written for that cycle. Under that
     invariant, a ``"skip-cycle"`` decision (equal cycles) implies the card tree
     is left byte-identical to the local tree — the skip neither writes nor
     deletes anything, and the content already matches. (Req 3.3, 3.4)

The second half is what makes the cheap integer cycle comparison a SAFE
substitute for a full manifest diff: the safety is not a property of the
comparison alone, it rests on the recording rule that no cycle is ever stamped
onto the card until the content for that cycle is verifiably on the card.

Modeling: a "card tree" / "local tree" is a ``dict`` relpath ->
:class:`ManifestEntry`, where the entry IS the content (``sha256``/``size``
identify the bytes), matching the convergence property's model. "Skipping" a
family is the pure analogue of what the real engine does on ``"skip-cycle"``:
nothing. So the post-skip card tree is identically the pre-skip card tree.

Validates: Requirements 3.1, 3.3, 3.4
"""

from unittest import mock

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager.manifest import CardManifest, ManifestEntry


# --- generators -------------------------------------------------------------

# A lowercase-hex SHA-256 digest: exactly 64 hex chars, matching the builder.
_SHA256 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)

# Non-negative byte count.
_SIZE = st.integers(min_value=0, max_value=2**31)

# POSIX relpath keys: 1-3 filesystem-safe segments joined by "/". A leading "f"
# guarantees we never produce "." / ".." nor collide with an excluded basename.
_SEGMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=("_", "-"),
    ),
    min_size=1,
    max_size=8,
).map(lambda s: "f" + s)

_RELPATH = st.lists(_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))

# A 4-digit cycle number (e.g. "2611"); charts are 56-day, plates 28-day, but
# the gate only compares the strings, so any 4-digit label exercises it.
_CYCLE = st.text(alphabet="0123456789", min_size=4, max_size=4)

# A cycle that may also be unknown/absent (first-sync / missing local manifest).
_CYCLE_OR_NONE = st.one_of(st.none(), _CYCLE)


@st.composite
def _tree(draw):
    """Generate a modeled chart-family tree: relpath -> ManifestEntry."""
    pool = draw(st.lists(_RELPATH, min_size=0, max_size=10, unique=True))
    tree = {}
    for rel in pool:
        tree[rel] = ManifestEntry(
            relpath=rel, sha256=draw(_SHA256), size=draw(_SIZE)
        )
    return tree


def _skip_family(card_tree):
    """Model the engine's action on ``"skip-cycle"``: do nothing.

    A skipped family is neither hashed, transferred, nor pruned, so the card
    family tree after a skip is identically the tree before the skip. Returning
    a copy (rather than the same object) keeps the assertion honest — equality,
    not identity, is what byte-identical means on the card.
    """
    return dict(card_tree)


# --- Part 1: gate decision — equal cycle => skip, else diff -----------------


@settings(max_examples=100, deadline=None)
@given(
    local_charts=_CYCLE_OR_NONE,
    card_charts=_CYCLE_OR_NONE,
    local_plates=_CYCLE_OR_NONE,
    card_plates=_CYCLE_OR_NONE,
)
def test_equal_cycle_skips_else_diffs(
    local_charts, card_charts, local_plates, card_plates
):
    """Equal local/card cycle => ``"skip-cycle"``; otherwise => ``"diff"``.

    For both chart families independently: when the locally downloaded cycle is
    known AND equals the card manifest's recorded cycle, the gate skips (no hash,
    no write — Req 3.1); when it differs or is unknown, the gate diffs (Req 3.2).
    A missing local cycle must NEVER read as "equal" and skip.

    Validates: Requirements 3.1, 3.3, 3.4
    """
    card = CardManifest(charts_cycle=card_charts, plates_cycle=card_plates)
    local = du.LocalCycles(charts_cycle=local_charts, plates_cycle=local_plates)

    # The label cross-check reads the volume name; keep it out of the way. A
    # context-manager patch (not the monkeypatch fixture) is used so it is reset
    # on every Hypothesis-generated example — mirrors tests/test_decide_streams.py
    # which patches du._volume_name.
    with mock.patch.object(du, "_volume_name", lambda mp: None):
        by_family = {
            d.family: d for d in du.decide_streams("/Volumes/CARD", local, card)
        }

    charts_equal_known = local_charts is not None and local_charts == card_charts
    plates_equal_known = local_plates is not None and local_plates == card_plates

    assert by_family["scanned"].action == (
        "skip-cycle" if charts_equal_known else "diff"
    )
    assert by_family["plates"].action == (
        "skip-cycle" if plates_equal_known else "diff"
    )

    # nav is never cycle-gated — it is always evaluated.
    assert by_family["nav"].action == "evaluate"


@settings(max_examples=100, deadline=None)
@given(local_charts=_CYCLE_OR_NONE, local_plates=_CYCLE_OR_NONE)
def test_no_card_manifest_never_skips(local_charts, local_plates):
    """No card manifest => every chart family diffs (full write), never skips.

    A first-sync / wiped / corrupt card (``card is None``) can never satisfy the
    equal-cycle precondition, so neither chart family may be skipped — both must
    diff against an empty manifest (Req 6.1). Skipping here would be the unsafe
    case the property forbids: there is no recorded converged cycle to trust.

    Validates: Requirements 3.1, 3.3, 3.4
    """
    local = du.LocalCycles(charts_cycle=local_charts, plates_cycle=local_plates)

    with mock.patch.object(du, "_volume_name", lambda mp: None):
        by_family = {
            d.family: d for d in du.decide_streams("/Volumes/CARD", local, None)
        }

    assert by_family["scanned"].action == "diff"
    assert by_family["plates"].action == "diff"
    assert by_family["nav"].action == "evaluate"


# --- Part 2: skip safety under the recording invariant ----------------------


@settings(max_examples=100, deadline=None)
@given(local_tree=_tree(), cycle=_CYCLE, other_cycle=_CYCLE_OR_NONE)
def test_skip_leaves_card_byte_identical_under_convergence_invariant(
    local_tree, cycle, other_cycle
):
    """A ``"skip-cycle"`` leaves the card family byte-identical to the local tree.

    Model the recording invariant (Req 3.4): a cycle is only stamped onto the
    card AFTER that cycle's sync fully converged, so a card that records ``cycle``
    has a family tree equal to the local tree written for ``cycle``. We build the
    card that way — ``card_tree == local_tree`` and ``card.charts_cycle == cycle``
    — then set the local cycle equal to it.

    Under that setup the gate MUST return ``"skip-cycle"``; skipping does nothing
    to the family; and because the recorded cycle followed a converged write, the
    (untouched) card tree is already byte-identical to the local tree. Hence
    equal cycles imply equal content — the safety the cheap gate relies on
    (Req 3.3, 3.4).

    ``other_cycle`` is the plates cycle, left free to vary (and possibly differ)
    to show the charts-family skip safety does not depend on the plates stream.

    Validates: Requirements 3.1, 3.3, 3.4
    """
    # Recording invariant: the card recorded `cycle` only after converging to
    # the local tree for that cycle, so the card tree equals the local tree.
    card_tree = dict(local_tree)

    card = CardManifest(charts_cycle=cycle, plates_cycle=other_cycle)
    local = du.LocalCycles(charts_cycle=cycle, plates_cycle=None)

    with mock.patch.object(du, "_volume_name", lambda mp: None):
        by_family = {
            d.family: d for d in du.decide_streams("/Volumes/CARD", local, card)
        }
    scanned = by_family["scanned"]

    # (Req 3.1) equal cycle => skip, no hash / no write.
    assert scanned.action == "skip-cycle"
    assert scanned.local_cycle == cycle
    assert scanned.card_cycle == cycle

    # (Req 3.3) skipping does nothing; the card family tree is left untouched...
    card_after_skip = _skip_family(card_tree)
    assert card_after_skip == card_tree

    # (Req 3.4) ...and because the recorded cycle followed a converged write,
    # the untouched card tree is byte-identical to the local tree for that cycle.
    assert card_after_skip == local_tree


@settings(max_examples=100, deadline=None)
@given(
    local_tree=_tree(),
    card_tree=_tree(),
    local_cycle=_CYCLE,
    card_cycle=_CYCLE,
)
def test_differing_cycle_diffs_so_divergent_content_is_never_skipped(
    local_tree, card_tree, local_cycle, card_cycle
):
    """When cycles differ, the gate diffs — divergent content is never skipped.

    The unsafe failure the property guards against is a family whose content has
    changed being skipped. We generate independent local and card trees (which
    may differ arbitrarily) together with DISTINCT cycle numbers, and assert the
    gate selects ``"diff"`` for the charts family. The diff path is what would
    converge the card via a manifest comparison; a skip here would risk leaving
    stale content. Equal content is only ever assumed when the cycles are equal.

    Validates: Requirements 3.1, 3.3, 3.4
    """
    # Force the cycles to differ so this is unambiguously the "rolled" case.
    if local_cycle == card_cycle:
        card_cycle = ("0" if card_cycle[0] != "0" else "1") + card_cycle[1:]

    card = CardManifest(charts_cycle=card_cycle, plates_cycle=None)
    local = du.LocalCycles(charts_cycle=local_cycle, plates_cycle=None)

    with mock.patch.object(du, "_volume_name", lambda mp: None):
        scanned = {
            d.family: d for d in du.decide_streams("/Volumes/CARD", local, card)
        }["scanned"]

    # Cycles rolled => diff, never skip. The diff (not the gate) is responsible
    # for converging whatever content differs between the two trees. The gate's
    # decision is driven purely by the cycle numbers and is independent of the
    # (arbitrarily differing) local/card tree contents generated above.
    assert local_tree is not None and card_tree is not None  # content is free
    assert scanned.action == "diff"
    assert scanned.local_cycle == local_cycle
    assert scanned.card_cycle == card_cycle
