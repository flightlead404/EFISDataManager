# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the USB monitor poller quiesce (task 4.2 / Req 6.4).

The mount-swap layer unmounts the FSKit volume and remounts it via
``mount_msdos`` inside a private window, so ``/Volumes/`` churns (the managed
mount disappears then reappears). ``USBMonitor.pause()`` must make the 2s
poller ignore that churn entirely: NO mount/unmount/adoption-candidate
callbacks fire while paused. ``resume()`` must re-read ``/Volumes/`` and adopt
whatever is currently mounted as the EXISTING baseline, so the restored mount
is not seen as a new mount (no spurious auto-sync / redundant archive).

These tests never import ``app.py`` (which pulls in ``rumps``). They drive the
real ``USBMonitor._poll_loop`` body exactly once by monkeypatching
``usb_monitor._scan_volumes`` (to inject controlled ``/Volumes/`` state),
``time.sleep`` (to skip the 2s wait), and ``_running`` (so the ``while`` loop
runs a single iteration and then exits). Driving the actual loop body — rather
than a reimplementation — is what exercises the pause gate as shipped.
"""

from unittest import mock

from efis_data_manager import usb_monitor as um


def _make_monitor():
    """Build a USBMonitor with mock callbacks (no real thread)."""
    on_mount = mock.Mock(name="on_efis_mount")
    on_unmount = mock.Mock(name="on_efis_unmount")
    on_candidate = mock.Mock(name="on_adoption_candidate")
    mon = um.USBMonitor(
        on_efis_mount=on_mount,
        on_efis_unmount=on_unmount,
        on_adoption_candidate=on_candidate,
    )
    return mon, on_mount, on_unmount, on_candidate


def _run_one_poll_iteration(monkeypatch, mon, scan_result):
    """Drive the real _poll_loop body exactly once, then stop.

    ``scan_result`` is the (managed, candidates) tuple every ``_scan_volumes``
    call inside the iteration should see. ``time.sleep`` is stubbed to a no-op
    that flips ``_running`` False so the ``while self._running`` loop exits
    after one pass (the sleep is the first statement in the loop body, so the
    single guarded iteration still runs to completion).
    """
    monkeypatch.setattr(um, "_scan_volumes", lambda: scan_result)

    def _sleep_then_stop(_seconds):
        mon._running = False

    monkeypatch.setattr(um.time, "sleep", _sleep_then_stop)

    mon._running = True
    mon._poll_loop()


# --- paused: /Volumes/ churn fires no callbacks -----------------------------


def test_paused_ignores_volume_churn(monkeypatch):
    """With the monitor paused, injected /Volumes/ churn (a totally different
    managed + candidate set) fires NO mount/unmount/candidate callbacks and
    leaves the baseline untouched — resume() owns the baseline."""
    mon, on_mount, on_unmount, on_candidate = _make_monitor()

    # Known baseline before the swap: one managed mount, no candidates.
    baseline_managed = {"/Volumes/EFIS_1"}
    baseline_candidates: set[str] = set()
    mon._known_efis_mounts = set(baseline_managed)
    mon._known_candidates = set(baseline_candidates)

    mon.pause()
    assert mon._paused is True

    # Simulate churn: the managed mount vanished and an unrelated candidate
    # appeared — exactly the transient the swap produces. A reacting poller
    # would fire on_efis_unmount for EFIS_1 and on_adoption_candidate for the
    # newcomer. Paused, it must fire neither.
    churn = ({"/Volumes/SOMETHING_ELSE"}, {"/Volumes/WINDRIVE"})
    _run_one_poll_iteration(monkeypatch, mon, churn)

    on_mount.assert_not_called()
    on_unmount.assert_not_called()
    on_candidate.assert_not_called()

    # Baseline unchanged: the paused iteration must not commit the churn.
    assert mon._known_efis_mounts == baseline_managed
    assert mon._known_candidates == baseline_candidates


# --- resume: rebaseline restored mount as existing --------------------------


def test_resume_rebaselines_current_volumes(monkeypatch):
    """resume() clears _paused and adopts the current /Volumes/ scan as the
    existing baseline, so the restored managed mount is folded into
    _known_efis_mounts (and candidates into _known_candidates)."""
    mon, *_ = _make_monitor()

    # After the swap, /Volumes/ shows the restored managed mount again plus a
    # candidate that was present all along.
    restored = ({"/Volumes/EFIS_1"}, {"/Volumes/WINDRIVE"})
    monkeypatch.setattr(um, "_scan_volumes", lambda: restored)

    # Start paused (as the swap window would have left it).
    mon.pause()
    assert mon._paused is True

    mon.resume()

    assert mon._paused is False
    assert mon._known_efis_mounts == {"/Volumes/EFIS_1"}
    assert mon._known_candidates == {"/Volumes/WINDRIVE"}


def test_resume_then_poll_sees_no_new_mount(monkeypatch):
    """End-to-end: after resume() rebaselines the restored mount, a subsequent
    normal (unpaused) poll against the SAME scan result treats it as existing —
    on_efis_mount is NOT called (no spurious auto-sync)."""
    mon, on_mount, on_unmount, on_candidate = _make_monitor()

    restored = ({"/Volumes/EFIS_1"}, {"/Volumes/WINDRIVE"})

    # Swap restored the mount; resume() rebaselines it as existing.
    monkeypatch.setattr(um, "_scan_volumes", lambda: restored)
    mon.pause()
    mon.resume()

    # Now run one real, unpaused poll iteration with the same /Volumes/ state.
    _run_one_poll_iteration(monkeypatch, mon, restored)

    # The restored mount was already in the baseline -> treated as existing.
    on_mount.assert_not_called()
    on_unmount.assert_not_called()
    on_candidate.assert_not_called()


def test_unpaused_poll_still_detects_a_genuinely_new_mount(monkeypatch):
    """Sanity guard: the rebaseline does not deafen the poller permanently. A
    genuinely new managed mount appearing after resume() still fires
    on_efis_mount."""
    mon, on_mount, on_unmount, on_candidate = _make_monitor()

    # Resume with EFIS_1 as the baseline.
    monkeypatch.setattr(um, "_scan_volumes", lambda: ({"/Volumes/EFIS_1"}, set()))
    mon.pause()
    mon.resume()

    # A second managed drive now appears alongside the first.
    new_state = ({"/Volumes/EFIS_1", "/Volumes/EFIS_2"}, set())
    _run_one_poll_iteration(monkeypatch, mon, new_state)

    on_mount.assert_called_once_with("/Volumes/EFIS_2")
    on_unmount.assert_not_called()
    on_candidate.assert_not_called()


# --- thread-safety smoke: idempotency ---------------------------------------


def test_pause_is_idempotent(monkeypatch):
    """Calling pause() twice is safe and leaves the monitor paused."""
    mon, *_ = _make_monitor()
    mon._known_efis_mounts = {"/Volumes/EFIS_1"}

    mon.pause()
    mon.pause()

    assert mon._paused is True
    # pause() must not touch the baseline.
    assert mon._known_efis_mounts == {"/Volumes/EFIS_1"}


def test_resume_is_idempotent(monkeypatch):
    """Calling resume() twice is safe; it re-reads /Volumes/ each time and
    leaves the monitor unpaused with the current scan as the baseline."""
    mon, *_ = _make_monitor()

    monkeypatch.setattr(um, "_scan_volumes", lambda: ({"/Volumes/EFIS_1"}, set()))
    mon.pause()

    mon.resume()
    mon.resume()

    assert mon._paused is False
    assert mon._known_efis_mounts == {"/Volumes/EFIS_1"}
    assert mon._known_candidates == set()
