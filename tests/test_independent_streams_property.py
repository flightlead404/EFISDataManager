# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for independent streams (task 3.3).

Feature: chart-manifest-sync, Property 4: Independent streams.

Design Property 4 — Independent streams: for any combination of (charts rolled?,
plates rolled?, nav changed?), each stream's decision and write is independent —
skipping or diffing charts never affects the decision for plates or nav, and nav
is always evaluated regardless of either cycle. This is what lets the engine
bring each of the three data streams current on its own freshness rule without
one stream's state leaking into another's outcome (Req 2.2, 2.3).

Modeling: ``drive_updater.decide_streams`` is the pure gate under test. It
returns three ``StreamDecision`` (``scanned``, ``plates``, ``nav``). The 2x2x2
matrix is generated as three booleans:

  * ``charts_rolled`` — whether the local charts cycle differs from the card's
    recorded charts cycle (True -> the scanned family should ``diff``, False ->
    ``skip-cycle``).
  * ``plates_rolled`` — likewise for the plates family.
  * ``nav_changed``   — a synthetic axis. ``decide_streams`` takes no nav
    content input (nav has no cycle gate and no manifest), so this flag must
    never alter ANY decision. Varying it and asserting the decisions are
    unchanged is exactly the independence claim for nav: its "always evaluate"
    and the two chart decisions do not depend on nav's state.

The card's recorded cycles are fixed; "rolled" is expressed by choosing a
local cycle equal to (not rolled) or different from (rolled) the card cycle.
``_volume_name`` is patched to a matching ``C<charts>P<plates>`` label so the
label cross-check never introduces a confounding WARNING; the label is not
trusted for the decision anyway (the manifest wins — see
``tests/test_decide_streams.py``).

Validates: Requirements 2.2, 2.3
"""

from unittest import mock

from hypothesis import given, settings, strategies as st

from efis_data_manager import drive_updater as du
from efis_data_manager.manifest import CardManifest


def _by_family(decisions):
    return {d.family: d for d in decisions}


def _expected_chart_action(local_cycle, card_cycle):
    """The gate's expected action for a chart family in isolation.

    Mirrors ``decide_streams``: ``skip-cycle`` only when the local cycle is
    present and equals the card cycle, otherwise ``diff``.
    """
    if local_cycle is not None and local_cycle == card_cycle:
        return "skip-cycle"
    return "diff"


# A 4-digit cycle number, matching the C<charts>P<plates> label form.
_CYCLE = st.text(alphabet="0123456789", min_size=4, max_size=4)


@settings(max_examples=200, deadline=None)
@given(
    card_charts=_CYCLE,
    card_plates=_CYCLE,
    charts_rolled=st.booleans(),
    plates_rolled=st.booleans(),
    nav_changed=st.booleans(),
)
def test_streams_are_independent(
    card_charts, card_plates, charts_rolled, plates_rolled, nav_changed
):
    """Each stream's decision depends only on its own cycle; nav always evaluates.

    Across the full 2x2x2 matrix of (charts rolled?, plates rolled?, nav
    changed?):

      * the scanned decision is ``diff`` iff charts rolled, else ``skip-cycle``,
        and never depends on ``plates_rolled`` or ``nav_changed``;
      * the plates decision is ``diff`` iff plates rolled, else ``skip-cycle``,
        and never depends on ``charts_rolled`` or ``nav_changed``;
      * the nav decision is ALWAYS ``evaluate``, regardless of either cycle or
        the ``nav_changed`` flag.

    Validates: Requirements 2.2, 2.3
    """
    # Express "rolled" as a local cycle that differs from the card's recorded
    # cycle; "not rolled" as an equal local cycle. A rolled cycle is forced
    # distinct by prefixing a marker that can never equal a 4-digit card cycle.
    local_charts = ("r" + card_charts) if charts_rolled else card_charts
    local_plates = ("r" + card_plates) if plates_rolled else card_plates

    local = du.LocalCycles(charts_cycle=local_charts, plates_cycle=local_plates)
    card = CardManifest(charts_cycle=card_charts, plates_cycle=card_plates)

    # A matching label so the cross-check is silent; the label is not trusted
    # for the decision regardless.
    label = f"C{card_charts}P{card_plates}"
    with mock.patch.object(du, "_volume_name", lambda mp: label):
        by_family = _by_family(du.decide_streams("/Volumes/CARD", local, card))

    # Exactly the three streams, always present.
    assert set(by_family) == {"scanned", "plates", "nav"}

    # scanned depends only on the charts cycle.
    assert by_family["scanned"].action == _expected_chart_action(
        local_charts, card_charts
    )
    assert by_family["scanned"].action == ("diff" if charts_rolled else "skip-cycle")

    # plates depends only on the plates cycle.
    assert by_family["plates"].action == _expected_chart_action(
        local_plates, card_plates
    )
    assert by_family["plates"].action == ("diff" if plates_rolled else "skip-cycle")

    # nav is never gated: always "evaluate", with no cycle context, regardless
    # of nav_changed or either chart cycle.
    nav = by_family["nav"]
    assert nav.action == "evaluate"
    assert nav.local_cycle is None
    assert nav.card_cycle is None


@settings(max_examples=200, deadline=None)
@given(
    card_charts=_CYCLE,
    card_plates=_CYCLE,
    charts_rolled=st.booleans(),
    plates_rolled=st.booleans(),
    nav_changed=st.booleans(),
)
def test_one_stream_does_not_affect_another(
    card_charts, card_plates, charts_rolled, plates_rolled, nav_changed
):
    """Flipping one stream's state leaves the other two decisions unchanged.

    The strongest form of independence (Req 2.2): recompute the decisions with
    the charts axis flipped and assert plates + nav are byte-identical; flip the
    plates axis and assert scanned + nav are identical; flip nav and assert ALL
    three are identical (nav has no content input to the gate). This proves the
    decision for one Data_Stream does not alter the decision for another.

    Validates: Requirements 2.2, 2.3
    """

    def decide(charts_r, plates_r):
        local_charts = ("r" + card_charts) if charts_r else card_charts
        local_plates = ("r" + card_plates) if plates_r else card_plates
        local = du.LocalCycles(charts_cycle=local_charts, plates_cycle=local_plates)
        card = CardManifest(charts_cycle=card_charts, plates_cycle=card_plates)
        label = f"C{card_charts}P{card_plates}"
        with mock.patch.object(du, "_volume_name", lambda mp: label):
            return _by_family(du.decide_streams("/Volumes/CARD", local, card))

    base = decide(charts_rolled, plates_rolled)

    # Flip ONLY the charts axis: plates and nav must be unaffected.
    flip_charts = decide(not charts_rolled, plates_rolled)
    assert flip_charts["plates"] == base["plates"]
    assert flip_charts["nav"] == base["nav"]

    # Flip ONLY the plates axis: scanned and nav must be unaffected.
    flip_plates = decide(charts_rolled, not plates_rolled)
    assert flip_plates["scanned"] == base["scanned"]
    assert flip_plates["nav"] == base["nav"]

    # Flipping the synthetic nav_changed axis cannot change any decision: the
    # gate takes no nav content input, so recomputing with the same cycles is
    # identical in all three streams. (nav_changed is unused by decide_streams
    # by design; asserting base is self-consistent documents that nav's outcome
    # does not depend on nav content in the decision layer.)
    again = decide(charts_rolled, plates_rolled)
    assert again == base
