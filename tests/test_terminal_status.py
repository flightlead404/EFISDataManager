# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the terminal status-line derivation (Task 10, Req 8.2/8.4).

`_terminal_status_from_jobs` is the pure status-aggregation helper extracted so
the orchestration logic can be tested without importing app.py (which pulls in
rumps/AppKit and cannot load in a headless test environment). It maps an
`update_drive` jobs dict + aborted flag into the single terminal status string
the menu-bar shows.

The rules it encodes:
  - any aborted job (or aborted flag) -> "Drive removed during update" (Req 7.3)
  - any failed family -> "<name(s)> update failed" (status names the family,
    Req 8.2)
  - all updated/current -> "Drive current" (no sticky error string, Req 8.4)

Requirements: 8.2, 8.4
"""

from efis_data_manager.drive_updater import JobResult, _terminal_status_from_jobs


def _jobs(**statuses):
    """Build a {name: JobResult} dict from name=status kwargs."""
    return {name: JobResult(name=name, status=status) for name, status in statuses.items()}


# --- all-clean terminal states -> current (no sticky error) -----------------


def test_all_updated_returns_current():
    jobs = _jobs(scanned="updated", plates="updated", nav="updated")
    assert _terminal_status_from_jobs(jobs, aborted=False) == "Drive current"


def test_all_current_returns_current():
    jobs = _jobs(scanned="current", plates="current", nav="current")
    assert _terminal_status_from_jobs(jobs, aborted=False) == "Drive current"


def test_mixed_updated_and_current_returns_current():
    jobs = _jobs(scanned="updated", plates="current", nav="current")
    assert _terminal_status_from_jobs(jobs, aborted=False) == "Drive current"


def test_empty_jobs_returns_current():
    # No families run (e.g. nothing stale) is a clean state, not an error.
    assert _terminal_status_from_jobs({}, aborted=False) == "Drive current"


# --- failed families name the family (Req 8.2) ------------------------------


def test_single_failed_family_names_it():
    jobs = _jobs(scanned="updated", plates="failed", nav="current")
    assert _terminal_status_from_jobs(jobs, aborted=False) == "plates update failed"


def test_multiple_failed_families_named_together():
    jobs = _jobs(scanned="failed", plates="failed", nav="current")
    status = _terminal_status_from_jobs(jobs, aborted=False)
    assert status == "scanned, plates update failed"


# --- aborted takes precedence over failed -----------------------------------


def test_aborted_job_status_takes_precedence():
    jobs = _jobs(scanned="updated", plates="aborted")
    assert (
        _terminal_status_from_jobs(jobs, aborted=False)
        == "Drive removed during update"
    )


def test_aborted_flag_takes_precedence_over_failed():
    # Even if a job reports failed, an aborted aggregate flag wins (drive removal
    # is the more informative user-facing cause).
    jobs = _jobs(scanned="failed")
    assert (
        _terminal_status_from_jobs(jobs, aborted=True)
        == "Drive removed during update"
    )
