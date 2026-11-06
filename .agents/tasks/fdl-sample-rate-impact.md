# FDL Sample-Rate Impact: 1000 ms (1 Hz) → 200 ms (5 Hz)

Read-only investigation. No code was changed.

## Summary answer (bottom line)

Changing the FDL recording interval from 1000 ms to 200 ms would **break data
import and corrupt the dashboard**, not just degrade performance. The root
cause is that the entire pipeline keys and renders on a **whole-second
timestamp** parsed with `"%Y-%m-%d %H:%M:%S"`. At 5 Hz, five consecutive
samples carry the **identical timestamp**, which collides with the design in two
places:

1. **Silent data loss on import.** Records are deduplicated on the unique key
   `(timestamp, content_hash)`. Within one second, any two of the five 200 ms
   samples that happen to have identical field values (routine in steady cruise)
   hash the same and get dropped by `INSERT OR IGNORE`. You do **not** get 5×
   the rows — you get an unpredictable fraction, biased toward discarding
   exactly the calm cruise data the trends and GAMI analysis depend on.

2. **Degenerate time axis.** Charts plot the raw second-resolution timestamp
   string on the X axis, so the up-to-5 surviving samples per second stack on
   the same X coordinate. Zoom/pan, "jump to timestamp," and episode start/peak
   markers all resolve to a whole second and cannot distinguish sub-second
   points.

Most *scalar* statistics (max/min/avg, spreads, percent-power) stay numerically
correct on whatever rows survive, because they do not assume a cadence. But
anything that (a) depends on the row *set* being complete, (b) measures a
duration or rate from timestamps, or (c) counts "N samples" as a proxy for
seconds will be wrong. Details and a per-computation table below.

**The crux question — does the parser derive time from a real timestamp or from
sample index?** It derives time from the FDL's `Date`/`Time` columns (good, no
`row == 1 second` index math anywhere), **but only at 1-second resolution**. The
format string drops any sub-second field. That single limitation is what
cascades into everything else.

---

## Evidence

### 1. Parser: real timestamp, but second-resolution only — the crux

`src/efis_data_manager/fdl_parser.py`, `_parse_row()`:

```python
timestamp = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
```

- Time **is** read from the file's `Date`/`Time` columns (COLUMN_MAP → `date`,
  `time`), not inferred from the row index. There is no `row_number * 1000ms`
  logic anywhere.
- But `%H:%M:%S` has **no sub-second component**. Five 200 ms samples in the
  same wall-clock second all parse to the same `datetime`. If the GRT FDL emits
  a fractional-seconds `Time` (e.g. `12:00:00.2`) the `strptime` would raise
  `ValueError` and `_parse_row` returns `None` → **every sub-second row is
  dropped on import** (caught per-row in `parse_fdl_file`, logged as a warning,
  skipped). If instead GRT emits a separate sub-second column or repeats the
  whole second, the rows survive parsing but collide downstream (sections 2–3).
- The `FDLRecord` docstring and the file header both state the assumption
  explicitly: *"1-second samples"* / *"Subsequent rows: 1-second samples"*.
- `Tick` is parsed (`tick` field) but is **not** used to derive time anywhere —
  it is stored and ignored by all analysis. It is *not* a fallback sub-second
  clock in the current code.

### 2. Database: identity key collides at 5 Hz → silent row loss

`src/efis_data_manager/database.py`:

- Schema: `timestamp TEXT NOT NULL,  -- ISO 8601`, plus
  `CREATE UNIQUE INDEX idx_fdl_data_record_identity ON fdl_data(timestamp, content_hash)`.
- `record_content_hash()` hashes **all FDLRecord fields except timestamp**
  (`_CONTENT_HASH_FIELDS`). So the record identity is `(second-resolution
  timestamp, content hash of the values)`.
- `_insert_fdl_data()` uses `INSERT OR IGNORE`. Two samples in the same second
  with identical sensor values ⇒ identical key ⇒ **second one silently
  dropped**. In steady cruise, CHT/EGT/RPM/oil barely change over 200 ms, so
  this is the *common* case, not an edge case.
- The de-dup pass in `import_fdl_files` / the migration (`seen_keys`,
  `(r.timestamp.isoformat(), record_content_hash(r))`) does the same collapse
  **before** segmentation, so the loss happens even if the unique index were
  relaxed.
- Operation identity is `(start_time, end_time, record_count)`. Since
  `record_count` now reflects the *post-collision* surviving count, it is stable
  but no longer "samples per flight"; harmless to identity, misleading as a stat.
- `GAP_THRESHOLD_SECONDS = 180` operation segmentation is timestamp-gap based
  and **unaffected** (180 s ≫ 0.2 s).

### 3. Dashboard charts: time axis and volume

`src/efis_data_manager/dashboard/app.py`, `api_flight_data()`:

- Docstring states the design assumption verbatim: *"Returns every 1-second
  sample… up to ~5hr = ~18k points… render fine in WebGL… no downsampling."*
- The endpoint sends **one array element per row** with the raw `timestamp`
  string as the time value. No downsampling, no server-side decimation, no
  `LIMIT`. At 5 Hz a 5 hr flight is ~90k points per series across ~30 series.
  - **Performance:** ~5× payload, ~5× JSON build time in Python, ~5× client
    memory and WebGL vertices. "18k renders fine" was the stated headroom; 90k
    is beyond the assumption and may stutter on pan/zoom, especially on older
    hardware.
  - **Correctness:** with a second-resolution timestamp string as X, the up-to-5
    surviving sub-second points per second share an X value → vertical stacks /
    an ambiguous axis. `extremes` ("jump to") and episode markers point to a
    whole second only.
- `api_flight_extremes()` and the per-row loops have the same per-row cost
  multiplier and the same timestamp-resolution ambiguity for the returned
  `timestamp`.

### 4. Analysis — cadence dependence, computation by computation

`src/efis_data_manager/analysis.py`:

- **Scalar stats** (`_max_col`, `_min_col`, `_avg_col`, `_max_spread`,
  cylinder max/avg, `max_rpm`, voltages, fuel-flow max/avg, GAMI min-EGT): these
  iterate rows and do **not** assume cadence. They stay correct *on the rows
  that survive import*. The averages are **unweighted per-row means**, so they
  are already biased toward whatever the sample density is; losing a
  density-dependent subset (section 2) shifts cruise averages unpredictably
  rather than by a clean factor.
- **Episode detection** (`_detect_episodes_series`, `detect_episodes`): hysteresis
  and durations are **timestamp-driven** (`_elapsed_s`, `episode_min_gap_s` in
  *seconds*), so they are conceptually cadence-correct. **But** `_elapsed_s`
  between two samples in the same second returns `0.0`, so at 5 Hz the
  "recovered for `min_gap_s`" logic and all `duration_s` values are computed off
  a clock that only ticks once per second — sub-second structure is invisible,
  and durations are quantized to whole seconds. Correct-ish in magnitude,
  low-resolution.
- **CHT shock-cooling rate** (`_cht_cooling_rate_series`): explicitly built
  around a **W-second window** with `_CHT_COOLING_WINDOW_S = 10`,
  `_CHT_COOLING_WINDOW_TOL_S = 2.0`, `_CHT_COOLING_SMOOTH_S = 5`, all in
  **seconds** against the real elapsed time (`dt/60`). This is the one place
  that was deliberately written to be cadence-agnostic, and it **stays
  correct** — provided timestamps have real spacing. With second-resolution
  timestamps, pairing/smoothing still works at ~1 s granularity (the comment
  even says "FDL is ~1 Hz… raw per-sample first differences are noise"); the
  noise rationale weakens at 5 Hz but the math does not break.
- **Trends / anomalies / oil consumption** (`compute_trends`,
  `_rolling_avg_by_hours`, `compute_oil_consumption*`, `detect_anomalies`):
  operate on **per-flight summary rows and engine hours (hourmeter)**, not on
  raw sample cadence. **Unaffected** by sample rate (beyond inheriting any bias
  from section 2's lossy per-flight stats).
- **Duration fields** (`FDLFile.duration_seconds`, `Operation.duration_seconds`):
  `int((end - start).total_seconds())` from timestamps. **Correct** regardless
  of cadence.

### 5. GAMI — sample-count windows, NOT seconds

`src/efis_data_manager/gami.py`:

- `MIN_STROKE_SECONDS = 15` is named "seconds" but is used as a **sample count**:
  `if i1 - i0 < MIN_STROKE_SECONDS` and `if len(rows) < MIN_STROKE_SECONDS`
  compare **index differences / row counts**, not elapsed time. At 5 Hz, 15
  samples = 3 seconds, so the minimum-stroke gate becomes 5× too short and valid
  strokes/garbage are mis-classified. **Silently wrong.**
- `SMOOTH_WINDOW = 5` (moving average) and the rise-tolerance bump counters are
  also **sample-count** windows. At 5 Hz they cover 1 s instead of 5 s, so
  smoothing is 5× weaker and noise rejection degrades. **Silently wrong.**
- The session-grouping gap `gap > 120` *is* true seconds (uses
  `datetime.fromisoformat`), so that part is fine.
- Net: GAMI spread detection would mis-fire or miss strokes at 5 Hz, and at the
  same time loses data to the import collision (steady-power leaning is exactly
  where 200 ms samples repeat values). **Breaks / silently wrong.**

### 6. Power — pure per-sample, cadence-independent

`src/efis_data_manager/power.py`: `percent_power()` is pure per-row math
(interpolation over the Power_Map). No cadence assumption. **Unaffected**
(aside from inheriting section-2 row loss in the cruise average).

### 7. Currency — unrelated

`src/efis_data_manager/currency.py` handles Seattle Avionics / GRT chart &
nav-DB downloads. **No FDL involvement; no impact.**

### 8. Tests bake in 1 Hz

`tests/test_airspeed_g_episodes.py` (`_insert`) generates rows with
`timedelta(seconds=i)` — exactly 1 Hz, one row per second — and reasons in
"N samples == N seconds" (e.g. `g=[1.0]*40 + [3.4]*40 + ...`). These tests would
**not** catch a sub-second regression and encode the 1 Hz assumption as truth.
The GAMI thresholds (`MIN_STROKE_SECONDS`) are likewise validated only at 1 Hz.

---

## Impact table

| Computation / area | Location | Impact | Verdict |
|---|---|---|---|
| Timestamp parse | `fdl_parser._parse_row` (`%H:%M:%S`) | Drops sub-second resolution; raises+skips row if Time has fractions | **Breaks** (data loss) |
| Import de-dup key | `database` `(timestamp, content_hash)` + `INSERT OR IGNORE` | Same-second, same-value samples silently dropped | **Breaks** (silent loss) |
| Scalar max/min/avg, spreads | `analysis._max_col` etc. | No cadence assumption; correct on surviving rows, but averages inherit import bias | Mostly correct / mildly wrong |
| Percent power | `power.percent_power` | Pure per-row | **Correct** |
| Episode hysteresis & durations | `analysis._detect_episodes_series`, `_elapsed_s` | Timestamp-based but quantized to whole seconds; sub-second invisible | Low-res, not 5× wrong |
| CHT cooling rate | `analysis._cht_cooling_rate_series` | Second-based windows vs real elapsed time | **Correct** |
| Trends / anomalies / oil | `analysis.compute_trends`, `compute_oil_*` | Per-flight / engine-hour based | **Correct** |
| Duration fields | `FDLFile/Operation.duration_seconds` | Timestamp delta | **Correct** |
| GAMI stroke length / `len(rows)` gate | `gami.MIN_STROKE_SECONDS` used as sample count | 5× too short; mis-detects strokes | **Silently wrong** |
| GAMI smoothing / noise tolerance | `gami.SMOOTH_WINDOW`, rise-tolerance | Sample-count windows → 5× shorter time span | **Silently wrong** |
| Chart time axis | `dashboard.api_flight_data` (raw timestamp X) | 5 points share one X value | **Breaks** (degenerate axis) |
| Chart / API volume | `api_flight_data`, `api_flight_extremes` | ~5× rows: payload, JSON build, WebGL vertices (~18k→~90k) | Perf degraded |
| DB storage | `fdl_data` rows | Up to ~5× rows per flight (less, due to collision loss) | Perf / size |
| Operation identity | `(start_time, end_time, record_count)` | Stable but record_count no longer "samples" | Correct, misleading stat |

---

## Conclusions and recommendations (implement nothing here)

The pipeline is architecturally timestamp-driven in the right places (durations,
episode hysteresis, cooling rate, segmentation), so it is *closer* to supporting
higher rates than a naive "1 row = 1 second" design would be. The blockers are
concentrated and fixable:

1. **Sub-second timestamps (must fix first — the crux).** Make `_parse_row`
   accept fractional seconds. Confirm the actual GRT 200 ms FDL header/`Time`
   format first (does it emit `HH:MM:SS.s`, a separate millisecond column, or
   reuse `Tick`?), then parse with a format that preserves sub-second (e.g.
   `%H:%M:%S.%f`, or combine `Tick`/ms column into the datetime). Without this,
   nothing else matters.

2. **Record identity (must fix with #1).** Once timestamps are unique per
   sample, the `(timestamp, content_hash)` collision disappears. Verify the
   unique index and the `seen_keys` de-dup both operate on the sub-second
   timestamp. If GRT can still emit genuinely identical sub-second rows,
   incorporate `Tick` (or a row ordinal) into the identity key.

3. **GAMI windows (correctness).** Convert `MIN_STROKE_SECONDS`, `SMOOTH_WINDOW`,
   and the index-based gates in `gami.py` from **sample counts** to
   **time-based** windows (derive expected cadence from timestamp spacing, or
   express windows in seconds and translate to indices per flight). This is the
   only pure "N samples == N seconds" assumption in the analysis layer.

4. **Dashboard volume + axis (perf + correctness).** With unique sub-second
   timestamps the X-axis degeneracy resolves itself. But ~90k points/series
   exceeds the stated ~18k design headroom — add server-side (or client-side)
   downsampling / LTTB decimation for the time-series endpoint, or cap returned
   resolution, to keep render responsive.

5. **Tests.** Add fixtures at 5 Hz (sub-second `timedelta`) and decouple GAMI
   test expectations from the 1 Hz "sample == second" shortcut, so a future
   cadence change is caught.

6. **Storage.** ~5× `fdl_data` growth per flight. Sqlite handles it, but note
   it for retention/VACUUM and for the per-flight API cost above.

If the FDL cannot be changed to emit sub-second time at all, 200 ms logging is
**not safely supportable** — the import layer will silently discard most of the
extra samples and the charts will mis-render, with no error surfaced to the user.
