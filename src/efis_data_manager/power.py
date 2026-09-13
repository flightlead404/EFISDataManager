# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Percent-power computation (GRT HXr EFIS method), pure and I/O-free.

Percent power is the engine output as a percentage of RATED_HP. The EFIS
computes it (Dial Source 5) from an engine Power_Map carried in the settings
backup, but does NOT log it in the FDL feed. This module reproduces the EFIS's
exact eight-step algorithm post-flight for each logged sample.

Layering:

- Pure math + data model here (``interp``, ``isa_std_oat_f``,
  ``oat_to_fahrenheit``, ``PowerMap``, ``build_power_map``, ``percent_power``,
  ``map_column_for``): no Flask, database, or file I/O.
- ``load_power_map`` / ``_read_settings`` are thin adapters over the single
  ``efis_settings.load_current_settings_sids`` integration point; they degrade
  to ``None`` (Percent_Power unavailable) when the settings module or a backup
  is absent.

There is no approximate fallback: if the Power_Map or RATED_HP is unavailable,
Percent_Power is unavailable for the whole flight; if a single sample is missing
an input, Percent_Power is undefined for that sample only. The computation does
NOT clamp — GRT extrapolates beyond the table ends.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure math helpers (Requirement 3, 2.8, 4.2, 4.3)
# ---------------------------------------------------------------------------

def interp(xs: list[float], ys: list[float], x: float) -> float:
    """Linear interpolation with 2-point extrapolation at the ends (Req 3).

    ``xs`` must be ascending and paired with ``ys``, len >= 2.
      - ``x`` between ``xs[i]`` and ``xs[i+1]``: interpolate on that segment
        (Req 3.1).
      - ``x`` below ``xs[0]``: extrapolate the line through ``(xs[0], ys[0])``
        and ``(xs[1], ys[1])`` (Req 3.2).
      - ``x`` above ``xs[-1]``: extrapolate the line through ``(xs[-2],
        ys[-2])`` and ``(xs[-1], ys[-1])`` (Req 3.2).

    ``interp(xs, ys, xs[k]) == ys[k]`` at every breakpoint.
    """
    n = len(xs)
    if n < 2:
        raise ValueError("interp requires at least two breakpoints")

    # Below the first breakpoint: extrapolate on the first segment.
    if x < xs[0]:
        i = 0
    # Above the last breakpoint: extrapolate on the last segment.
    elif x > xs[-1]:
        i = n - 2
    else:
        # Interior (or exactly on a breakpoint): find the containing segment.
        # Exact-breakpoint identity: return ys[k] without any float blend.
        for k in range(n):
            if x == xs[k]:
                return ys[k]
        i = 0
        for j in range(n - 1):
            if xs[j] <= x <= xs[j + 1]:
                i = j
                break

    x0, x1 = xs[i], xs[i + 1]
    y0, y1 = ys[i], ys[i + 1]
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def isa_std_oat_f(pressure_alt_ft: float) -> float:
    """ISA standard OAT in degrees F at a pressure altitude (Req 2.8, 4.2).

    ISA: 15 C (59 F) at sea level, lapse 1.98 C / 1000 ft. Equivalently
    ``std_F = 59.0 - 3.564 * (pressure_alt_ft / 1000.0)``.
    """
    return 59.0 - 3.564 * (pressure_alt_ft / 1000.0)


def oat_to_fahrenheit(oat: float, temp_units_celsius: bool) -> float:
    """Convert a sample OAT to Fahrenheit if Temp_Units is Celsius (Req 4.3)."""
    if temp_units_celsius:
        return oat * 9 / 5 + 32
    return oat


# ---------------------------------------------------------------------------
# Power-map SID layout (constants). SID keys in ParsedBackup.sids are STRINGS.
# ---------------------------------------------------------------------------

SID_RATED_HP = "167"
SID_RPM_COLUMN = [str(s) for s in range(168, 178)]       # "168".."177"
SID_MAP55_COLUMN = [str(s) for s in range(178, 188)]     # "178".."187"
SID_MAP75_COLUMN = [str(s) for s in range(188, 198)]     # "188".."197"
SID_ALTITUDE_COLUMN = [str(s) for s in range(198, 208)]  # "198".."207"
SID_DELTA_HP_COLUMN = [str(s) for s in range(208, 218)]  # "208".."217"


# ---------------------------------------------------------------------------
# PowerMap data model (Requirement 1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PowerMap:
    """The engine Power_Map read from the settings backup (Req 1).

    The rpm/map55/map75 arrays are the *valid* Sea_Level_Rows only (blank and
    invalid rows already dropped), sorted ascending by RPM. The altitude/
    delta_hp arrays are the *valid* Altitude_Rows only, sorted ascending by
    altitude. rated_hp is guaranteed > 0 by construction.
    """

    rated_hp: float
    rpm: list[float]        # sea-level rows, ascending by rpm
    map55: list[float]      # paired with rpm[]
    map75: list[float]      # paired with rpm[]
    altitude: list[float]   # altitude rows, ascending
    delta_hp: list[float]   # paired with altitude[]


def _cell(sids: dict, key: str) -> Optional[float]:
    """Read one SID cell as a float; absent or non-numeric -> None (blank)."""
    raw = sids.get(key)
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def build_power_map(sids: dict) -> Optional[PowerMap]:
    """Build a PowerMap from a SID->value map (Req 1.1-1.7).

    ``sids`` has STRING keys (e.g. ``sids.get("167")``), matching
    ``ParsedBackup.sids``.

    Returns ``None`` (Percent_Power unavailable) when:
      - RATED_HP (SID "167") is missing or <= 0 (Req 1.6), or
      - no valid Sea_Level_Row exists (Req 1.7), or
      - no valid Altitude_Row exists (Req 1.7).

    Row rules:
      - Sea_Level_Row (rpm/map55/map75): blank when rpm <= 0 (Req 1.3);
        otherwise included only when rpm, map55, map75 are ALL > 0 (Req 1.4).
      - Altitude_Row (altitude/delta_hp): blank when altitude <= 0 (Req 1.5);
        delta_hp may hold any value (including <= 0 or blank -> treated as 0.0).
      - A SID absent or non-numeric is treated as blank for its cell.
    """
    rated_hp = _cell(sids, SID_RATED_HP)
    if rated_hp is None or rated_hp <= 0:
        return None

    # Sea-level rows: keep only when rpm, map55, map75 are ALL > 0.
    sea_rows = []
    for rpm_k, m55_k, m75_k in zip(
        SID_RPM_COLUMN, SID_MAP55_COLUMN, SID_MAP75_COLUMN
    ):
        rpm = _cell(sids, rpm_k)
        m55 = _cell(sids, m55_k)
        m75 = _cell(sids, m75_k)
        # rpm <= 0 (incl. blank) -> row is blank/excluded (Req 1.3).
        if rpm is None or rpm <= 0:
            continue
        # Included only when all three are > 0 (Req 1.4).
        if m55 is None or m55 <= 0 or m75 is None or m75 <= 0:
            continue
        sea_rows.append((rpm, m55, m75))

    if not sea_rows:
        return None

    # Altitude rows: keep only when altitude > 0; delta_hp may be anything.
    alt_rows = []
    for alt_k, dhp_k in zip(SID_ALTITUDE_COLUMN, SID_DELTA_HP_COLUMN):
        alt = _cell(sids, alt_k)
        if alt is None or alt <= 0:
            continue
        dhp = _cell(sids, dhp_k)
        alt_rows.append((alt, dhp if dhp is not None else 0.0))

    if not alt_rows:
        return None

    # Sort by rpm / altitude, preserving pairing.
    sea_rows.sort(key=lambda r: r[0])
    alt_rows.sort(key=lambda r: r[0])

    return PowerMap(
        rated_hp=rated_hp,
        rpm=[r[0] for r in sea_rows],
        map55=[r[1] for r in sea_rows],
        map75=[r[2] for r in sea_rows],
        altitude=[r[0] for r in alt_rows],
        delta_hp=[r[1] for r in alt_rows],
    )


# ---------------------------------------------------------------------------
# Eight-step percent-power pipeline (Requirement 2)
# ---------------------------------------------------------------------------

def _is_finite_number(v) -> bool:
    """True when v is a real number (int/float, not bool) and finite."""
    if v is None or isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


def percent_power(rpm, map_inhg, pressure_alt, oat_f,
                  power_map: PowerMap) -> Optional[float]:
    """Percent_Power for one FDL_Sample via GRT's 8-step algorithm (Req 2).

    ``oat_f`` is OAT already in degrees Fahrenheit (the Celsius conversion, if
    any, happens upstream via ``oat_to_fahrenheit`` at the call sites).

    Returns ``None`` if any of rpm, map_inhg, pressure_alt, oat_f is ``None`` or
    not a finite number (NaN/inf) (Req 5.1), if the sea-level anchors coincide
    (``m75 == m55`` — degenerate map at that RPM), or if ``460 + oat_f <= 0``.
    Result is a fraction * 100 (e.g. 65.3 means 65.3%); NOT clamped (Req 3.2).
    """
    if not (_is_finite_number(rpm) and _is_finite_number(map_inhg)
            and _is_finite_number(pressure_alt) and _is_finite_number(oat_f)):
        return None

    rated_hp = power_map.rated_hp

    # (1)+(2) anchors: MAP for 55% and 75% at this RPM.
    m55 = interp(power_map.rpm, power_map.map55, rpm)
    m75 = interp(power_map.rpm, power_map.map75, rpm)
    if m75 == m55:
        # Degenerate anchor: dividing by (m75 - m55) is undefined.
        return None

    # (3) sea-level power percentage (affine through the 55%/75% anchors).
    sea_level_pct = 55.0 + (map_inhg - m55) * 20.0 / (m75 - m55)

    # (4) sea-level horsepower.
    sea_level_hp = sea_level_pct / 100.0 * rated_hp

    # (5) delta HP at this pressure altitude.
    delta_hp = interp(power_map.altitude, power_map.delta_hp, pressure_alt)

    # (6) altitude-corrected horsepower.
    alt_corrected_hp = sea_level_hp + delta_hp

    # (7) OAT correction (Fahrenheit): sqrt((460 + std) / (460 + oat)).
    denom = 460.0 + oat_f
    if denom <= 0:
        return None
    std_oat = isa_std_oat_f(pressure_alt)
    numer = 460.0 + std_oat
    if numer < 0:
        # Physically implausible; guard the square root domain.
        return None
    correction = math.sqrt(numer / denom)
    corrected_hp = alt_corrected_hp * correction

    # (8) Percent_Power.
    return corrected_hp / rated_hp * 100.0


# ---------------------------------------------------------------------------
# MAP source resolution (pure)
# ---------------------------------------------------------------------------

def map_column_for(resolved_aux: dict) -> str:
    """Return the fdl_data column that holds manifold pressure (inHg).

    Prefer the aux channel the user mapped to ``manifold_pressure`` (the same
    column the dashboard plots as MAP); otherwise fall back to
    ``internal_map``. Pure — takes the already-resolved ``resolve_aux()`` dict,
    keeping ``aux_map`` the one place aux meaning is derived.
    """
    info = resolved_aux.get("manifold_pressure")
    return info["channel"] if info else "internal_map"


# ---------------------------------------------------------------------------
# Settings integration adapters (thin; degrade to None)
# ---------------------------------------------------------------------------

def _read_settings():
    """Return ``(sids, temp_units_celsius)`` or ``None``.

    Wrapped in ``try/except ImportError`` so percent-power stays import-safe
    even if ``efis_settings`` is unavailable at build time: any ImportError or
    absent backup yields ``None`` => Percent_Power unavailable (Req 5.2).
    ``sids`` is a ``dict[str, str]`` with STRING SID keys (e.g. "167").
    """
    try:
        from efis_data_manager.efis_settings import load_current_settings_sids
    except ImportError:
        return None
    return load_current_settings_sids()  # (sids, temp_units_celsius) or None


def load_power_map() -> Optional[PowerMap]:
    """Obtain the current settings backup and build the PowerMap (Req 1, 5.2).

    Calls ``_read_settings()``; on ``None`` returns ``None``, otherwise unpacks
    ``(sids, temp_units_celsius)`` and returns ``build_power_map(sids)``.
    Callers treat ``None`` as "Percent_Power unavailable for the flight".
    """
    settings = _read_settings()
    if settings is None:
        return None
    sids, _temp_units_celsius = settings
    return build_power_map(sids)
