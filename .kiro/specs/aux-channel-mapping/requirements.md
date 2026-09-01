# Requirements — Configurable EIS Aux Channel Mapping

## Introduction

Owners wire their GRT EIS auxiliary inputs (Aux1–Aux6) to different sensors.
Today the EFIS Data Manager hardcodes a single install's convention
(aux1 = amps, aux2 = MAP, aux3 = fuel pressure) in four separate places, so the
dashboard mislabels or misroutes data for any owner with a different panel. This
feature makes the aux-channel-to-parameter mapping user-configurable in the
Analysis dashboard Settings, and routes all display/analysis through that
mapping.

Scope is intentionally limited to **traditional (air-cooled) engines**, the
primary Vans/RV community target. Water-cooled/Rotax support (coolant temp
surfacing, 2-cylinder, high-RPM scaling) is explicitly deferred; this feature
adds only a signpost inviting Rotax testers.

Grounding facts (verified against code + EIS Rev M manual):
- The EIS has 6 aux inputs; scale/offset are applied inside the EIS, so FDL aux
  values are already in engineering units.
- The parser already reads aux1–aux6 (and carb_temp, coolant_temp) but the DB
  stores only aux1–aux3; aux4–aux6 are dropped at insert.
- Common aux uses per the manual: manifold pressure, fuel pressure, fuel level,
  vacuum, coolant pressure, ammeter, and custom 0–5V.

## Requirements

### Requirement 1 — Aux channel mapping configuration
**User Story:** As an owner, I want to tell the tool what each EIS aux channel
represents, so my data is labeled and analyzed correctly.

#### Acceptance Criteria
1. WHEN the user opens Settings THEN the system SHALL show an "Aux Channel
   Mapping" section listing each supported aux channel (Aux1–Aux3 at minimum;
   Aux4–Aux6 only if/when stored — see Req 5).
2. WHEN the user assigns an aux channel THEN the system SHALL offer a fixed list
   of parameter choices: None, Manifold Pressure, Fuel Pressure, Fuel Level Left,
   Fuel Level Right, Vacuum, Coolant Pressure, Amps, Carb Temp, Custom.
3. WHEN a channel is set to Custom THEN the system SHALL let the user enter a
   display label and unit.
4. WHEN the user saves THEN the system SHALL persist the mapping in config and
   apply it without reimporting data.
5. IF no mapping is configured THEN the system SHALL fall back to the current
   default (aux1=Amps, aux2=Manifold Pressure, aux3=Fuel Pressure) so existing
   installs are unaffected.

### Requirement 2 — Single resolver drives all display and analysis
**User Story:** As a developer, I want one place that resolves aux channels to
meanings, so display, extremes, and episodes never disagree.

#### Acceptance Criteria
1. WHEN any dashboard feature needs an aux-derived parameter THEN it SHALL obtain
   the channel→parameter mapping from one shared resolver, not a local hardcode.
2. WHEN the mapping changes THEN the flight panels, "jump to" extreme buttons,
   and episode detection SHALL all reflect the new mapping consistently.
3. WHEN a parameter (e.g. fuel pressure) is not mapped to any channel THEN
   features depending on it (e.g. fuel-pressure episodes) SHALL be skipped rather
   than reading the wrong channel.

### Requirement 3 — Dynamic parameter availability in the flight view
**User Story:** As a user, I want the flight-detail panels to offer exactly the
aux-derived parameters I actually have, labeled the way I configured them.

#### Acceptance Criteria
1. WHEN the flight data loads THEN the "+ add parameter" list SHALL include an
   entry for each mapped aux channel using its configured label.
2. **Mapping is the sole gate.** WHEN an aux channel is unmapped (parameter =
   None, or has no label) THEN it SHALL be treated as if it does not exist —
   absent from the parameter picker, extreme buttons, episodes, and the data
   response — REGARDLESS of whether the underlying column contains data. Data
   presence SHALL NEVER cause a parameter to appear; only an explicit mapping
   with a label does.
3. WHEN MAP is mapped THEN the default Power panel SHALL continue to show it; IF
   MAP is not mapped THEN the Power panel SHALL omit it gracefully.
4. WHEN a channel is Custom with an empty label THEN it SHALL be treated as
   unmapped (not shown), NOT given a fallback "AuxN" label.

### Requirement 4 — Engine category selector
**User Story:** As the maintainer, I want an engine-category choice that keeps the
UI honest about what's supported.

#### Acceptance Criteria
1. WHEN the user opens Settings Engine Configuration THEN the first control SHALL
   be an "Engine Category" selector with options "Traditional (air-cooled)" and
   "Water-Cooled (e.g. Rotax)".
2. WHEN "Traditional" is selected THEN the system SHALL show Engine Type first,
   then Number of Cylinders (reordered from today).
3. WHEN "Water-Cooled" is selected THEN the system SHALL hide the traditional
   engine fields and show a short message stating water-cooled/Rotax support is
   not yet implemented and inviting the user to volunteer as a tester.
4. WHEN the category is saved THEN it SHALL persist in config.

### Requirement 5 — Persist aux4–aux6 (enabling condition)
**User Story:** As an owner using aux4–aux6, I want those channels available to map.

#### Acceptance Criteria
1. WHEN a flight is imported THEN the system SHALL store aux4, aux5, aux6 in
   fdl_data (added via the existing lightweight-migration pattern).
2. WHEN the DB is an older one lacking these columns THEN the migration SHALL add
   them without data loss.
3. Note: previously imported flights will have NULL aux4–aux6 (not re-imported);
   this is acceptable. Mapping UI MAY show all six channels; unmapped/empty ones
   simply produce no data.

### Requirement 6 — No regression for the existing install
**User Story:** As the current user, I want everything to keep working exactly as
now if I don't touch the new settings.

#### Acceptance Criteria
1. WHEN no aux mapping or engine category is set THEN behavior SHALL match the
   current hardcoded defaults (amps/MAP/fuel-pressure), including the Power panel,
   extreme buttons, and fuel-pressure episodes.
2. WHEN the feature ships THEN existing config files SHALL load without error and
   gain the new keys via the existing shallow-merge default backfill.
