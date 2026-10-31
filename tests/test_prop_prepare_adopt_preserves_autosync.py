# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preservation property tests for the prepare-drive-mount-swap bugfix.

Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync
and sync semantics unchanged.

Observation-first methodology: these tests capture the ACTUAL behavior of the
current (UNFIXED) code for inputs where the bug condition does NOT hold — a
normal managed-drive mount with no prepare/adopt in progress, and the
identity-before-populate ordering + update_drive call contract of
prepare_drive/adopt_drive. They MUST PASS on the unfixed code; that confirms the
baseline the fix must preserve (see bugfix.md Regression Prevention 3.1-3.4 and
design.md Preservation Requirements).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Every device / mount / subprocess boundary is mocked (mirroring the mocking
style in ``tests/test_adopt_drive.py``), so no real device is ever touched.
Assertions (2)/(3) call ``prepare_drive``/``adopt_drive``. With chart-manifest-
sync the mount-swap path was retired: ``prepare_drive``/``adopt_drive`` now run
``update_drive(mount_point, ...)`` DIRECTLY against the FSKit ``/Volumes/``
mount, so there is no swap to mock. These assertions are about identity-ordering
and the ``update_drive`` call contract (progress_callback passed through,
families default None). ``update_drive`` is stubbed so it does no real work.
"""

import os
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager import app as app_module
from efis_data_manager import drive_updater as du
from efis_data_manager.app import EFISDataManagerApp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StandInApp:
    """Minimal stand-in exposing only what ``_on_efis_drive_mounted`` touches.

    Constructing the full rumps ``EFISDataManagerApp`` is impractical in a
    headless test (it builds a live macOS menu-bar item), so we bind the REAL
    unbound ``EFISDataManagerApp._on_efis_drive_mounted`` to this stand-in. The
    decision logic under test (launch ``_run_archive`` on a thread) is therefore
    the genuine production code path, not a re-implementation.
    """

    # Guard API consulted by the fixed ``_on_efis_drive_mounted``: the handler
    # calls ``self._is_provisioning(mount_point)`` and returns early if True.
    # This stand-in is the NON-prepare baseline — nothing is provisioning — so
    # ``_provisioning_drives`` is empty and ``_is_provisioning`` returns False,
    # which means a normal managed mount MUST still launch auto-sync.
    _provisioning_drives = set()

    def __init__(self):
        self.status_calls = []
        self.drive_status_calls = []
        self.notify_calls = []
        self.archive_targets = []

    def _is_provisioning(self, mount_point):
        # Non-prepare baseline: nothing is being provisioned, so the handler
        # must proceed and launch auto-sync.
        return False

    def _resolve_and_gate_drive(self, mount_point):
        # multi-device-settings (task 7.1) inserted a resolution gate before
        # the archive thread starts: a RESOLVED outcome returns True to proceed.
        # This stand-in is the baseline managed-mount case, so it resolves and
        # proceeds, preserving the original "auto-sync launches once" behavior
        # under test. The resolution logic itself is covered by
        # tests/test_device_resolution.py and tests/test_learn_prompt_integration.py.
        self.resolve_calls = getattr(self, "resolve_calls", [])
        self.resolve_calls.append(mount_point)
        return True

    def _set_status(self, msg):
        self.status_calls.append(msg)

    def _set_drive_status(self, msg):
        self.drive_status_calls.append(msg)

    def _notify(self, title, msg):
        self.notify_calls.append((title, msg))

    def _run_archive(self, mount_point):
        # Recorded only so we can assert it is the thread target; never actually
        # invoked (threading.Thread is patched out).
        self.archive_targets.append(mount_point)


class _RecordingThread:
    """Stand-in for ``threading.Thread`` that records target/args and never runs.

    ``.start()`` is a no-op so no background work happens; the test inspects the
    captured ``target``/``args`` instead.
    """

    instances = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.started = False
        _RecordingThread.instances.append(self)

    def start(self):
        self.started = True


def _invoke_on_mounted(mount_point, drive_id="drive-xyz", pending=False):
    """Call the real ``_on_efis_drive_mounted`` bound to a stand-in app.

    ``threading.Thread`` and the drive_updater id/pending helpers are patched so
    nothing real runs. Returns ``(standin_app, [RecordingThread, ...])``.
    """
    standin = _StandInApp()
    _RecordingThread.instances = []

    with mock.patch.object(app_module.threading, "Thread", _RecordingThread), \
        mock.patch.object(du, "resolve_drive_id", return_value=drive_id), \
        mock.patch.object(du, "pending_families", return_value=pending):
        EFISDataManagerApp._on_efis_drive_mounted(standin, mount_point)

    return standin, list(_RecordingThread.instances)


# --------------------------------------------------------------------------- #
# Baseline 1 — normal managed mount launches auto-sync exactly once
# (Requirement 3.1)
# --------------------------------------------------------------------------- #


# A managed-drive label -> mount path under /Volumes. macOS may disambiguate a
# label with a numeric suffix, so allow an optional suffix; the label chars are
# constrained to a realistic volume-name alphabet.
_label = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip("_") != "")


@settings(max_examples=200)
@given(label=_label, pending=st.booleans())
def test_prop_normal_managed_mount_launches_autosync_once(label, pending):
    """Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync and sync semantics unchanged.

    For a managed-drive mount event with NO prepare/adopt in progress,
    ``_on_efis_drive_mounted`` launches exactly one background thread whose
    target is ``_run_archive`` for that mount point. This is the behavior the
    fix must preserve for a non-prepare mount (Requirement 3.1). ``pending`` is
    varied because pending-families only affects informational logging on the
    current code and must not change the launch behavior.
    """
    mount_point = os.path.join("/Volumes", label)

    standin, threads = _invoke_on_mounted(mount_point, pending=pending)

    # Exactly one background thread was launched...
    archive_threads = [
        t for t in threads if t.target == standin._run_archive
    ]
    assert len(archive_threads) == 1, (
        f"expected exactly one _run_archive thread, got {len(archive_threads)}"
    )
    t = archive_threads[0]
    # ...targeting _run_archive for THIS mount point, started, as a daemon.
    assert t.args == (mount_point,)
    assert t.started is True
    assert t.daemon is True


def test_normal_managed_mount_launches_autosync_example():
    """Concrete example: a managed EFIS mount auto-syncs once (Requirement 3.1)."""
    standin, threads = _invoke_on_mounted("/Volumes/EFIS_4")

    archive_threads = [t for t in threads if t.target == standin._run_archive]
    assert len(archive_threads) == 1
    assert archive_threads[0].args == ("/Volumes/EFIS_4",)
    assert archive_threads[0].started is True


# --------------------------------------------------------------------------- #
# Shared fixture-style patching for prepare/adopt orchestration observation
# --------------------------------------------------------------------------- #


def _run_prepare(mount_point, label, progress_callback=None, order=None):
    """Drive ``prepare_drive`` with format/diskutil/identity/update stubbed.

    ``order`` (a list) records ``"identity"`` and ``"update"`` in call order.
    Returns the ``update_drive`` call recorder dict.
    """
    calls = {}

    def _fake_ensure_identity(mp):
        (order if order is not None else []).append("identity")
        return "id-123"

    def _fake_update_drive(mp, families=None, progress_callback=None, **kw):
        (order if order is not None else []).append("update")
        calls["mount_point"] = mp
        calls["families"] = families
        calls["progress_callback"] = progress_callback
        calls["extra_kwargs"] = kw
        # Minimal clean result so _summarize_update reports success.
        return {
            "jobs": {
                "scanned": du.JobResult(
                    name="scanned", status="updated", verified=True
                ),
            },
            "errors": [],
            "aborted": False,
        }

    # Fake `diskutil info -plist` so prepare_drive resolves a whole disk without
    # touching a real device, and a fake `diskutil eraseDisk` Popen that exits 0.
    import plistlib
    import subprocess as sp

    info_plist = plistlib.dumps(
        {"DeviceIdentifier": "disk9s1", "ParentWholeDisk": "disk9"}
    )

    def _fake_run(cmd, *a, **k):
        class _P:
            returncode = 0
            stdout = info_plist
            stderr = b""
        return _P()

    class _FakeEraseProc:
        def __init__(self, *a, **k):
            self.returncode = 0

        def poll(self):
            return 0  # already done -> skip the heartbeat loop

        def wait(self, timeout=None):
            return 0

        def communicate(self):
            return ("", "")

    with mock.patch.object(du, "_ensure_identity", _fake_ensure_identity), \
        mock.patch.object(du, "update_drive", _fake_update_drive), \
        mock.patch.object(du, "_safe_update_provenance", lambda *a, **k: None), \
        mock.patch.object(du, "_current_data_cycle", lambda: "2601"), \
        mock.patch.object(du.subprocess, "run", _fake_run), \
        mock.patch.object(du.subprocess, "Popen", _FakeEraseProc), \
        mock.patch.object(du.os.path, "isdir", lambda p: p == mount_point), \
        mock.patch.object(du.os, "makedirs", lambda *a, **k: None), \
        mock.patch.object(du.time, "sleep", lambda *a, **k: None):
        result = du.prepare_drive(
            os.path.join("/Volumes", "UNTITLED"),
            label=label,
            progress_callback=progress_callback,
        )

    calls["result"] = result
    return calls


def _run_adopt(mount_point, progress_callback=None, order=None):
    """Drive ``adopt_drive`` with identity/update/diskutil/manifest stubbed.

    chart-sync-wiring-fix (task 12.1) made ``adopt_drive`` keyword-only in
    ``chart_capable`` / ``nav_label_prefix`` / ``content_action`` and inserted a
    ``Manifest_Store.save_card`` BETWEEN the identity write and the populate. We
    drive the ``content_action="fresh"`` branch (chart-capable, no existing data)
    because it is the plain "write identity, then update_drive(all families)"
    path whose identity-before-populate ordering and update_drive delegation
    shape these tests assert. ``save_card`` is mocked to a no-op so the unit test
    never touches disk; it records ``"save_card"`` in ``order`` only to prove it
    lands between identity and update, not to change what is asserted.
    """
    calls = {}

    def _fake_ensure_identity(mp, **kw):
        # Production now passes chart_capable=/nav_label_prefix= kwargs.
        (order if order is not None else []).append("identity")
        return "id-123"

    def _fake_update_drive(mp, families=None, progress_callback=None, **kw):
        (order if order is not None else []).append("update")
        calls["mount_point"] = mp
        calls["families"] = families
        calls["progress_callback"] = progress_callback
        calls["extra_kwargs"] = kw
        return {
            "jobs": {
                "scanned": du.JobResult(
                    name="scanned", status="updated", verified=True
                ),
            },
            "errors": [],
            "aborted": False,
        }

    def _fake_save_card(mp, card):
        (order if order is not None else []).append("save_card")

    from efis_data_manager import manifest as _manifest

    with mock.patch.object(du, "_ensure_identity", _fake_ensure_identity), \
        mock.patch.object(du, "update_drive", _fake_update_drive), \
        mock.patch.object(du, "_safe_update_provenance", lambda *a, **k: None), \
        mock.patch.object(du, "_current_data_cycle", lambda: "2601"), \
        mock.patch.object(
            _manifest.Manifest_Store, "save_card", staticmethod(_fake_save_card)
        ), \
        mock.patch.object(du.os, "makedirs", lambda *a, **k: None):
        result = du.adopt_drive(
            mount_point,
            chart_capable=True,
            nav_label_prefix="HXR",
            content_action="fresh",
            progress_callback=progress_callback,
        )

    calls["result"] = result
    return calls


# --------------------------------------------------------------------------- #
# Baseline 2 — identity is written BEFORE populate (Requirement 3.2)
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(label=_label)
def test_prop_prepare_writes_identity_before_populate(label):
    """Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync and sync semantics unchanged.

    ``prepare_drive`` calls ``_ensure_identity`` BEFORE ``update_drive`` for any
    label (Requirement 3.2). The fix keeps identity before the swap, so this
    ordering must remain true.
    """
    mount_point = os.path.join("/Volumes", label)
    order = []
    _run_prepare(mount_point, label, order=order)

    assert "identity" in order and "update" in order
    assert order.index("identity") < order.index("update"), order


@settings(max_examples=100)
@given(
    label=st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
        min_size=1,
        max_size=12,
    ).filter(lambda s: s.strip("_") != "")
)
def test_prop_adopt_writes_identity_before_populate(label):
    """Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync and sync semantics unchanged.

    ``adopt_drive`` calls ``_ensure_identity`` BEFORE ``update_drive``
    (Requirement 3.2).
    """
    mount_point = os.path.join("/Volumes", label)
    order = []
    _run_adopt(mount_point, order=order)

    assert "identity" in order and "update" in order
    assert order.index("identity") < order.index("update"), order


def test_prepare_identity_before_populate_example():
    order = []
    _run_prepare("/Volumes/EFIS_4", "EFIS_4", order=order)
    assert order == ["identity", "update"]


def test_adopt_identity_before_populate_example():
    order = []
    _run_adopt("/Volumes/EFIS_4", order=order)
    # task 12.1 inserted a save_card between identity and the populate; the
    # invariant under test is only that identity is written BEFORE update_drive.
    assert order == ["identity", "save_card", "update"]
    assert order.index("identity") < order.index("update")


# --------------------------------------------------------------------------- #
# Baseline 3 — update_drive call contract is delegated unchanged
# (Requirements 3.2, 3.4)
# --------------------------------------------------------------------------- #


def test_prepare_delegates_to_update_drive_with_expected_shape():
    """Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync and sync semantics unchanged.

    ``prepare_drive`` invokes ``update_drive`` with the progress_callback passed
    through and families defaulted (None). The verify/marker/sync-state
    semantics live inside ``update_drive`` and are delegated unchanged
    (Requirements 3.2, 3.4).
    """
    def _cb(_msg):
        pass

    calls = _run_prepare("/Volumes/EFIS_4", "EFIS_4", progress_callback=_cb)

    assert calls["progress_callback"] is _cb
    assert calls["families"] is None
    assert calls["extra_kwargs"] == {}
    assert calls["result"]["success"] is True


def test_adopt_delegates_to_update_drive_with_expected_shape():
    """Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync and sync semantics unchanged.

    ``adopt_drive`` invokes ``update_drive`` with the progress_callback passed
    through and families defaulted (None) (Requirements 3.2, 3.4).
    """
    def _cb(_msg):
        pass

    calls = _run_adopt("/Volumes/EFIS_4", progress_callback=_cb)

    assert calls["progress_callback"] is _cb
    assert calls["families"] is None
    # task 12.1 threads the labeling snapshot through as chart_capable=; the
    # delegation contract under test is progress_callback pass-through +
    # families default None, so only assert the new kwarg is the capability.
    assert calls["extra_kwargs"] == {"chart_capable": True}
    assert calls["result"]["success"] is True


@settings(max_examples=100)
@given(label=_label)
def test_prop_prepare_update_drive_families_default(label):
    """Feature: prepare-drive-mount-swap, Property 2: Preservation - managed auto-sync and sync semantics unchanged.

    Across labels, ``prepare_drive`` always calls ``update_drive`` with
    ``families`` defaulted to None (every family stale on a fresh drive)
    (Requirement 3.4).
    """
    calls = _run_prepare(os.path.join("/Volumes", label), label)
    assert calls["families"] is None
