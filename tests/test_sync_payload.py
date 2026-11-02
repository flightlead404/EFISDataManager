# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the tree-job payload sync (sync_payload) in drive_updater.

Covers Task 4 of the drive-sync-integrity spec:
  - rsync converges a temp source -> dest (efficient size+mtime delta)
  - a second run with no changes transfers nothing (idempotency)
  - the family commit marker is excluded from the payload copy
  - rsync failures are captured into errors and logged at ERROR
  - the is_aborted predicate terminates the transfer and records an abort

Requirements: 4.1, 4.2, 8.1

These tests run rsync against real temporary directories (no physical USB).
"""

import logging
import os
import shutil

import pytest

from efis_data_manager import drive_updater as du


pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not available on PATH"
)


def _tree_job(src_dir, dst_dir, excludes=None):
    """Build a minimal tree SyncJob pointing at two temp dirs."""
    return du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src_dir) + os.sep,
        payload_root_drive=str(dst_dir) + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES) + (excludes or []),
    )


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _rel_files(root):
    """Set of file paths relative to root (posix separators)."""
    out = set()
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            out.add(rel.replace(os.sep, "/"))
    return out


# --- convergence + idempotency ---------------------------------------------
#
# manifest-only-chart-sync R2.1 retired the legacy ``--size-only`` whole-tree
# job: sync_payload is now manifest-only and a ``files_from is None`` tree job is
# rejected loudly. These invariants (convergence, no-op, marker/metadata
# exclusion) are re-expressed against the manifest ``--files-from`` path: the
# change set is the authoritative list of relpaths to transfer, nothing outside
# it is sent or deleted, and removals go through ``_prune_removed(job.removed)``.
# The helper for building such jobs (``_files_from_job``) is defined lower in
# this file alongside the newer manifest-path tests.


def test_sync_converges_source_to_dest(tmp_path):
    """The relpaths listed in files_from are transferred to the drive."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    _write(src / "LO" / "b.tif", "bbbb")
    _write(src / "top.txt", "c")
    dst.mkdir()

    job = _files_from_job(
        src, dst, files_from=["SEC/a.tif", "LO/b.tif", "top.txt"]
    )
    files_updated, errors = du.sync_payload(job)

    assert errors == []
    assert files_updated == 3
    assert _rel_files(dst) == {"SEC/a.tif", "LO/b.tif", "top.txt"}
    assert (dst / "SEC" / "a.tif").read_text() == "aaa"


def test_second_run_is_noop(tmp_path):
    """Under the manifest path the no-op is an EMPTY files_from change set.

    The old size-only contract reused the same job and relied on rsync's
    size/mtime quick-check to transfer nothing the second time. Under
    manifest-only the converged state is modeled as an empty change set: a
    diff that found nothing to add/change yields ``files_from == []``, which
    transfers nothing and leaves the drive untouched.
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    _write(src / "LO" / "b.tif", "bbbb")
    dst.mkdir()

    # First converge: the full change set is listed and transferred.
    first = _files_from_job(src, dst, files_from=["SEC/a.tif", "LO/b.tif"])
    first_updated, first_errors = du.sync_payload(first)
    assert first_errors == []
    assert first_updated == 2

    # Second run: nothing changed, so the diff yields an EMPTY change set and
    # the transfer is a no-op.
    second = _files_from_job(src, dst, files_from=[])
    second_updated, second_errors = du.sync_payload(second)
    assert second_errors == []
    assert second_updated == 0
    assert _rel_files(dst) == {"SEC/a.tif", "LO/b.tif"}


def test_removed_relpaths_are_pruned_from_dest(tmp_path):
    """Extra dest files gone from the local manifest are pruned via removed.

    ``--delete`` is retired (it cannot run under ``--files-from``); removals now
    flow through the explicit ``_prune_removed(job.removed)`` pass (R2.5).
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "keep.txt", "keep")
    _write(dst / "keep.txt", "keep")
    _write(dst / "stale.txt", "stale")  # gone locally -> in diff.removed

    job = _files_from_job(src, dst, files_from=[], removed=["stale.txt"])
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert _rel_files(dst) == {"keep.txt"}


# --- marker exclusion -------------------------------------------------------


def test_marker_is_excluded_from_payload(tmp_path):
    """The commit marker is never listed in the manifest change set.

    Under ``--files-from`` only the listed relpaths are transferred; the marker
    is written separately as the atomic commit step, so it is never part of
    files_from and never pushed by the payload sync. (The marker basename also
    stays in ``excludes`` as defense in depth.)
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    _write(src / "ScannedCharts.sqlite", "MARKER")  # must NOT be copied
    dst.mkdir()

    # The manifest diff never lists the marker; it is not in files_from.
    job = _files_from_job(
        src, dst, files_from=["SEC/a.tif"], excludes=["ScannedCharts.sqlite"]
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert "ScannedCharts.sqlite" not in _rel_files(dst)
    assert "SEC/a.tif" in _rel_files(dst)


# --- error handling ---------------------------------------------------------


def test_rsync_failure_captured_and_logged(tmp_path, caplog):
    # Source directory does not exist -> rsync exits non-zero. Build a
    # manifest-driven job (files_from set) so the failure is captured on the
    # manifest path, not the retired size-only one.
    src = tmp_path / "missing"
    dst = tmp_path / "drive"
    dst.mkdir()

    job = _files_from_job(src, dst, files_from=["SEC/a.tif"])
    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        files_updated, errors = du.sync_payload(job)

    assert files_updated == 0
    assert len(errors) == 1
    assert "scanned" in errors[0]
    # Req 8.1: the error is logged at ERROR so Recent Errors mirrors status.
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_non_tree_job_rejected():
    nav = du.SyncJob(name="nav", kind="files", files=["NAV.DB"])
    with pytest.raises(ValueError):
        du.sync_payload(nav)


def test_files_from_none_tree_job_rejected(tmp_path):
    """A tree job with files_from=None is rejected (legacy size-only retired).

    manifest-only-chart-sync R2.1: the only chart-transfer mechanism is the
    manifest ``--files-from`` diff. A tree job without a change set would mean a
    size-only transfer slipped back in — the silent-wrong-tile hazard — so
    sync_payload raises rather than running it.
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.tif", "aaa")
    dst.mkdir()

    legacy = _tree_job(src, dst)  # files_from left None by construction
    assert legacy.files_from is None
    with pytest.raises(ValueError):
        du.sync_payload(legacy)


# --- abort path -------------------------------------------------------------


def test_is_aborted_terminates_and_records_error(tmp_path, caplog):
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    # Enough files that rsync doesn't finish in the very first poll tick.
    rels = []
    for i in range(200):
        _write(src / f"f{i}.bin", "x" * 4096)
        rels.append(f"f{i}.bin")
    dst.mkdir()

    job = _files_from_job(src, dst, files_from=rels)

    # Abort on the first poll.
    with caplog.at_level(logging.ERROR, logger=du.logger.name):
        files_updated, errors = du.sync_payload(job, is_aborted=lambda: True)

    assert files_updated == 0
    assert len(errors) == 1
    assert "aborted" in errors[0].lower()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


# --- metadata excludes on the manifest path ---------------------------------
#
# The retired ``--size-only --delete --delete-excluded`` machinery (which
# purged pre-existing ._* / .DS_Store on the drive and protected structural
# excludes from --delete) is gone with the legacy branch. Under ``--files-from``
# nothing outside the listed relpaths is sent or deleted, so there is no
# whole-tree delete pass to purge drive-side junk or to threaten a sibling
# family's subtree. What remains and still matters is the SOURCE-side metadata
# exclude: macOS ``._*`` / ``.DS_Store`` sidecars must never be pushed to the
# drive even if a relpath names one. COPYFILE_DISABLE=1 is also kept.


def test_metadata_sidecars_are_never_copied_from_source(tmp_path):
    """._* / .DS_Store relpaths are not pushed even if listed in files_from.

    The metadata excludes are passed to rsync as ``--exclude`` patterns, so a
    stray sidecar named in the change set is filtered out of the transfer.
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    _write(src / "._a.png", "junk")
    _write(src / "SEC" / "._a.png", "junk")
    _write(src / ".DS_Store", "junk")
    dst.mkdir()

    # Even if the sidecars are (perversely) listed, the metadata --exclude
    # patterns keep them off the drive.
    job = _files_from_job(
        src,
        dst,
        files_from=["SEC/a.png", "._a.png", "SEC/._a.png", ".DS_Store"],
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    files = _rel_files(dst)
    assert "SEC/a.png" in files
    assert not any(f.rsplit("/", 1)[-1].startswith("._") for f in files)
    assert ".DS_Store" not in files


def test_files_outside_changeset_on_dest_are_left_alone(tmp_path):
    """Under --files-from nothing outside files_from/removed is touched.

    This is the manifest-path replacement for the retired "structural exclude /
    sibling subtree / commit marker protected from --delete" assertions: there
    is no --delete pass at all, so a drive-side file the change set does not name
    (a sibling family's subtree, an existing commit marker, drive-side junk) is
    simply never considered — it survives untouched.
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    # Pre-existing drive-side files OUTSIDE the change set:
    _write(dst / "Plates" / "KABC.pdf", "plate-bytes")   # sibling family subtree
    _write(dst / "ScannedCharts.sqlite", "existing-marker")  # commit marker
    _write(dst / "._junk.png", "junk")                    # stray metadata

    job = _files_from_job(
        src, dst, files_from=["SEC/a.png"], excludes=["Plates/", "ScannedCharts.sqlite"]
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    files = _rel_files(dst)
    # The listed file landed.
    assert "SEC/a.png" in files
    # Everything outside the change set is left exactly as it was: no --delete
    # pass can remove the sibling subtree or the commit marker.
    assert "Plates/KABC.pdf" in files
    assert "ScannedCharts.sqlite" in files
    assert "._junk.png" in files


# --- files_updated parsing robustness (defensive noise filter) --------------
#
# We no longer pass --info=progress2 (see the command test below), but the
# _is_progress2_noise filter is retained defensively. Historically progress2
# made rsync stream transfer progress (percent, xfr#,
# to-chk=, carriage-return in-place rewrites) plus a trailing summary onto the
# SAME stdout stream as the --out-format=%n filenames. The file counter must
# ignore that noise and count only real transferred filenames.


def test_is_progress2_noise_classification():
    """The noise filter accepts filenames and rejects progress/summary lines."""
    # Real filenames rsync would emit via %n -> NOT noise.
    assert du._is_progress2_noise("Plates/KABC.pdf") is False
    assert du._is_progress2_noise("SEC/a.png") is False
    assert du._is_progress2_noise("ChartData/LO/chart 12%off.png".replace("%", "pct")) is False

    # progress2 progress lines -> noise.
    assert du._is_progress2_noise(
        "        1,234,567  43%   1.20MB/s    0:00:03 (xfr#120, to-chk=812/22143)"
    ) is True
    assert du._is_progress2_noise("  0%    0.00kB/s    0:00:00") is True
    assert du._is_progress2_noise("(xfr#3, ir-chk=1006/1024)") is True

    # trailing summary lines -> noise.
    assert du._is_progress2_noise("sent 1,024 bytes  received 35 bytes  2,118.00 bytes/sec") is True
    assert du._is_progress2_noise("total size is 123,456  speedup is 116.58") is True

    # blank fragments (from \r splitting) -> noise.
    assert du._is_progress2_noise("") is True
    assert du._is_progress2_noise("   ") is True

    # directory entries are not "noise" here (they're filtered separately by the
    # trailing-slash rule in the counter), but must not be misclassified.
    assert du._is_progress2_noise("SEC/") is False


def test_files_updated_ignores_progress2_lines(tmp_path, monkeypatch):
    """End-to-end: a progress2-laden stdout yields only real filenames counted.

    Built as a manifest ``--files-from`` job (the only transfer mode now); the
    stdout parsing / noise filtering is unchanged by the manifest wiring.
    """
    import subprocess

    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "a.png", "a")
    dst.mkdir()
    job = _files_from_job(src, dst, files_from=["a.png"])

    # Simulated rsync stdout: two real filenames interleaved with progress2
    # progress rewrites (carriage returns) and a trailing summary block.
    fake_stdout = (
        "\r        0   0%    0.00kB/s    0:00:00"
        "\r    1,024 100%    1.00MB/s    0:00:00 (xfr#1, to-chk=1/2)\n"
        "SEC/a.png\n"
        "\r    2,048 100%    2.00MB/s    0:00:00 (xfr#2, to-chk=0/2)\n"
        "LO/b.png\n"
        "\n"
        "sent 4,096 bytes  received 57 bytes  8,306.00 bytes/sec\n"
        "total size is 3,072  speedup is 0.74\n"
    )

    class _WritingProc:
        def __init__(self, out_fh):
            self._out_fh = out_fh
            self._polls = 0
            self.returncode = None
            self.pid = 5555
            # sync_payload feeds the files_from change set over stdin.
            import io

            self.stdin = io.StringIO()

        def poll(self):
            self._polls += 1
            if self._polls == 1:
                return None
            # Write the payload once, then report clean exit.
            self._out_fh.write(fake_stdout)
            self._out_fh.flush()
            self.returncode = 0
            return 0

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def fake_popen(cmd, stdin=None, stdout=None, stderr=None, **k):
        return _WritingProc(stdout)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    files_updated, errors = du.sync_payload(job)

    assert errors == []
    # Exactly the two real filenames; none of the progress/summary lines.
    assert files_updated == 2


def test_rsync_command_has_no_info_progress2_flag(tmp_path, monkeypatch):
    """The rsync invocation must NOT include --info=progress2.

    Regression guard for the v1.5.2 -> v1.5.3 fix: the app runs under launchd,
    whose minimal PATH resolves bare ``rsync`` to /usr/bin/rsync (openrsync
    2.6.9), which rejects ``--info`` with "unrecognized option" and aborts the
    whole transfer, so the flag must stay out of the command.
    ``--out-format=%n`` (the file-count stdout signal) must remain.

    Built as a manifest ``--files-from`` job (the only transfer mode now); the
    ``--info=progress2`` exclusion is unchanged by the manifest wiring.
    """
    import subprocess

    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "a.png", "a")
    dst.mkdir()
    job = _files_from_job(src, dst, files_from=["a.png"])

    captured = {}

    def fake_popen(cmd, stdin=None, stdout=None, stderr=None, **k):
        captured["cmd"] = list(cmd)
        raise OSError("stop here — we only want the command line")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    du.sync_payload(job)  # returns an error (OSError), but captures cmd

    assert "--info=progress2" not in captured["cmd"]
    assert not any(str(a).startswith("--info") for a in captured["cmd"])
    assert "--out-format=%n" in captured["cmd"]


# --- manifest-driven --files-from transfer (chart-manifest-sync task 3.4) ---
#
# When a tree job carries a manifest-driven change set (job.files_from), the
# payload is transferred via ``rsync --files-from=-`` (exactly the added+changed
# relpaths) and removals are pruned explicitly, instead of the retired
# ``--size-only`` whole-tree delta. These run rsync against real temp dirs.


def _files_from_job(src_dir, dst_dir, files_from, removed=None, excludes=None):
    """Build a manifest-driven tree SyncJob pointing at two temp dirs."""
    return du.SyncJob(
        name="scanned",
        kind="tree",
        payload_root_local=str(src_dir) + os.sep,
        payload_root_drive=str(dst_dir) + os.sep,
        excludes=list(du.COMMON_TREE_EXCLUDES) + (excludes or []),
        files_from=list(files_from),
        removed=list(removed or []),
    )


def test_files_from_transfers_only_listed_relpaths(tmp_path):
    """Only the relpaths in files_from are transferred; others are untouched."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    _write(src / "SEC" / "b.png", "bbb")  # present locally but NOT in files_from
    _write(src / "LO" / "c.png", "ccc")
    dst.mkdir()

    job = _files_from_job(src, dst, files_from=["SEC/a.png", "LO/c.png"])
    files_updated, errors = du.sync_payload(job)

    assert errors == []
    # Exactly the listed files land on the drive; b.png is not transferred.
    assert _rel_files(dst) == {"SEC/a.png", "LO/c.png"}
    assert (dst / "SEC" / "a.png").read_text() == "aaa"
    assert files_updated == 2


def test_files_from_overwrites_changed_same_size_content(tmp_path):
    """A same-byte-size content change IS written (closes the --size-only gap).

    This is the core correctness win: --size-only would skip a same-size change;
    feeding the relpath via files_from forces the new content onto the drive.
    """
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "NEW")  # 3 bytes
    _write(dst / "SEC" / "a.png", "OLD")  # 3 bytes, SAME size, stale content

    job = _files_from_job(src, dst, files_from=["SEC/a.png"])
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert (dst / "SEC" / "a.png").read_text() == "NEW"


def test_files_from_empty_changeset_transfers_nothing(tmp_path):
    """An empty files_from leaves the drive untouched and reports no updates."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    _write(dst / "SEC" / "a.png", "aaa")
    _write(dst / "keep.png", "keep")

    job = _files_from_job(src, dst, files_from=[])
    files_updated, errors = du.sync_payload(job)

    assert errors == []
    assert files_updated == 0
    assert _rel_files(dst) == {"SEC/a.png", "keep.png"}


def test_files_from_removed_relpaths_are_pruned(tmp_path):
    """diff.removed relpaths are unlinked from the drive explicitly."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    _write(dst / "SEC" / "a.png", "aaa")
    _write(dst / "SEC" / "gone.png", "stale")  # removed locally -> prune
    _write(dst / "OLD" / "old.png", "stale")   # removed locally -> prune

    job = _files_from_job(
        src, dst, files_from=[], removed=["SEC/gone.png", "OLD/old.png"]
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert "SEC/gone.png" not in _rel_files(dst)
    assert "OLD/old.png" not in _rel_files(dst)
    # The surviving file stays.
    assert "SEC/a.png" in _rel_files(dst)
    # The now-empty OLD/ directory is pruned; SEC/ still holds a.png so it stays.
    assert not (dst / "OLD").exists()
    assert (dst / "SEC").is_dir()


def test_files_from_prune_is_idempotent_for_absent_paths(tmp_path):
    """Pruning a relpath that is already absent on the drive is a no-op."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "SEC" / "a.png", "aaa")
    dst.mkdir()

    # 'ghost.png' never existed on the drive; prune must not raise.
    job = _files_from_job(
        src, dst, files_from=["SEC/a.png"], removed=["SEC/ghost.png"]
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert "SEC/a.png" in _rel_files(dst)


def test_files_from_add_and_remove_converge_to_local(tmp_path):
    """Added+changed written AND removed pruned => drive matches the intent."""
    src = tmp_path / "local"
    dst = tmp_path / "drive"
    # Local intent: SEC/a.png (changed) and SEC/new.png (added) present.
    _write(src / "SEC" / "a.png", "NEW")
    _write(src / "SEC" / "new.png", "added")
    # Drive before: a.png stale (same size), old.png to be removed.
    _write(dst / "SEC" / "a.png", "OLD")
    _write(dst / "SEC" / "old.png", "stale")

    job = _files_from_job(
        src, dst,
        files_from=["SEC/a.png", "SEC/new.png"],
        removed=["SEC/old.png"],
    )
    _updated, errors = du.sync_payload(job)

    assert errors == []
    assert _rel_files(dst) == {"SEC/a.png", "SEC/new.png"}
    assert (dst / "SEC" / "a.png").read_text() == "NEW"
    assert (dst / "SEC" / "new.png").read_text() == "added"


def test_files_from_command_shape(tmp_path, monkeypatch):
    """The manifest-driven invocation uses --files-from=- and drops --size-only.

    Regression guard: --files-from must be present and fed over stdin, and the
    retired --size-only / --delete flags must NOT appear in this mode.
    """
    import subprocess

    src = tmp_path / "local"
    dst = tmp_path / "drive"
    _write(src / "a.png", "a")
    dst.mkdir()
    job = _files_from_job(src, dst, files_from=["a.png"])

    captured = {}

    class _StdinProc:
        def __init__(self):
            self.returncode = None
            self.pid = 4242
            self._polls = 0
            import io

            captured_stdin = {"text": ""}

            class _CapturingStdin(io.StringIO):
                def close(self_inner):
                    captured_stdin["text"] = self_inner.getvalue()
                    super().close()

            self.stdin = _CapturingStdin()
            self._captured_stdin = captured_stdin

        def poll(self):
            self._polls += 1
            if self._polls == 1:
                return None
            self.returncode = 0
            return 0

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    proc = _StdinProc()

    def fake_popen(cmd, stdin=None, stdout=None, stderr=None, **k):
        captured["cmd"] = list(cmd)
        captured["stdin_requested"] = stdin
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    du.sync_payload(job)

    assert "--files-from=-" in captured["cmd"]
    # The manifest diff is authoritative, so rsync must not re-apply its own
    # size/time quick-check skip (that is the --size-only gap we are closing).
    assert "--ignore-times" in captured["cmd"]
    assert "--size-only" not in captured["cmd"]
    assert "--delete" not in captured["cmd"]
    assert "--delete-excluded" not in captured["cmd"]
    assert "--out-format=%n" in captured["cmd"]
    # The change set was fed over stdin (one relpath per line).
    assert captured["stdin_requested"] is subprocess.PIPE
    assert proc._captured_stdin["text"] == "a.png\n"
