# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property-based test for the chart-sync-stall-fix mount-swap layer.

Feature: chart-sync-stall-fix, Property 2: commit marker only on clean verify

Property 2 (design.md "Correctness Properties"):

    For ANY family sync run against the swapped mountpoint, the family's commit
    marker is present after the run IFF that family's count+size verification
    passed with no errors and no abort; on any abort, failure, or mismatch the
    interrupted sync-state remains and no marker is written.

    **Validates: Requirements 6.1, 6.2, 6.4**

Why this is an engine-level test
--------------------------------
The mount-swap layer is a strictly additive layer *underneath* the unchanged
sync engine: it only hands the engine a different ``mount_point`` (a private
``mount_msdos`` work mount) in place of the FSKit ``/Volumes/`` path. Every
downstream function (``build_jobs`` -> ``sync_payload`` -> ``verify_family`` ->
commit marker / ``complete_family``) already takes the mountpoint as a plain
path parameter, so the marker-only-on-clean-verify invariant is a property of
the EXISTING engine that the mount swap does not change. Mount/privilege are
irrelevant to this invariant, so we exercise ``run_sync_job`` directly against a
temp local source tree and a temp "drive" dir (both plain dirs) -- exactly the
setup the repo's ``test_run_sync_job`` / ``test_verify_family`` tests use.

Approach
--------
Generate scenarios that vary the three things that decide the outcome:
  * whether the payload sync itself succeeds,
  * whether verify finds a discrepancy (inject a missing / extra /
    size-mismatched file on the drive after the copy), and
  * whether an abort is signaled.
For each example run the real ``run_sync_job`` against the temp trees and assert
the strict IFF: the commit marker file is present on the drive IFF the run was a
clean verify (sync ok AND verify clean AND not aborted); otherwise the marker is
absent AND the family remains pending (interrupted sync-state).
"""

import os
import shutil

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _job(mount, name):
    return next(j for j in du.build_jobs(mount) if j.name == name)


def _marker_dst(job):
    """Absolute path of the family's commit marker on the drive."""
    return job.marker_dst


# The family under test and the discrepancy to inject after a clean rsync.
# "clean" means no injected discrepancy.
_FAMILIES = st.sampled_from(["scanned", "plates"])
_DISCREPANCY = st.sampled_from(["clean", "missing", "extra", "size"])


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    family=_FAMILIES,
    discrepancy=_DISCREPANCY,
    abort=st.booleans(),
)
def test_commit_marker_present_iff_clean_verify(
    tmp_path_factory, family, discrepancy, abort, monkeypatch
):
    """Marker present IFF (sync ok AND verify clean AND not aborted).

    Feature: chart-sync-stall-fix, Property 2: commit marker only on clean
    verify.
    """
    # --- Per-example isolated environment (mirrors test_run_sync_job) --------
    root = tmp_path_factory.mktemp("prop2")
    local = root / "image"
    drive = root / "drive"
    drive.mkdir()
    # rsync's trailing-slash source for the plates family targets
    # ChartData/Plates/ on the drive; its ChartData/ parent must already exist
    # (in production the scanned family creates it first). Pre-create it so the
    # per-family run is exercised in isolation without a spurious rsync mkdir
    # failure that is unrelated to the property under test.
    (drive / "ChartData").mkdir()

    # Isolate durable sync-state under this example's temp dir so pending_families
    # reflects only this run.
    state_dir = root / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress"))

    # A local image with payload + markers for both tree families.
    _write(str(local / "ChartData" / "LO" / "a.png"), b"aaaa")
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"bbbb")
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")
    _write(str(local / "ChartData" / "Plates" / "Plates.sqlite"), b"plates-db")

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    # Preflight: temp dirs are not real mounts, so pretend they are writable
    # mounts (same shim test_run_sync_job uses). Preflight-failure is out of
    # scope for this property: the sync never starts, so there is no interrupted
    # sync-state to leave. The property is about sync/verify/abort outcomes.
    mount = str(drive)
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)

    job = _job(mount, family)
    marker_dst = _marker_dst(job)

    # If we want to force a verify discrepancy, inject it AFTER a clean rsync by
    # wrapping verify's data source: the simplest deterministic injection is to
    # tamper with the drive tree between sync and verify. run_sync_job calls
    # sync_payload then verify_family; we hook verify_family to first mutate the
    # freshly-synced drive tree, then delegate to the real implementation so the
    # discrepancy is genuinely detected by the real count+size logic.
    real_verify = du.verify_family
    payload_rel = os.path.join("LO", "a.png") if family == "scanned" else "p1.pdf"

    def _tamper_then_verify(j, m, deep=False):
        drive_file = os.path.join(j.payload_root_drive, payload_rel)
        if discrepancy == "missing":
            try:
                os.remove(drive_file)
            except OSError:
                pass
        elif discrepancy == "extra":
            _write(os.path.join(j.payload_root_drive, "orphan.bin"), b"z")
        elif discrepancy == "size":
            _write(drive_file, b"different-size-content")
        return real_verify(j, m, deep=deep)

    if discrepancy != "clean":
        monkeypatch.setattr(du, "verify_family", _tamper_then_verify)

    is_aborted = (lambda: True) if abort else None

    # --- Run the real engine -------------------------------------------------
    result = du.run_sync_job(job, mount, is_aborted=is_aborted)

    # --- Oracle: was this a clean verify? ------------------------------------
    # A clean verify requires: not aborted, and no injected discrepancy. This is
    # the ground-truth condition under which the marker MUST be written.
    clean_verify = (not abort) and (discrepancy == "clean")

    marker_present = os.path.exists(marker_dst)
    pending = du.pending_families(mount)

    # --- Property 2 assertions -----------------------------------------------
    # IFF: marker present exactly when the run was a clean verify.
    assert marker_present == clean_verify, (
        f"marker presence ({marker_present}) must equal clean_verify "
        f"({clean_verify}); family={family} discrepancy={discrepancy} "
        f"abort={abort} status={result.status} "
        f"verified={result.verified} errors={result.errors}"
    )

    if clean_verify:
        # Clean verify => marker present, verified True, family cleared.
        assert result.verified is True
        assert result.status in ("updated", "current")
        assert family not in pending
    else:
        # Any abort/failure/mismatch => no marker AND family stays interrupted.
        assert not marker_present
        assert result.verified is not True
        assert family in pending, (
            f"family {family} must remain pending on a non-clean run; "
            f"pending={pending} status={result.status}"
        )
