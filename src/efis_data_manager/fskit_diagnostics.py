# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FSKit root-cause reproducibility harness (chart-sync-stall-fix, task 8).

This is an **on-demand diagnostic harness**, NOT part of the live auto-sync
path. It exists so Requirement 2 (confirm/refute the FSKit `msdos`/`lifs` driver
as the write-stall root cause) stays reproducible after a future macOS update
that might change FSKit behavior.

What it does (all read-only observation — it never unmounts/remounts a drive):

* **App-side stall onset** — provides :func:`make_stall_hook`, a factory for the
  optional ``stall_hook`` callback that ``drive_updater.sync_payload`` already
  accepts. When the existing no-progress watchdog latches a stall it hands the
  hook a record carrying the onset wall-clock time and the write-progress state
  (log-byte size) at that instant (Req 2.1). The hook only *records* what the
  engine already computes; it adds no second detector and cannot change the
  sync (the engine swallows any hook exception). A log-parsing fallback
  (:func:`parse_stall_events_from_log`) recovers the same onset timing from the
  DataManager log after the fact when no live hook was wired.
* **Concurrent FSKit/kernel events** — :func:`capture_fskit_log_events` runs
  ``log show`` filtered to ``com.apple.fskit.msdos`` client lifecycle
  (init / ``clientDied``) and "Denying dirty-tracking opt-in" messages (Req 2.2).
* **Correlation** — :func:`correlate` aligns app stall intervals with the
  captured kernel events by timestamp (Req 2.3).
* **Mount flags & kexts** — :func:`observe_mount_flags` parses ``mount`` output
  for a device/mountpoint and reports whether the ``fskit`` flag is present;
  :func:`observe_fs_kexts` runs ``kmutil showloaded`` and reports the loaded
  msdos/lifs/fskit kexts (Req 2.3, 3.5).
* **Determination artifact** — :func:`write_determination` emits a markdown or
  JSON artifact under ``~/EFIS/Reports/`` recording either confirmation of the
  FSKit root cause or, if inconclusive, the evidence gap and the additional
  instrumentation needed (Req 2.4, 2.5).

Entry point: :func:`main` / ``python -m efis_data_manager.fskit_diagnostics``
runs a full observe-and-write pass on demand.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Where determination artifacts are written. ~/EFIS/Reports/ matches the
# existing ~/EFIS/{Exports,DataManagerLogs} convention; created on demand.
REPORTS_DIR = os.path.expanduser("~/EFIS/Reports")

# The DataManager log the menu-bar tool writes (app.py configures logging here).
# Used by the log-parsing fallback that recovers stall onset after the fact.
DATAMANAGER_LOG = os.path.expanduser("~/EFIS/DataManagerLogs/datamanager.log")

# Read-only observation binaries. Pinned absolute paths so the harness never
# resolves a binary off $PATH; all three are unprivileged, need no root.
LOG_BIN = "/usr/bin/log"
MOUNT_BIN = "/sbin/mount"
KMUTIL_BIN = "/usr/bin/kmutil"

# FSKit/kernel log signatures we care about (Req 2.2). Substrings, matched
# case-insensitively against each streamed log line.
_FSKIT_SUBSYSTEM = "com.apple.fskit.msdos"
_DIRTY_TRACKING_DENIAL = "Denying dirty-tracking opt-in"
_CLIENT_DIED = "clientDied"
# Client init shows up as msdos appex/volume connector setup; we tag any
# fskit.msdos line that is not a clientDied/denial as generic "lifecycle".
_CLIENT_INIT_HINTS = ("client", "mount", "FSVolume", "probe", "load")

# Loaded-filesystem kexts of interest in `kmutil showloaded` output (Req 3.5).
_KEXT_RE = re.compile(r"(com\.apple\.filesystems\.(?:msdosfs|lifs))\s*\(([^)]*)\)")
_FSKIT_KEXT_RE = re.compile(r"(com\.apple\.fskit\.\S+)")

# `log show` timestamp, e.g. "2026-09-12 19:14:20.799885-0400". We keep the
# whole prefix as an ISO-ish string and also parse it to an aware datetime.
_LOG_TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+[+-]\d{4})\b"
)


# ---------------------------------------------------------------------------
# App-side stall onset (Req 2.1)
# ---------------------------------------------------------------------------


@dataclass
class StallEvent:
    """One app-side stall onset, as latched by the no-progress watchdog.

    Fields mirror the dict ``sync_payload`` hands its ``stall_hook`` (see
    drive_updater.sync_payload): the family that stalled, the ISO-8601 UTC
    wall-clock onset time, the monotonic onset/last-progress timestamps, how
    many seconds elapsed with no log growth, the rsync stdout-log byte size at
    onset (the liveness/write-progress proxy) and the configured threshold.
    """

    family: str
    onset_wall: str
    onset_monotonic: Optional[float] = None
    last_progress_monotonic: Optional[float] = None
    no_progress_seconds: Optional[float] = None
    progress_bytes: Optional[int] = None
    timeout_seconds: Optional[int] = None

    @property
    def onset_dt(self) -> Optional[datetime]:
        """The onset wall clock parsed to an aware datetime, or None."""
        return _parse_iso(self.onset_wall)


def make_stall_hook(sink: List[StallEvent]) -> Callable[[dict], None]:
    """Build a ``stall_hook`` for ``sync_payload`` that records onsets.

    The returned callable is safe to pass as ``sync_payload(..., stall_hook=h)``.
    It appends a :class:`StallEvent` to ``sink`` for each latched stall. It is
    a pure recorder: it performs no I/O and cannot influence the sync. (The
    engine additionally swallows any exception the hook raises, but this hook
    is written not to raise on well-formed input.)

    This is the read-only integration the task calls for: it surfaces the
    watchdog's EXISTING liveness timing without touching the sync path's logic.
    """

    def _hook(record: dict) -> None:
        sink.append(
            StallEvent(
                family=str(record.get("family", "")),
                onset_wall=str(record.get("onset_wall", "")),
                onset_monotonic=record.get("onset_monotonic"),
                last_progress_monotonic=record.get("last_progress_monotonic"),
                no_progress_seconds=record.get("no_progress_seconds"),
                progress_bytes=record.get("progress_bytes"),
                timeout_seconds=record.get("timeout_seconds"),
            )
        )

    return _hook


def parse_stall_events_from_log(
    log_path: str = DATAMANAGER_LOG,
) -> List[StallEvent]:
    """Recover stall onsets from the DataManager log when no live hook ran.

    Fallback path for after-the-fact analysis: the engine logs a
    ``"<family> sync stalled: no progress for <N>s ..."`` line at ERROR when
    the watchdog trips (see drive_updater.sync_payload). We scan the log for
    those lines and reconstruct a :class:`StallEvent` per occurrence, using the
    log line's own leading timestamp as the onset wall clock. Returns an empty
    list if the log is absent/unreadable (best-effort, never raises).
    """
    events: List[StallEvent] = []
    try:
        with open(log_path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return events

    stall_re = re.compile(
        r"(?P<family>\w+) sync stalled: no progress for (?P<secs>\d+)s"
    )
    # Python logging default asctime is "YYYY-MM-DD HH:MM:SS,mmm".
    line_ts_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d+)")
    for line in lines:
        m = stall_re.search(line)
        if not m:
            continue
        onset = ""
        tsm = line_ts_re.search(line)
        if tsm:
            onset = tsm.group(1)
        events.append(
            StallEvent(
                family=m.group("family"),
                onset_wall=onset,
                no_progress_seconds=float(m.group("secs")),
                timeout_seconds=int(m.group("secs")),
            )
        )
    return events


# ---------------------------------------------------------------------------
# Concurrent FSKit/kernel events (Req 2.2)
# ---------------------------------------------------------------------------


@dataclass
class KernelEvent:
    """One captured FSKit/kernel log line of interest.

    ``kind`` is one of ``"client_died"``, ``"dirty_tracking_denied"``,
    ``"lifecycle"`` — the classification used when correlating with app stalls.
    ``ts`` is the parsed aware datetime (or None if the line had no parseable
    timestamp); ``raw`` is the original line for the artifact.
    """

    ts: Optional[datetime]
    kind: str
    raw: str


def _classify_log_line(line: str) -> Optional[str]:
    """Classify an fskit.msdos log line, or None if it is not of interest."""
    if _DIRTY_TRACKING_DENIAL.lower() in line.lower():
        return "dirty_tracking_denied"
    if _CLIENT_DIED.lower() in line.lower():
        return "client_died"
    if _FSKIT_SUBSYSTEM in line:
        return "lifecycle"
    return None


def capture_fskit_log_events(
    since: str = "1h", log_text: Optional[str] = None
) -> List[KernelEvent]:
    """Capture FSKit/kernel events via ``log show`` (Req 2.2).

    Runs ``log show --last <since> --predicate ...`` filtered to the
    ``com.apple.fskit.msdos`` subsystem, then keeps only the lines of interest:
    client lifecycle (init / ``clientDied``) and "Denying dirty-tracking
    opt-in" denials. ``since`` is any value ``log show --last`` accepts
    (e.g. ``"1h"``, ``"30m"``).

    ``log show`` is read-only and needs no root. Pass ``log_text`` to parse a
    pre-captured dump instead of shelling out (used by tests and for offline
    analysis of a saved log). Best-effort: any subprocess failure yields an
    empty list rather than raising.
    """
    if log_text is None:
        log_text = _run_log_show(since)
    events: List[KernelEvent] = []
    if not log_text:
        return events
    for line in log_text.splitlines():
        kind = _classify_log_line(line)
        if kind is None:
            continue
        events.append(KernelEvent(ts=_parse_log_ts(line), kind=kind, raw=line.strip()))
    return events


def _run_log_show(since: str) -> str:
    """Shell out to ``log show`` for the fskit.msdos subsystem. None-safe -> ''."""
    predicate = (
        'subsystem == "com.apple.fskit.msdos" '
        '|| eventMessage CONTAINS "com.apple.fskit.msdos" '
        '|| eventMessage CONTAINS "dirty-tracking"'
    )
    cmd = [LOG_BIN, "show", "--last", since, "--style", "syslog",
           "--predicate", predicate]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("log show failed", exc_info=True)
        return ""
    if result.returncode != 0:
        logger.debug("log show exit=%s: %s", result.returncode,
                     (result.stderr or "").strip()[:200])
        return ""
    return result.stdout or ""


# ---------------------------------------------------------------------------
# Mount flags and loaded fs kexts (Req 2.3, 3.5)
# ---------------------------------------------------------------------------


@dataclass
class MountObservation:
    """Observed mount flags for a device/mountpoint.

    ``fskit`` is True when the ``fskit`` flag is present in the mount line (the
    stalling FSKit path), False when absent (the classic in-kernel msdosfs path,
    which the spike observed as ``noowners`` and no ``fskit``). ``matched`` is
    False when no mount line matched the target at all.
    """

    target: str
    matched: bool
    fskit: bool
    flags: List[str] = field(default_factory=list)
    raw: str = ""


def observe_mount_flags(
    target: str, mount_text: Optional[str] = None
) -> MountObservation:
    """Parse ``mount`` output for ``target`` and report the ``fskit`` flag (Req 2.3).

    ``target`` may be a device node (``/dev/disk4s1``) or a mountpoint
    (``/Volumes/EFIS_3`` or a private ``mount_msdos`` path). A mount line looks
    like::

        /dev/disk4s1 on /Volumes/EFIS_3 (msdos, local, noatime, fskit)

    We match a line whose device or mountpoint equals ``target`` and parse the
    parenthesised, comma-separated flag list. Pass ``mount_text`` to parse a
    captured dump instead of shelling out. Best-effort; on failure returns an
    unmatched observation rather than raising.
    """
    if mount_text is None:
        mount_text = _run_mount()
    obs = MountObservation(target=target, matched=False, fskit=False)
    if not mount_text:
        return obs
    line_re = re.compile(r"^(?P<dev>\S+) on (?P<mp>.+?) \((?P<flags>[^)]*)\)\s*$")
    for line in mount_text.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        dev = m.group("dev")
        mp = m.group("mp")
        if target not in (dev, mp):
            continue
        flags = [f.strip() for f in m.group("flags").split(",") if f.strip()]
        obs.matched = True
        obs.flags = flags
        obs.fskit = "fskit" in flags
        obs.raw = line.strip()
        break
    return obs


def _run_mount() -> str:
    """Shell out to ``mount``. Read-only, no root. None-safe -> ''."""
    try:
        result = subprocess.run(
            [MOUNT_BIN], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


@dataclass
class KextObservation:
    """Loaded filesystem kexts relevant to the FSKit vs classic-path question.

    ``msdosfs`` / ``lifs`` carry the version string parsed from
    ``kmutil showloaded`` (or None when not loaded); ``fskit`` lists any
    ``com.apple.fskit.*`` bundle ids seen. The msdosfs kext being loaded is the
    spike's direct evidence that ``mount_msdos`` routed through the classic
    in-kernel path.
    """

    msdosfs: Optional[str] = None
    lifs: Optional[str] = None
    fskit: List[str] = field(default_factory=list)
    raw: str = ""


def observe_fs_kexts(kmutil_text: Optional[str] = None) -> KextObservation:
    """Parse ``kmutil showloaded`` for msdos/lifs/fskit kexts (Req 3.5).

    Recognises lines like::

        269  1 0x... com.apple.filesystems.msdosfs (1.10) 2AA659E7-... <...>
        214  0 0x... com.apple.filesystems.lifs (1) CA1554EB-... <...>

    recording each kext's version. Pass ``kmutil_text`` to parse a captured
    dump instead of shelling out. Best-effort; failure yields an empty
    observation rather than raising.
    """
    if kmutil_text is None:
        kmutil_text = _run_kmutil_showloaded()
    obs = KextObservation()
    if not kmutil_text:
        return obs
    obs.raw = kmutil_text.strip()
    for m in _KEXT_RE.finditer(kmutil_text):
        bundle, version = m.group(1), m.group(2).strip()
        if bundle.endswith("msdosfs"):
            obs.msdosfs = version
        elif bundle.endswith("lifs"):
            obs.lifs = version
    obs.fskit = sorted(set(_FSKIT_KEXT_RE.findall(kmutil_text)))
    return obs


def _run_kmutil_showloaded() -> str:
    """Shell out to ``kmutil showloaded``. Read-only, no root. None-safe -> ''."""
    try:
        result = subprocess.run(
            [KMUTIL_BIN, "showloaded"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


# ---------------------------------------------------------------------------
# Correlation (Req 2.3)
# ---------------------------------------------------------------------------


@dataclass
class Correlation:
    """A stall correlated with the kernel events near its onset.

    ``kernel_events`` are those whose timestamp falls within ``window_seconds``
    of the stall onset. ``has_fskit_signature`` is True when any correlated
    event is a clientDied or dirty-tracking denial — the FSKit signatures the
    hypothesis predicts around a freeze.
    """

    stall: StallEvent
    kernel_events: List[KernelEvent]
    window_seconds: float

    @property
    def has_fskit_signature(self) -> bool:
        return any(
            e.kind in ("client_died", "dirty_tracking_denied")
            for e in self.kernel_events
        )


def correlate(
    stalls: List[StallEvent],
    kernel_events: List[KernelEvent],
    window_seconds: float = 90.0,
) -> List[Correlation]:
    """Align app stall onsets with kernel events by timestamp (Req 2.3).

    For each stall with a parseable onset time, gathers the kernel events whose
    timestamps fall within +/- ``window_seconds`` of the onset. Stalls without a
    parseable onset (e.g. recovered from a log line missing a timestamp) yield a
    correlation with an empty event list rather than being dropped, so the
    artifact still records that the stall happened.
    """
    results: List[Correlation] = []
    for stall in stalls:
        onset = stall.onset_dt
        matched: List[KernelEvent] = []
        if onset is not None:
            for ev in kernel_events:
                if ev.ts is None:
                    continue
                if abs((ev.ts - onset).total_seconds()) <= window_seconds:
                    matched.append(ev)
        results.append(
            Correlation(stall=stall, kernel_events=matched,
                        window_seconds=window_seconds)
        )
    return results


# ---------------------------------------------------------------------------
# Determination (Req 2.4, 2.5)
# ---------------------------------------------------------------------------


@dataclass
class Determination:
    """The harness's confirm/refute verdict plus its supporting evidence.

    ``verdict`` is one of ``"confirmed"`` (FSKit is the stall root cause),
    ``"refuted"`` (it is not), or ``"inconclusive"``. For an inconclusive
    verdict ``evidence_gap`` and ``additional_instrumentation`` explain what is
    missing and what to add (Req 2.5).
    """

    verdict: str
    rationale: str
    correlations: List[Correlation]
    mount_fskit: MountObservation
    mount_msdos: Optional[MountObservation]
    kexts: KextObservation
    evidence_gap: str = ""
    additional_instrumentation: List[str] = field(default_factory=list)
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def determine(
    correlations: List[Correlation],
    mount_fskit: MountObservation,
    kexts: KextObservation,
    mount_msdos: Optional[MountObservation] = None,
) -> Determination:
    """Reach a confirm/refute/inconclusive verdict from the observations.

    Logic (Req 2.4, 2.5):

    * **confirmed** — at least one stall correlates in time with an FSKit
      signature (clientDied / dirty-tracking denial) AND the FSKit mount carried
      the ``fskit`` flag. This is the recorded-correlation the requirement asks
      for.
    * **refuted** — a stall occurred while the target was NOT on the FSKit path
      (no ``fskit`` flag, classic msdosfs kext loaded), i.e. the stall does not
      require FSKit.
    * **inconclusive** — otherwise; the evidence gap and the additional
      instrumentation needed are recorded so a future run can close it.
    """
    stalls = [c.stall for c in correlations]
    any_stall = bool(stalls)
    correlated_signature = any(c.has_fskit_signature for c in correlations)
    on_fskit = mount_fskit.matched and mount_fskit.fskit

    if any_stall and correlated_signature and on_fskit:
        return Determination(
            verdict="confirmed",
            rationale=(
                "An app-side stall onset correlates in time with FSKit "
                "signatures (clientDied and/or dirty-tracking opt-in denial) "
                "while the drive was mounted on the FSKit msdos/lifs path "
                "(fskit flag present). This reproduces the recorded correlation "
                "behind the CONFIRMED root-cause determination."
            ),
            correlations=correlations,
            mount_fskit=mount_fskit,
            mount_msdos=mount_msdos,
            kexts=kexts,
        )

    if any_stall and mount_fskit.matched and not mount_fskit.fskit:
        return Determination(
            verdict="refuted",
            rationale=(
                "A stall occurred while the drive was NOT on the FSKit path "
                "(no fskit mount flag), so FSKit is not required to reproduce "
                "the stall."
            ),
            correlations=correlations,
            mount_fskit=mount_fskit,
            mount_msdos=mount_msdos,
            kexts=kexts,
        )

    gaps = []
    add: List[str] = []
    if not any_stall:
        gaps.append("no app-side stall was captured in this run")
        add.append(
            "run an unmitigated heavy-write reproduction (task 9) with a live "
            "stall_hook wired via make_stall_hook, or point "
            "parse_stall_events_from_log at the run's DataManager log"
        )
    if any_stall and not correlated_signature:
        gaps.append(
            "a stall was captured but no FSKit signature (clientDied / "
            "dirty-tracking denial) fell within the correlation window"
        )
        add.append(
            "widen the log-show window / correlation window, or confirm the "
            "log show predicate matches this macOS build's fskit.msdos messages"
        )
    if not mount_fskit.matched:
        gaps.append("the FSKit mount could not be observed (no matching mount line)")
        add.append(
            "capture `mount` output while the managed drive is mounted at "
            "/Volumes/ and pass its device/mountpoint as the target"
        )
    return Determination(
        verdict="inconclusive",
        rationale="Evidence was insufficient to confirm or refute FSKit as the "
                  "stall root cause in this run.",
        correlations=correlations,
        mount_fskit=mount_fskit,
        mount_msdos=mount_msdos,
        kexts=kexts,
        evidence_gap="; ".join(gaps) or "unspecified",
        additional_instrumentation=add,
    )


def _determination_to_dict(d: Determination) -> dict:
    """Serialise a Determination (and nested dataclasses) to plain dict/JSON."""
    def _corr(c: Correlation) -> dict:
        return {
            "stall": {
                "family": c.stall.family,
                "onset_wall": c.stall.onset_wall,
                "no_progress_seconds": c.stall.no_progress_seconds,
                "progress_bytes": c.stall.progress_bytes,
                "timeout_seconds": c.stall.timeout_seconds,
            },
            "window_seconds": c.window_seconds,
            "has_fskit_signature": c.has_fskit_signature,
            "kernel_events": [
                {"ts": e.ts.isoformat() if e.ts else None, "kind": e.kind,
                 "raw": e.raw}
                for e in c.kernel_events
            ],
        }

    return {
        "verdict": d.verdict,
        "rationale": d.rationale,
        "captured_at": d.captured_at,
        "mount_fskit": {
            "target": d.mount_fskit.target,
            "matched": d.mount_fskit.matched,
            "fskit": d.mount_fskit.fskit,
            "flags": d.mount_fskit.flags,
            "raw": d.mount_fskit.raw,
        },
        "mount_msdos": (
            None if d.mount_msdos is None else {
                "target": d.mount_msdos.target,
                "matched": d.mount_msdos.matched,
                "fskit": d.mount_msdos.fskit,
                "flags": d.mount_msdos.flags,
                "raw": d.mount_msdos.raw,
            }
        ),
        "kexts": {
            "msdosfs": d.kexts.msdosfs,
            "lifs": d.kexts.lifs,
            "fskit": d.kexts.fskit,
        },
        "correlations": [_corr(c) for c in d.correlations],
        "evidence_gap": d.evidence_gap,
        "additional_instrumentation": d.additional_instrumentation,
    }


def _determination_to_markdown(d: Determination) -> str:
    """Render a Determination as a human-readable markdown artifact."""
    lines: List[str] = []
    lines.append("# FSKit stall root-cause determination")
    lines.append("")
    lines.append(f"- **Verdict:** {d.verdict.upper()}")
    lines.append(f"- **Captured (UTC):** {d.captured_at}")
    lines.append(f"- **Rationale:** {d.rationale}")
    lines.append("")

    lines.append("## Mount flags")
    fk = d.mount_fskit
    if fk.matched:
        lines.append(
            f"- FSKit target `{fk.target}`: fskit flag "
            f"{'PRESENT' if fk.fskit else 'ABSENT'} — flags: {', '.join(fk.flags)}"
        )
        lines.append(f"  - `{fk.raw}`")
    else:
        lines.append(f"- FSKit target `{fk.target}`: no matching mount line")
    if d.mount_msdos is not None:
        md = d.mount_msdos
        if md.matched:
            lines.append(
                f"- mount_msdos target `{md.target}`: fskit flag "
                f"{'PRESENT' if md.fskit else 'ABSENT'} — flags: {', '.join(md.flags)}"
            )
            lines.append(f"  - `{md.raw}`")
        else:
            lines.append(f"- mount_msdos target `{md.target}`: no matching mount line")
    lines.append("")

    lines.append("## Loaded filesystem kexts")
    lines.append(f"- msdosfs: {d.kexts.msdosfs or 'not loaded'}")
    lines.append(f"- lifs: {d.kexts.lifs or 'not loaded'}")
    lines.append(f"- fskit: {', '.join(d.kexts.fskit) if d.kexts.fskit else 'none'}")
    lines.append("")

    lines.append("## Stall / kernel-event correlations")
    if not d.correlations:
        lines.append("- (no app-side stalls captured)")
    for i, c in enumerate(d.correlations, 1):
        s = c.stall
        lines.append(
            f"- Stall {i}: family `{s.family}`, onset {s.onset_wall or 'unknown'}, "
            f"no-progress {s.no_progress_seconds}s, progress {s.progress_bytes} B; "
            f"FSKit signature within {c.window_seconds:g}s: "
            f"{'YES' if c.has_fskit_signature else 'no'}"
        )
        for ev in c.kernel_events:
            ts = ev.ts.isoformat() if ev.ts else "?"
            lines.append(f"  - [{ev.kind}] {ts} {ev.raw}")
    lines.append("")

    if d.verdict == "inconclusive":
        lines.append("## Evidence gap and additional instrumentation")
        lines.append(f"- **Gap:** {d.evidence_gap}")
        for item in d.additional_instrumentation:
            lines.append(f"- **Add:** {item}")
        lines.append("")

    return "\n".join(lines)


def write_determination(
    determination: Determination,
    reports_dir: str = REPORTS_DIR,
    fmt: str = "markdown",
) -> str:
    """Write the determination artifact under ``~/EFIS/Reports/`` (Req 2.4, 2.5).

    Creates ``reports_dir`` if needed and writes a timestamped artifact
    (``fskit-determination-YYYYMMDD-HHMMSS.{md,json}``). ``fmt`` is
    ``"markdown"`` (default) or ``"json"``. Returns the path written.
    """
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        path = os.path.join(reports_dir, f"fskit-determination-{stamp}.json")
        body = json.dumps(_determination_to_dict(determination), indent=2)
    else:
        path = os.path.join(reports_dir, f"fskit-determination-{stamp}.md")
        body = _determination_to_markdown(determination)
    with open(path, "w") as fh:
        fh.write(body)
    logger.info("wrote FSKit determination artifact: %s", path)
    return path


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 (or logging asctime) string to an aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # Try the Python logging asctime form "YYYY-MM-DD HH:MM:SS" (no tz).
        try:
            dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_log_ts(line: str) -> Optional[datetime]:
    """Extract and parse the leading ``log show`` timestamp from a line, or None."""
    m = _LOG_TS_RE.match(line.strip())
    if not m:
        return None
    return _parse_iso(m.group("ts"))


# ---------------------------------------------------------------------------
# On-demand entry point
# ---------------------------------------------------------------------------


def run_observation(
    fskit_target: str,
    since: str = "1h",
    log_path: str = DATAMANAGER_LOG,
    window_seconds: float = 90.0,
    msdos_target: Optional[str] = None,
) -> Determination:
    """Run one full read-only observe-and-determine pass (Req 2.1-2.5, 3.5).

    Recovers app-side stall onsets from the DataManager log, captures FSKit
    kernel events, observes mount flags and loaded kexts, correlates, and
    returns the :class:`Determination`. Nothing here mounts, unmounts, or writes
    to a drive — every step is read-only observation.
    """
    stalls = parse_stall_events_from_log(log_path)
    kernel_events = capture_fskit_log_events(since=since)
    mount_fskit = observe_mount_flags(fskit_target)
    mount_msdos = observe_mount_flags(msdos_target) if msdos_target else None
    kexts = observe_fs_kexts()
    correlations = correlate(stalls, kernel_events, window_seconds=window_seconds)
    return determine(correlations, mount_fskit, kexts, mount_msdos=mount_msdos)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for the on-demand FSKit diagnostic harness.

    Example::

        ./venv/bin/python -m efis_data_manager.fskit_diagnostics \\
            --fskit-target /Volumes/EFIS_3 --since 2h --format markdown
    """
    parser = argparse.ArgumentParser(
        prog="fskit_diagnostics",
        description="On-demand FSKit stall root-cause reproducibility harness "
                    "(read-only; observes only, never unmounts/remounts).",
    )
    parser.add_argument(
        "--fskit-target", required=True,
        help="Device node or mountpoint of the FSKit-mounted drive "
             "(e.g. /Volumes/EFIS_3 or /dev/disk4s1).",
    )
    parser.add_argument(
        "--msdos-target", default=None,
        help="Optional device node or private mountpoint of a mount_msdos mount "
             "to observe alongside (e.g. /private/tmp/efis-datamanager/EFIS_3).",
    )
    parser.add_argument(
        "--since", default="1h",
        help="How far back to pull FSKit kernel events (log show --last). "
             "Default: 1h.",
    )
    parser.add_argument(
        "--log-path", default=DATAMANAGER_LOG,
        help="DataManager log to scan for stall onsets. "
             f"Default: {DATAMANAGER_LOG}",
    )
    parser.add_argument(
        "--window-seconds", type=float, default=90.0,
        help="Correlation half-window around each stall onset. Default: 90.",
    )
    parser.add_argument(
        "--format", choices=("markdown", "json"), default="markdown",
        help="Determination artifact format. Default: markdown.",
    )
    parser.add_argument(
        "--reports-dir", default=REPORTS_DIR,
        help=f"Where to write the artifact. Default: {REPORTS_DIR}",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    determination = run_observation(
        fskit_target=args.fskit_target,
        since=args.since,
        log_path=args.log_path,
        window_seconds=args.window_seconds,
        msdos_target=args.msdos_target,
    )
    path = write_determination(
        determination, reports_dir=args.reports_dir, fmt=args.format
    )
    print(f"Verdict: {determination.verdict.upper()}")
    print(f"Artifact: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
