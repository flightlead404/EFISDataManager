# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the flat-archive migration wiring at DM startup (settings-management
task 6.2; Requirements 11.1, 11.6).

``EFISDataManagerApp.__init__`` runs the one-time, idempotent
``migrate_flat_archives`` against the configured archive root exactly once at
launch. The migration is wrapped so a failure is logged and never blocks the
menu-bar app from starting.

Constructing the full rumps ``EFISDataManagerApp`` is impractical in a headless
test (it builds a live macOS menu-bar item), so — mirroring
``test_app_write_routing`` — these tests bind the REAL unbound
``_run_startup_migration`` to a minimal stand-in ``self`` and mock the migration
entry point.
"""

from types import SimpleNamespace
from unittest import mock

from efis_data_manager.app import EFISDataManagerApp
from efis_data_manager.settings_archive import MigrationResult


def test_startup_runs_migration_against_configured_archive_root():
    """Startup invokes migrate_flat_archives once with the config archive root."""
    standin = SimpleNamespace()

    result = MigrationResult(adopted=2, unresolved=1, collisions=0, errors=[])

    with mock.patch(
        "efis_data_manager.app.load_config",
        return_value={"archive_path": "/tmp/efis-archive"},
    ), mock.patch(
        "efis_data_manager.settings_archive.migrate_flat_archives",
        return_value=result,
    ) as mock_migrate:
        EFISDataManagerApp._run_startup_migration(standin)

    mock_migrate.assert_called_once()
    # Called with the configured archive root, as a Path.
    called_arg = mock_migrate.call_args[0][0]
    assert str(called_arg) == "/tmp/efis-archive"


def test_migration_failure_does_not_block_startup():
    """A migration that raises is swallowed — startup is never blocked (R11.6)."""
    standin = SimpleNamespace()

    with mock.patch(
        "efis_data_manager.app.load_config",
        return_value={"archive_path": "/tmp/efis-archive"},
    ), mock.patch(
        "efis_data_manager.settings_archive.migrate_flat_archives",
        side_effect=RuntimeError("archive volume offline"),
    ):
        # Must not raise.
        EFISDataManagerApp._run_startup_migration(standin)


def test_missing_archive_path_config_does_not_block_startup():
    """A config without an archive_path key is handled without raising."""
    standin = SimpleNamespace()

    with mock.patch(
        "efis_data_manager.app.load_config", return_value={}
    ), mock.patch(
        "efis_data_manager.settings_archive.migrate_flat_archives"
    ) as mock_migrate:
        # A KeyError inside the guarded block must be swallowed.
        EFISDataManagerApp._run_startup_migration(standin)

    mock_migrate.assert_not_called()
