# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for flush-means-done (task 6.3).

Feature: chart-manifest-sync, Property 6: Flush-means-done.

Design Property 6 — Flush-means-done: for any successful sync, when the
top-level auto-sync operation reports "done", the written bytes and the card
manifest are already durable on the card — ``os.sync`` has run and the card has
been unmounted — so a *subsequent* read of the card sees exactly what the sync
wrote. An aborted or errored run does NOT flush+unmount: it leaves a family
pending with a stale manifest, so the card must stay mounted for the retry on
the next insert (Req 7.1, 7.2).

Where this lives in the real code (anchored by task 6.2): the clean "done" path
of ``app._run_drive_update`` calls ``self._flush_and_unmount(mount_point)`` as
its FINAL step before setting the terminal status; ``_flush_and_unmount``
delegates to ``drive_updater.flush_and_unmount``, which does ``os.sync()`` THEN
``diskutil unmount`` (NOT eject). The flush deliberately lives at the top-level
operation's end, never inside ``update_drive`` (which prepare/adopt reuse and
which must stay mounted).

Modeling. We drive the REAL unbound ``_run_drive_update`` bound to a minimal
``SimpleNamespace`` stand-in (mirroring tests/test_app_write_routing.py), and
mock the sync-engine entry points so nothing real is written. Two things are
modeled instead of mocked away:

  * **A card** with a buffered write cache and a durable store. ``update_drive``
    (mocked) writes the family payload into the card's *buffer*; the card
    becomes durable (buffer flushed into the persistent store) only when
    ``os.sync`` runs; a subsequent *read* of the card returns the durable store.
    This is the model of "a subsequent read sees what was written" — it only
    holds if ``os.sync`` ran before the read.

  * **An ordered operation log** recording the sequence of (write, os.sync,
    unmount, report-done) events, so we can assert ordering: on the clean path
    os.sync precedes unmount which precedes the "done" status; the write
    precedes all three.

The generated matrix is the full space of successful and unsuccessful sync
shapes: any combination of pending (interrupted) families to repair and stale
families to update, each either completing cleanly or aborting. "Successful"
means the modeled run finishes on the clean terminal path; "unsuccessful" means
the engine reported ``aborted``/errors (or a verify+repair left the card dirty).

Validates: Requirements 7.1, 7.2
"""

from types import SimpleNamespace
from unittest import mock

from hypothesis import given, settings, strategies as st

from efis_data_manager.app import EFISDataManagerApp


MOUNT = "/Volumes/EFIS_3"

# The chart/nav family names the engine knows; any non-empty subset is a valid
# stale/pending set. (nav is never cycle-gated but is a legitimate family here.)
_FAMILIES = ("charts", "plates", "nav")

_FAMILY_SET = st.lists(
    st.sampled_from(_FAMILIES), min_size=0, max_size=3, unique=True
)

# A small payload of relpath -> content bytes that a family "write" deposits in
# the card's buffer. Content is opaque; equality of the read-back dict is what
# "a subsequent read sees exactly what was written" means.
_PAYLOAD = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters=("_", "-")
        ),
        min_size=1,
        max_size=8,
    ).map(lambda s: "f" + s),
    values=st.binary(min_size=0, max_size=16),
    min_size=0,
    max_size=6,
)


class _ModelCard:
    """A card with a write buffer and a durable store, plus an op log.

    A family "write" lands in ``buffer``. ``os.sync`` flushes ``buffer`` into the
    durable ``store``. A ``read`` returns the durable ``store`` — i.e. what a
    subsequent insert would see — which equals the full set of writes ONLY if
    ``os.sync`` ran after the last write. ``unmount`` detaches the volume. Every
    step is appended to ``ops`` so ordering can be asserted.
    """

    def __init__(self):
        self.buffer = {}
        self.store = {}
        self.mounted = True
        self.ops = []

    def write(self, payload):
        self.buffer.update(payload)
        self.ops.append("write")

    def os_sync(self):
        # Flush buffered writes to the durable store (what a later read sees).
        self.store.update(self.buffer)
        self.ops.append("sync")

    def unmount(self):
        self.mounted = False
        self.ops.append("unmount")

    def read(self):
        """What a subsequent insert reads: only durably-stored bytes."""
        return dict(self.store)


def _make_standin(card, all_payloads):
    """Build the `_run_drive_update` stand-in wired to a `_ModelCard`.

    ``_flush_and_unmount`` is the REAL contract under test, re-implemented here
    against the model exactly as ``drive_updater.flush_and_unmount`` behaves:
    ``os.sync`` THEN unmount. (The real method is a one-line delegate to that
    function; its own unit tests in test_app_write_routing.py pin the delegation
    and the os.sync-then-unmount ordering. Here we model the durability effect so
    the "subsequent read sees what was written" half of Property 6 is checked.)
    """
    standin = SimpleNamespace()
    standin.status_calls = []

    def _set_status(msg):
        standin.status_calls.append(msg)
        card.ops.append(("status", msg))

    standin._set_status = _set_status
    standin._notify = lambda title, msg: None
    standin._device_chart_capable = lambda mount_point: True
    standin._usb_monitor = None
    standin._with_active_watchdog = lambda mount, run: run(lambda: False)

    def _flush_and_unmount(mount_point):
        # Mirror drive_updater.flush_and_unmount: os.sync first (durable), then
        # unmount. This is the "flush = done" step under test.
        card.os_sync()
        card.unmount()
        return True

    standin._flush_and_unmount = _flush_and_unmount
    standin._all_payloads = all_payloads
    return standin


def _patched_engine(card, payloads, pending, aborted):
    """Context managers mocking the sync-engine entry points against the model.

    * ``resolve_drive_id`` / ``pending_families`` -> supplied interrupted set.
    * ``check_drive_currency`` -> the stale families are not current.
    * ``verify_drive`` -> clean repair (unless there is nothing to repair); it
      "writes" the pending families' payloads into the card buffer, modeling the
      repair rewriting files + card manifest.
    * ``update_drive`` -> writes the stale families' payloads into the buffer and
      reports ``aborted`` as given.
    * ``_terminal_status_from_jobs`` -> "Drive current" on a clean run, a removal
      status on an aborted run (matching the real terminal mapping).
    """
    stale = [f for f in payloads if f not in pending]

    currency = {
        "is_current": False,
        "families": {f: {"current": False} for f in payloads},
        "stale_items": [f"{f} data" for f in stale],
    }

    def _verify(mount_point, families, repair, progress_callback=None, is_aborted=None):
        for fam in families:
            card.write(payloads.get(fam, {}))
        return {"clean": True, "families": {}}

    def _update(
        mount_point,
        families,
        progress_callback=None,
        is_aborted=None,
        chart_capable=None,
    ):
        # task 11.2 threads chart_capable into update_drive; accept it so the
        # real _run_drive_update's call does not raise and fall to the error
        # path. The value does not affect what this model records.
        for fam in families:
            card.write(payloads.get(fam, {}))
        jobs = {f: SimpleNamespace(files_updated=len(payloads.get(f, {}))) for f in families}
        return {"jobs": jobs, "errors": [], "aborted": aborted}

    terminal = "Drive removed during update" if aborted else "Drive current"

    return (
        mock.patch(
            "efis_data_manager.drive_updater.resolve_drive_id", return_value="DID-1"
        ),
        mock.patch(
            "efis_data_manager.drive_updater.pending_families",
            return_value=list(pending),
        ),
        mock.patch(
            "efis_data_manager.drive_updater.check_drive_currency",
            return_value=currency,
        ),
        mock.patch(
            "efis_data_manager.drive_updater.verify_drive", side_effect=_verify
        ),
        mock.patch(
            "efis_data_manager.drive_updater.update_drive", side_effect=_update
        ),
        mock.patch(
            "efis_data_manager.drive_updater._terminal_status_from_jobs",
            return_value=terminal,
        ),
    )


@settings(max_examples=100, deadline=None)
@given(
    stale=_FAMILY_SET,
    pending=_FAMILY_SET,
    payload=_PAYLOAD,
    aborted=st.booleans(),
)
def test_done_implies_durable_and_readable(stale, pending, payload, aborted):
    """Done on the clean path => os.sync + unmount ran first, read sees writes.

    Across the full matrix of (interrupted families, stale families, abort?):

      * If the run reaches the clean terminal "Drive current" status, then
        ``os.sync`` ran, then ``unmount`` ran, and both happened BEFORE the
        "done" status was reported; and a subsequent read of the card returns
        exactly the bytes that were written (durability — Req 7.1).
      * If the run did not complete cleanly (aborted/errored, or there was no
        write work at all), the clean-path flush+unmount contract does not apply;
        when it WAS aborted with real write work, the card is NOT flushed and
        stays mounted (Req 7.2).

    Validates: Requirements 7.1, 7.2
    """
    # Build the set of families with work and give each a distinct payload so the
    # read-back can be checked against the union of what was written.
    families_with_work = sorted(set(stale) | set(pending))
    payloads = {}
    expected_written = {}
    for i, fam in enumerate(families_with_work):
        # Namespacing keys by family keeps writes from different families
        # distinct in the modeled card store.
        fam_payload = {f"{fam}/{k}": v for k, v in payload.items()} or {f"{fam}/_": b""}
        payloads[fam] = fam_payload
        expected_written.update(fam_payload)

    card = _ModelCard()
    standin = _make_standin(card, expected_written)

    # Only pending families that actually have work are "interrupted"; the stale
    # set is everything with work that is not pending.
    pending_eff = [f for f in pending if f in payloads]

    cms = _patched_engine(card, payloads, pending_eff, aborted)
    with cms[0], cms[1], cms[2], cms[3], cms[4], cms[5]:
        EFISDataManagerApp._run_drive_update(standin, MOUNT)

    reported_done = "Drive current" in standin.status_calls
    did_flush = "sync" in card.ops and "unmount" in card.ops

    # --- No write work at all: no-op path, no flush, not the clean "done" we
    #     are reasoning about (it reports current without any sync/unmount). ---
    if not families_with_work:
        assert not did_flush
        assert card.read() == {}
        return

    if reported_done:
        # Clean "done" path: the flush ran and in the right order.
        assert did_flush, "a clean done must flush+unmount"

        # Ordering: write(s) precede os.sync, os.sync precedes unmount, and the
        # "Drive current" status is reported only AFTER both.
        sync_i = card.ops.index("sync")
        unmount_i = card.ops.index("unmount")
        first_write_i = card.ops.index("write")
        done_i = max(
            i
            for i, op in enumerate(card.ops)
            if isinstance(op, tuple) and op == ("status", "Drive current")
        )
        assert first_write_i < sync_i < unmount_i < done_i

        # Durability: a subsequent read sees EXACTLY what was written, and the
        # card is no longer mounted.
        assert card.read() == expected_written
        assert card.mounted is False
    else:
        # Not a clean done. The only way to have work but not report current is
        # an aborted update (verify is modeled clean, so a pending-only run with
        # no stale families still reports current). An aborted run must NOT have
        # flushed/unmounted — the card stays mounted for the retry (Req 7.2).
        assert aborted
        assert not did_flush
        assert card.mounted is True


@settings(max_examples=100, deadline=None)
@given(stale=st.lists(st.sampled_from(_FAMILIES), min_size=1, max_size=3, unique=True),
       payload=_PAYLOAD)
def test_aborted_update_never_flushes_and_stays_mounted(stale, payload):
    """An aborted update with real stale work leaves the card mounted, unflushed.

    This isolates the Req 7.2 half: when ``update_drive`` reports ``aborted`` (a
    drive pulled mid-write), the clean-path flush+unmount is skipped entirely, so
    the bytes are NOT forced durable and the mount is NOT released — the family
    stays pending with a stale manifest and is retried on the next insert. A
    subsequent read therefore does NOT reflect the interrupted write as durable.

    Validates: Requirements 7.1, 7.2
    """
    payloads = {}
    for fam in stale:
        payloads[fam] = {f"{fam}/{k}": v for k, v in payload.items()} or {f"{fam}/_": b""}

    card = _ModelCard()
    standin = _make_standin(card, {})

    cms = _patched_engine(card, payloads, pending=[], aborted=True)
    with cms[0], cms[1], cms[2], cms[3], cms[4], cms[5]:
        EFISDataManagerApp._run_drive_update(standin, MOUNT)

    # No flush, still mounted, nothing durably stored (os.sync never ran).
    assert "sync" not in card.ops
    assert "unmount" not in card.ops
    assert card.mounted is True
    assert card.read() == {}
    assert "Drive current" not in standin.status_calls
