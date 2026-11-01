# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared pytest fixtures / test isolation.

Autouse isolation of the LOCAL (SSD-side) chart manifest store. The real store
lives under the user's Application Support dir
(``manifest.LOCAL_MANIFEST_DIR``). Tests that build sync jobs
(``build_jobs`` -> ``_chart_family_job``) read it via
``Manifest_Store.load_local``; if a REAL local manifest happens to exist there
(e.g. after a real chart download), those entries leak into tiny test fixtures
and make ``build_jobs`` emit a manifest ``--files-from`` job listing real-world
relpaths against a 2-file temp image — breaking otherwise-hermetic unit tests.

Redirecting ``LOCAL_MANIFEST_DIR`` to a fresh per-test tmp dir makes every test
hermetic: ``load_local`` finds nothing unless the test itself writes a manifest.
Tests that DO exercise the manifest path write their own manifests after this
fixture runs (autouse fixtures run before the test's own fixtures that depend on
them only if ordered; here we also allow a test/fixture to re-``setattr`` the
same attribute, which simply points at its own tmp dir).
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_manifest_dir(tmp_path, monkeypatch):
    """Point the SSD-side local-manifest store at a per-test tmp dir."""
    try:
        from efis_data_manager import manifest as _mf
    except Exception:  # manifest not importable in some minimal envs
        return
    monkeypatch.setattr(_mf, "LOCAL_MANIFEST_DIR", tmp_path / "_local_manifests")
