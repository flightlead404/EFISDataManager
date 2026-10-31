# Bugfix Requirements Document

## Introduction

The per-flight `fuel_used` statistic is grossly overstated whenever the pilot resets the
EFIS fuel-total gauge after starting the engine — the normal procedure after a refuel.
The `fuel_total` channel is a fuel-REMAINING gauge that counts DOWN as fuel burns, but it
STEPS UP (in a single sample) when the pilot resets it to a full-tank value. The current
calculation takes `first - last` of `fuel_total` and `abs()`-es the sign, which counts
the upward reset step as if it were fuel burned.

Concrete case (operation 34, KPIM -> KCCO, 2026-10-24, GRT FDL 0005.CSV): the raw
`fuel_total` series starts at 18.8 gal (engine start / pre-takeoff), jumps to 42.0 gal at
sample index 12 when the pilot reset the gauge after refuel, then counts down to 39.0 gal
by the end. The real burn for this ~15-minute flight (~11 gph) is about 3 gallons
(42.0 -> 39.0). The current formula computes `18.8 - 39.0 = -20.2`, then `abs()` -> 20.2
gal — off by ~17 gal, entirely from the reset step.

This refuel-then-reset pattern is TYPICAL, not an edge case: engine start, EFIS start,
fuel reset, so the gauge jumps up seconds-to-minutes after the log begins. The fuel-burn
calculation must be robust to a mid-log upward reset regardless of where in the log it
occurs.

The fix computes fuel burned as the sum of the per-sample DECREASES in `fuel_total`,
ignoring any upward step (a reset/refuel). This is position-independent: the reset may
occur anywhere in the log and may occur more than once; only downward movement counts as
burn.

### Explicitly out of scope (user decision)

Two theoretically-ambiguous cases are OUT OF SCOPE and need NOT be handled:

- A DOWNWARD manual gauge correction (reducing `fuel_total` to fix an over-read) is
  indistinguishable from real burn using `fuel_total` alone; it will be counted as burn.
  Accepted.
- Sensor-noise transients during the reset (a brief dip-then-rise) may contribute a
  negligible phantom decrease. No noise threshold / smoothing is required. Accepted.

(No fuel-flow-integral cross-check is included; the gauge sum-of-decreases is the whole
fix.)

### Bug Condition

`fuel_total` is the GRT "Fuel Total" FDL channel (`fdl_parser.py`), a fuel-remaining
gauge. A "reset" is any sample-to-sample INCREASE in `fuel_total` within a single
operation's records.

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type Operation         // its ordered fuel_total sample series
  OUTPUT: boolean

  // Some sample is greater than the preceding sample (a reset / refuel step),
  // so first - last does not equal the true fuel burned.
  RETURN EXISTS i IN 1..LEN(X.fuel_totals)-1 SUCH THAT
         X.fuel_totals[i] > X.fuel_totals[i-1]
END FUNCTION
```

### Property Specification (Fix Checking)

```pascal
// Property: Fix Checking - burn ignores upward resets
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  // Fuel used equals the summed downward movement only; upward steps add nothing.
  ASSERT result.fuel_used = SUM over i IN 1..N-1 of MAX(0, fuel_totals[i-1] - fuel_totals[i])
END FOR
```

Where **F** is the fuel-used calculation before the fix (`abs(first - last)`) and **F'**
is the sum-of-decreases calculation after the fix.

### Preservation Goal (Preservation Checking)

```pascal
// Property: Preservation Checking - clean (no-reset) flights unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  // With no upward step, fuel_total is monotonically non-increasing, so the
  // sum of per-sample decreases telescopes to exactly first - last.
  ASSERT F'(X).fuel_used = (fuel_totals[0] - fuel_totals[-1])
  ASSERT F'(X) = F(X)   // identical to the old result for non-reset logs
END FOR
```

Concrete counterexample demonstrating the bug: GRT FDL 0005.CSV (op 34), fuel_total
18.8 -> (reset) 42.0 -> 39.0; old formula yields 20.2 gal; correct burn is ~3.0 gal.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN an operation's `fuel_total` series contains an upward step (a post-start refuel reset) THEN the system computes `fuel_used = abs(fuel_totals[0] - fuel_totals[-1])`, which includes the reset magnitude as burned fuel
1.2 WHEN the last recorded `fuel_total` is GREATER than the first (because a reset raised the gauge above its starting value) THEN the `first - last` result is negative and the `abs()` fallback reports the magnitude of the net rise as fuel used
1.3 WHEN the computed `fuel_used` is persisted to the `operations` row and surfaced in the dashboard / CSV export THEN the pilot sees a fuel-burn figure that can exceed the physically possible burn for the flight duration by an order of magnitude

### Expected Behavior (Correct)

2.1 WHEN an operation has two or more `fuel_total` samples THEN the system SHALL compute `fuel_used` as the sum of per-consecutive-sample decreases: `SUM of MAX(0, prev - curr)`
2.2 WHEN the `fuel_total` series contains one or more upward steps (resets/refuels) THEN the system SHALL NOT count any upward step as fuel burned, regardless of where in the log the step occurs
2.3 WHEN the `fuel_total` series contains multiple upward steps THEN the system SHALL correctly accumulate the drains between every pair of resets
2.4 WHEN `fuel_used` is computed for op 34 (GRT FDL 0005.CSV, 18.8 -> 42.0 -> 39.0) THEN the result SHALL be approximately 3.0 gallons, consistent with ~15 minutes at ~11 gph

### Unchanged Behavior (Regression Prevention)

3.1 WHEN an operation's `fuel_total` series is monotonically non-increasing (no reset) THEN the system SHALL CONTINUE TO report `fuel_used = fuel_totals[0] - fuel_totals[-1]` (the sum of decreases telescopes to the same value)
3.2 WHEN an operation has fewer than two non-null `fuel_total` samples THEN the system SHALL CONTINUE TO report `fuel_used = None`
3.3 WHEN other per-operation statistics are computed in the same path (avg_fuel_flow_cruise, hourmeter, oil, CHT/EGT, airborne_seconds, etc.) THEN the system SHALL leave them unchanged — this fix touches ONLY the fuel_used computation
3.4 WHEN the fuel-burn scope is considered THEN the system SHALL CONTINUE TO compute over the whole operation's records (engine start to shutdown), matching the current record set — this fix does not change which records are included

### Data Repair (already-imported flights)

4.1 WHEN this fix lands THEN already-imported operations (such as op 34) retain the old inflated `fuel_used` in the database until recomputed; the stored value is NOT auto-corrected by changing the algorithm alone
4.2 The design SHALL decide whether to include a one-time recompute/migration that recalculates `fuel_used` for existing operations from their stored `fdl_data` samples, or whether correction relies on re-import. (Open design decision; not prescribed here.)

## Notes (non-authoritative — for design phase)

- Location: `compute_fuel_used` logic is inline in `database.py` (the per-operation stats
  update, around lines ~732-739): `fuel_totals = [...]; fuel_used = fuel_totals[0] -
  fuel_totals[-1]; if fuel_used < 0: fuel_used = abs(...)`. Replace the first/last/abs with
  the sum-of-decreases accumulation over consecutive non-null samples.
- The "Fuel Total" -> `fuel_total` channel mapping is in `fdl_parser.py`; per-sample
  `fuel_total` is persisted in `fdl_data`, so a recompute migration (if chosen) has the
  raw series available without re-reading the CSV.
- Validation: property tests (monotonic series -> equals first-last; inject one or more
  upward steps -> result unchanged by those steps) plus a concrete regression fixture
  built from GRT FDL 0005.CSV asserting ~3.0 gal. Keep an exploration test that FAILS on
  the current `abs(first-last)` code (expected ~20.2 today) to confirm the bug before the
  fix, per the bugfix workflow.
- Backlog item: captured 2026-10-31 while reviewing the KPIM->KCCO flight. Not yet scheduled.
