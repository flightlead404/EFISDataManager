# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration test for the nav stream end-to-end (chart-manifest-sync task 7.2).

The integration-level companion to the 3.7 property test. Where the property
test (Property 7) asserts the checksum invariant over many generated inputs,
this test drives the REAL nav files job end-to-end via ``run_sync_job`` against
temp dirs standing in for the local image and the EFIS card mount, and asserts
the shipped ``_run_files_job`` behavior for NAV.DB / NAV-proc.DB is unchanged by
the manifest/cycle work:

  - Req 8.1: every insert evaluates each nav file by size + SHA-256 against the
    card copy, with no cycle gate and no manifest.
  - Req 8.2: a nav file whose content differs from the card copy is rewritten —
    including the --size-only gap (same byte size, different content).
  - Req 8.3: the family is verified by a DEEP checksum pass before the family is
    completed.

This exercises the production path (preflight -> per-file checksum copy ->
deep verify -> complete_family), not a stand-in sync_state: ``run_sync_job``
resolves the durable sync-state key itself and uses this module as the state
backend. rsync is not involved in the files job; real temp dirs are used and
the mount preflight is mocked because temp dirs are not real mount points.

Requirements: 8.1, 8.2, 8.3
"""

import os

import pytest

from efis_data_manager import drive_updater as du


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


@pytest.fixture(autouse=True)
def _mount_ok(monkeypatch):
    """Preflight passes for temp dirs: pretend they are writable mounts."""
    monkeypatch.setattr(du.os.path, "ismount", lambda p: True)


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
def env(tmp_path, monkeypatch, state_paths):
    """A local image with both nav files and an empty card to sync into."""
    local = tmp_path / "image"
    card = tmp_path / "card"

    _write(str(local / "NAV.DB"), b"nav-content")
    _write(str(local / "NAV-proc.DB"), b"navproc-content")

    card.mkdir()

    monkeypatch.setattr(du, "load_config", lambda: {"usb_image_path": str(local)})

    return {"local": local, "card": card, "mount": str(card)}


def _nav_job(env):
    return next(j for j in du.build_jobs(env["mount"]) if j.name == "nav")


# --- Req 8.1 / 8.3: first insert evaluates, copies, deep-verifies, completes --


def test_nav_first_sync_copies_both_files_and_completes(env, monkeypatch):
    """On an empty card both nav files are evaluated, written, DEEP-verified,
    and the family is completed — end-to-end through run_sync_job.
    """
    nav = _nav_job(env)

    # Spy on verify_family to prove the completion gate used a DEEP checksum
    # pass (Req 8.3) rather than a shallow size/count compare.
    seen = {}
    real_verify = du.verify_family

    def _spy_verify(job, mount, deep=False):
        seen["deep"] = deep
        return real_verify(job, mount, deep=deep)

    monkeypatch.setattr(du, "verify_family", _spy_verify)

    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    assert result.files_updated == 2
    assert result.verified is True
    assert result.errors == []

    # Both nav files landed on the card byte-for-byte (Req 8.1/8.2 write).
    assert (env["card"] / "NAV.DB").read_bytes() == b"nav-content"
    assert (env["card"] / "NAV-proc.DB").read_bytes() == b"navproc-content"

    # Deep checksum verify ran before completion (Req 8.3).
    assert seen["deep"] is True

    # No cycle gate / no manifest left any pending interrupted state.
    assert du.pending_families(env["mount"]) == []


# --- Req 8.1: an already-current card is a checksum no-op --------------------


def test_nav_second_sync_is_checksum_noop(env):
    """A re-run with identical content copies nothing: size + SHA-256 match."""
    nav = _nav_job(env)
    du.run_sync_job(nav, env["mount"])

    again = du.run_sync_job(nav, env["mount"])

    assert again.status == "current"
    assert again.files_updated == 0
    assert again.verified is True


# --- Req 8.2: the --size-only gap — same size, different content, rewritten --


def test_nav_rewrites_on_same_size_different_content(env):
    """A nav file mutated to the SAME byte size but different bytes is detected
    by SHA-256 and rewritten end-to-end (the correctness gap the feature closes).
    """
    nav = _nav_job(env)
    du.run_sync_job(nav, env["mount"])
    assert (env["card"] / "NAV.DB").read_bytes() == b"nav-content"

    new_bytes = b"NAV-CONTENT"  # same 11 bytes, different content
    assert len(new_bytes) == len(b"nav-content")
    _write(str(env["local"] / "NAV.DB"), new_bytes)

    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    assert result.files_updated == 1  # only NAV.DB changed
    assert result.verified is True
    assert (env["card"] / "NAV.DB").read_bytes() == new_bytes
    # The unchanged file stayed put.
    assert (env["card"] / "NAV-proc.DB").read_bytes() == b"navproc-content"


# --- Req 8.2: a differing-size change is rewritten too ----------------------


def test_nav_rewrites_on_different_size_content(env):
    """A nav file changed to a DIFFERENT size is rewritten; the other file,
    unchanged, is a no-op within the same end-to-end job.
    """
    nav = _nav_job(env)
    du.run_sync_job(nav, env["mount"])

    _write(str(env["local"] / "NAV-proc.DB"), b"navproc-content-grown-longer")

    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "updated"
    assert result.files_updated == 1  # only NAV-proc.DB changed
    assert (env["card"] / "NAV-proc.DB").read_bytes() == b"navproc-content-grown-longer"
    assert (env["card"] / "NAV.DB").read_bytes() == b"nav-content"


# --- Req 8.3: a deep-verify discrepancy fails the job without completing -----


def test_nav_deep_verify_failure_does_not_complete(env, monkeypatch):
    """If the deep checksum verify reports a discrepancy, the family is NOT
    completed and the job fails — the files are the marker, so content must
    match before completion (Req 8.3).
    """
    nav = _nav_job(env)

    monkeypatch.setattr(
        du,
        "verify_family",
        lambda job, mount, deep=False: {
            "missing": [],
            "extra": [],
            "size_mismatch": ["NAV.DB"],
        },
    )

    result = du.run_sync_job(nav, env["mount"])

    assert result.status == "failed"
    assert result.verified is False
    assert result.errors
    # Family left pending: deep-verify failure must not complete the stream.
    assert "nav" in du.pending_families(env["mount"])
