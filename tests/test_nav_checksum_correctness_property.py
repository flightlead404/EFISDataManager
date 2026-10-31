# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property test for nav-stream checksum correctness (task 3.7).

Feature: chart-manifest-sync, Property 7: nav checksum correctness (preserved).

Design Property 7 — the manifest/cycle work (tasks 3.1-3.5) must NOT weaken the
nav stream. For ANY nav file whose content differs from the card copy, the sync
MUST rewrite that file to the card (Req 8.1, 8.2), and the family MUST be
verified by a DEEP checksum pass before it is completed (Req 8.3). nav carries
no cycle gate and no manifest — it is evaluated by size + SHA-256 on every
insert (Req 8.1, 2.1).

The critical case this property exists to prove is the ``--size-only`` gap: a
nav file edited to the EXACT same byte size but different content. A size-only
comparison would call those equal and silently keep the stale card copy; the
checksum path must detect the difference and rewrite. The generator therefore
deliberately mixes:

  - same-byte-size content changes (the gap case),
  - size-changing content changes,
  - identical content (must NOT be rewritten — no false positives), and
  - a card copy entirely absent (first insert — must be written).

Each example drives the real ``run_sync_job`` for the nav (``kind="files"``)
job against fresh temp dirs standing in for the local image and the card mount,
following the env-fixture pattern in ``tests/test_run_sync_job.py`` (mount
preflight is mocked because a temp dir is not a real mount point). To avoid the
Hypothesis + function-scoped-fixture reuse pitfall, every example builds its own
``tempfile.mkdtemp`` sandbox and tears it down afterward, and all module-level
state (sync-state paths, ``load_config``, mount/identity probes) is patched for
the duration of the single ``run_sync_job`` call.

A spy around ``verify_family`` records the ``deep`` flag and the call order
relative to ``complete_family`` so the test can assert the deep verify happened
BEFORE completion (Req 8.3).

Validates: Requirements 8.1, 8.2, 8.3
"""

import os
import shutil
import tempfile
from unittest import mock

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


# A nav file's bytes. min_size=1 so there is always a byte to flip for a
# same-size mutation.
_CONTENT = st.binary(min_size=1, max_size=256)

# How the card (drive) copy relates to the local copy for one nav file.
#   "absent"      -> no card copy exists yet (first insert; must be written)
#   "same"        -> card copy is byte-identical (must NOT be rewritten)
#   "same_size"   -> card copy differs but is the EXACT same byte size
#                    (the --size-only gap; must be rewritten by checksum)
#   "diff_size"   -> card copy differs and has a different size (must rewrite)
_CARD_STATE = st.sampled_from(["absent", "same", "same_size", "diff_size"])

# NAV.DB is always present locally; NAV-proc.DB is optional (mirrors real nav).
_NAV_FILES = ["NAV.DB", "NAV-proc.DB"]


def _card_bytes(local_bytes, state, data):
    """Return the bytes to seed on the card for a given relation ``state``.

    Returns ``None`` for ``"absent"`` (no card file). For the two "differs"
    states the returned bytes are guaranteed to differ from ``local_bytes``
    (and, for ``same_size``, to match its length exactly).
    """
    if state == "absent":
        return None
    if state == "same":
        return local_bytes
    if state == "same_size":
        mutated = bytearray(local_bytes)
        mutated[0] ^= 0xFF  # flip a byte in place -> same length, different bytes
        return bytes(mutated)
    # diff_size: a different length AND different content.
    other = data.draw(_CONTENT.filter(lambda c: c != local_bytes))
    # Force a length change so this case is distinct from same_size.
    if len(other) == len(local_bytes):
        other = other + b"\x00"
    return other


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    local_nav=st.fixed_dictionaries(
        {
            "NAV.DB": _CONTENT,  # always present locally
            "NAV-proc.DB": st.one_of(st.none(), _CONTENT),  # optional
        }
    ),
    card_states=st.fixed_dictionaries(
        {"NAV.DB": _CARD_STATE, "NAV-proc.DB": _CARD_STATE}
    ),
    data=st.data(),
)
def test_nav_differing_content_is_rewritten_and_deep_verified(
    local_nav, card_states, data
):
    """Any nav file differing from the card copy is rewritten; the family is
    verified by deep checksum before completion.

    Validates: Requirements 8.1, 8.2, 8.3
    """
    local_dir = tempfile.mkdtemp(prefix="efis_nav_local_")
    drive_dir = tempfile.mkdtemp(prefix="efis_nav_drive_")
    state_dir = tempfile.mkdtemp(prefix="efis_nav_state_")
    try:
        # --- Seed the local image (the source of truth) --------------------
        for name in _NAV_FILES:
            content = local_nav[name]
            if content is None:
                continue  # NAV-proc.DB may be absent locally
            with open(os.path.join(local_dir, name), "wb") as handle:
                handle.write(content)

        # --- Seed the card (drive) copies per the generated relation -------
        # Track which files should end up rewritten: a file that differs from
        # the card (absent, same_size, or diff_size) when it exists locally.
        expected_rewrites = set()
        for name in _NAV_FILES:
            content = local_nav[name]
            if content is None:
                # Not present locally -> nothing to sync for this file; also do
                # not seed a card copy (verify ignores files absent both sides).
                continue
            state = card_states[name]
            seed = _card_bytes(content, state, data)
            if seed is not None:
                with open(os.path.join(drive_dir, name), "wb") as handle:
                    handle.write(seed)
            if state != "same":
                expected_rewrites.add(name)

        # --- Spy on verify_family: record deep flag + order vs complete ----
        events = []
        real_verify = du.verify_family

        def _spy_verify(job, mount_point, deep=False):
            events.append(("verify", deep))
            return real_verify(job, mount_point, deep=deep)

        real_complete = du.complete_family

        def _spy_complete(drive_id, family):
            events.append(("complete", family))
            return real_complete(drive_id, family)

        nav_job = du.SyncJob(name="nav", kind="files", files=list(_NAV_FILES))

        with mock.patch.object(du, "load_config",
                               lambda: {"usb_image_path": local_dir}), \
             mock.patch.object(du, "SYNC_STATE_DIR", state_dir), \
             mock.patch.object(du, "SYNC_STATE_PATH",
                               os.path.join(state_dir, ".sync_state.json")), \
             mock.patch.object(du, "LEGACY_SYNC_MARKER_PATH",
                               os.path.join(state_dir, ".sync_in_progress")), \
             mock.patch.object(du.os.path, "ismount", lambda p: True), \
             mock.patch.object(du, "_volume_name", lambda mp: None), \
             mock.patch.object(du, "verify_family", _spy_verify), \
             mock.patch.object(du, "complete_family", _spy_complete):
            result = du.run_sync_job(nav_job, drive_dir)

        # --- Req 8.1 / 8.2: differing files were rewritten -----------------
        # The job must succeed and, when anything differed, report "updated".
        assert result.status in ("updated", "current"), result.errors
        assert result.verified is True, result.errors
        assert result.errors == []

        if expected_rewrites:
            assert result.status == "updated"
            assert result.files_updated == len(expected_rewrites), (
                f"expected {len(expected_rewrites)} rewrites "
                f"({sorted(expected_rewrites)}), got {result.files_updated}"
            )
        else:
            # Every present file already matched the card -> nothing rewritten.
            assert result.status == "current"
            assert result.files_updated == 0

        # After the sync every present local file is byte-identical on the card
        # (Req 8.2: a content difference, including a same-size one, is closed).
        for name in _NAV_FILES:
            content = local_nav[name]
            if content is None:
                continue
            drive_path = os.path.join(drive_dir, name)
            assert os.path.isfile(drive_path)
            with open(drive_path, "rb") as handle:
                assert handle.read() == content, (
                    f"card copy of {name} not rewritten to match local content"
                )

        # --- Req 8.3: deep-checksum verify ran BEFORE completion -----------
        deep_verifies = [i for i, (kind, val) in enumerate(events)
                         if kind == "verify" and val is True]
        completes = [i for i, (kind, _v) in enumerate(events)
                     if kind == "complete"]
        assert deep_verifies, "nav family was not verified by deep checksum"
        assert completes, "nav family was never completed"
        assert min(deep_verifies) < min(completes), (
            "deep checksum verify must run before the family is completed"
        )
    finally:
        shutil.rmtree(local_dir, ignore_errors=True)
        shutil.rmtree(drive_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
