# Design Document

## Overview

Percent power is the engine's output as a percentage of RATED_HP. The GRT HXr
EFIS computes and displays it (Dial Source 5 = "Percent Power 1") but does NOT
log it in the FDL flight-data feed. This feature reproduces the EFIS's exact
computation post-flight, for every logged FDL sample, so Percent_Power can be
plotted and summarized in the dashboard alongside the parameters that ARE logged
(RPM, MAP, CHT, EGT, fuel flow).

The computation is a fixed eight-step algorithm (Requirement 2) driven by an
engine Power_Map (Requirement 1) read from the aircraft's EFIS settings backup
via the `efis-settings-import` spec's `Settings_Parser`. There is no approximate
fallback: if the Power_Map or RATED_HP is unavailable, Percent_Power is
unavailable for the whole flight (Requirement 5.2); if a single sample is
missing an input, Percent_Power is undefined for that sample only
(Requirement 5.1).

Design shape:

- A **pure computation module** `src/efis_data_manager/power.py` that owns the
  Power_Map data model, the interpolation/extrapolation helper, the ISA
  standard-temperature helper, and the eight-step `percent_power()` function.
  It has no Flask, database, or file-I/O dependencies, which makes it directly
  property-testable and matches how the rest of the analysis logic is layered.
- A **minimal read interface** onto `efis-settings-import` (a single function,
  `load_current_settings_sids()`, added to `efis_settings.py` as part of this
  spec's work, that returns the parsed SID→value map for the current settings
  backup plus a Temp_Units flag). `power.py` degrades gracefully when that
  module or a backup is absent — it simply yields `None` and the flight shows
  no Percent_Power.
- **Compute-on-the-fly integration** in the dashboard: the
  `/api/flight/<id>/data` route appends a `percent_power` series computed per
  row (mirroring how `detect_episodes` and the extremes route compute from raw
  rows), and `get_flight_stats` gains a cruise-average Percent_Power stat. No
  database schema change; no stored/cached values to go stale.

### Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Where computation lives | New pure module `power.py` | No Flask/DB/IO deps → property-testable in isolation; matches `analysis.py` layering. |
| Compute-on-the-fly vs stored | On-the-fly in the data route + stats | Mirrors `detect_episodes`/extremes; avoids `fdl_data`/`operations` schema migration; no stale cache when the Power_Map changes after a settings re-import. Cost is trivial (a few float ops per 1-second row, ~18k rows worst case). |
| MAP source per sample | Reuse `resolve_aux()` (`manifold_pressure`) with `internal_map` fallback | The dashboard already sources MAP through the single aux resolver; MAP must agree with the plotted MAP series. |
| Power_Map storage | Read from settings backup on demand; not stored in dashboard config | Requirement 1 mandates reading from the backup; storing a copy would drift from the aircraft. |
| Clamping extrapolated % | Do NOT clamp the computed value | GRT extrapolates beyond table ends (Requirement 3.2); clamping would diverge from the EFIS. Display may round; see Error Handling. |

## Architecture

```mermaid
flowchart LR
    subgraph settings["efis-settings-import (dependency)"]
        SP["Settings_Parser<br/>SID → value map (dict[str,str])"]
        SEL["Settings_Selector<br/>current backup"]
        LCS["load_current_settings_sids()<br/>→ (sids, temp_units_celsius)"]
    end

    subgraph power["power.py (new, pure)"]
        RD["load_power_map()"]
        PM["PowerMap dataclass"]
        IN["interp() linear<br/>interp + extrapolate"]
        ISA["isa_std_oat_f()"]
        PP["percent_power(rpm, map, palt, oat_f, power_map)"]
    end

    subgraph dash["dashboard/app.py"]
        DATA["/api/flight/&lt;id&gt;/data"]
        AUX["resolve_aux() → MAP column"]
    end

    subgraph an["analysis.py"]
        FS["get_flight_stats()<br/>avg_percent_power_cruise"]
    end

    FE["flight.html<br/>PARAMS + Power panel"]

    SP --> SEL
    SEL --> LCS
    LCS -->|"(sids, temp_units_celsius)"| RD
    RD --> PM
    PM --> PP
    IN --> PP
    ISA --> PP
    DATA -->|per row| PP
    AUX -->|MAP per row| DATA
    DATA -->|percent_power[] + aux_params| FE
    PM --> FS
    PP --> FS
```

`power.py` sits below the dashboard and analysis layers and depends only on the
settings read-interface. Both the data route and `get_flight_stats` obtain the
`PowerMap` once (via `load_power_map()`), then call `percent_power()` per row.

### Settings dependency and graceful degradation

`efis-settings-import` (now built, in `efis_data_manager.efis_settings`)
provides `Settings_Parser.parse(path) -> ParsedBackup` — whose `.sids` is a
`dict[str, str]` mapping SID **string** keys (e.g. `"167"`) to raw string
values — and `Settings_Selector.select(list[ParsedBackup]) -> SelectionResult`
whose `.current` is the chosen `ParsedBackup` (or `None`). Temp_Units lives in
SID 345 (`"345"` == `"1"` means Celsius). `percent-power` consumes a **minimal**
slice of that behind one integration point.

**Single integration point — a new helper added to `efis_settings.py` as part
of this spec's work.** This is a thin, spec-appropriate addition that keeps
`power.py`'s coupling to exactly one function. It mirrors the archive discovery
already done by `Import_Workflow.detect_new_backup` (glob
`<archive_root>/Settings/Settings*.bak` and `*.dat`, parse each tolerantly,
select the current one):

```python
# Added to efis_data_manager.efis_settings (percent-power's single integration point).
def load_current_settings_sids() -> Optional[tuple[dict[str, str], bool]]:
    """Return (sids, temp_units_celsius) for the current archived Settings
    backup, or None.

    archive_root = Path(load_config()["archive_path"]). Globs
    <archive_root>/Settings/Settings*.bak and *.dat, parses each via
    Settings_Parser (skipping parse failures), and selects the current one via
    Settings_Selector. On success returns (parsed.sids, temp_units_celsius),
    where:

        temp_units_celsius = parsed.sids.get("345", "").strip() == "1"

    Returns None when no archived backup exists, none parses, or selection
    fails (SelectionResult.current is None). Note SID keys in parsed.sids are
    STRINGS (e.g. "167").
    """
```

`power.py` accesses this behind a thin adapter so that — for build-order
independence — a missing module degrades to "unavailable" rather than an
ImportError:

```python
def _read_settings():
    """Return (sids, temp_units_celsius) or None.

    Wrapped in try/except ImportError so percent-power stays import-safe even
    if efis_settings is unavailable at build time: any ImportError or absent
    backup yields None => Percent_Power unavailable (Req 5.2). sids is a
    dict[str, str] with STRING SID keys (e.g. "167").
    """
    try:
        from efis_data_manager.efis_settings import load_current_settings_sids
    except ImportError:
        return None
    return load_current_settings_sids()  # (sids, temp_units_celsius) or None
```

This keeps the coupling to one function. `load_current_settings_sids()` returns
the `(sids, temp_units_celsius)` tuple directly, so `_read_settings` simply
forwards it (or `None`).

## Components and Interfaces

All new code is in `src/efis_data_manager/power.py`. Names follow the
requirements Glossary (`Power_Map`, `RATED_HP`, `MAP55_Column`, `MAP75_Column`,
`Altitude_Column`, `Delta_HP_Column`, `Standard_OAT`, `OAT_Correction`,
`Percent_Power`).

### Power-map SID layout (constants)

Confirmed from GRT's `Settings.h`. Each column is 10 consecutive SIDs. SID keys
in `ParsedBackup.sids` are **strings**, so the constants are string keys and the
column ranges yield string SIDs (`[str(s) for s in range(...)]`).

```python
SID_RATED_HP = "167"
SID_RPM_COLUMN      = [str(s) for s in range(168, 178)]   # "168".."177"  RPM breakpoints
SID_MAP55_COLUMN    = [str(s) for s in range(178, 188)]   # "178".."187"  MAP for 55% at that RPM
SID_MAP75_COLUMN    = [str(s) for s in range(188, 198)]   # "188".."197"  MAP for 75% at that RPM
SID_ALTITUDE_COLUMN = [str(s) for s in range(198, 208)]   # "198".."207"  altitude breakpoints
SID_DELTA_HP_COLUMN = [str(s) for s in range(208, 218)]   # "208".."217"  delta HP at that altitude
```

Cells are read with string keys, e.g. `sids.get("167")`, `sids.get(str(s))`.

### PowerMap dataclass

```python
@dataclass(frozen=True)
class PowerMap:
    """The engine Power_Map read from the settings backup (Req 1).

    The RPM/MAP55/MAP75 arrays are the *valid* Sea_Level_Rows only (blank and
    invalid rows already dropped), sorted ascending by RPM. The altitude/
    delta_hp arrays are the *valid* Altitude_Rows only, sorted ascending by
    altitude. All arrays used for interpolation are guaranteed length >= 1 by
    construction; percent_power additionally requires >= 2 points to
    interpolate/extrapolate a line (see build validity below).
    """
    rated_hp: float
    rpm: list[float]        # sea-level rows, ascending by rpm
    map55: list[float]      # paired with rpm[]
    map75: list[float]      # paired with rpm[]
    altitude: list[float]   # altitude rows, ascending
    delta_hp: list[float]   # paired with altitude[]
```

### Public functions

```python
def build_power_map(sids: dict[str, str]) -> Optional[PowerMap]:
    """Build a PowerMap from a SID->value map (Req 1.1-1.7).

    sids has STRING keys (e.g. sids.get("167")), matching ParsedBackup.sids.

    Returns None (Percent_Power unavailable) when:
      - RATED_HP (SID "167") is missing or <= 0 (Req 1.6), or
      - no valid Sea_Level_Row exists (Req 1.7), or
      - no valid Altitude_Row exists (Req 1.7).

    Row rules:
      - Sea_Level_Row (rpm/map55/map75 by index i): blank when rpm <= 0
        (Req 1.3); otherwise included only when rpm, map55, map75 are ALL > 0
        (Req 1.4).
      - Altitude_Row (altitude/delta_hp by index i): blank when altitude <= 0
        (Req 1.5). delta_hp may be any value (including <= 0).
      - A SID absent or non-numeric is treated as blank for its row.
    """

def load_power_map() -> Optional[PowerMap]:
    """Obtain the current settings backup and build the PowerMap (Req 1, 5.2).

    Calls `_read_settings()` (which forwards `load_current_settings_sids()`);
    on `None` returns `None`, otherwise unpacks `(sids, temp_units_celsius)`
    and calls `build_power_map(sids)`. Returns None when no settings
    module/backup is available or the map is invalid. Callers treat None as
    'Percent_Power unavailable for the flight'.
    """

def interp(xs: list[float], ys: list[float], x: float) -> float:
    """Linear interpolation with 2-point extrapolation at the ends (Req 3).

    xs must be ascending and paired with ys, len >= 2.
      - x between xs[i] and xs[i+1]: interpolate on that segment (Req 3.1).
      - x below xs[0]: extrapolate the line through (xs[0], ys[0]) and
        (xs[1], ys[1]) (Req 3.2).
      - x above xs[-1]: extrapolate the line through (xs[-2], ys[-2]) and
        (xs[-1], ys[-1]) (Req 3.2).
    """

def isa_std_oat_f(pressure_alt_ft: float) -> float:
    """ISA standard OAT in degrees F at a pressure altitude (Req 2.8, 4.2).

    ISA: 15 C (59 F) at sea level, lapse 1.98 C / 1000 ft.
      std_C = 15.0 - 1.98 * (pressure_alt_ft / 1000.0)
      std_F = std_C * 9/5 + 32
    Equivalently std_F = 59.0 - 3.564 * (pressure_alt_ft / 1000.0).
    """

def oat_to_fahrenheit(oat: float, temp_units_celsius: bool) -> float:
    """Convert a sample OAT to Fahrenheit if Temp_Units is Celsius (Req 4.3)."""

def percent_power(rpm, map_inhg, pressure_alt, oat_f,
                  power_map: PowerMap) -> Optional[float]:
    """Percent_Power for one FDL_Sample via GRT's 8-step algorithm (Req 2).

    Returns None if any of rpm, map_inhg, pressure_alt, oat_f is None or not a
    finite number (Req 5.1). rated_hp > 0 is guaranteed by PowerMap
    construction, so step 9 never divides by zero. Result is a fraction * 100
    (e.g. 65.3 means 65.3%); NOT clamped (GRT extrapolates, Req 3.2).
    """
```

### MAP sourcing (per sample)

The dashboard sources MAP through the single aux resolver `resolve_aux()`
(`aux_map.py`), which returns the `manifold_pressure` parameter mapped to a
concrete `aux*` column. Percent_Power MUST use the SAME value that is plotted as
MAP, so it reads the resolved MAP column, falling back to `internal_map` when no
aux channel is mapped to `manifold_pressure`:

```python
def map_column_for(resolved_aux: dict) -> str:
    """Return the fdl_data column that holds manifold pressure (inHg).

    Prefer the aux channel the user mapped to 'manifold_pressure' (the same
    column the dashboard plots as MAP); otherwise fall back to 'internal_map'.
    """
    info = resolved_aux.get("manifold_pressure")
    return info["channel"] if info else "internal_map"
```

This helper lives in `power.py` (pure; takes the already-resolved dict) and the
data route passes `resolve_aux()` output into it, keeping `aux_map` as the one
place aux meaning is derived.

## Data Models

### PowerMap (in memory only)

Defined above. Built from the SID map, holds only valid rows. There is **no
database table and no schema change**: Percent_Power is computed on the fly from
the existing `fdl_data` columns (`rpm1`, the resolved MAP column,
`pressure_altitude`, `oat`) plus the PowerMap. This mirrors `detect_episodes`
and the extremes route, which recompute from raw rows rather than storing
derived series.

### Configuration

No new required config. Percent_Power reads its inputs from existing `fdl_data`
columns and the Power_Map from the settings backup. The MAP source is already
governed by the existing `aux_mapping` config (`manifold_pressure`). Temp_Units
comes from the settings backup (SID 345), not dashboard config. No
`analysis_thresholds` entry is needed.

### API response addition

`/api/flight/<id>/data` gains one engine series and one `aux_params`-style
descriptor so the client can plot it:

- `data["engine"]["percent_power"]`: list aligned with `timestamps`; each entry
  is a float percent or `null` when that sample lacks a valid input (Req 5.1),
  and the entire list is omitted (key absent) when `load_power_map()` is `None`
  (Req 5.2, 6.3).
- When present, a descriptor `{ "key": "percent_power", "label": "% Power",
  "group": "engine", "precision": 0 }` is appended to `data["aux_params"]` so
  the existing `mergeAuxParams()` path registers it in `PARAMS` and it appears
  in the picker and the Power panel with graceful omission when absent (Req 6.1,
  6.3). Reusing `aux_params` avoids editing `flight.html`'s static `PARAMS`
  block and matches how mapped aux parameters already flow in.

### Worked reference table (this aircraft: 190 HP IO-360)

Used as the golden reference for tests (Req 7). RATED_HP = 190.

Sea-level rows (RPM / MAP55 / MAP75, inHg):

| RPM  | MAP55 | MAP75 |
|------|-------|-------|
| 2000 | 21.6  | 26.7  |
| 2100 | 21.1  | 26.1  |
| 2200 | 20.6  | 25.5  |
| 2300 | 20.1  | 24.9  |
| 2400 | 19.7  | 24.4  |
| 2500 | 19.2  | 23.8  |
| 2600 | 18.7  | 23.2  |
| 2700 | 18.2  | 22.7  |

Altitude rows (ALTITUDE ft / DELTA_HP):

| Altitude | Delta_HP |
|----------|----------|
| 2000     | 2.3      |
| 4000     | 4.6      |
| 6000     | 6.9      |
| 8000     | 9.2      |
| 10000    | 11.5     |
| 12000    | 13.8     |
| 14000    | 16.0     |

(Exact interior values are illustrative of the shape the user provided —
MAP55 ~21.6→18.2, MAP75 ~26.7→22.7, delta 2.3→16.0. The tests pin to the
actual imported values; hand-computed reference outputs are derived in the
Testing Strategy.)

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

Reflection on redundancy (performed before writing the properties below):

- Requirements 3.1 (interpolate between breakpoints) and 3.2 (extrapolate beyond
  ends) are consolidated into a single interp-correctness statement — `interp`
  agrees with the exact linear function on every segment, and its extension is
  collinear with the first/last two points beyond the ends. Requirements 2.2,
  2.3, and 2.6 are all uses of `interp`, so they are validated by that same
  statement.
- Requirements 1.3, 1.4 (sea-level row validity) are consolidated into one
  row-validity property; Requirement 1.5 (altitude row validity) is a second.
- Requirements 1.6 and 1.7 (map/rated unavailability) are consolidated into one
  unavailability property.
- Requirement 4.1 (correction evaluated in F) is folded into the OAT_Correction
  property; Requirement 4.2 (Standard_OAT definition) into the ISA property.
- Requirements 2.5, 2.7, 2.9 are single arithmetic steps with no independent
  universal statement; they are covered by the pipeline reference examples in
  the Testing Strategy rather than a property.

### Property 1: Interpolation and extrapolation are piecewise-linear

*For any* ascending breakpoints `xs` (length ≥ 2) with paired `ys`, and *for
any* real `x`: `interp(xs, ys, x)` equals `ys[i] + t*(ys[i+1]-ys[i])` where the
segment `i` and blend `t = (x-xs[i])/(xs[i+1]-xs[i])` are chosen as the segment
containing `x`, the first segment when `x < xs[0]`, or the last segment when
`x > xs[-1]` (so `t` may be `< 0` or `> 1` for extrapolation). In particular
`interp(xs, ys, xs[k]) == ys[k]` at every breakpoint.

**Validates: Requirements 3.1, 3.2, 2.2, 2.3, 2.6**

### Property 2: Sea-level power percentage is affine through the 55%/75% anchors

*For any* PowerMap and *for any* RPM, letting `m55 = interp(rpm, map55, RPM)`
and `m75 = interp(rpm, map75, RPM)` with `m55 != m75`, the sea-level power
percentage as a function of actual MAP is the affine line through
`(m55, 55)` and `(m75, 75)`: it equals `55` at `MAP == m55`, `75` at
`MAP == m75`, and `55 + (MAP - m55) * (75 - 55) / (m75 - m55)` elsewhere,
including linear extrapolation beyond the anchors.

**Validates: Requirements 2.4**

### Property 3: ISA standard OAT is linear in altitude and 59 °F at sea level

*For any* pressure altitude `h` (feet), `isa_std_oat_f(h)` equals
`59.0 - 3.564 * (h / 1000.0)` (equivalently `(15 - 1.98*h/1000)*9/5 + 32`); in
particular `isa_std_oat_f(0) == 59.0`, and the value decreases by `3.564 °F`
per 1000 ft.

**Validates: Requirements 2.8, 4.2**

### Property 4: OAT correction matches its Fahrenheit formula and is monotonic

*For any* Standard_OAT `s` and sample OAT `o` (both °F, with `460 + s > 0` and
`460 + o > 0`), the OAT_Correction equals `sqrt((460 + s) / (460 + o))`; it
equals `1` exactly when `o == s`, is `> 1` when `o < s` (colder-than-standard
air makes more power), and is strictly decreasing in `o`.

**Validates: Requirements 2.8, 4.1**

### Property 5: OAT unit conversion

*For any* numeric temperature `v`, `oat_to_fahrenheit(v, temp_units_celsius=True)`
equals `v * 9/5 + 32`, and `oat_to_fahrenheit(v, temp_units_celsius=False)`
equals `v` unchanged.

**Validates: Requirements 4.3**

### Property 6: Sea-level row validity

*For any* set of 10 (rpm, map55, map75) rows built into a PowerMap, a row at
index `i` appears in the built `rpm`/`map55`/`map75` arrays *if and only if*
`rpm[i] > 0` AND `map55[i] > 0` AND `map75[i] > 0` (rows with `rpm[i] <= 0`, or
any of the three not `> 0`, are excluded), and the surviving rows preserve their
pairing.

**Validates: Requirements 1.3, 1.4**

### Property 7: Altitude row validity

*For any* set of 10 (altitude, delta_hp) rows built into a PowerMap, a row at
index `i` appears in the built `altitude`/`delta_hp` arrays *if and only if*
`altitude[i] > 0` (delta_hp may hold any value), and the surviving rows preserve
their pairing.

**Validates: Requirements 1.5**

### Property 8: Map unavailability

*For any* SID map in which RATED_HP (SID 167) is missing or `<= 0`, OR that
yields zero valid Sea_Level_Rows, OR that yields zero valid Altitude_Rows,
`build_power_map` returns `None`; otherwise (RATED_HP `> 0` and at least one
valid Sea_Level_Row and at least one valid Altitude_Row) it returns a non-`None`
PowerMap.

**Validates: Requirements 1.6, 1.7**

### Property 9: Percent_Power is undefined on missing or invalid inputs

*For any* valid PowerMap and *for any* input tuple `(rpm, map_inhg,
pressure_alt, oat_f)` in which at least one element is `None` or a non-finite
number (`NaN`/`inf`), `percent_power(...)` returns `None`.

**Validates: Requirements 5.1**

### Property 10: Cruise-average percent power ignores undefined samples

*For any* set of cruise-classified rows and a valid PowerMap, the per-flight
cruise-average Percent_Power equals the arithmetic mean of the defined
per-sample Percent_Power values over those rows (samples whose Percent_Power is
`None` are excluded), and equals `None` when no cruise sample has a defined
Percent_Power.

**Validates: Requirements 6.2**

## Error Handling

- **Settings module absent (efis-settings-import not yet built).**
  `_read_settings()` catches `ImportError` and returns `None`;
  `load_power_map()` returns `None`; the flight shows no Percent_Power
  (Requirement 5.2). No crash, no log spam beyond a single debug line.
- **No settings backup / no verified backup.** `load_current_settings_sids()`
  returns `None` (no archived backup, none parses, or selection fails) →
  `load_power_map()` returns `None` (Requirement 5.2).
- **Invalid / incomplete Power_Map.** `build_power_map` returns `None` when
  RATED_HP ≤ 0 or missing (Requirement 1.6) or there is no valid Sea_Level_Row
  or Altitude_Row (Requirement 1.7). Non-numeric or absent SIDs are treated as
  blank cells, never raising.
- **Missing per-sample inputs.** `percent_power` returns `None` when any of
  RPM, MAP, pressure altitude, or OAT is `None` or non-finite (Requirement
  5.1). The series holds `null` at that index; the plot uses `connectgaps:
  false` so gaps show as gaps, not zeros (Requirement 6.3).
- **Divide-by-zero.** Step 9 divides by RATED_HP, which is guaranteed `> 0` by
  PowerMap construction, so it never divides by zero. Step-2.4 anchor
  interpolation guards `m75 == m55` (degenerate map at that RPM) by returning
  `None` for that sample rather than dividing by zero.
- **OAT correction domain.** `460 + OAT` and `460 + Standard_OAT` are positive
  for any physically plausible temperature; if a corrupt sample yields
  `460 + OAT <= 0`, the sample returns `None` rather than taking a square root
  of a negative or dividing by zero.
- **Extrapolation sanity.** The COMPUTATION does NOT clamp: GRT extrapolates
  beyond the table ends (Requirement 3.2), so clamping would diverge from the
  EFIS. Extreme engine states (e.g. a spurious 40 inHg MAP) can therefore yield
  values above 100% or below 0%; these are surfaced faithfully. Display MAY
  round to a whole percent for readability (precision 0 in the descriptor); it
  does not clamp the underlying series.

## Testing Strategy

### Dual approach

- **Property tests** (Hypothesis) cover the universal properties above:
  interpolation/extrapolation correctness, the affine sea-level anchor, ISA and
  OAT-correction formulas, unit conversion, row validity, unavailability, the
  None-on-missing-input rule, and the cruise-average rule.
- **Unit/example tests** pin the 8-step pipeline to hand-computed reference
  values from this aircraft's real Power_Map, and pin the SID layout.
- **Integration tests** cover the Flask route wiring (series present/omitted,
  descriptor present/omitted) and the stats plumbing — these are NOT property
  tests because they exercise Flask/DB behavior that does not vary meaningfully
  with input (Requirements 5.2, 6.1, 6.3 route side).

### Property-based testing setup

- Library: **Hypothesis** (Python). Add `hypothesis` to `requirements.txt`.
  Do NOT hand-roll random testing.
- Each property test runs **≥ 100 iterations** (Hypothesis default `max_examples`
  is 100; set explicitly with `@settings(max_examples=100)` where clarity helps).
- Each property test carries a comment tag referencing its design property:
  `# Feature: percent-power, Property N: <property text>`.
- Each of Properties 1–10 is implemented by a **single** property-based test.

Generators:

- Ascending breakpoints: generate a list of distinct floats and sort; pair with
  arbitrary finite `ys`. Choose `x` as `xs[i] + t*(xs[i+1]-xs[i])` with `t` in
  `[0,1]` for interior and `t < 0` / `t > 1` for extrapolation.
- PowerMaps: generate valid rows (positive rpm/map55/map75; ascending) plus
  altitude/delta rows; also generate deliberately invalid SID maps for
  Property 8.
- Inputs for Property 9: draw one of the four fields as `None`/`NaN`/`inf`.

### Example / reference tests (Requirements 2, 7)

Hand-computed reference points from the worked table (RATED_HP = 190). Example
walk-through of the 8 steps at RPM = 2400, MAP = 22.0 inHg, pressure altitude =
6000 ft, OAT = 40 °F:

1. `map55_at_rpm = interp(rpm, map55, 2400) = 19.7`
2. `map75_at_rpm = interp(rpm, map75, 2400) = 24.4`
3. sea-level % = `55 + (22.0 - 19.7) * (75 - 55) / (24.4 - 19.7)`
   `= 55 + 2.3 * 20 / 4.7 ≈ 64.79%`
4. sea-level HP = `0.6479 * 190 ≈ 123.1 HP`
5. `delta_hp = interp(altitude, delta_hp, 6000) = 6.9`
6. altitude-corrected HP = `123.1 + 6.9 = 130.0 HP`
7. `Standard_OAT = isa_std_oat_f(6000) = 59 - 3.564*6 ≈ 37.6 °F`;
   `OAT_Correction = sqrt((460+37.6)/(460+40)) = sqrt(497.6/500) ≈ 0.99760`;
   corrected HP `≈ 130.0 * 0.99760 ≈ 129.7 HP`
8. `Percent_Power = 129.7 / 190 * 100 ≈ 68.3%`

The test suite recomputes these anchors from the imported table and asserts
`percent_power(2400, 22.0, 6000, 40.0, pm)` is within tolerance (e.g. ±0.5%) of
the hand value, and repeats for several points including at least one
extrapolation case (RPM below 2000 / above 2700, altitude above 14000, MAP
outside the 55–75% band). Where a known EFIS-displayed % is available for a
recorded engine state, an example test asserts the computed value is within a
small tolerance of it (Requirements 7.1, 7.2).

Additional example tests:

- **SID layout (Requirement 1.2):** build from the worked-table SID dict; assert
  each column array equals the expected values in order.
- **RATED_HP read (Requirement 1.1):** SID `"167"` present → `rated_hp` equals
  it; absent → `None`.
- **Degenerate anchor:** a map where `map55 == map75` at some RPM → that sample
  returns `None`.

### Integration tests (Flask)

- With a valid PowerMap (patched `load_power_map`), `GET /api/flight/<id>/data`
  returns `engine.percent_power` aligned to `timestamps` and a `percent_power`
  descriptor in `aux_params` (Requirements 6.1).
- With `load_power_map()` patched to `None`, the same route omits both the
  series and the descriptor (Requirements 5.2, 6.3), and
  `get_flight_stats(...).avg_percent_power_cruise` is `None` so the detail route
  reports it as `None`.
- With a valid map, the detail route reports a numeric
  `avg_percent_power_cruise` (Requirement 6.2).

### Integration points to modify

- `efis_settings.py` (modified): add `load_current_settings_sids() ->
  Optional[tuple[dict[str, str], bool]]` — the single integration point.
  Globs `<archive_root>/Settings/Settings*.bak` and `*.dat` (archive_root =
  `Path(load_config()["archive_path"])`), parses each via `Settings_Parser`
  (skipping parse failures), selects the current one via `Settings_Selector`,
  and returns `(parsed.sids, temp_units_celsius)` (with
  `temp_units_celsius = parsed.sids.get("345", "").strip() == "1"`) or `None`.
- `power.py` (new): all pure logic + `map_column_for`, `load_power_map`,
  `_read_settings` (thin adapter over `load_current_settings_sids()`).
- `dashboard/app.py` `api_flight_data`: obtain `load_power_map()` once; if not
  `None`, resolve the MAP column via `map_column_for(resolve_aux())`, compute
  `percent_power` per row into `engine["percent_power"]`, and append the
  descriptor to `aux_params`.
- `analysis.py` `FlightStats` + `get_flight_stats`: add
  `avg_percent_power_cruise: Optional[float]`, computed over the existing
  `cruise` row classification using `load_power_map()`; `dashboard/app.py`
  `api_flight_detail` adds it to the JSON; the flight page shows it in the
  summary when non-`None`.
