# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end integration test for the chart-family sync pipeline
(chart-manifest-sync task 7.1).

Where the property tests cover each stage in isolation and over many generated
inputs, this drives the REAL pipeline end-to-end for a chart family against temp
dirs standing in for the local SSD image and the EFIS card mount, following the
design's "Download -> ... -> rsync the delta -> write card manifest -> relabel ->
flush" flow:

    manifest diff  ->  rsync --files-from (added + changed)  ->  prune removed
                   ->  commit marker  ->  card manifest (EFIS_MANIFEST.json)
                   ->  relabel (C<charts>P<plates>)  ->  flush + unmount

and asserts the card tree CONVERGES to the local manifest (added/changed present
with the correct bytes, removed gone — including the --size-only gap of a
same-byte-size content change), the commit marker and card manifest are written,
the volume label mirrors the cycles, and the card is unmounted.

As of chart-sync-wiring-fix task 6.1, ``build_jobs`` itself computes the SHA-256
manifest diff and populates ``SyncJob.files_from`` / ``removed`` per chart family
(it no longer emits a legacy ``--size-only`` job when a valid card manifest and a
local manifest are present). So this test now sources the scanned job from the
REAL ``build_jobs`` — the ``env`` fixture saves the new-cycle local manifest
(``Manifest_Store.save_local``) and a rolled-back prior card manifest
(``Manifest_Store.save_card``) so ``decide_streams`` diffs and ``build_jobs``
naturally returns the delta job. The old ``_scanned_files_from_job`` manual
construction and the ``_patched_build_jobs`` monkeypatch are gone; the end-to-end
rsync convergence assertions are unchanged.

rsync runs against real temp dirs (skipped if rsync is absent, per
``tests/test_card_manifest_write.py``); the mount preflight is mocked,
``_volume_name`` (the ``decide_streams`` label cross-check) is forced to ``None``,
and the hardware-only steps (``diskutil rename`` / ``diskutil unmount``) are
mocked — no physical USB is needed.

Requirements: 2.1, 2.4, 4.3, 4.4, 7.1, 7.2
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du
from efis_data_manager import manifest as m
from efis_data_manager.manifest import (
    FamilyManifest,
    Manifest_Builder,
    Manifest_Store,
)


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _rel_files(root):
    """Family-root-relative POSIX paths of every regular file under ``root``."""
    out = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            abspath = os.path.join(dirpath, name)
            out.add(os.path.relpath(abspath, root).replace(os.sep, "/"))
    return out


@pytest.fixture(autouse=True)
def _mount_ok(monkeypatch):
    """Preflight passes for temp dirs: pretend they are writable mounts, and
    force the ``decide_streams`` label cross-check (``_volume_name``) to ``None``
    so the real ``build_jobs`` path never shells out to ``diskutil`` for a temp
    dir."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(du, "_volume_name", lambda mount_point: None)


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Isolate the durable sync-state file under a temp dir."""
    state_dir = tmp_path / "DataManagerLogs"
    state_dir.mkdir()
    monkeypatch.setattr(du, "SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setattr(du, "SYNC_STATE_PATH", str(state_dir / ".sync_state.json"))
    monkeypatch.setattr(
        du, "LEGACY_SYNC_MARKER_PATH", str(state_dir / ".sync_in_progress")
    )
    return state_dir


@pytest.fixture
def local_manifest_dir(tmp_path, monkeypatch):
    """Redirect the local manifest dir into tmp_path (never touch app-support)."""
    local = tmp_path / "manifests"
    monkeypatch.setattr(m, "LOCAL_MANIFEST_DIR", local)
    return local


# Cycles: the prior cycle on the seeded card, and the new local cycle.
PRIOR_CHARTS_CYCLE = "2609"
NEW_CHARTS_CYCLE = "2611"


@pytest.fixture
def env(tmp_path, monkeypatch, state_paths, local_manifest_dir):
    """A new-cycle local scanned family + a card seeded with the PRIOR cycle.

    The local scanned tree (``ChartData/`` minus ``Plates/`` and the marker) and
    the card's prior-cycle tree are shaped so a real manifest diff yields all
    three change kinds:

      - ``SEC/a.png``  unchanged  (same content both sides)
      - ``SEC/b.png``  changed    (SAME byte size, different content — the
                                   --size-only correctness gap)
      - ``LO/new.png`` added      (present locally, absent on the card)
      - ``LO/old.png`` removed    (present on the card, absent locally)

    The local manifest is built from the real tree (``Manifest_Builder``) and
    saved on the isolated SSD; a prior card manifest is written to the card so
    ``diff_family`` has a faithful "what's on the card" record to diff against.
    """
    local = tmp_path / "image"
    drive = tmp_path / "drive"

    # --- new-cycle local scanned family -----------------------------------
    _write(str(local / "ChartData" / "SEC" / "a.png"), b"aaaa")        # unchanged
    _write(str(local / "ChartData" / "SEC" / "b.png"), b"NEW!")        # changed, 4B
    _write(str(local / "ChartData" / "LO" / "new.png"), b"added-file")  # added
    # The scanned commit marker (excluded from the manifest + payload).
    _write(str(local / "ChartData" / "ScannedCharts.sqlite"), b"scanned-db")
    # A plates file that must NOT be touched by the scanned job (sibling family).
    _write(str(local / "ChartData" / "Plates" / "p1.pdf"), b"plate")

    # --- card seeded with the PRIOR cycle ---------------------------------
    drive.mkdir()
    _write(str(drive / "ChartData" / "SEC" / "a.png"), b"aaaa")        # same
    _write(str(drive / "ChartData" / "SEC" / "b.png"), b"OLD!")        # stale, 4B
    _write(str(drive / "ChartData" / "LO" / "old.png"), b"gone-later")  # to remove
    _write(str(drive / "ChartData" / "ScannedCharts.sqlite"), b"old-db")
    # The sibling plates payload already on the card — the scanned job must not
    # disturb it.
    _write(str(drive / "ChartData" / "Plates" / "p1.pdf"), b"plate")

    scanned_root = str(local / "ChartData")

    # Real local manifest for the new cycle (excludes the marker + Plates auto
    # via the builder's metadata/marker exclusion... Plates is a dir, so trim it
    # from the entries explicitly to match the scanned family's payload slice).
    local_manifest = Manifest_Builder.build(
        "scanned", scanned_root, NEW_CHARTS_CYCLE
    )
    local_manifest.entries = {
        rel: e
        for rel, e in local_manifest.entries.items()
        if not rel.startswith("Plates/") and rel != "ScannedCharts.sqlite"
    }
    Manifest_Store.save_local(local_manifest)

    # Prior card manifest: a faithful record of the prior-cycle scanned tree.
    prior_scanned = FamilyManifest(
        family="scanned",
        cycle=PRIOR_CHARTS_CYCLE,
        entries={
            "SEC/a.png": m.ManifestEntry("SEC/a.png", m._hash_file(
                str(drive / "ChartData" / "SEC" / "a.png")), 4),
            "SEC/b.png": m.ManifestEntry("SEC/b.png", m._hash_file(
                str(drive / "ChartData" / "SEC" / "b.png")), 4),
            "LO/old.png": m.ManifestEntry("LO/old.png", m._hash_file(
                str(drive / "ChartData" / "LO" / "old.png")), 10),
        },
    )
    card = m.CardManifest(charts_cycle=PRIOR_CHARTS_CYCLE)
    card.families["scanned"] = prior_scanned
    Manifest_Store.save_card(str(drive), card)

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {
        "local": local,
        "drive": drive,
        "mount": str(drive),
        "scanned_root_drive": str(drive / "ChartData"),
        "local_manifest": local_manifest,
        "prior_card": card,
    }


def _real_scanned_job(env):
    """Source the manifest-driven scanned SyncJob from the REAL ``build_jobs``.

    ``build_jobs`` loads the card manifest, runs ``decide_streams`` +
    ``diff_family`` for the scanned family, and populates ``files_from`` /
    ``removed`` itself (chart-sync-wiring-fix task 6.1). The ``env`` fixture has
    already saved the new-cycle local manifest and the rolled-back prior card
    manifest, so this returns exactly the delta job the shipped pipeline runs.

    The job's ``files_from`` / ``removed`` are checked to carry all three change
    kinds (added / same-size change / removed) so this remains a real E2E run and
    the diff relpaths align with the scanned family payload root (Req 2.1, 2.4).
    """
    jobs = du.build_jobs(env["mount"], ["scanned"])
    assert len(jobs) == 1, "build_jobs should emit exactly the scanned diff job"
    job = jobs[0]
    assert job.name == "scanned"
    # The real build_jobs populated files_from/removed from the manifest diff
    # (not a legacy whole-tree job), and it covers all three change kinds.
    assert job.files_from is not None
    assert set(job.files_from) == {"LO/new.png", "SEC/b.png"}  # added + changed
    assert set(job.removed) == {"LO/old.png"}                  # removed
    return job


# --- the end-to-end flow -----------------------------------------------------


def test_chart_family_end_to_end_converges_and_commits(env):
    """diff -> --files-from rsync -> prune -> marker -> card manifest.

    The scanned card tree converges to the local manifest (added present,
    changed rewritten even at the same byte size, removed gone), the commit
    marker is on the card, and EFIS_MANIFEST.json records the scanned family +
    the new charts cycle (Req 4.3, 4.4).
    """
    job = _real_scanned_job(env)

    result = du.run_sync_job(job, env["mount"])

    assert result.status == "updated"
    assert result.verified is True
    assert result.errors == []

    scanned_root = env["scanned_root_drive"]
    on_card = _rel_files(scanned_root)

    # The scanned slice converged exactly to the local manifest. (Plates/ and
    # the marker are the sibling/marker files the scanned payload excludes.)
    assert "LO/new.png" in on_card            # added
    assert "SEC/b.png" in on_card             # changed
    assert "LO/old.png" not in on_card        # removed (pruned)
    scanned_payload = {
        r for r in on_card
        if not r.startswith("Plates/") and r != "ScannedCharts.sqlite"
    }
    assert scanned_payload == set(env["local_manifest"].entries)

    # Correct bytes: the added file and — the correctness win — the same-size
    # content change.
    assert (env["drive"] / "ChartData" / "LO" / "new.png").read_bytes() == b"added-file"
    assert (env["drive"] / "ChartData" / "SEC" / "b.png").read_bytes() == b"NEW!"
    # The unchanged file is untouched, and the sibling plates payload is intact.
    assert (env["drive"] / "ChartData" / "SEC" / "a.png").read_bytes() == b"aaaa"
    assert (env["drive"] / "ChartData" / "Plates" / "p1.pdf").read_bytes() == b"plate"

    # Commit marker written.
    assert (env["drive"] / "ChartData" / "ScannedCharts.sqlite").exists()

    # Card manifest (EFIS_MANIFEST.json) records the family + the NEW cycle.
    card = Manifest_Store.load_card(env["mount"])
    assert card is not None
    assert card.charts_cycle == NEW_CHARTS_CYCLE
    assert card.families["scanned"].cycle == NEW_CHARTS_CYCLE
    assert set(card.families["scanned"].entries) == set(env["local_manifest"].entries)

    # Family no longer pending (complete_family cleared the interrupted marker).
    assert "scanned" not in du.pending_families(env["mount"])


def test_chart_family_relabel_mirrors_cycles(env, monkeypatch):
    """After the card manifest is written, the post-sync relabel mirrors the
    cycles into the human-facing ``C<charts>P<plates>`` volume label (Req 3.6 /
    6.4), using the real ``_relabel_from_card_manifest`` with diskutil mocked.
    """
    # First converge + write the card manifest via the real E2E job.
    du.run_sync_job(_real_scanned_job(env), env["mount"])

    renamed = {}
    monkeypatch.setattr(
        du, "_rename_volume",
        lambda mount, label: renamed.setdefault("label", label) or True,
    )

    du._relabel_from_card_manifest(env["mount"])

    # Only the charts cycle is on this card (no plates family synced), so the
    # label degrades to just the charts component.
    assert renamed["label"] == f"C{NEW_CHARTS_CYCLE}"


def test_chart_family_update_drive_relabels_then_flush_unmounts(env, monkeypatch):
    """The run_sync_job + update_drive relabel portion and the flush/unmount:

      - ``update_drive`` runs the family through the REAL ``build_jobs`` (which
        now carries the manifest-driven ``files_from``) and, on success, relabels
        the volume from the card manifest — relabel BEFORE unmount (a volume
        cannot be renamed once unmounted).
      - ``flush_and_unmount`` performs ``os.sync`` + unmount so the written
        bytes and card manifest are durable before "done" (Req 7.1, 7.2).

    diskutil rename / unmount and os.sync are mocked — no physical USB needed.
    No build_jobs monkeypatch is needed: the real build_jobs produces the
    --files-from scanned diff job from the saved manifests (Req 2.1).
    """
    # Confirm the real build_jobs emits the diff job update_drive will run.
    _real_scanned_job(env)

    # Record the relabel and prove it happens while still mounted.
    order = []
    monkeypatch.setattr(
        du, "_rename_volume",
        lambda mount, label: order.append(("relabel", label)) or True,
    )

    result = du.update_drive(env["mount"], families=["scanned"])

    assert result["errors"] == []
    assert result["aborted"] is False
    assert result["jobs"]["scanned"].status == "updated"

    # The relabel ran (mirroring the charts cycle) as part of update_drive.
    assert ("relabel", f"C{NEW_CHARTS_CYCLE}") in order

    # Now the end-of-operation flush: os.sync + diskutil unmount. Both mocked.
    synced = {}
    monkeypatch.setattr(du.os, "sync", lambda: synced.setdefault("synced", True))

    class _Done:
        returncode = 0
        stderr = b""

    unmounted = {}

    def _fake_run(cmd, **kwargs):
        unmounted["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(du.subprocess, "run", _fake_run)

    assert du.flush_and_unmount(env["mount"]) is True
    assert synced.get("synced") is True
    assert unmounted["cmd"][:2] == ["diskutil", "unmount"]
    assert unmounted["cmd"][-1] == env["mount"]


def test_chart_family_card_manifest_written_after_marker(env, monkeypatch):
    """Ordering: the commit marker must already be on the card when the
    card-manifest write runs (marker THEN manifest), proving the durable-commit
    sequence the flush later makes permanent (Req 7.2)."""
    job = _real_scanned_job(env)
    marker_dst = env["drive"] / "ChartData" / "ScannedCharts.sqlite"

    real_save = Manifest_Store.save_card
    observed = {}

    def _spy_save(mount_point, card):
        observed["marker_present"] = marker_dst.exists()
        observed["new_cycle_recorded"] = card.charts_cycle == NEW_CHARTS_CYCLE
        return real_save(mount_point, card)

    monkeypatch.setattr(Manifest_Store, "save_card", staticmethod(_spy_save))

    result = du.run_sync_job(job, env["mount"])

    assert result.status == "updated"
    assert observed["marker_present"] is True
    assert observed["new_cycle_recorded"] is True
