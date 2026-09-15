# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reproduction / acceptance harness for the chart-sync stall fix (task 9).

This is a **human-run, on-demand** harness. It drives a REAL chart sync to a
physical managed EFIS drive through the app's engine + the mount-swap layer and
turns the run into a measurable pass/fail acceptance result. It is NOT wired
into the live auto-sync path, never runs at import, and never requires root at
import — it only needs the interim ``sudoers.d`` grant (task 2) installed at the
moment it actually drives ``mount_msdos`` on the real drive.

Two reproductions (Req 1.1, 1.2):

* **Incremental top-up** — sync the local chart image
  (``~/EFIS/USB/ChartData``, ~101,563 files / 9.1 GB via the configured
  ``usb_image_path``) to a partially-populated managed drive (e.g. ``EFIS_3``),
  exercising the mount-swap path so the heavy write goes through the
  non-stalling in-kernel msdosfs mount.
* **From-scratch full populate** — optionally reformat/prepare the drive
  (guarded behind an explicit ``--reformat --yes`` confirmation), then populate
  the full image.

Each run records (Req 1.3, 1.4): whether a 120s no-progress stall occurred,
write throughput + cumulative write-progress over time, and the count+size
verify result per family.

**Acceptance gate (pass/fail, Req 1.5, 5.4).** A run PASSES iff:

1. **no 120s stall** occurred (the ``sync_payload`` watchdog never latched — we
   observe this via a :func:`fskit_diagnostics.make_stall_hook` sink);
2. **every synced family verifies clean** on count+size
   (``verify_drive(deep=False)`` reports ``clean`` with no discrepancies); and
3. an **idempotent re-run** over the now-current drive reports **current** with
   **no re-attempted stalled write** (``check_drive_currency`` is current and no
   new stall event fires on the second pass).

Integration seams (all reused unchanged — this module only CALLS them):

* engine — :func:`drive_updater.build_jobs`, :func:`drive_updater.update_drive`,
  :func:`drive_updater.verify_drive`, :func:`drive_updater.check_drive_currency`;
* mount-swap — :func:`mount_swap.msdos_mount` (heavy write goes through the
  non-stalling private mount);
* diagnostics — :func:`fskit_diagnostics.make_stall_hook` /
  :class:`fskit_diagnostics.StallEvent` and
  :func:`fskit_diagnostics.parse_stall_events_from_log` (task 8's helpers) to
  observe the watchdog's own stall onset without adding a second detector.

The pure logic (gate evaluation, throughput calc, report formatting) is factored
out of the side-effecting drive path so it is testable with no drive present;
:func:`main` exposes a ``--self-test`` dry run that exercises exactly that logic.

Entry point: :func:`main` / ``python -m efis_data_manager.repro_harness``.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Where acceptance/determination artifacts are written. Matches the
# fskit_diagnostics convention (~/EFIS/Reports/); created on demand. Overridable
# on the CLI so a dry-run self-test can drop its artifact under ./temp/.
REPORTS_DIR = os.path.expanduser("~/EFIS/Reports")

# The DataManager log the menu-bar tool writes; used as a fallback source of
# stall onsets when no live stall_hook was wired (delegated to task 8's parser).
DATAMANAGER_LOG = os.path.expanduser("~/EFIS/DataManagerLogs/datamanager.log")

# diskutil, used ONLY for the guarded from-scratch reformat path. Pinned
# absolute path; this is the one privileged/destructive call the harness can
# make and it is gated behind --reformat --yes. mount_msdos itself is reached
# only indirectly, through mount_swap.msdos_mount / mount_swap.run_privileged.
DISKUTIL_BIN = "/usr/sbin/diskutil"

# FAT32 filesystem type token diskutil eraseVolume expects for a FAT32 volume.
FAT32_FS_TYPE = "MS-DOS FAT32"

# The three product families the engine syncs, in build order.
ALL_FAMILIES = ("scanned", "plates", "nav")


# ---------------------------------------------------------------------------
# Progress sampling (Req 1.4: throughput + cumulative progress over time)
# ---------------------------------------------------------------------------


@dataclass
class ProgressSample:
    """One point-in-time sample of write progress during a run.

    ``elapsed`` is seconds since the write window started; ``files`` is the
    cumulative count of files present under the drive family trees at that
    instant; ``nbytes`` is their cumulative size in bytes. Sampled by a
    background poller (:class:`ProgressSampler`) so a run yields a progress
    curve, not just a start/end pair.
    """

    elapsed: float
    files: int
    nbytes: int


def compute_throughput(samples: List[ProgressSample]) -> Dict[str, float]:
    """Derive throughput + cumulative-progress metrics from progress samples.

    Pure function (no I/O) so it is unit-testable without a drive. Given the
    ordered samples, returns a dict with:

      * ``elapsed_seconds`` — span from first to last sample;
      * ``files_written`` — cumulative files gained over the span
        (last.files - first.files, floored at 0);
      * ``bytes_written`` — cumulative bytes gained over the span (floored at 0);
      * ``mean_files_per_second`` / ``mean_bytes_per_second`` — the gains
        divided by the elapsed span (0.0 when the span is 0);
      * ``peak_bytes_per_second`` — the largest inter-sample byte rate (0.0 when
        fewer than two samples). This surfaces the healthy-transfer peak vs. a
        stalled plateau.

    An empty or single-sample input yields a zeroed metric set rather than
    raising, so a run that captured no meaningful progress still formats.
    """
    if not samples:
        return {
            "elapsed_seconds": 0.0,
            "files_written": 0,
            "bytes_written": 0,
            "mean_files_per_second": 0.0,
            "mean_bytes_per_second": 0.0,
            "peak_bytes_per_second": 0.0,
        }

    first = samples[0]
    last = samples[-1]
    elapsed = max(0.0, last.elapsed - first.elapsed)
    files_written = max(0, last.files - first.files)
    bytes_written = max(0, last.nbytes - first.nbytes)

    mean_fps = files_written / elapsed if elapsed > 0 else 0.0
    mean_bps = bytes_written / elapsed if elapsed > 0 else 0.0

    peak_bps = 0.0
    for prev, cur in zip(samples, samples[1:]):
        dt = cur.elapsed - prev.elapsed
        if dt <= 0:
            continue
        rate = max(0, cur.nbytes - prev.nbytes) / dt
        if rate > peak_bps:
            peak_bps = rate

    return {
        "elapsed_seconds": elapsed,
        "files_written": files_written,
        "bytes_written": bytes_written,
        "mean_files_per_second": mean_fps,
        "mean_bytes_per_second": mean_bps,
        "peak_bytes_per_second": peak_bps,
    }


def _tree_count_and_size(root: str) -> tuple:
    """Return (file_count, total_bytes) under ``root``; (0, 0) if absent.

    Best-effort walk used by the progress sampler and the final result capture.
    Symlinks are not followed and unreadable entries are skipped so a transient
    permission/racing-unlink during a live sync never raises.
    """
    count = 0
    total = 0
    if not root or not os.path.isdir(root):
        return 0, 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                st = os.lstat(fpath)
            except OSError:
                continue
            count += 1
            total += st.st_size
    return count, total


def sample_drive_progress(mount_point: str, start_monotonic: float) -> ProgressSample:
    """Capture one :class:`ProgressSample` of the drive's ChartData + nav trees.

    Counts files/bytes under ``<mount>/ChartData`` (covers the scanned and
    plates families) plus the two top-level nav files, so the sample reflects
    all three families' cumulative on-drive footprint. ``elapsed`` is measured
    from ``start_monotonic``. Read-only; never raises (delegates to the
    best-effort walk).
    """
    chart_root = os.path.join(mount_point, "ChartData")
    files, nbytes = _tree_count_and_size(chart_root)
    for nav in ("NAV.DB", "NAV-proc.DB"):
        npath = os.path.join(mount_point, nav)
        try:
            st = os.lstat(npath)
        except OSError:
            continue
        files += 1
        nbytes += st.st_size
    return ProgressSample(
        elapsed=time.monotonic() - start_monotonic,
        files=files,
        nbytes=nbytes,
    )


class ProgressSampler:
    """Background poller that samples drive write-progress on an interval.

    Started around the write window; each tick appends a
    :func:`sample_drive_progress` to :attr:`samples`. Runs on a daemon thread so
    it never blocks teardown. The heavy per-tick cost is a filesystem walk, so
    the default interval is generous (5s) — enough resolution for a throughput
    curve without hammering the drive being measured.

    This is intentionally a thin wrapper (no engine coupling) so it can be
    exercised in the self-test against a local temp dir.
    """

    def __init__(self, mount_point: str, interval: float = 5.0):
        self.mount_point = mount_point
        self.interval = interval
        self.samples: List[ProgressSample] = []
        self._start_monotonic = 0.0
        self._stop = None
        self._thread = None

    def start(self) -> None:
        import threading

        self._start_monotonic = time.monotonic()
        self._stop = threading.Event()
        # Seed with a zero-time baseline so the first delta is measured from the
        # write window's true start, not the first interval tick.
        self.samples.append(
            sample_drive_progress(self.mount_point, self._start_monotonic)
        )

        def _run():
            while not self._stop.wait(self.interval):
                try:
                    self.samples.append(
                        sample_drive_progress(self.mount_point, self._start_monotonic)
                    )
                except Exception:  # pragma: no cover - sampling is best-effort
                    logger.debug("progress sample failed", exc_info=True)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1.0)
        # Capture a final sample so the curve ends at the true post-write state.
        try:
            self.samples.append(
                sample_drive_progress(self.mount_point, self._start_monotonic)
            )
        except Exception:  # pragma: no cover
            logger.debug("final progress sample failed", exc_info=True)


# ---------------------------------------------------------------------------
# Per-family verify result (Req 6.1, 6.2 — count+size verification)
# ---------------------------------------------------------------------------


@dataclass
class FamilyVerify:
    """The count+size verify outcome for one product family.

    Distilled from a ``verify_drive`` per-family discrepancy dict
    (``{"missing": [...], "extra": [...], "size_mismatch": [...]}``): ``clean``
    is True iff all three discrepancy lists are empty. The counts are retained
    so the report can show WHY a family failed.
    """

    family: str
    clean: bool
    missing: int = 0
    extra: int = 0
    size_mismatch: int = 0

    @classmethod
    def from_discrepancy(cls, family: str, disc: dict) -> "FamilyVerify":
        missing = len(disc.get("missing", []) or [])
        extra = len(disc.get("extra", []) or [])
        size_mismatch = len(disc.get("size_mismatch", []) or [])
        return cls(
            family=family,
            clean=(missing == 0 and extra == 0 and size_mismatch == 0),
            missing=missing,
            extra=extra,
            size_mismatch=size_mismatch,
        )


def summarize_verify(verify_result: dict) -> List[FamilyVerify]:
    """Turn a ``verify_drive`` result into a list of :class:`FamilyVerify`.

    Pure; reads only the ``"families"`` sub-dict, so it is testable with a canned
    verify result. Family order follows the verify result's dict order (build
    order: scanned, plates, nav).
    """
    families = verify_result.get("families", {}) if verify_result else {}
    return [
        FamilyVerify.from_discrepancy(name, disc) for name, disc in families.items()
    ]


# ---------------------------------------------------------------------------
# Acceptance gate (Req 1.5, 5.1, 5.2, 5.4)
# ---------------------------------------------------------------------------


@dataclass
class GateInputs:
    """The three measured facts the acceptance gate is computed from.

    * ``stall_occurred`` — did the ``sync_payload`` 120s no-progress watchdog
      latch at any point during the write window? (from a stall_hook sink.)
    * ``family_verifies`` — the per-family count+size verify outcomes of the
      synced families.
    * ``rerun_current`` — did the idempotent re-run report the drive current?
    * ``rerun_stall_occurred`` — did the re-run trigger a NEW stall (i.e. it
      re-attempted a stalled write)? Must be False to pass.
    """

    stall_occurred: bool
    family_verifies: List[FamilyVerify]
    rerun_current: bool
    rerun_stall_occurred: bool


@dataclass
class GateResult:
    """The pass/fail verdict plus the itemised reasons behind it.

    ``passed`` is the AND of the three conditions; ``reasons`` lists every
    condition with its individual pass/fail so a failing run says exactly which
    of the three gate conditions broke.
    """

    passed: bool
    no_stall: bool
    all_families_clean: bool
    idempotent_rerun: bool
    reasons: List[str] = field(default_factory=list)


def evaluate_gate(inputs: GateInputs) -> GateResult:
    """Compute the pass/fail acceptance gate from measured facts (Req 1.5, 5.4).

    PASS iff ALL of:

      1. **no 120s stall** — ``inputs.stall_occurred`` is False;
      2. **every synced family verifies clean** — every
         :attr:`FamilyVerify.clean` is True (and at least one family was
         verified — an empty verify set does NOT pass, since "nothing verified"
         cannot demonstrate a clean sync);
      3. **idempotent no-op re-run** — the re-run reported current
         (``rerun_current``) AND did not re-attempt a stalled write
         (``rerun_stall_occurred`` is False).

    Pure and side-effect-free. Returns a :class:`GateResult` carrying the overall
    verdict, the three component booleans, and human-readable reasons.
    """
    no_stall = not inputs.stall_occurred

    verified_any = len(inputs.family_verifies) > 0
    all_clean = verified_any and all(fv.clean for fv in inputs.family_verifies)

    idempotent = inputs.rerun_current and not inputs.rerun_stall_occurred

    reasons: List[str] = []
    reasons.append(
        f"[{'PASS' if no_stall else 'FAIL'}] no 120s stall during the write window"
        + ("" if no_stall else " (a stall was latched by the watchdog)")
    )
    if not verified_any:
        reasons.append("[FAIL] every family verifies clean (no families were verified)")
    else:
        dirty = [fv.family for fv in inputs.family_verifies if not fv.clean]
        reasons.append(
            f"[{'PASS' if all_clean else 'FAIL'}] every synced family verifies "
            f"clean on count+size"
            + ("" if all_clean else f" (dirty: {', '.join(dirty)})")
        )
    reasons.append(
        f"[{'PASS' if idempotent else 'FAIL'}] idempotent re-run reports current "
        f"without re-attempting a stalled write"
        + (
            ""
            if idempotent
            else f" (rerun_current={inputs.rerun_current}, "
            f"rerun_stall={inputs.rerun_stall_occurred})"
        )
    )

    passed = no_stall and all_clean and idempotent
    return GateResult(
        passed=passed,
        no_stall=no_stall,
        all_families_clean=all_clean,
        idempotent_rerun=idempotent,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Run record + report formatting (Req 1.3, 1.4, 1.5)
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    """Everything one acceptance run measured, ready to serialise/report.

    Carries the mode (incremental / full-populate), the families that were
    synced, the stall observation, the throughput metrics, the per-family verify
    outcomes, the re-run facts, and the final gate verdict.
    """

    mode: str
    mount_point: str
    families_synced: List[str]
    stall_occurred: bool
    stall_detail: str
    throughput: Dict[str, float]
    family_verifies: List[FamilyVerify]
    rerun_current: bool
    rerun_stall_occurred: bool
    gate: GateResult
    errors: List[str] = field(default_factory=list)
    aborted: bool = False
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _run_record_to_dict(rec: RunRecord) -> dict:
    """Serialise a :class:`RunRecord` (and nested dataclasses) to plain dict."""
    return {
        "mode": rec.mode,
        "mount_point": rec.mount_point,
        "families_synced": rec.families_synced,
        "captured_at": rec.captured_at,
        "stall_occurred": rec.stall_occurred,
        "stall_detail": rec.stall_detail,
        "throughput": rec.throughput,
        "family_verifies": [
            {
                "family": fv.family,
                "clean": fv.clean,
                "missing": fv.missing,
                "extra": fv.extra,
                "size_mismatch": fv.size_mismatch,
            }
            for fv in rec.family_verifies
        ],
        "rerun_current": rec.rerun_current,
        "rerun_stall_occurred": rec.rerun_stall_occurred,
        "errors": rec.errors,
        "aborted": rec.aborted,
        "gate": {
            "passed": rec.gate.passed,
            "no_stall": rec.gate.no_stall,
            "all_families_clean": rec.gate.all_families_clean,
            "idempotent_rerun": rec.gate.idempotent_rerun,
            "reasons": rec.gate.reasons,
        },
    }


def _fmt_bytes(nbytes: float) -> str:
    """Render a byte count as a compact human string (B/KB/MB/GB)."""
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_report_markdown(rec: RunRecord) -> str:
    """Render a :class:`RunRecord` as a human-readable acceptance report.

    Pure (no I/O) so it is unit-testable. Leads with the PASS/FAIL verdict so a
    human sees the acceptance outcome first, then the measured throughput,
    stall observation, per-family verify, and re-run facts.
    """
    tp = rec.throughput
    lines: List[str] = []
    lines.append("# Chart-sync stall-fix acceptance report")
    lines.append("")
    lines.append(f"- **Result:** {'PASS' if rec.gate.passed else 'FAIL'}")
    lines.append(f"- **Mode:** {rec.mode}")
    lines.append(f"- **Drive:** {rec.mount_point}")
    lines.append(f"- **Families synced:** {', '.join(rec.families_synced) or 'none'}")
    lines.append(f"- **Captured (UTC):** {rec.captured_at}")
    lines.append("")

    lines.append("## Acceptance gate")
    for reason in rec.gate.reasons:
        lines.append(f"- {reason}")
    lines.append("")

    lines.append("## Stall observation (Req 1.3)")
    lines.append(
        f"- 120s no-progress stall occurred: {'YES' if rec.stall_occurred else 'no'}"
    )
    if rec.stall_detail:
        lines.append(f"  - {rec.stall_detail}")
    lines.append("")

    lines.append("## Throughput / cumulative progress (Req 1.4)")
    lines.append(f"- elapsed: {tp.get('elapsed_seconds', 0.0):.1f} s")
    lines.append(f"- files written: {tp.get('files_written', 0)}")
    lines.append(f"- bytes written: {_fmt_bytes(tp.get('bytes_written', 0))}")
    lines.append(
        f"- mean rate: {tp.get('mean_files_per_second', 0.0):.1f} files/s, "
        f"{_fmt_bytes(tp.get('mean_bytes_per_second', 0.0))}/s"
    )
    lines.append(
        f"- peak rate: {_fmt_bytes(tp.get('peak_bytes_per_second', 0.0))}/s"
    )
    lines.append("")

    lines.append("## Per-family verify (count+size, Req 6.1/6.2)")
    if not rec.family_verifies:
        lines.append("- (no families verified)")
    for fv in rec.family_verifies:
        if fv.clean:
            lines.append(f"- {fv.family}: clean")
        else:
            lines.append(
                f"- {fv.family}: {fv.missing} missing, {fv.extra} extra, "
                f"{fv.size_mismatch} size mismatch"
            )
    lines.append("")

    lines.append("## Idempotent re-run (Req 5.4)")
    lines.append(f"- re-run reports current: {'YES' if rec.rerun_current else 'no'}")
    lines.append(
        f"- re-run re-attempted a stalled write: "
        f"{'YES' if rec.rerun_stall_occurred else 'no'}"
    )
    lines.append("")

    if rec.errors or rec.aborted:
        lines.append("## Errors")
        lines.append(f"- aborted: {'YES' if rec.aborted else 'no'}")
        for err in rec.errors:
            lines.append(f"- {err}")
        lines.append("")

    return "\n".join(lines)


def write_report(
    rec: RunRecord, reports_dir: str = REPORTS_DIR, fmt: str = "markdown"
) -> str:
    """Write the acceptance report artifact; return the path written.

    Creates ``reports_dir`` if needed and writes a timestamped artifact
    (``acceptance-<mode>-YYYYMMDD-HHMMSS.{md,json}``). ``fmt`` is ``"markdown"``
    (default) or ``"json"``.
    """
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        path = os.path.join(reports_dir, f"acceptance-{rec.mode}-{stamp}.json")
        body = json.dumps(_run_record_to_dict(rec), indent=2)
    else:
        path = os.path.join(reports_dir, f"acceptance-{rec.mode}-{stamp}.md")
        body = format_report_markdown(rec)
    with open(path, "w") as fh:
        fh.write(body)
    logger.info("wrote acceptance report: %s", path)
    return path


# ---------------------------------------------------------------------------
# Guarded from-scratch reformat (Req 1.2) — DESTRUCTIVE, double-gated
# ---------------------------------------------------------------------------


def reformat_drive_fat32(
    device_node: str, label: str, confirm: bool
) -> None:
    """Reformat ``device_node`` to an empty FAT32 volume named ``label``.

    DESTRUCTIVE. Guard rails:

      * ``confirm`` MUST be True (the CLI sets it only when BOTH ``--reformat``
        and ``--yes`` are given). Without it this raises ``PermissionError`` and
        does nothing — the from-scratch path can NEVER reformat unattended.
      * runs ``diskutil eraseVolume "MS-DOS FAT32" <label> <device_node>``.

    This is the only destructive call in the harness and is never reached in the
    self-test or the incremental path.
    """
    if not confirm:
        raise PermissionError(
            "refusing to reformat: explicit --reformat --yes confirmation "
            "required (the from-scratch path never reformats without it)"
        )
    cmd = [DISKUTIL_BIN, "eraseVolume", FAT32_FS_TYPE, label, device_node]
    logger.warning("reformatting %s to FAT32 %r: %s", device_node, label, cmd)
    subprocess.run(cmd, check=True, timeout=600)


# ---------------------------------------------------------------------------
# Live acceptance run (drives a REAL sync through the engine + mount swap)
# ---------------------------------------------------------------------------


def _detect_stall(stall_sink: list, log_path: str) -> tuple:
    """Return (stall_occurred, detail) from a live sink, log fallback second.

    Prefers the live ``stall_hook`` sink (a list of
    :class:`fskit_diagnostics.StallEvent`); if it is empty, falls back to
    scanning the DataManager log for the watchdog's stall lines (task 8's
    ``parse_stall_events_from_log``). Either source being non-empty means the
    120s watchdog latched during the window.
    """
    if stall_sink:
        first = stall_sink[0]
        detail = (
            f"watchdog latched for family {getattr(first, 'family', '?')!r} "
            f"at {getattr(first, 'onset_wall', '?')} "
            f"({len(stall_sink)} stall event(s))"
        )
        return True, detail

    # Fallback: after-the-fact log scan (only meaningful for a live run).
    try:
        from efis_data_manager.fskit_diagnostics import parse_stall_events_from_log

        events = parse_stall_events_from_log(log_path)
    except Exception:
        events = []
    if events:
        return True, f"recovered {len(events)} stall event(s) from {log_path}"
    return False, "no stall latched (live hook + log both clean)"


def run_acceptance(
    mount_point: str,
    mode: str = "incremental",
    families: Optional[List[str]] = None,
    reports_dir: str = REPORTS_DIR,
    report_fmt: str = "markdown",
    sample_interval: float = 5.0,
    reformat: bool = False,
    confirm: bool = False,
) -> RunRecord:
    """Drive ONE real acceptance run against ``mount_point`` and score the gate.

    This is the side-effecting entry the CLI invokes. It requires a real managed
    drive mounted at ``mount_point`` and (to actually mount_msdos) the interim
    ``sudoers.d`` grant installed. Sequence:

      1. (full-populate only, guarded) if ``reformat`` — resolve the device node
         via :func:`mount_swap.resolve_device_node` and
         :func:`reformat_drive_fat32` (which itself requires ``confirm``).
      2. Wire a live ``stall_hook`` sink via
         :func:`fskit_diagnostics.make_stall_hook` and monkeypatch it onto the
         engine's ``sync_payload`` for the window (so the EXISTING watchdog
         reports onset to us without changing its behaviour).
      3. Enter :func:`mount_swap.msdos_mount` so the heavy write goes through the
         non-stalling private mount; start the :class:`ProgressSampler`.
      4. Run the engine unchanged against the private work mount:
         :func:`drive_updater.check_drive_currency` then
         :func:`drive_updater.update_drive` on the stale families.
      5. Verify count+size via :func:`drive_updater.verify_drive` and distil
         per-family outcomes.
      6. Idempotent re-run: :func:`drive_updater.check_drive_currency` again and
         watch for any NEW stall event.
      7. Score :func:`evaluate_gate` and write the report artifact.

    Returns the :class:`RunRecord` (also written to ``reports_dir``).

    NOTE: importing the engine + mount-swap modules is deferred to call time so
    this module imports cleanly (and the self-test runs) even where those side
    effects are undesirable at import.
    """
    from efis_data_manager import drive_updater
    from efis_data_manager.fskit_diagnostics import make_stall_hook
    from efis_data_manager.mount_swap import (
        MountSwapError,
        msdos_mount,
        resolve_device_node,
    )

    families = list(families) if families else list(ALL_FAMILIES)
    errors: List[str] = []
    aborted = False

    # Step 1 (guarded): from-scratch reformat. Only when explicitly requested
    # AND confirmed. resolve_device_node must run BEFORE any unmount.
    if reformat:
        device_node = resolve_device_node(mount_point)
        if device_node is None:
            raise RuntimeError(
                f"cannot reformat: could not resolve device node for {mount_point!r}"
            )
        label = os.path.basename(os.path.normpath(mount_point))
        reformat_drive_fat32(device_node, label, confirm=confirm)

    # Step 2: wire a live stall_hook sink onto sync_payload for the window.
    stall_sink: list = []
    hook = make_stall_hook(stall_sink)
    original_sync_payload = drive_updater.sync_payload

    def _sync_payload_with_hook(job, is_aborted=None, stall_hook=None):
        # Prefer the harness hook; if a caller ever passes its own, chain both.
        def _chained(record):
            hook(record)
            if stall_hook is not None:
                try:
                    stall_hook(record)
                except Exception:  # pragma: no cover
                    pass

        return original_sync_payload(job, is_aborted=is_aborted, stall_hook=_chained)

    sampler = ProgressSampler(mount_point, interval=sample_interval)
    throughput: Dict[str, float] = compute_throughput([])
    family_verifies: List[FamilyVerify] = []

    drive_updater.sync_payload = _sync_payload_with_hook
    try:
        # Step 3-4: swap to the non-stalling mount and run the engine on it.
        try:
            with msdos_mount(mount_point) as work_mount:
                sampler.mount_point = work_mount
                sampler.start()
                try:
                    currency = drive_updater.check_drive_currency(work_mount)
                    stale = [
                        name
                        for name, detail in currency["families"].items()
                        if not detail["current"] and name in families
                    ]
                    if stale:
                        results = drive_updater.update_drive(
                            work_mount, families=stale
                        )
                        errors.extend(results.get("errors", []))
                        aborted = bool(results.get("aborted"))
                    # Step 5: count+size verify of the requested families.
                    verify_result = drive_updater.verify_drive(
                        work_mount, families=families, deep=False
                    )
                    family_verifies = summarize_verify(verify_result)
                finally:
                    sampler.stop()
                    throughput = compute_throughput(sampler.samples)
        except MountSwapError as e:
            errors.append(f"mount swap failed: {e}")
            logger.error("acceptance run: mount swap failed: %s", e)

        # Step 6: idempotent re-run over the now-restored FSKit mount. A second
        # currency check must report current and must NOT trigger a new stall.
        rerun_stall_before = len(stall_sink)
        rerun_current = False
        try:
            rerun_currency = drive_updater.check_drive_currency(mount_point)
            rerun_current = bool(rerun_currency.get("is_current"))
        except Exception as e:  # pragma: no cover - defensive
            errors.append(f"re-run currency check failed: {e}")
        rerun_stall_occurred = len(stall_sink) > rerun_stall_before

    finally:
        # Always restore the real engine function, even on error.
        drive_updater.sync_payload = original_sync_payload

    stall_occurred, stall_detail = _detect_stall(stall_sink, DATAMANAGER_LOG)

    gate = evaluate_gate(
        GateInputs(
            stall_occurred=stall_occurred,
            family_verifies=family_verifies,
            rerun_current=rerun_current,
            rerun_stall_occurred=rerun_stall_occurred,
        )
    )

    rec = RunRecord(
        mode=mode,
        mount_point=mount_point,
        families_synced=families,
        stall_occurred=stall_occurred,
        stall_detail=stall_detail,
        throughput=throughput,
        family_verifies=family_verifies,
        rerun_current=rerun_current,
        rerun_stall_occurred=rerun_stall_occurred,
        gate=gate,
        errors=errors,
        aborted=aborted,
    )
    write_report(rec, reports_dir=reports_dir, fmt=report_fmt)
    return rec


# ---------------------------------------------------------------------------
# Dry-run self-test (pure logic only; no drive, no root, no engine side effects)
# ---------------------------------------------------------------------------


def self_test(reports_dir: str) -> bool:
    """Exercise the harness's PURE logic with mocked data; return True if OK.

    Verifies, with no drive and no privileged calls:

      * :func:`compute_throughput` derives sane metrics from synthetic samples;
      * :func:`summarize_verify` distils a canned ``verify_drive`` result;
      * :func:`evaluate_gate` passes a clean scenario and fails each broken one;
      * :func:`format_report_markdown` / :func:`write_report` render + write;
      * :func:`reformat_drive_fat32` REFUSES without confirmation.

    This is the compile/import + pure-logic verification the task calls for,
    runnable safely on any machine.
    """
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {msg}")
        if not cond:
            ok = False

    # --- throughput ---
    samples = [
        ProgressSample(elapsed=0.0, files=0, nbytes=0),
        ProgressSample(elapsed=10.0, files=500, nbytes=50_000_000),
        ProgressSample(elapsed=20.0, files=1200, nbytes=180_000_000),
    ]
    tp = compute_throughput(samples)
    check(tp["files_written"] == 1200, "throughput: cumulative files == 1200")
    check(tp["bytes_written"] == 180_000_000, "throughput: cumulative bytes")
    check(
        abs(tp["mean_bytes_per_second"] - 9_000_000.0) < 1.0,
        "throughput: mean bytes/s == 9 MB/s",
    )
    check(
        tp["peak_bytes_per_second"] >= tp["mean_bytes_per_second"],
        "throughput: peak >= mean",
    )
    check(
        compute_throughput([])["elapsed_seconds"] == 0.0,
        "throughput: empty samples -> zeroed metrics",
    )

    # --- verify summary ---
    verify_result = {
        "families": {
            "scanned": {"missing": [], "extra": [], "size_mismatch": []},
            "plates": {"missing": ["a", "b"], "extra": [], "size_mismatch": []},
            "nav": {"missing": [], "extra": [], "size_mismatch": []},
        }
    }
    fvs = summarize_verify(verify_result)
    by_name = {fv.family: fv for fv in fvs}
    check(by_name["scanned"].clean, "verify: scanned clean")
    check(not by_name["plates"].clean and by_name["plates"].missing == 2,
          "verify: plates dirty with 2 missing")
    check(by_name["nav"].clean, "verify: nav clean")

    # --- gate: clean PASS ---
    clean_fvs = [
        FamilyVerify(family="scanned", clean=True),
        FamilyVerify(family="plates", clean=True),
        FamilyVerify(family="nav", clean=True),
    ]
    passing = evaluate_gate(
        GateInputs(
            stall_occurred=False,
            family_verifies=clean_fvs,
            rerun_current=True,
            rerun_stall_occurred=False,
        )
    )
    check(passing.passed, "gate: clean scenario PASSES")

    # --- gate: each broken condition FAILS ---
    fail_stall = evaluate_gate(
        GateInputs(True, clean_fvs, True, False)
    )
    check(not fail_stall.passed and not fail_stall.no_stall,
          "gate: a stall FAILS the gate")

    fail_dirty = evaluate_gate(
        GateInputs(
            False,
            [FamilyVerify(family="plates", clean=False, missing=3)],
            True,
            False,
        )
    )
    check(not fail_dirty.passed and not fail_dirty.all_families_clean,
          "gate: a dirty family FAILS the gate")

    fail_rerun = evaluate_gate(
        GateInputs(False, clean_fvs, False, False)
    )
    check(not fail_rerun.passed and not fail_rerun.idempotent_rerun,
          "gate: non-current re-run FAILS the gate")

    fail_rerun_stall = evaluate_gate(
        GateInputs(False, clean_fvs, True, True)
    )
    check(not fail_rerun_stall.passed and not fail_rerun_stall.idempotent_rerun,
          "gate: re-run that re-stalls FAILS the gate")

    fail_empty = evaluate_gate(
        GateInputs(False, [], True, False)
    )
    check(not fail_empty.passed and not fail_empty.all_families_clean,
          "gate: no families verified FAILS the gate")

    # --- report formatting + write ---
    rec = RunRecord(
        mode="self-test",
        mount_point="/Volumes/EFIS_3",
        families_synced=list(ALL_FAMILIES),
        stall_occurred=False,
        stall_detail="no stall latched (live hook + log both clean)",
        throughput=tp,
        family_verifies=clean_fvs,
        rerun_current=True,
        rerun_stall_occurred=False,
        gate=passing,
    )
    md = format_report_markdown(rec)
    check("Result:** PASS" in md, "report: markdown leads with PASS verdict")
    check("Throughput" in md, "report: markdown includes throughput section")
    md_path = write_report(rec, reports_dir=reports_dir, fmt="markdown")
    json_path = write_report(rec, reports_dir=reports_dir, fmt="json")
    check(os.path.isfile(md_path), f"report: markdown written to {md_path}")
    check(os.path.isfile(json_path), f"report: json written to {json_path}")
    # Round-trip the JSON so we know it is well-formed.
    try:
        with open(json_path) as fh:
            loaded = json.load(fh)
        check(loaded["gate"]["passed"] is True, "report: json round-trips, gate passed")
    except Exception as e:
        check(False, f"report: json round-trip failed: {e}")

    # --- destructive guard ---
    refused = False
    try:
        reformat_drive_fat32("/dev/disk9s1", "EFIS_3", confirm=False)
    except PermissionError:
        refused = True
    check(refused, "reformat: refuses without explicit confirmation")

    return ok


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """CLI for the reproduction / acceptance harness.

    Examples::

        # Safe dry run of the pure gate/throughput/report logic (no drive):
        ./venv/bin/python -m efis_data_manager.repro_harness --self-test

        # Incremental top-up acceptance run against a real managed drive:
        ./venv/bin/python -m efis_data_manager.repro_harness \\
            --mode incremental --mount /Volumes/EFIS_3

        # From-scratch full populate (DESTRUCTIVE — double-gated):
        ./venv/bin/python -m efis_data_manager.repro_harness \\
            --mode full --mount /Volumes/EFIS_3 --reformat --yes
    """
    parser = argparse.ArgumentParser(
        prog="repro_harness",
        description="Reproduction / acceptance harness for the chart-sync stall "
                    "fix. Drives a real sync through the engine + mount-swap and "
                    "scores a pass/fail acceptance gate. Human-run; needs the "
                    "sudoers grant to mount_msdos. Never runs at import.",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run the pure-logic dry-run self-test (no drive, no root). "
             "Writes its sample artifacts under --reports-dir.",
    )
    parser.add_argument(
        "--mode", choices=("incremental", "full"), default="incremental",
        help="incremental top-up (default) or from-scratch full populate.",
    )
    parser.add_argument(
        "--mount", default=None,
        help="Mountpoint of the managed EFIS drive (e.g. /Volumes/EFIS_3). "
             "Required for a live run.",
    )
    parser.add_argument(
        "--families", default=None,
        help="Comma-separated subset of scanned,plates,nav to sync/verify. "
             "Default: all three.",
    )
    parser.add_argument(
        "--reformat", action="store_true",
        help="(full mode only) reformat the drive to empty FAT32 BEFORE "
             "populating. DESTRUCTIVE — also requires --yes.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm a destructive --reformat. Without it, --reformat refuses.",
    )
    parser.add_argument(
        "--sample-interval", type=float, default=5.0,
        help="Progress-sampling interval in seconds. Default: 5.",
    )
    parser.add_argument(
        "--format", choices=("markdown", "json"), default="markdown",
        help="Acceptance report artifact format. Default: markdown.",
    )
    parser.add_argument(
        "--reports-dir", default=REPORTS_DIR,
        help=f"Where to write the artifact. Default: {REPORTS_DIR}",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.self_test:
        print("Running repro_harness self-test (pure logic; no drive)...")
        ok = self_test(args.reports_dir)
        print(f"Self-test: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    if not args.mount:
        parser.error("--mount is required for a live run (or use --self-test)")

    if args.reformat and not args.yes:
        parser.error(
            "--reformat is destructive and requires --yes to confirm; refusing"
        )
    if args.reformat and args.mode != "full":
        parser.error("--reformat is only valid with --mode full")

    families = (
        [f.strip() for f in args.families.split(",") if f.strip()]
        if args.families
        else None
    )

    rec = run_acceptance(
        mount_point=args.mount,
        mode=args.mode,
        families=families,
        reports_dir=args.reports_dir,
        report_fmt=args.format,
        sample_interval=args.sample_interval,
        reformat=args.reformat,
        confirm=args.yes,
    )
    print(f"Result: {'PASS' if rec.gate.passed else 'FAIL'}")
    for reason in rec.gate.reasons:
        print(f"  {reason}")
    return 0 if rec.gate.passed else 1


if __name__ == "__main__":
    sys.exit(main())
