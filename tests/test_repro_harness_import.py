# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Import + self-test smoke tests for ``repro_harness`` (task 1.4).

Task 1.2 severed the ``stall_hook`` wiring from
``src/efis_data_manager/repro_harness.py``: it dropped ``make_stall_hook``, the
``_sync_payload_with_hook`` wrapper, and the ``drive_updater.sync_payload``
monkeypatch, and made ``_detect_stall`` a permanent no-op returning
``(False, "stall detector removed")``. This module asserts the result still
holds together:

1. ``import efis_data_manager.repro_harness`` succeeds (clean import).
2. The module's pure-logic self-test passes — exercised in-process via
   :func:`repro_harness.self_test` (fast, no drive, no subprocess) AND through
   the ``--self-test`` CLI entry (:func:`repro_harness.main`) which must exit 0.
3. There is NO ``stall_hook`` wiring left: ``drive_updater.sync_payload`` has no
   ``stall_hook`` parameter, the harness exposes none of the removed
   wrapper/factory symbols, and ``_detect_stall`` is the no-op.

Self-test artifacts are redirected under ``./temp`` and cleaned up so no files
land in the user's ``~/EFIS/Reports`` tree.

Requirements: 1.6
"""

import importlib
import inspect
import os
import shutil

import pytest


def test_repro_harness_imports_cleanly():
    """`import efis_data_manager.repro_harness` succeeds (module imports)."""
    mod = importlib.import_module("efis_data_manager.repro_harness")
    # Re-import defensively so a stale cached import can't mask a breakage.
    importlib.reload(mod)
    assert mod is not None
    assert hasattr(mod, "self_test")
    assert hasattr(mod, "main")


def test_self_test_passes_in_process(tmp_path):
    """The in-process pure-logic self-test returns True, artifacts under ./temp."""
    from efis_data_manager import repro_harness

    # Keep the self-test's report artifacts out of ~/EFIS/Reports: route them
    # into a workspace ./temp subdir and clean up afterwards.
    temp_root = os.path.join(os.getcwd(), "temp")
    os.makedirs(temp_root, exist_ok=True)
    reports_dir = os.path.join(temp_root, "repro_harness_selftest")
    if os.path.isdir(reports_dir):
        shutil.rmtree(reports_dir, ignore_errors=True)
    try:
        assert repro_harness.self_test(reports_dir) is True
        # The self-test writes a markdown + json artifact; confirm it stayed
        # inside our ./temp sandbox.
        written = os.listdir(reports_dir)
        assert any(name.endswith(".md") for name in written)
        assert any(name.endswith(".json") for name in written)
    finally:
        shutil.rmtree(reports_dir, ignore_errors=True)


def test_self_test_cli_exit_zero():
    """`main(["--self-test", ...])` runs the self-test and exits 0."""
    from efis_data_manager import repro_harness

    temp_root = os.path.join(os.getcwd(), "temp")
    os.makedirs(temp_root, exist_ok=True)
    reports_dir = os.path.join(temp_root, "repro_harness_selftest_cli")
    if os.path.isdir(reports_dir):
        shutil.rmtree(reports_dir, ignore_errors=True)
    try:
        rc = repro_harness.main(["--self-test", "--reports-dir", reports_dir])
        assert rc == 0
    finally:
        shutil.rmtree(reports_dir, ignore_errors=True)


def test_sync_payload_has_no_stall_hook_parameter():
    """`drive_updater.sync_payload` no longer accepts a `stall_hook` parameter."""
    from efis_data_manager import drive_updater

    params = inspect.signature(drive_updater.sync_payload).parameters
    assert "stall_hook" not in params


def test_harness_has_no_stall_hook_wiring():
    """The severed stall_hook wiring symbols are gone and _detect_stall is a no-op."""
    from efis_data_manager import repro_harness

    # The removed wiring (task 1.2) exposed these module-level symbols; none may
    # remain.
    assert not hasattr(repro_harness, "make_stall_hook")
    assert not hasattr(repro_harness, "_sync_payload_with_hook")

    # _detect_stall is now a permanent no-op: it must accept arbitrary args and
    # report "no stall" without referencing any stall_hook.
    occurred, detail = repro_harness._detect_stall()
    assert occurred is False
    assert detail == "stall detector removed"
    # Permissive signature: extra positional / keyword args don't break it.
    assert repro_harness._detect_stall("x", foo=1) == (False, "stall detector removed")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
