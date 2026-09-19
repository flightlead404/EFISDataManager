# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bug-condition exploration test for the prepare-drive-mount-swap bugfix.

Feature: prepare-drive-mount-swap, Property 1: Bug Condition - prepare/adopt
populate routes through the mount swap and does not race.

**Validates: Requirements 1.1, 1.2, 1.3**

This is a *bugfix exploration* property test. It encodes the EXPECTED (fixed)
behavior described by design.md Property 1, and it is EXPECTED TO FAIL on the
current (unfixed) code. Each failure is a counterexample proving the bug:

  (a) POPULATE GOES THROUGH THE SWAP — for both ``prepare_drive`` and
      ``adopt_drive``, the ``update_drive`` populate call must run INSIDE an
      ``msdos_mount(mount_point, ...)`` context and must receive the yielded
      private work-mount, never the raw FSKit ``/Volumes/<label>`` path. On the
      current code ``drive_updater`` never references ``msdos_mount`` and calls
      ``update_drive`` directly against the FSKit mount → this FAILS.

  (b) NO CONCURRENT AUTO-SYNC DURING PREPARE — while a prepare/adopt is in
      progress for a drive (recorded in an app-level "provisioning" guard set),
      the mount-detect handler ``_on_efis_drive_mounted`` must NOT launch a
      second archive+auto-sync for that same drive. On the current code no such
      guard exists and the handler unconditionally launches
      ``_run_archive`` (→ ``_run_drive_update``) → this FAILS.

Scoped-PBT rationale
--------------------
The defect is deterministic on the current code (the swap wiring is simply
absent and the guard does not exist), so the property is scoped to concrete
prepare/adopt orchestration scenarios rather than a broad input domain, per the
tasks.md "Scoped PBT Approach". Hypothesis drives >=100 iterations over a small
scenario strategy (which flow, which label). ``msdos_mount``, ``update_drive``,
and the poller are all mocked so no real device is touched (mirrors the mocking
style in ``tests/test_prop_mount_swap_never_orphaned.py`` and
``tests/test_prop_marker_only_on_clean_verify.py``).

Assertion (b) approach (documented)
-----------------------------------
``app.EFISDataManagerApp`` subclasses ``rumps.App`` and a full construction is
impractical to stand up in a unit test. We therefore invoke the REAL, unbound
``EFISDataManagerApp._on_efis_drive_mounted`` against a minimal fake ``self``
(a ``SimpleNamespace`` exposing the small set of attributes/methods the handler
touches PLUS the intended guard API ``_provisioning_drives`` + an
``_is_provisioning`` predicate). We mark the drive as provisioning-in-progress
and then assert the handler launches NO ``threading.Thread`` / ``_run_archive``
for it. Because the current handler contains no guard consultation at all, it
launches auto-sync regardless → the assertion fails, which correctly proves the
missing-guard defect (Defect 2). The same fake ``self`` will let the assertion
PASS once the fix adds the guard check to ``_on_efis_drive_mounted``.
"""

from types import SimpleNamespace
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from efis_data_manager import drive_updater as du


# A sentinel private work-mount the mocked msdos_mount yields. The FIXED code
# must hand THIS to update_drive; the raw FSKit /Volumes/<label> path must never
# reach update_drive.
WORK_MOUNT = "/private/tmp/efis-datamanager/EFIS_TEST"


class _RecordingSwap:
    """Records msdos_mount enter/exit and the order relative to update_drive.

    Acts as ``drive_updater.msdos_mount``: a callable returning a context
    manager. ``events`` is a shared list capturing the interleaving of
    ``msdos_mount`` ENTER/EXIT and the ``update_drive`` call, so the test can
    assert the populate ran strictly inside the swap.
    """

    def __init__(self, events, work_mount=WORK_MOUNT):
        self.events = events
        self.work_mount = work_mount
        self.entered = False
        self.called_with = None

    def __call__(self, mount_point, poller=None):
        # msdos_mount(mount_point, poller=...) → context manager.
        self.called_with = mount_point
        self.poller = poller
        return self

    def __enter__(self):
        self.entered = True
        self.events.append("msdos_mount:ENTER")
        return self.work_mount

    def __exit__(self, *exc):
        self.events.append("msdos_mount:EXIT")
        return False


def _stub_drive_updater_side_effects(monkeypatch_stack):
    """Patch the format/identity/diskutil/config steps so only orchestration runs.

    Returns nothing; enters patches on the supplied ``ExitStack`` so the caller
    controls their lifetime. update_drive and msdos_mount are patched by the
    caller (they are what the test observes).
    """
    # Identity + provenance writes are best-effort side effects irrelevant to
    # the orchestration invariant — stub them out.
    monkeypatch_stack.enter_context(
        mock.patch.object(du, "_ensure_identity", return_value="drive-id-xyz")
    )
    monkeypatch_stack.enter_context(
        mock.patch.object(du, "_safe_update_provenance", return_value=None)
    )
    monkeypatch_stack.enter_context(
        mock.patch.object(du, "_current_data_cycle", return_value=None)
    )
    # load_config is consulted by build_jobs inside update_drive; update_drive
    # is itself stubbed, but stub the config too for safety in case any orch
    # path reads it.
    monkeypatch_stack.enter_context(
        mock.patch.object(du, "load_config", lambda: {"usb_image_path": "/tmp/img"})
    )
    # os.makedirs (GRTCHARTS creation) must not touch the filesystem.
    monkeypatch_stack.enter_context(mock.patch("os.makedirs"))


# --- Assertion (a): populate goes through the swap --------------------------

# Scenario strategy: which explicit-provisioning flow, and the cosmetic label
# (which determines the raw FSKit /Volumes/<label> path). Deterministic defect,
# so a small scenario space exercised many times is sufficient (scoped PBT).
_flow = st.sampled_from(["prepare", "adopt"])
_label = st.sampled_from(["EFIS_TEST", "EFIS_4", "EFIS", "EFIS_3"])


@settings(max_examples=120)
@given(flow=_flow, label=_label)
def test_prop_prepare_adopt_populate_runs_inside_msdos_mount(flow, label):
    """Feature: prepare-drive-mount-swap, Property 1(a): populate inside the swap.

    For prepare_drive AND adopt_drive: msdos_mount must be ENTERED before
    update_drive is called, AND update_drive must receive the yielded work-mount
    (NOT the raw FSKit /Volumes/<label> path).

    EXPECTED ON UNFIXED CODE: FAILS — drive_updater never enters msdos_mount and
    update_drive is called with the raw FSKit mount.
    """
    from contextlib import ExitStack

    fskit_mount = f"/Volumes/{label}"
    events = []
    swap = _RecordingSwap(events)

    update_calls = []

    def _fake_update_drive(mount_point, families=None, progress_callback=None,
                           is_aborted=None):
        events.append("update_drive")
        update_calls.append(mount_point)
        # Minimal well-formed result so _summarize_update is happy.
        return {"jobs": {}, "errors": [], "aborted": False}

    with ExitStack() as stack:
        _stub_drive_updater_side_effects(stack)
        # update_drive is what we observe the mount_point argument on.
        stack.enter_context(
            mock.patch.object(du, "update_drive", side_effect=_fake_update_drive)
        )
        # msdos_mount does not exist on drive_updater in the unfixed code, so
        # patch with create=True. On the FIXED code drive_updater imports it and
        # this simply replaces the real one with our recorder.
        stack.enter_context(
            mock.patch.object(du, "msdos_mount", swap, create=True)
        )
        # _summarize_update should exist; leave it real (operates on our result).

        if flow == "prepare":
            # Drive the REAL prepare_drive body with the format/diskutil/remount
            # steps stubbed so control reaches the populate step.
            _run_real_prepare(stack, fskit_mount, label)
        else:
            du.adopt_drive(fskit_mount, progress_callback=None)

    # --- Property 1(a) assertions -------------------------------------------
    assert swap.entered, (
        f"[{flow}] BUG: msdos_mount was never entered — the populate ran on the "
        f"raw FSKit mount instead of the swap. update_drive mount_points="
        f"{update_calls}"
    )
    assert "msdos_mount:ENTER" in events and "update_drive" in events, (
        f"[{flow}] expected both msdos_mount ENTER and update_drive; events={events}"
    )
    assert events.index("msdos_mount:ENTER") < events.index("update_drive"), (
        f"[{flow}] BUG: update_drive was not called inside msdos_mount; "
        f"events={events}"
    )
    assert update_calls == [WORK_MOUNT], (
        f"[{flow}] BUG: update_drive received {update_calls}, expected the "
        f"yielded work-mount [{WORK_MOUNT!r}] — NOT the raw FSKit path "
        f"{fskit_mount!r}."
    )


def _run_real_prepare(stack, fskit_mount, label):
    """Drive the REAL prepare_drive body with format/remount stubbed.

    prepare_drive: diskutil info → eraseDisk (Popen) → wait for remount →
    GRTCHARTS → identity → update_drive. We stub the diskutil/format/remount so
    control reaches the populate step against ``fskit_mount``.
    """
    import subprocess as _sp

    # diskutil info -plist → returns a whole-disk identifier.
    def _fake_run(argv, *a, **k):
        # prepare_drive calls subprocess.run(["diskutil","info","-plist",vol])
        return _sp.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=(
                b"<?xml version='1.0'?><!DOCTYPE plist><plist version='1.0'>"
                b"<dict><key>DeviceIdentifier</key><string>disk9s1</string>"
                b"<key>ParentWholeDisk</key><string>disk9</string></dict></plist>"
            ),
        )

    class _FakeEraseProc:
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def communicate(self):
            return ("", "")

    stack.enter_context(mock.patch.object(du.subprocess, "run", side_effect=_fake_run))
    stack.enter_context(
        mock.patch.object(du.subprocess, "Popen", return_value=_FakeEraseProc())
    )
    # Remount discovery: prepare_drive waits for /Volumes/<label> via
    # os.path.isdir; make the expected path appear immediately.
    expected = f"/Volumes/{label}"
    stack.enter_context(
        mock.patch("os.path.isdir", side_effect=lambda p: p == expected)
    )
    stack.enter_context(mock.patch("time.sleep"))

    du.prepare_drive(fskit_mount, label=label, progress_callback=None)


# --- Assertion (b): no concurrent auto-sync during prepare ------------------


def _make_fake_app(provisioning_keys):
    """Build a minimal fake ``self`` for EFISDataManagerApp._on_efis_drive_mounted.

    Exposes only what the handler touches, plus the INTENDED guard API:
      * ``_provisioning_drives`` — the in-progress guard set (design's chosen
        name), and ``_is_provisioning(mount_point)`` predicate the fixed handler
        is expected to consult.
    The status/notify methods are inert recorders.
    """
    launched = {"threads": []}

    fake = SimpleNamespace(
        _provisioning_drives=set(provisioning_keys),
        _set_drive_status=lambda *a, **k: None,
        _set_status=lambda *a, **k: None,
        _notify=lambda *a, **k: None,
        _run_archive=lambda mount_point: launched["threads"].append(mount_point),
        _run_drive_update=lambda mount_point: launched["threads"].append(mount_point),
    )

    def _is_provisioning(mount_point):
        return mount_point in fake._provisioning_drives

    fake._is_provisioning = _is_provisioning
    return fake, launched


@settings(max_examples=120)
@given(label=_label)
def test_prop_no_autosync_launched_while_prepare_in_progress(label):
    """Feature: prepare-drive-mount-swap, Property 1(b): no concurrent auto-sync.

    While a prepare/adopt is in progress for a drive, _on_efis_drive_mounted
    must NOT launch archive/auto-sync for that same drive.

    EXPECTED ON UNFIXED CODE: FAILS — no guard exists, so the handler launches
    _run_archive via threading.Thread unconditionally.
    """
    from efis_data_manager.app import EFISDataManagerApp

    mount_point = f"/Volumes/{label}"
    fake, launched = _make_fake_app({mount_point})

    thread_targets = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kwargs):
            self._target = target
            self._args = args

        def start(self):
            thread_targets.append((self._target, self._args))

    with mock.patch("efis_data_manager.app.threading.Thread", _FakeThread), \
        mock.patch(
            "efis_data_manager.drive_updater.resolve_drive_id",
            return_value=None,
        ), \
        mock.patch(
            "efis_data_manager.drive_updater.pending_families",
            return_value=[],
        ):
        # Invoke the REAL handler logic against the fake self.
        EFISDataManagerApp._on_efis_drive_mounted(fake, mount_point)

    # Any thread started for this in-progress drive is a concurrent auto-sync.
    started_for_drive = [
        (t, args) for (t, args) in thread_targets if mount_point in args
    ]

    assert started_for_drive == [], (
        "BUG: _on_efis_drive_mounted launched a concurrent auto-sync for a "
        f"prepare-in-progress drive {mount_point!r} — no guard suppressed it. "
        f"Started threads (target, args)={thread_targets}"
    )
