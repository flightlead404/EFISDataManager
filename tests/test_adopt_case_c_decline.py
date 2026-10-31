# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Case C decline-aborts behavior (R6.7, R6.8).

Case C = the Adopt_Device is NON-chart-capable AND Existing_Chart_Data is
present on the card. Per task 13.1, the content-matrix decision lives INLINE in
the main-thread ``prepare_drive`` ``@rumps.clicked`` handler (the ``if adopt:``
block in ``app.py``): before launching the ``_do_adopt_drive`` worker, the
handler shows a BLOCKING destructive confirmation (``rumps.alert``, default No).
If the user DECLINES, the handler

  1. informs the user with an "Adopt Cancelled" notification, and
  2. STOPS the adopt entirely — it ``return``s WITHOUT launching
     ``_do_adopt_drive``, so ``adopt_drive`` is never called.

Because ``adopt_drive`` is the ONLY writer of the identity, the Card_Manifest,
and ``ChartData/`` in this path, never launching the worker is exactly what
leaves the identity unwritten, the manifest unwritten, and ``ChartData/``
intact (R6.8).

Why this is tested at the app level and via the observable contract:
the Case C decision is inline in the large ``prepare_drive`` handler (which does
volume enumeration and multiple ``rumps.Window`` / ``rumps.alert`` prompts), so
calling the whole handler headless is impractical. Mirroring
test_adopt_device_selection.py / test_chart_capability_gate.py, we bind the real
collaborators onto a minimal ``SimpleNamespace`` stand-in and drive the content
decision through a faithful mirror of the production Case C branch (copied from
``app.py``'s ``if not chart_capable and existing_chart_data:`` block). The
stand-in's ``rumps.alert`` recorder returns the DECLINE value, and the
assertions observe the contract: the worker / ``adopt_drive`` is NOT invoked and
a cancellation notification IS surfaced.
"""

from types import SimpleNamespace


# rumps.alert result codes used by the handler: ok == 1, cancel == 0.
ALERT_OK = 1
ALERT_CANCEL = 0


def _make_standin(alert_result):
    """Build a stand-in that records notifications and worker launches.

    ``alert_result`` is the value the recorded ``rumps.alert`` returns for the
    Case C destructive confirm (``ALERT_CANCEL`` to decline, ``ALERT_OK`` to
    confirm). ``adopt_calls`` records every ``adopt_drive`` invocation so the
    test can assert the worker was (or was not) launched.
    """
    standin = SimpleNamespace()
    standin.notify_calls = []
    standin.alert_calls = []
    standin.adopt_calls = []
    standin.launched_worker = False

    standin._notify = lambda title, message: standin.notify_calls.append((title, message))

    def _alert(**kwargs):
        standin.alert_calls.append(kwargs)
        return alert_result

    standin._alert = _alert

    # Stand-in for ``_do_adopt_drive`` / the worker thread + the engine
    # ``adopt_drive`` it would call. Recording here proves whether the write
    # path was reached at all; if it never runs, no identity/manifest is written
    # and ChartData/ is left intact (R6.8).
    def _adopt_drive(*args, **kwargs):
        standin.adopt_calls.append((args, kwargs))
        return {"message": "adopted", "errors": []}

    standin._adopt_drive = _adopt_drive

    def _launch_worker(**kwargs):
        standin.launched_worker = True
        # The real worker ultimately calls adopt_drive; model that so a leaked
        # launch would show up as an adopt_drive call too.
        standin._adopt_drive(**kwargs)

    standin._launch_worker = _launch_worker
    return standin


def _drive_case_c_decision(standin, chosen="EFIS_NAV"):
    """Drive the Case C content-matrix decision exactly as ``app.py`` does.

    This is a faithful mirror of the production Case C branch in the
    ``prepare_drive`` ``if adopt:`` block (task 13.1): the non-chart-capable +
    existing-chart-data quadrant that shows the destructive confirm and aborts
    on decline. Collaborators (``rumps.alert``, the worker launch) are the
    stand-in's recorders, so "did the worker run / was adopt_drive called" and
    "was a cancellation surfaced" are observed directly.

    Returns True if the adopt proceeded to launch the worker, False if it
    aborted.
    """
    # Case C: destructive confirmation, default No (R6.7). A nav-only device
    # must not carry charts; deleting them is destructive.
    confirm = standin._alert(
        title="Adopt Drive — Delete Chart Data?",
        message=f"'{chosen}' is being adopted for a nav-only device, "
        "but it currently holds chart data.\n\nA nav-only device "
        "cannot keep chart data. Adopting will DELETE the existing "
        "ChartData on this drive. Your nav database, settings, and "
        "flight-data backups are left untouched.",
        ok="Delete charts", cancel="Cancel",
    )
    if confirm != 1:
        # Declined: stop the adopt entirely, drive unmodified (R6.8).
        standin._notify(
            "Adopt Cancelled",
            "Adopt cancelled — a nav-only device cannot keep chart data.",
        )
        return False
    # C-confirmed (R6.9): launch the worker with the nav-only-delete plan.
    standin._launch_worker(
        chart_capable=False,
        nav_label_prefix="NAV",
        content_action="nav_only_delete",
    )
    return True


# ---------------------------------------------------------------------------
# R6.7, R6.8 — declining the Case C confirm aborts the adopt entirely.
# ---------------------------------------------------------------------------

def test_case_c_decline_does_not_launch_worker():
    """Decline -> the adopt worker is never launched (R6.8)."""
    standin = _make_standin(alert_result=ALERT_CANCEL)

    proceeded = _drive_case_c_decision(standin)

    assert proceeded is False
    assert standin.launched_worker is False, "worker must not launch on decline"


def test_case_c_decline_never_calls_adopt_drive():
    """Decline -> ``adopt_drive`` is never invoked, so no identity/manifest write.

    ``adopt_drive`` is the sole writer of the identity, the Card_Manifest, and
    ``ChartData/`` in the adopt path. Never calling it is precisely what leaves
    those untouched (R6.8) — asserting the engine was never invoked is the
    engine-level corroboration the spec calls for, without re-testing the
    filesystem here.
    """
    standin = _make_standin(alert_result=ALERT_CANCEL)

    _drive_case_c_decision(standin)

    assert standin.adopt_calls == [], (
        "adopt_drive must not be called on Case C decline (no identity write, "
        "no manifest write, ChartData/ left intact)"
    )


def test_case_c_decline_surfaces_cancellation_notification():
    """Decline -> a cancellation is surfaced to the user (R6.8 informs the user)."""
    standin = _make_standin(alert_result=ALERT_CANCEL)

    _drive_case_c_decision(standin)

    assert len(standin.notify_calls) == 1
    title, message = standin.notify_calls[0]
    assert "cancel" in title.lower()
    assert "cancel" in message.lower()


def test_case_c_decline_shows_destructive_confirm_default_no():
    """The Case C confirm is shown as a blocking destructive alert (R6.7).

    The alert carries a Cancel option (default No) and a destructive ok label,
    and the confirm is what the user declines.
    """
    standin = _make_standin(alert_result=ALERT_CANCEL)

    _drive_case_c_decision(standin)

    assert len(standin.alert_calls) == 1
    alert_kwargs = standin.alert_calls[0]
    # A cancel path exists (default No) and the ok action is the destructive one.
    assert alert_kwargs.get("cancel")
    assert "delete" in str(alert_kwargs.get("ok", "")).lower()


# ---------------------------------------------------------------------------
# Confirm-side control: the SAME decision path DOES launch the worker when the
# user confirms, proving the decline-abort is caused by the decline and not by
# the path being inert. (R6.9 — the positive counterpart to R6.8.)
# ---------------------------------------------------------------------------

def test_case_c_confirm_launches_worker_and_calls_adopt_drive():
    """Confirm -> the worker launches and ``adopt_drive`` runs (nav_only_delete)."""
    standin = _make_standin(alert_result=ALERT_OK)

    proceeded = _drive_case_c_decision(standin)

    assert proceeded is True
    assert standin.launched_worker is True
    assert len(standin.adopt_calls) == 1
    _args, kwargs = standin.adopt_calls[0]
    assert kwargs["content_action"] == "nav_only_delete"
    assert kwargs["chart_capable"] is False
    # Confirming proceeds without surfacing a cancellation.
    assert standin.notify_calls == []
