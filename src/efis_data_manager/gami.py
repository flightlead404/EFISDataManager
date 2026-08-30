# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GAMI lean test detection and analysis.

A GAMI sweep: at stable cruise power, the mixture is leaned (fuel flow
decreasing) and each cylinder's EGT rises, peaks, then falls. The fuel-flow
spread between the first and last cylinder to peak is the "GAMI spread" — a
measure of fuel injector balance. Tight spread (< ~0.5 GPH) = well balanced.

Detection approach (tuned to this pilot's technique — lean through peak to
well-LOP, then richen and re-adjust, sometimes multiple times per session):

1. Find stable-power regions (RPM and MAP steady, airborne).
2. Within them, find lean strokes: sustained fuel-flow decreases.
3. For each stroke, smooth each EGT and find its peak (value + fuel flow at
   peak). A valid stroke has all cylinders showing a clear rise-then-fall.
4. Pick the best (usually first/deepest) stroke per session.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from efis_data_manager.database import get_db_connection

logger = logging.getLogger(__name__)

# --- Tunables ---
NUM_CYL_DEFAULT = 4
SMOOTH_WINDOW = 5           # samples for moving-average smoothing of EGT/FF
RPM_STABLE_BAND = 100       # RPM must stay within this band over the stroke
MAP_STABLE_BAND = 1.5       # inHg band
MIN_STROKE_SECONDS = 15     # a lean stroke must last at least this long
MIN_FF_DROP = 1.0           # fuel flow must drop at least this (GPH) in a stroke
MIN_EGT_RISE = 8            # each cyl EGT must rise at least this (°F) to count
AIRBORNE_IAS = 40           # kts, to ensure we're flying


@dataclass
class CylPeak:
    cyl: int
    peak_egt: float
    ff_at_peak: float
    time_at_peak: str


@dataclass
class LeanStroke:
    start_time: str
    end_time: str
    start_ff: float
    end_ff: float
    peaks: list[CylPeak] = field(default_factory=list)  # one per cylinder, ordered by cyl#
    gami_spread: Optional[float] = None                 # GPH between first & last to peak
    peak_order: list[int] = field(default_factory=list) # cyl numbers, leanest-peaking first


def _smooth(vals, window=SMOOTH_WINDOW):
    """Simple centered moving average, ignoring None."""
    n = len(vals)
    out = [None] * n
    half = window // 2
    for i in range(n):
        acc, cnt = 0.0, 0
        for j in range(max(0, i - half), min(n, i + half + 1)):
            v = vals[j]
            if v is not None:
                acc += v; cnt += 1
        out[i] = (acc / cnt) if cnt else None
    return out


def stroke_to_dict(s: "LeanStroke") -> dict:
    """Serialize a LeanStroke for the API/UI."""
    return {
        "start_time": s.start_time,
        "end_time": s.end_time,
        "start_ff": s.start_ff,
        "end_ff": s.end_ff,
        "gami_spread": s.gami_spread,
        "peak_order": s.peak_order,
        "peaks": [
            {"cyl": p.cyl, "peak_egt": p.peak_egt,
             "ff_at_peak": p.ff_at_peak, "time_at_peak": p.time_at_peak}
            for p in s.peaks
        ],
    }


def get_stroke_curves(operation_id: int, start_time: str, end_time: str,
                      num_cyl: int = None) -> dict:
    """Return smoothed EGT-vs-fuel-flow curves for one stroke window, for the
    classic GAMI plot (fuel flow on X, each EGT on Y).

    Returns {"fuel_flow": [...], "egt1": [...], ...} over the stroke, ordered
    by sample time.
    """
    if num_cyl is None:
        from efis_data_manager.config import load_config
        num_cyl = load_config().get("num_cylinders", NUM_CYL_DEFAULT)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT timestamp, fuel_flow, egt1, egt2, egt3, egt4, egt5, egt6
               FROM fdl_data
               WHERE operation_id = ? AND timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp""",
            (operation_id, start_time, end_time)
        ).fetchall()
    finally:
        conn.close()

    egt_cols = [f"egt{i}" for i in range(1, num_cyl + 1)]
    result = {"fuel_flow": _smooth([r["fuel_flow"] for r in rows]),
              "time": [r["timestamp"] for r in rows]}
    for c in egt_cols:
        result[c] = _smooth([r[c] for r in rows])
    return result


def detect_gami_strokes(operation_id: int, num_cyl: int = None) -> list[LeanStroke]:
    """Detect and analyze GAMI lean strokes for an operation.

    Returns a list of the best lean stroke per stable-power leaning session.
    """
    if num_cyl is None:
        from efis_data_manager.config import load_config
        num_cyl = load_config().get("num_cylinders", NUM_CYL_DEFAULT)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT timestamp, fuel_flow, rpm1, aux2 AS map, indicated_airspeed,
                      egt1, egt2, egt3, egt4, egt5, egt6
               FROM fdl_data WHERE operation_id = ? ORDER BY timestamp""",
            (operation_id,)
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < MIN_STROKE_SECONDS:
        return []

    egt_cols = [f"egt{i}" for i in range(1, num_cyl + 1)]
    times = [r["timestamp"] for r in rows]
    ff = _smooth([r["fuel_flow"] for r in rows])
    rpm = [r["rpm1"] for r in rows]
    mp = [r["map"] for r in rows]
    ias = [r["indicated_airspeed"] for r in rows]
    egts = {c: _smooth([r[c] for r in rows]) for c in egt_cols}

    # 1. Find candidate lean strokes: sustained fuel-flow decrease while
    #    airborne and power stable.
    strokes = _find_lean_strokes(times, ff, rpm, mp, ias)

    # 2. Analyze each; keep valid ones (all cylinders peak within the stroke)
    analyzed = []
    for (i0, i1) in strokes:
        stroke = _analyze_stroke(times, ff, egts, egt_cols, i0, i1)
        if stroke:
            analyzed.append((i0, i1, stroke))

    # 3. Group strokes into sessions (contiguous-ish) and pick best per session.
    #    Sessions: strokes separated by a large time gap belong to different
    #    leaning events. Within a session, prefer the deepest FF drop (usually
    #    the first, full lean-through-peak-to-LOP stroke).
    return _best_per_session(analyzed)


def _find_lean_strokes(times, ff, rpm, mp, ias):
    """Return [(i0, i1), ...] index ranges of sustained fuel-flow decreases
    during stable-power airborne flight."""
    n = len(ff)
    strokes = []
    i = 0
    while i < n - 1:
        # Skip until we're in a valid flying/stable state with decreasing FF
        if not _valid_point(ff, rpm, mp, ias, i):
            i += 1
            continue
        # Start of a potential decrease
        if ff[i] is None or ff[i + 1] is None or ff[i + 1] >= ff[i]:
            i += 1
            continue

        j = i
        # Extend while FF is generally decreasing (allow small noise bumps)
        rise_tolerance = 0
        while j < n - 1 and _valid_point(ff, rpm, mp, ias, j + 1):
            if ff[j + 1] is None:
                break
            if ff[j + 1] < ff[j]:
                rise_tolerance = 0
                j += 1
            elif ff[j + 1] - ff[j] < 0.3 and rise_tolerance < 3:
                # small noise bump — tolerate a few
                rise_tolerance += 1
                j += 1
            else:
                break

        # Validate the stroke
        if _stroke_qualifies(times, ff, rpm, mp, i, j):
            strokes.append((i, j))
        i = j + 1
    return strokes


def _valid_point(ff, rpm, mp, ias, i):
    return (ff[i] is not None and rpm[i] is not None and mp[i] is not None
            and ias[i] is not None and ias[i] > AIRBORNE_IAS
            and rpm[i] > 1800)


def _stroke_qualifies(times, ff, rpm, mp, i0, i1):
    if i1 - i0 < MIN_STROKE_SECONDS:
        return False
    ff_drop = ff[i0] - ff[i1]
    if ff_drop < MIN_FF_DROP:
        return False
    # Power stability across the stroke
    rpms = [rpm[k] for k in range(i0, i1 + 1) if rpm[k] is not None]
    maps = [mp[k] for k in range(i0, i1 + 1) if mp[k] is not None]
    if rpms and (max(rpms) - min(rpms)) > RPM_STABLE_BAND:
        return False
    if maps and (max(maps) - min(maps)) > MAP_STABLE_BAND:
        return False
    return True


def _analyze_stroke(times, ff, egts, egt_cols, i0, i1) -> Optional[LeanStroke]:
    """Find each cylinder's EGT peak within [i0, i1]. Returns a LeanStroke or
    None if the cylinders don't show clear peaks."""
    peaks = []
    for idx, c in enumerate(egt_cols, start=1):
        series = egts[c]
        best_v, best_k = None, None
        for k in range(i0, i1 + 1):
            v = series[k]
            if v is None:
                continue
            if best_v is None or v > best_v:
                best_v, best_k = v, k
        if best_v is None:
            return None
        # Require a real rise: peak must exceed the stroke's starting EGT
        start_v = next((series[k] for k in range(i0, i1 + 1) if series[k] is not None), None)
        if start_v is not None and (best_v - start_v) < MIN_EGT_RISE:
            # This cylinder didn't clearly peak within the stroke
            return None
        peaks.append(CylPeak(cyl=idx, peak_egt=round(best_v, 1),
                             ff_at_peak=round(ff[best_k], 2),
                             time_at_peak=times[best_k]))

    # GAMI spread: FF difference between first-to-peak (richest) and
    # last-to-peak (leanest). First to peak = highest FF at peak.
    ffs = [p.ff_at_peak for p in peaks]
    gami_spread = round(max(ffs) - min(ffs), 2)

    # Peak order rich-to-lean: the cylinder that peaks first (at the highest
    # fuel flow) is richest and listed first, down to the leanest (lowest FF).
    order = [p.cyl for p in sorted(peaks, key=lambda p: p.ff_at_peak, reverse=True)]

    return LeanStroke(
        start_time=times[i0], end_time=times[i1],
        start_ff=round(ff[i0], 2), end_ff=round(ff[i1], 2),
        peaks=peaks, gami_spread=gami_spread, peak_order=order,
    )


def _best_per_session(analyzed):
    """Group analyzed strokes into sessions by time gaps and pick the best
    (deepest FF drop) in each."""
    from datetime import datetime
    if not analyzed:
        return []

    def parse(ts):
        return datetime.fromisoformat(ts)

    # Sort by start time
    analyzed.sort(key=lambda a: a[2].start_time)

    sessions = []
    current = [analyzed[0]]
    for prev, cur in zip(analyzed, analyzed[1:]):
        gap = (parse(cur[2].start_time) - parse(prev[2].end_time)).total_seconds()
        if gap > 120:   # >2 min gap => new leaning session
            sessions.append(current)
            current = [cur]
        else:
            current.append(cur)
    sessions.append(current)

    best = []
    for sess in sessions:
        # deepest FF drop
        chosen = max(sess, key=lambda a: a[2].start_ff - a[2].end_ff)
        best.append(chosen[2])
    return best
