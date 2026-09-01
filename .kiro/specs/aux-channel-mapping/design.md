# Design — Configurable EIS Aux Channel Mapping

## Overview

Replace the four hardcoded aux→meaning assumptions with a config-driven mapping
resolved through a single helper. Add an aux-mapping UI and an engine-category
selector to Settings. Persist aux4–aux6 so they can be mapped. Keep the current
install's behavior as the default so nothing regresses.

## Current state (verified)

Hardcoded aux convention appears in 4 places:
- `dashboard/app.py` `api_flight_data`: `map←aux2`, `amps←aux1`, `fuel_pressure←aux3`.
- `dashboard/app.py` `api_flight_extremes`: SELECT aliases `aux2 AS map, aux1 AS amps`.
- `analysis.py` `detect_episodes`: SELECT alias `aux3 AS fuel_pressure`.
- `dashboard/templates/flight.html` `PARAMS`: keys `map`, `amps`, `fuel_pressure`.

DB stores only aux1–aux3. Parser already reads aux1–aux6 + carb_temp/coolant_temp.
Config load = shallow merge over `DEFAULT_CONFIG`; `/api/config` POST = `dict.update`.

## Config schema

Add to `DEFAULT_CONFIG` (config.py):

```python
"engine_category": "traditional",   # "traditional" | "water_cooled"
"aux_mapping": {
    # channel -> {"parameter": <key>, "label": <str>, "unit": <str>}
    "aux1": {"parameter": "amps",          "label": "Amps",       "unit": "A"},
    "aux2": {"parameter": "manifold_pressure", "label": "MAP",    "unit": "\""},
    "aux3": {"parameter": "fuel_pressure", "label": "Fuel Press", "unit": "psi"},
    "aux4": {"parameter": "none", "label": "", "unit": ""},
    "aux5": {"parameter": "none", "label": "", "unit": ""},
    "aux6": {"parameter": "none", "label": "", "unit": ""},
},
```

Defaults preserve today's behavior (Req 6). Because load_config does a shallow
merge, the whole `aux_mapping` dict is taken from saved config if present; that's
fine — the UI always writes the full dict. A helper will backfill per-channel
defaults defensively when reading.

### Known parameters (fixed catalog)
`none, amps, manifold_pressure, fuel_pressure, fuel_level_left,
fuel_level_right, vacuum, coolant_pressure, carb_temp, custom`

Each has a canonical label + unit + display precision. `custom` uses the user's
label/unit. This catalog is defined once (Python side, mirrored to the client).

## The single resolver

Add to a new small module `aux_map.py` (or a section of config.py):

```python
PARAM_CATALOG = {                      # parameter -> (default_label, unit, precision)
    "amps": ("Amps", "A", 0),
    "manifold_pressure": ("MAP", '"', 1),
    "fuel_pressure": ("Fuel Press", "psi", 1),
    "fuel_level_left": ("Fuel L", "gal", 1),
    "fuel_level_right": ("Fuel R", "gal", 1),
    "vacuum": ("Vacuum", "inHg", 1),
    "coolant_pressure": ("Coolant Press", "psi", 1),
    "carb_temp": ("Carb Temp", "\u00b0F", 0),
    "custom": (None, None, 1),          # label/unit from config
}

def get_aux_mapping(config=None) -> dict:
    """Return {channel: {parameter,label,unit}} with defaults backfilled."""

def resolve_aux(config=None) -> dict:
    """Return {parameter_key: {channel, label, unit, precision}} for every
    channel that is MAPPED. A channel is mapped IFF parameter not in (None,
    'none') AND it resolves to a non-empty label (Custom with empty label is
    treated as unmapped). Mapping is the sole gate: data presence in a column
    is irrelevant — an unmapped channel is omitted entirely. Parameter->channel
    is unique; if two channels map to the same parameter (misconfig), first wins
    and a warning is logged. This is the ONE place meaning is derived."""
```

All four call sites consume `resolve_aux()`:
- `api_flight_data`: build the engine bucket keys dynamically from resolve_aux()
  instead of hardcoding map/amps/fuel_pressure. Emit one series per mapped
  parameter, keyed by parameter_key, pulling from the channel column.
- `api_flight_extremes`: build extreme specs for mapped parameters that have an
  extreme (amps min/max, MAP max, fuel-pressure has no extreme button today —
  keep as-is). Skip parameters not mapped.
- `detect_episodes`: fuel-pressure episodes only run IF fuel_pressure is mapped;
  read the resolved channel column instead of hardcoded aux3.
- `flight.html PARAMS`: the client fetches the resolved mapping (new tiny
  endpoint `/api/aux-map` or embed in `/api/flight/<id>/data` response) and adds
  a PARAMS entry per mapped parameter with the configured label.

### Client delivery of the mapping
Add the resolved mapping to the `/api/flight/<id>/data` response as
`data["aux_params"] = [{key,label,group:"engine",precision}, ...]`, containing
ONLY mapped channels (resolve_aux already excludes unmapped ones). The flight
page merges these into PARAMS at load, so only mapped aux parameters ever appear
in pickers/panels (Req 3.2). Avoids a second round-trip.

## Data flow (per aux-derived parameter)

FDL column (auxN) → DB auxN → resolve_aux() says auxN = parameter P with label L
→ api_flight_data emits engine[P] = column auxN → flight.html PARAMS[P] = {L} →
panel/extreme/episode use P.

## Persist aux4–aux6 (Req 5)

- `database.py` SCHEMA_SQL: add `aux4, aux5, aux6 REAL` to fdl_data CREATE.
- `_ensure_schema` migration loop: extend `("aux1","aux2","aux3")` to include
  aux4–aux6 (ALTER TABLE ADD COLUMN, no-op-safe).
- `_insert_fdl_data`: add r.aux4/aux5/aux6 to the row tuple, column list, and
  placeholders (46 → 49).
- Old rows keep NULL for aux4–aux6; acceptable (no reimport).

## Settings UI

### Engine Configuration card (reordered + category)
- New first control: Engine Category select (traditional / water_cooled).
- Traditional selected → show Engine Type, then Number of Cylinders (order
  swapped from today), then the aux-mapping section.
- Water-cooled selected → hide those + show an info `.note` inviting Rotax
  testers; hide the aux-mapping section too (out of scope for v1).

### Aux Channel Mapping section (traditional only)
- One row per channel aux1..aux6: label "Aux N", a `<select>` of the parameter
  catalog, and (shown only when Custom) a label input + unit input.
- Loaded from `/api/config` `aux_mapping`; saved back in the flat POST like the
  other fields. Client sends the full `aux_mapping` object.
- Tooltips (reuse the `.help`/`.tip` pattern) explaining each channel and the
  Custom option, and noting the EIS applies scale/offset (values are already in
  engineering units).

## Error handling / edge cases

- **Mapping is the sole gate (core rule):** an unmapped channel is omitted from
  the data response, parameter picker, extreme buttons, and episodes — even if
  its column is full of data. Nothing is ever surfaced from column data alone.
- Duplicate parameter across channels: resolver keeps first, logs a warning; UI
  could later warn, not required for v1.
- Custom with empty label: treated as UNMAPPED (hidden). No "AuxN" fallback.
- A mapped parameter that happens to have all-NULL data (e.g. aux4 mapped but old
  flights predate its storage): it IS listed (it's mapped), the series is just
  empty; panels/extremes show nothing for it. No error. (Mapping, not data,
  decides visibility.)
- Fuel-pressure thresholds already exist; episodes run only when fuel_pressure
  is mapped.

## Testing strategy

- Unit-test `resolve_aux()`: default (no config) yields amps/MAP/fuel-pressure;
  custom mapping; duplicate handling; None channels excluded.
- Verify `/api/flight/<id>/data` emits the mapped parameter keys against the
  running dashboard with (a) default config and (b) a remapped config.
- Verify extremes and episodes honor the mapping (fuel-pressure episode absent
  when unmapped).
- Verify aux4–aux6 migration adds columns on the existing DB without data loss
  (check row counts before/after).
- Regression: with default config, flight page Power panel + extreme buttons +
  episodes match current output for flight 3.
- Settings: category toggle shows/hides correctly; save round-trips the mapping.

## Out of scope (deferred)

- Water-cooled/Rotax: coolant temp surfacing end-to-end, 2-cylinder handling,
  high-RPM scaling. Only the signpost message ships now.
- Re-importing old flights to backfill aux4–aux6.
- Fuel-level totalizing / tank math; fuel level is just a displayable series.
