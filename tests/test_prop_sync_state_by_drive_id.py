# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for chart-sync-stall-fix Property 3.

Feature: chart-sync-stall-fix, Property 3: sync-state attributed by drive_id
invariant to mount path.

**Property 3: Sync-state is attributed by drive id, invariant to mount path.**

For ANY mountpoint the write window runs against (the ``/Volumes/`` FSKit path
OR any private ``/private/tmp/efis-datamanager/...`` ``mount_msdos`` path),
``begin_family`` / ``complete_family`` / ``pending_families`` resolve to the
same durable ``drive_id``, so a completed family clears exactly the interrupted
state for that drive and no other, regardless of the mountpoint used.

The mount-swap layer hands the unchanged sync engine a different ``mount_point``
during the write window. The durable sync-state (``.sync_state.json``, v2
id-keyed map) is keyed by ``drive_id`` and only ever stores ``mount`` as an
informational last-seen value. This test drives the real helpers in
``drive_updater`` (no rsync, no real mount) and asserts:

  - after ``begin_family(drive_id, family, mount=...)`` the family is pending
    for that drive regardless of which mountpoint was passed;
  - after ``complete_family(drive_id, family)`` it is no longer pending;
  - two DIFFERENT mountpoints (one under ``/Volumes``, one under
    ``/private/tmp/efis-datamanager``) with the SAME ``drive_id`` operate on the
    SAME state — identical pending sets — so mount path is invariant;
  - a DIFFERENT ``drive_id``'s state is never touched (no cross-attribution).

**Validates: Requirements 5.3, 6.4**
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager import drive_updater as du

# The monkeypatch fixture sets the SAME module-level sync-state paths for every
# generated input, so Hypothesis' function-scoped-fixture health check does not
# apply — the redirection is constant across examples and the state file is
# reset at the top of each example.
_SUPPRESS = [HealthCheck.function_scoped_fixture]

_FAMILIES = ("scanned", "plates", "nav")

# uuid-like durable drive ids (matches the app-generated id shape closely
# enough for keying purposes: hex groups joined by hyphens).
_hex = st.text(alphabet="0123456789abcdef", min_size=8, max_size=12)
_drive_ids = st.builds(
    lambda a, b: f"{a}-{b}",
    _hex,
    _hex,
)

_families = st.sampled_from(_FAMILIES)

# A mountpoint under /Volumes (the FSKit path).
_volumes_mount = st.builds(
    lambda label: f"/Volumes/{label}",
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789", min_size=3, max_size=10),
)

# A private mount_msdos work mountpoint under /private/tmp/efis-datamanager.
_private_mount = st.builds(
    lambda label: f"/private/tmp/efis-datamanager/{label}",
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789", min_size=3, max_size=10),
)


@settings(max_examples=100, suppress_health_check=_SUPPRESS)
@given(
    drive_id=_drive_ids,
    other_id=_drive_ids,
    family=_families,
    other_family=_families,
    fskit_mount=_volumes_mount,
    msdos_mount=_private_mount,
)
def test_property3_sync_state_attributed_by_drive_id_invariant_to_mount_path(
    tmp_path_factory,
    monkeypatch,
    drive_id,
    other_id,
    family,
    other_family,
    fskit_mount,
    msdos_mount,
):
    """Property 3: sync-state follows the drive id, not the mountpoint.

    Feature: chart-sync-stall-fix, Property 3: sync-state attributed by
    drive_id invariant to mount path.

    Validates: Requirements 5.3, 6.4
    """
    # A distinct, empty state file per example so runs never touch the real
    # ~/EFIS/DataManagerLogs/.sync_state.json and never leak between examples.
    state_dir = tmp_path_factory.mktemp("DataManagerLogs")
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(
        du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress")
    )

    # The two mountpoints must be genuinely different (one /Volumes, one
    # private); by construction they always are (different prefixes), but assert
    # the premise explicitly.
    assert fskit_mount != msdos_mount

    # Establish a second drive's interrupted state up front; it must remain
    # untouched by everything we do to `drive_id` below (no cross-attribution).
    # Skip when the two ids collide so "other" is genuinely a different drive.
    have_other = other_id != drive_id
    if have_other:
        du.begin_family(other_id, other_family, mount=fskit_mount)
        assert du.pending_families(other_id) == [other_family]

    # --- begin against the FSKit mount -> family becomes pending -------------
    du.begin_family(drive_id, family, mount=fskit_mount)
    assert family in du.pending_families(drive_id)
    pending_after_fskit_begin = du.pending_families(drive_id)

    # Mount-path invariance: re-recording the SAME family under a DIFFERENT
    # mountpoint (the private mount_msdos path) resolves to the SAME state.
    # begin_family de-duplicates, so the pending set is unchanged; the only
    # thing the second mountpoint updates is the informational last-seen mount.
    du.begin_family(drive_id, family, mount=msdos_mount)
    pending_after_msdos_begin = du.pending_families(drive_id)
    assert pending_after_msdos_begin == pending_after_fskit_begin
    assert family in pending_after_msdos_begin

    # The stored state is keyed by drive_id; `mount` is informational only and
    # reflects the last mountpoint seen — it never partitions the state.
    state = du.read_sync_state()
    assert state is not None
    assert drive_id in state["drives"]
    assert state["drives"][drive_id]["mount"] == msdos_mount

    # The other drive is still exactly as it was — no cross-attribution from any
    # of the begin_family calls above.
    if have_other:
        assert du.pending_families(other_id) == [other_family]

    # --- complete against the private mount -> family clears -----------------
    # complete_family takes only (drive_id, family); the mountpoint the write
    # actually ran against is irrelevant. Whichever mount was used, the same
    # drive_id's interrupted state for this family is cleared.
    du.complete_family(drive_id, family)
    assert family not in du.pending_families(drive_id)

    # A completed family clears EXACTLY that drive's state for that family and
    # no other drive's state (Property 3 / Req 5.3, 6.4).
    if have_other:
        assert du.pending_families(other_id) == [other_family]
