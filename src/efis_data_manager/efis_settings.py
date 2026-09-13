# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GRT HXr EFIS settings-backup import primitives.

Pure (I/O-light) logic for the EFIS Settings Import feature:

- ``Settings_Parser`` / ``ParsedBackup`` — read-only, tolerant parse of a
  ``Settings.bak`` / ``Settings.dat`` file into a SID -> value map.
- ``ChecksumStatus`` / ``verify_checksum`` — best-effort integrity boundary
  (GRT's checksum algorithm is undocumented; defaults to ``UNVERIFIABLE``).
- ``Settings_Selector`` / ``SelectionResult`` — pick the current backup from a
  ``.bak`` / ``.dat`` pair.
- ``Settings_Mapper`` / ``MappedValue`` / ``MappedSettings`` — translate parsed
  SID values into dashboard threshold values (units, rounding, disabled-value,
  Vne mutual-exclusivity, cylinder-count precedence).
- ``SourceIdentity`` / ``extract_source_key`` / ``content_hash`` — per-display
  identity and a content fingerprint for source-keyed, regression-proof
  import detection.

All parsing is read-only on the source file: the module never writes,
truncates, renames, or deletes the backup it is given.
"""

import copy
import glob
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings_Parser (Requirement 1, 10.1, 10.2)
# ---------------------------------------------------------------------------

class SettingsParseError(Exception):
    """Raised when a Settings_Backup cannot be read or has no valid pairs."""


@dataclass
class ParsedBackup:
    """A tolerant, read-only representation of one Settings_Backup file."""

    path: str
    sids: dict[str, str]          # SID -> raw value (both strings) — Req 1.1
    update_value: Optional[int]   # from UPDATE= — Req 1.4
    checksize: Optional[str]      # raw CHECKSIZE= value — Req 1.5
    checksum: Optional[str]       # raw CHECKSUM= value — Req 1.5
    line_count: int
    valid_pairs: int


# Special (non-SID) recognized keys.
_KEY_UPDATE = "UPDATE"
_KEY_CHECKSIZE = "CHECKSIZE"
_KEY_CHECKSUM = "CHECKSUM"


class Settings_Parser:
    """Reads a Settings_Backup file into a ``ParsedBackup`` (read-only)."""

    @staticmethod
    def parse(path: str) -> ParsedBackup:
        """Parse a Settings_Backup file read-only.

        Opens the file for reading only, decodes as ASCII with
        ``errors="replace"`` so non-ASCII bytes never raise. Each line is split
        on the FIRST "=" only; the left side must be a non-empty token to count
        as a key. Malformed lines (no "=", empty key) are skipped and parsing
        continues. ``UPDATE=`` / ``CHECKSIZE=`` / ``CHECKSUM=`` are recognized
        specially and also retained in ``sids`` (harmless — the mapper ignores
        unknown SIDs). Unknown SIDs are retained.

        Raises:
            SettingsParseError: on OSError (unreadable) or zero valid pairs.

        The source file is never written, truncated, renamed, or deleted.
        """
        sids: dict[str, str] = {}
        update_value: Optional[int] = None
        checksize: Optional[str] = None
        checksum: Optional[str] = None
        line_count = 0
        valid_pairs = 0

        try:
            # Read-only ("r"), ASCII with replacement so odd bytes never raise.
            with open(path, "r", encoding="ascii", errors="replace") as fh:
                for raw_line in fh:
                    line_count += 1
                    line = raw_line.rstrip("\r\n")
                    if "=" not in line:
                        # Malformed: no delimiter — skip, continue (Req 1.2).
                        continue
                    key, value = line.split("=", 1)  # split on FIRST "=" only
                    key = key.strip()
                    if not key:
                        # Malformed: empty key — skip, continue (Req 1.2).
                        continue

                    valid_pairs += 1

                    if key == _KEY_UPDATE:
                        try:
                            update_value = int(value.strip())
                        except (TypeError, ValueError):
                            update_value = None
                    elif key == _KEY_CHECKSIZE:
                        checksize = value
                    elif key == _KEY_CHECKSUM:
                        checksum = value

                    # Retain every valid pair, including unknown SIDs and the
                    # special keys (mapper ignores non-mapped keys) (Req 1.3).
                    sids[key] = value
        except OSError as exc:
            # Unreadable file — report parse failure, leave source unmodified.
            raise SettingsParseError(f"cannot read {path!r}: {exc}") from exc

        if valid_pairs == 0:
            # No valid KEY=VALUE lines — report parse failure (Req 1.6).
            raise SettingsParseError(f"no valid KEY=VALUE pairs in {path!r}")

        return ParsedBackup(
            path=path,
            sids=sids,
            update_value=update_value,
            checksize=checksize,
            checksum=checksum,
            line_count=line_count,
            valid_pairs=valid_pairs,
        )


# ---------------------------------------------------------------------------
# Checksum verification — best-effort (algorithm unknown) (Requirement 2.3)
# ---------------------------------------------------------------------------

class ChecksumStatus(Enum):
    """Result of a best-effort integrity check on a Settings_Backup."""

    VERIFIED = "verified"          # a known algorithm confirmed integrity
    FAILED = "failed"              # a known algorithm ran and mismatched
    UNVERIFIABLE = "unverifiable"  # no known algorithm / missing lines


def verify_checksum(parsed: ParsedBackup) -> ChecksumStatus:
    """Best-effort integrity check for a Settings_Backup.

    GRT's exact CHECKSIZE=/CHECKSUM= algorithm is NOT documented to us, so this
    deliberately does not invent a formula. It always returns ``UNVERIFIABLE``
    until a confirmed algorithm is implemented here. This keeps the boundary
    honest: the code never claims a file is integrity-checked when it is not.
    The pluggable shape lets a real verifier drop in later and return
    ``VERIFIED`` / ``FAILED`` without changing callers (Req 2.3, 10.1).
    """
    return ChecksumStatus.UNVERIFIABLE


# ---------------------------------------------------------------------------
# Settings_Selector (Requirement 2)
# ---------------------------------------------------------------------------

@dataclass
class SelectionResult:
    """Outcome of choosing the current backup from a candidate set."""

    current: Optional[ParsedBackup]     # chosen file, or None on total failure
    reason: str                         # human-readable selection rationale
    checksum_note: Optional[str] = None  # e.g. "checksum unverifiable; used UPDATE="


class Settings_Selector:
    """Chooses the current Settings_Backup from a ``.bak`` / ``.dat`` pair."""

    @staticmethod
    def select(candidates: list[ParsedBackup]) -> SelectionResult:
        """Select the current backup per Requirement 2.

        Rules:
          1. Run verify_checksum on each candidate.
          2. If some VERIFIED and some FAILED, restrict to the VERIFIED set
             (discard FAILED); UNVERIFIABLE candidates are retained (Req 2.4).
          3. If ALL candidates are FAILED, return current=None with a
             verification-failure reason (Req 2.5). An UNVERIFIABLE-only set is
             NOT a verification failure — it proceeds on UPDATE=.
          4. Among survivors, pick the highest update_value (Req 2.1). A present
             update_value beats None; if all None, deterministic tiebreak
             (prefer .dat, then path) with a note.
          5. A single candidate is selected outright (Req 2.2).
        """
        if not candidates:
            return SelectionResult(current=None, reason="no candidates provided")

        statuses = {id(c): verify_checksum(c) for c in candidates}
        verified = [c for c in candidates if statuses[id(c)] == ChecksumStatus.VERIFIED]
        failed = [c for c in candidates if statuses[id(c)] == ChecksumStatus.FAILED]
        unverifiable = [c for c in candidates if statuses[id(c)] == ChecksumStatus.UNVERIFIABLE]

        checksum_note: Optional[str] = None

        # All FAILED under a known algorithm => verification failure (Req 2.5).
        if failed and not verified and not unverifiable:
            return SelectionResult(
                current=None,
                reason="all candidates failed checksum verification",
                checksum_note="checksum verification failed for every candidate",
            )

        # Some verify, some fail => keep only the verified (Req 2.4).
        if verified and failed:
            survivors = verified + unverifiable
            checksum_note = "discarded checksum-failed candidate(s); kept verified"
        elif verified:
            survivors = verified + unverifiable
        else:
            # No integrity signal (UNVERIFIABLE only) — best-effort (Req 2.3).
            survivors = unverifiable
            checksum_note = "checksum unverifiable; used UPDATE= ordering"

        if not survivors:
            # Defensive: nothing survived (e.g. every candidate FAILED but the
            # earlier branch was bypassed). Report a verification failure.
            return SelectionResult(
                current=None,
                reason="no candidate survived checksum verification",
                checksum_note="checksum verification failed for every candidate",
            )

        if len(survivors) == 1:
            return SelectionResult(
                current=survivors[0],
                reason="single candidate selected",
                checksum_note=checksum_note,
            )

        # Highest update_value wins; a present value beats None (Req 2.1).
        with_update = [c for c in survivors if c.update_value is not None]
        if with_update:
            best = max(with_update, key=lambda c: c.update_value)
            reason = f"selected highest UPDATE= ({best.update_value})"
            return SelectionResult(current=best, reason=reason, checksum_note=checksum_note)

        # All update_value None — deterministic tiebreak: prefer .dat, then path.
        def _tiebreak(c: ParsedBackup) -> tuple[int, str]:
            is_dat = 0 if c.path.lower().endswith(".dat") else 1
            return (is_dat, c.path)

        best = min(survivors, key=_tiebreak)
        note = "no UPDATE= on any candidate; tiebreak prefer .dat then path"
        checksum_note = "; ".join(n for n in [checksum_note, note] if n)
        return SelectionResult(
            current=best,
            reason="no UPDATE= values; deterministic tiebreak",
            checksum_note=checksum_note,
        )


# ---------------------------------------------------------------------------
# Archive_Date cross-date ordering layer (Requirement 17)
# ---------------------------------------------------------------------------

# The Archiver stamps a "Settings-YYYY-MM-DD.<ext>" datestamp into the archived
# filename (see archiver._copy_with_datestamp). We extract that datestamp; it is
# the ONLY reliable cross-capture ordering signal, because real HXr UPDATE= is
# not globally monotonic and EFIS-written FAT modification times are unreliable
# (design "honest unknowns", Req 17.2/17.4).
_ARCHIVE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class ArchivedBackup:
    """A parsed Settings backup tagged with its Archive_Date (Requirement 17)."""

    path: str
    archive_date: date          # parsed from the "Settings-YYYY-MM-DD" filename
    parsed: ParsedBackup


def parse_archive_date(path: str) -> Optional[date]:
    """Extract the ``YYYY-MM-DD`` Archive_Date the Archiver stamps into a filename.

    The Archiver renames ``Settings.bak`` -> ``Settings-2026-09-12.bak`` at copy
    time (``archiver._copy_with_datestamp``). This reads that datestamp from the
    base filename and returns it as a ``date``. Returns ``None`` when the
    filename carries no parseable ``YYYY-MM-DD`` datestamp — such a candidate
    sorts last / is ignored for cross-date ordering (Req 17.1).
    """
    name = os.path.basename(path)
    match = _ARCHIVE_DATE_RE.search(name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        # A syntactically YYYY-MM-DD-shaped but invalid date (e.g. month 13).
        return None


def select_current_backup(archived: list[ArchivedBackup]) -> SelectionResult:
    """Select the current Settings_Backup across dates and the same-date pair.

    Cross-date + same-date selection (Requirement 17):

      1. Group the archived backups by ``archive_date``.
      2. Take the group with the LATEST ``archive_date`` as the current capture
         (Req 17.1) — never an older date regardless of any ``UPDATE=`` an
         older-dated file may carry (Req 17.2). File mtimes are never consulted
         (Req 17.4).
      3. Within that latest date, hand the ``.bak``/``.dat`` pair to
         ``Settings_Selector.select()`` to pick the newer slot by ``UPDATE=``
         (Req 17.3, consistent with Req 2.1).
      4. A single backup selects it unchanged (Req 17.6, mirroring Req 2.2).

    Candidates with no parseable Archive_Date (``archive_date is None``) are not
    representable here (``ArchivedBackup.archive_date`` is a ``date``); callers
    drop those / sort them last before calling this helper.
    """
    if not archived:
        return SelectionResult(current=None, reason="no archived candidates provided")

    latest = max(ab.archive_date for ab in archived)
    same_date = [ab for ab in archived if ab.archive_date == latest]

    result = Settings_Selector.select([ab.parsed for ab in same_date])
    if result.current is None:
        return result

    note = f"latest Archive_Date {latest.isoformat()}"
    combined_note = "; ".join(n for n in [note, result.checksum_note] if n)
    return SelectionResult(
        current=result.current,
        reason=f"{result.reason} (Archive_Date {latest.isoformat()})",
        checksum_note=combined_note or None,
    )


# ---------------------------------------------------------------------------
# Settings_Mapper (Requirements 4, 5, 6, 7, 12.1, 13)
# ---------------------------------------------------------------------------

@dataclass
class MappedValue:
    """One proposed dashboard threshold produced by the mapper."""

    threshold_key: str          # e.g. "cht_redline", "rpm_redline", "num_cylinders"
    value: Union[float, int]
    source_sid: str             # e.g. "151"
    needs_tier_choice: bool = False   # CHT/EGT/Oil single-limit -> user picks tier


@dataclass
class MappedSettings:
    """The full result of mapping a parsed backup to dashboard thresholds."""

    values: list[MappedValue] = field(default_factory=list)         # direct mappings
    tier_choices: list[MappedValue] = field(default_factory=list)   # CHT/EGT/Oil awaiting tier pick
    temp_units_celsius: bool = False    # from SID 345
    vne_is_tas: bool = True             # from SID 889 (1 => TAS)
    skipped_disabled: list[str] = field(default_factory=list)       # SIDs skipped as Disabled_Value
    num_cylinders: Optional[int] = None


# Value kinds for the declarative table.
_KIND_TEMP = "temp"
_KIND_SPEED = "speed"
_KIND_G = "g"
_KIND_G_NEG = "g_neg"
_KIND_VOLTAGE = "voltage"
_KIND_PRESSURE = "pressure"
_KIND_RPM = "rpm"
_KIND_RATE = "rate"
_KIND_COUNT = "count"

# Vne sentinel used to disable the inactive Vne key (matches analysis.py).
VNE_DISABLED_SENTINEL = 9999

# SID constants shared with identity helpers.
SID_TEMP_UNITS = "345"      # 0=Fahrenheit, 1=Celsius (Req 7.4, 7.5)
SID_VNE = "22"              # Vne in knots (Req 5.1)
SID_VNE_CONVERT = "889"     # Convert Vne TAS->IAS (1=TAS) (Req 5.3, 5.4)
SID_NUM_CHT = "1055"        # Num CHT (Req 13.1)
SID_NUM_EGT = "1054"        # Num EGT (Req 13.1)
SID_NUM_CYL = "58"          # Num Cylinders fallback (Req 13.2)


@dataclass(frozen=True)
class _SidSpec:
    """One row of the declarative SID -> threshold mapping table."""

    threshold_key: str
    kind: str
    disabled: Optional[float] = None    # value that means "limit not set"
    tier_choice: bool = False           # single EFIS limit -> user picks tier


# Declarative SID table (design "Mapping table"). Vne (22/889), temp-units
# (345), and cylinder-count (1055/1054/58) are handled with dedicated logic
# below, not through this simple lookup.
_SID_TABLE: dict[str, _SidSpec] = {
    # Oil pressure (Req 4.1)
    "120": _SidSpec("oil_pressure_low_cruise", _KIND_PRESSURE),
    "118": _SidSpec("oil_pressure_high", _KIND_PRESSURE),
    # Oil temp / CHT / EGT — single EFIS max, user picks tier (Req 4.2-4.4, 8)
    "121": _SidSpec("oil_temp", _KIND_TEMP, tier_choice=True),
    "151": _SidSpec("cht", _KIND_TEMP, tier_choice=True),
    "144": _SidSpec("egt", _KIND_TEMP, tier_choice=True),
    # EGT span (Req 4.5) — disabled at 0
    "147": _SidSpec("egt_spread_caution", _KIND_TEMP, disabled=0),
    # Fuel pressure (Req 4.6)
    "1463": _SidSpec("fuel_pressure_low", _KIND_PRESSURE),
    "1464": _SidSpec("fuel_pressure_high", _KIND_PRESSURE),
    # Voltage (Req 4.7)
    "353": _SidSpec("voltage_low", _KIND_VOLTAGE),
    "354": _SidSpec("voltage_high", _KIND_VOLTAGE),
    # RPM redline (Req 4.8, 11) — disabled at 0
    "123": _SidSpec("rpm_redline", _KIND_RPM, disabled=0),
    # CHT cooling rate (Req 4.9, 12) — disabled at 0
    "150": _SidSpec("cht_cooling_rate_max", _KIND_RATE, disabled=0),
    # G-meter (Req 6) — caution + limit, both tiers mapped directly
    "331": _SidSpec("g_pos_caution", _KIND_G),
    "329": _SidSpec("g_pos_limit", _KIND_G),
    "332": _SidSpec("g_neg_caution", _KIND_G_NEG),
    "330": _SidSpec("g_neg_limit", _KIND_G_NEG),
}


def _parse_number(raw: str) -> Optional[float]:
    """Parse a raw SID value into a float, or None if not numeric."""
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


def _round_value(kind: str, value: float, celsius: bool) -> Union[float, int]:
    """Apply units + rounding per value kind (Requirement 7)."""
    if kind == _KIND_TEMP:
        # Absolute temperature: C->F full conversion when Celsius (Req 7.4).
        v = value * 9 / 5 + 32 if celsius else value
        return int(round(v))
    if kind == _KIND_RATE:
        # Temperature DELTA (°/min): ratio conversion only, no +32 (Req 12.1).
        v = value * 9 / 5 if celsius else value
        return int(round(v))
    if kind == _KIND_SPEED:
        return int(round(value))
    if kind in (_KIND_G, _KIND_G_NEG):
        # One decimal (Req 7.1). Negative-G kept <= 0 (Req 6.3).
        v = round(value, 1)
        if kind == _KIND_G_NEG:
            v = -abs(v)
        return v
    if kind == _KIND_VOLTAGE:
        return round(value, 1)  # one decimal (Req 7.2)
    if kind in (_KIND_PRESSURE, _KIND_RPM, _KIND_COUNT):
        return int(round(value))
    return value


class Settings_Mapper:
    """Translates a ParsedBackup into proposed dashboard threshold values."""

    @staticmethod
    def map(parsed: ParsedBackup) -> MappedSettings:
        """Map parsed SID values to dashboard thresholds.

        Applies units/rounding by kind, skips Disabled_Value SIDs, preserves
        negative-G sign, resolves Vne TAS/IAS mutual exclusivity via SID 889,
        and picks the cylinder count with the 1055 -> 1054 -> 58 precedence.
        Unknown SIDs are never emitted (Req 1.3, 4.10).
        """
        sids = parsed.sids
        result = MappedSettings()

        # Temp units (SID 345): 1 => Celsius (Req 7.4, 7.5).
        temp_raw = sids.get(SID_TEMP_UNITS)
        celsius = temp_raw is not None and temp_raw.strip() == "1"
        result.temp_units_celsius = celsius

        # --- Straightforward table-driven SIDs ---
        for sid, spec in _SID_TABLE.items():
            raw = sids.get(sid)
            if raw is None:
                continue
            num = _parse_number(raw)
            if num is None:
                continue
            # Disabled_Value: skip, leave threshold unchanged (Req 4.10, etc.).
            if spec.disabled is not None and num == spec.disabled:
                result.skipped_disabled.append(sid)
                continue
            value = _round_value(spec.kind, num, celsius)
            mv = MappedValue(
                threshold_key=spec.threshold_key,
                value=value,
                source_sid=sid,
                needs_tier_choice=spec.tier_choice,
            )
            if spec.tier_choice:
                result.tier_choices.append(mv)
            else:
                result.values.append(mv)

        # --- Vne mutual exclusivity (SID 22 + SID 889) (Req 5.1, 5.3, 5.4) ---
        vne_raw = sids.get(SID_VNE)
        if vne_raw is not None:
            vne_num = _parse_number(vne_raw)
            # Disabled_Value for Vne: 0 => skip entirely (Req 5.7).
            if vne_num is not None and vne_num != 0:
                convert_raw = sids.get(SID_VNE_CONVERT)
                # SID 889 == 1 => TAS-based; default TAS when absent.
                is_tas = not (convert_raw is not None and convert_raw.strip() == "0")
                result.vne_is_tas = is_tas
                vne_val = _round_value(_KIND_SPEED, vne_num, celsius=False)
                if is_tas:
                    active_key, inactive_key = "vne_tas_redline", "vne_ias_redline"
                else:
                    active_key, inactive_key = "vne_ias_redline", "vne_tas_redline"
                result.values.append(
                    MappedValue(active_key, vne_val, source_sid=SID_VNE)
                )
                # Clear the inactive key with the disabled sentinel (Req 5.3/5.4).
                result.values.append(
                    MappedValue(inactive_key, VNE_DISABLED_SENTINEL, source_sid=SID_VNE)
                )
            elif vne_num is not None:
                result.skipped_disabled.append(SID_VNE)

        # --- Cylinder count precedence: 1055 -> 1054 -> 58 (Req 13) ---
        for sid in (SID_NUM_CHT, SID_NUM_EGT, SID_NUM_CYL):
            raw = sids.get(sid)
            if raw is None:
                continue
            num = _parse_number(raw)
            if num is None:
                continue
            result.num_cylinders = int(round(num))
            break

        return result


# ---------------------------------------------------------------------------
# Source identity + Content_Hash (Requirement 14)
# ---------------------------------------------------------------------------

# Identity SIDs (from the requirements Glossary).
SID_MODE_S = "1063"     # Mode_S_Address, ICAO 24-bit hex, e.g. "A60670"
SID_LINK_ID = "387"     # Link_ID / inter-display link (DULINK_ID), e.g. "1"
SID_FLIGHT_ID = "1062"  # Flight_ID / N-number (secondary label only)

# Reserved key name for the bound import source inside the settings_import map.
BOUND_IMPORT_SOURCE_KEY = "bound_import_source"

# Lines excluded from the Content_Hash (volatile / non-configuration).
_HASH_EXCLUDED_KEYS = {_KEY_UPDATE, _KEY_CHECKSIZE, _KEY_CHECKSUM}


@dataclass
class SourceIdentity:
    """The identity of the display that wrote a backup (Requirement 14)."""

    source_key: Optional[str]   # "A60670:1"; None when undetermined (Req 14.8)
    mode_s: Optional[str]       # SID 1063 if present
    link_id: Optional[str]      # SID 387 if present
    flight_id: Optional[str]    # SID 1062 if present (label only)


def extract_source_key(parsed: ParsedBackup) -> SourceIdentity:
    """Derive the Source_Key of the display that wrote ``parsed``.

    Source_Key = "<mode_s>:<link_id>" when both identity SIDs are present.
    Graceful fallback (Req 14.1, Glossary):
      - only Mode_S present   -> source_key = mode_s        (e.g. "A60670")
      - only Link_ID present  -> source_key = ":<link_id>"  (weak identity)
      - neither present       -> source_key = None (undetermined -> Req 14.8)

    Flight_ID (SID 1062) is carried for display but is never part of the key.
    """
    mode_s_raw = parsed.sids.get(SID_MODE_S)
    link_raw = parsed.sids.get(SID_LINK_ID)
    flight_raw = parsed.sids.get(SID_FLIGHT_ID)

    mode_s = mode_s_raw.strip() if mode_s_raw is not None and mode_s_raw.strip() else None
    link_id = link_raw.strip() if link_raw is not None and link_raw.strip() else None
    flight_id = flight_raw.strip() if flight_raw is not None and flight_raw.strip() else None

    if mode_s is not None and link_id is not None:
        source_key: Optional[str] = f"{mode_s}:{link_id}"
    elif mode_s is not None:
        source_key = mode_s
    elif link_id is not None:
        source_key = f":{link_id}"
    else:
        source_key = None

    return SourceIdentity(
        source_key=source_key,
        mode_s=mode_s,
        link_id=link_id,
        flight_id=flight_id,
    )


def content_hash(parsed: ParsedBackup) -> str:
    """SHA-256 over the parsed SID/value content, order-independent.

    Volatile / non-configuration lines (UPDATE=, CHECKSIZE=, CHECKSUM=) are
    EXCLUDED so the hash tracks settings content, not incidental churn. All
    remaining SID=value pairs (including identity SIDs) are included and sorted
    by SID so two backups with identical settings hash equal regardless of line
    order or a differing UPDATE= counter (Req 14.8).
    """
    items = sorted(
        (sid, value)
        for sid, value in parsed.sids.items()
        if sid not in _HASH_EXCLUDED_KEYS
    )
    canonical = "\n".join(f"{sid}={value}" for sid, value in items)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Import_Workflow (Requirements 8, 9, 10.3, 13.3, 14, 15)
# ---------------------------------------------------------------------------

# Threshold key -> human label, reused by the preview change list. The dashboard
# UI keeps its own richer label map; this is a server-side fallback so a preview
# is self-describing even outside the browser.
_THRESHOLD_LABELS = {
    "oil_pressure_low_cruise": "Oil Pressure Low (cruise)",
    "oil_pressure_high": "Oil Pressure High",
    "oil_temp_caution": "Oil Temp Caution",
    "oil_temp_redline": "Oil Temp Redline",
    "cht_caution": "CHT Caution",
    "cht_redline": "CHT Redline",
    "egt_caution": "EGT Caution",
    "egt_redline": "EGT Redline",
    "egt_spread_caution": "EGT Spread Caution",
    "fuel_pressure_low": "Fuel Pressure Low",
    "fuel_pressure_high": "Fuel Pressure High",
    "voltage_low": "Voltage Low",
    "voltage_high": "Voltage High",
    "rpm_redline": "RPM Redline",
    "cht_cooling_rate_max": "CHT Cooling Rate Max",
    "vne_tas_redline": "Vne (TAS)",
    "vne_ias_redline": "Vne (IAS)",
    "g_pos_caution": "G Positive Caution",
    "g_pos_limit": "G Positive Limit",
    "g_neg_caution": "G Negative Caution",
    "g_neg_limit": "G Negative Limit",
    "num_cylinders": "Number of Cylinders",
}

# Per-concept tier key pairs for the CHT/EGT/Oil-Temp caution-vs-redline prompt
# (Requirement 8). The mapper emits a single "cht"/"egt"/"oil_temp" tier choice;
# build_preview maps that single limit to one of these two keys per the user's
# tier selection, then guards caution < redline.
_TIER_CONCEPTS = {
    "cht": ("cht_caution", "cht_redline"),
    "egt": ("egt_caution", "egt_redline"),
    "oil_temp": ("oil_temp_caution", "oil_temp_redline"),
}

_MARKER_LAST_UPDATE = "last_update_value"
_MARKER_CONTENT_HASH = "content_hash"
_MARKER_BACKUP_NAME = "backup_name"
_MARKER_IMPORTED_AT = "imported_at"


@dataclass
class NewBackupInfo:
    """Result of source-keyed, primary-gated new-backup detection (R14, R15)."""

    available: bool                 # True => an import may be offered
    source_key: Optional[str] = None       # candidate Source_Key, None if undetermined
    is_first_for_source: bool = False       # no prior marker for this Source_Key (R14.3)
    is_different_source: bool = False       # differs from most-recent prior import (R14.6/7)
    candidate_update_value: Optional[int] = None        # candidate UPDATE= (R14.4/5)
    source_last_imported_update: Optional[int] = None   # that source's marker UPDATE=
    candidate_content_hash: Optional[str] = None        # Content_Hash of candidate
    is_primary: bool = False        # candidate SID 387 == 1 (Primary_Display) — R15.1
    link_id: Optional[str] = None   # candidate SID 387 value (0/1/2-254) — R15.1/15.2
    bound_import_source: Optional[str] = None    # currently bound Source_Key — R15.3/15.4
    is_bound_source_mismatch: bool = False       # primary but Source_Key != bound — R15.5
    reason: str = ""                # human-readable why offered / suppressed
    parsed: Optional[ParsedBackup] = None        # the selected candidate


@dataclass
class ThresholdChange:
    """One row of the preview diff table: current -> proposed for one key."""

    key: str                    # threshold key or "num_cylinders"
    label: str                  # human label
    current: Optional[Union[float, int]]    # current dashboard value (None if unset)
    proposed: Optional[Union[float, int]]   # proposed imported value
    source_sid: Optional[str] = None
    tier: Optional[str] = None  # "caution" | "redline" when user chose a tier
    selected: bool = True       # ticked for import (default all-selected) — R16.1
    selection_key: str = ""     # UI row identity selected_keys refers to; equals
                                # `key` for ordinary rows, "vne" for both vne_*
                                # rows (the atomic Vne unit) — R16.3/16.4


@dataclass
class ImportPreview:
    """The preview of an import: diff table, tier prompts, guards, gating."""

    changes: list[ThresholdChange] = field(default_factory=list)
    tier_prompts: list[str] = field(default_factory=list)   # concepts needing a tier decision
    warnings: list[str] = field(default_factory=list)       # e.g. caution >= redline conflicts
    checksum_note: Optional[str] = None
    backup_update_value: Optional[int] = None
    source_key: Optional[str] = None              # candidate Source_Key (R14.1)
    is_different_source: bool = False      # flag "from a different display/aircraft" (R14.6/7)
    source_note: Optional[str] = None      # human note about a different source
    bound_import_source: Optional[str] = None    # currently bound Source_Key (R15)
    is_bound_source_mismatch: bool = False       # candidate primary but != bound (R15.5)
    can_apply: bool = False
    parsed: Optional[ParsedBackup] = None
    num_cylinders_proposed: Optional[int] = None


@dataclass
class ApplyResult:
    """Outcome of an apply attempt (double-confirm gated, atomic)."""

    applied: bool
    reason: str
    config: dict                # the resulting config (unchanged on no-op)
    updated_thresholds: dict = field(default_factory=dict)


def _default_thresholds() -> dict:
    """Return analysis.DEFAULT_THRESHOLDS (imported lazily to avoid a cycle)."""
    from efis_data_manager import analysis
    return dict(analysis.DEFAULT_THRESHOLDS)


def _current_thresholds(config: dict) -> dict:
    """Current dashboard thresholds: analysis_thresholds over DEFAULT_THRESHOLDS."""
    thresholds = _default_thresholds()
    thresholds.update(config.get("analysis_thresholds", {}) or {})
    return thresholds


def _iter_source_markers(settings_import: dict):
    """Yield (source_key, marker) pairs, skipping the reserved bound key.

    Only dict-valued entries under a plausible Source_Key are treated as
    Import_Markers; the reserved ``bound_import_source`` string and any legacy
    scalar fields are skipped (they are handled via the undetermined path).
    """
    for key, marker in settings_import.items():
        if key == BOUND_IMPORT_SOURCE_KEY:
            continue
        if isinstance(marker, dict):
            yield key, marker


def _most_recent_marker(settings_import: dict):
    """Return (source_key, marker) of the most recent prior import, or (None, None).

    "Most recent" is by ``imported_at`` when available, else by
    ``last_update_value``. Used for the different-source flag (R14.6/7) and the
    undetermined-source fallback (R14.8).
    """
    best_key = None
    best_marker = None
    best_sort = None
    for key, marker in _iter_source_markers(settings_import):
        imported_at = marker.get(_MARKER_IMPORTED_AT) or ""
        last_update = marker.get(_MARKER_LAST_UPDATE)
        sort_key = (imported_at, last_update if last_update is not None else -1)
        if best_sort is None or sort_key > best_sort:
            best_sort = sort_key
            best_key = key
            best_marker = marker
    return best_key, best_marker


def _decide(parsed: ParsedBackup, config: dict) -> NewBackupInfo:
    """Pure decision core for detect_new_backup (Requirements 14, 15).

    Takes an already-parsed candidate backup plus the config and returns a
    ``NewBackupInfo``. All filesystem I/O (archive discovery, selection, parse)
    lives in ``detect_new_backup``; extracting this pure function lets the
    source-keyed and primary-gate properties (23-32) be tested without any
    archive setup, exactly as the design describes.

    Gate order is faithful to the design and must not be reordered:
      0a. PRIMARY GATE (R15.1/15.2) — device-agnostic, checks only SID 387.
      0b. BOUND-SOURCE GATE (R15.3/15.4/15.5).
      1+. Requirement 14 source-keyed logic among eligible candidates.
    """
    settings_import = config.get("settings_import", {}) or {}
    identity = extract_source_key(parsed)
    source_key = identity.source_key
    chash = content_hash(parsed)
    update_value = parsed.update_value
    bound = settings_import.get(BOUND_IMPORT_SOURCE_KEY)

    # --- 0a. PRIMARY GATE (R15.1, R15.2) — device-agnostic ------------------
    # Checks ONLY SID 387 (Link_ID / DULINK_ID); never a model/class SID, so
    # eligibility is identical across any GRT hardware mix (R15.1).
    link_id = identity.link_id  # normalized SID 387 value (None if absent)
    is_primary = (link_id == "1")
    if not is_primary:
        return NewBackupInfo(
            available=False,
            source_key=source_key,
            candidate_update_value=update_value,
            candidate_content_hash=chash,
            is_primary=False,
            link_id=link_id,
            bound_import_source=bound,
            reason="not the primary display",
            parsed=parsed,
        )

    # --- 0b. BOUND-SOURCE GATE (R15.3, R15.4, R15.5) ------------------------
    # bound is None  -> not yet bound; the first import will bind (R15.3).
    # bound == key   -> eligible; continue to R14.
    # bound != key   -> a DIFFERENT primary source; never offered/applied
    #                   silently, requires explicit rebind (R15.5).
    if bound is not None and source_key is not None and source_key != bound:
        return NewBackupInfo(
            available=False,
            source_key=source_key,
            candidate_update_value=update_value,
            candidate_content_hash=chash,
            is_primary=True,
            link_id=link_id,
            bound_import_source=bound,
            is_bound_source_mismatch=True,
            reason=(
                "primary backup from a different source than the bound import "
                "source; explicit rebind required"
            ),
            parsed=parsed,
        )

    # --- Requirement 14 source-keyed decision (among eligible candidates) ---
    prior_key, _ = _most_recent_marker(settings_import)

    if source_key is not None:
        # (a) Determined Source_Key.
        marker = settings_import.get(source_key)
        is_different_source = prior_key is not None and prior_key != source_key
        if not isinstance(marker, dict):
            # No marker for this source -> first import for the source (R14.3).
            return NewBackupInfo(
                available=True,
                source_key=source_key,
                is_first_for_source=True,
                is_different_source=is_different_source,
                candidate_update_value=update_value,
                candidate_content_hash=chash,
                is_primary=True,
                link_id=link_id,
                bound_import_source=bound,
                reason=f"first import for source {source_key}",
                parsed=parsed,
            )
        marker_update = marker.get(_MARKER_LAST_UPDATE)
        marker_hash = marker.get(_MARKER_CONTENT_HASH)
        # REQUIREMENT 17.5 refinement: the offer decision rests on Content_Hash
        # (equivalently GRT CHECKSUM, proven 1:1 with content on real HXr data),
        # NOT on UPDATE= magnitude — real HXr UPDATE= is not globally monotonic.
        # An identical Content_Hash means "nothing new to import" regardless of
        # Archive_Date or UPDATE=; a differing hash means content changed (R14.4
        # as refined). UPDATE= is retained only for same-date .bak/.dat pairing
        # and for display (last_update_value).
        if chash != marker_hash:
            # Content changed for this source -> offer (R14.4 / R17.5).
            return NewBackupInfo(
                available=True,
                source_key=source_key,
                is_first_for_source=False,
                is_different_source=is_different_source,
                candidate_update_value=update_value,
                source_last_imported_update=marker_update,
                candidate_content_hash=chash,
                is_primary=True,
                link_id=link_id,
                bound_import_source=bound,
                reason=(
                    f"content changed for source {source_key} "
                    f"(content hash differs from last import)"
                ),
                parsed=parsed,
            )
        # Identical Content_Hash -> nothing new; suppress the stale n-1 AND the
        # newer-date-but-unchanged case (R14.5 as refined by R17.5).
        return NewBackupInfo(
            available=False,
            source_key=source_key,
            is_first_for_source=False,
            is_different_source=is_different_source,
            candidate_update_value=update_value,
            source_last_imported_update=marker_update,
            candidate_content_hash=chash,
            is_primary=True,
            link_id=link_id,
            bound_import_source=bound,
            reason=(
                f"nothing new for source {source_key} "
                f"(content unchanged since last import); suppressed"
            ),
            parsed=parsed,
        )

    # (b) Undetermined Source_Key -> Content_Hash + UPDATE fallback (R14.8).
    # Also the path a legacy single-object marker is tolerated through.
    prior_key2, prior_marker = _most_recent_marker(settings_import)
    if prior_marker is None:
        # No prior import at all -> treat as first import (never regresses).
        return NewBackupInfo(
            available=True,
            source_key=None,
            is_first_for_source=True,
            is_different_source=False,
            candidate_update_value=update_value,
            candidate_content_hash=chash,
            is_primary=True,
            link_id=link_id,
            bound_import_source=bound,
            reason="first import (source undetermined)",
            parsed=parsed,
        )
    prior_hash = prior_marker.get(_MARKER_CONTENT_HASH)
    prior_update = prior_marker.get(_MARKER_LAST_UPDATE)
    cand_u = update_value if update_value is not None else -1
    prior_u = prior_update if prior_update is not None else -1
    content_differs = chash != prior_hash
    update_greater = cand_u > prior_u
    available = content_differs and update_greater
    if available:
        reason = "undetermined source: content changed and UPDATE greater"
    else:
        reason = "undetermined source: content unchanged or not newer; suppressed"
    return NewBackupInfo(
        available=available,
        source_key=None,
        is_first_for_source=False,
        is_different_source=True,   # undetermined identity != any known source
        candidate_update_value=update_value,
        source_last_imported_update=prior_update,
        candidate_content_hash=chash,
        is_primary=True,
        link_id=link_id,
        bound_import_source=bound,
        reason=reason,
        parsed=parsed,
    )


def _select_current_archived_backup(settings_dir) -> Optional[SelectionResult]:
    """Glob, parse, and select the current archived Settings backup (Req 17).

    Shared by ``load_current_settings_sids`` and
    ``Import_Workflow.detect_new_backup`` so both route file selection through
    the Archive_Date layer: glob ``Settings*.bak``/``*.dat``, parse each
    (skipping parse failures), wrap every ``ParsedBackup`` in an
    ``ArchivedBackup`` with its ``parse_archive_date`` (dropping files with no
    parseable datestamp), group by Archive_Date, take the latest date, and pick
    the same-date ``.bak``/``.dat`` slot via ``Settings_Selector.select()``.

    Returns ``None`` when the directory is missing, no candidate exists, or none
    parses; otherwise a ``SelectionResult`` (whose ``current`` may still be
    ``None`` on a checksum-verification failure).
    """
    settings_dir = Path(settings_dir)
    if not settings_dir.is_dir():
        return None

    candidate_paths = sorted(
        glob.glob(os.path.join(str(settings_dir), "Settings*.bak"))
        + glob.glob(os.path.join(str(settings_dir), "Settings*.dat"))
    )
    if not candidate_paths:
        return None

    archived: list[ArchivedBackup] = []
    undated: list[ParsedBackup] = []
    for path in candidate_paths:
        try:
            parsed = Settings_Parser.parse(path)
        except SettingsParseError:
            continue
        adate = parse_archive_date(path)
        if adate is None:
            # No parseable datestamp -> sorts last / ignored for cross-date
            # ordering (Req 17.1). Retained only as a last resort below.
            undated.append(parsed)
        else:
            archived.append(ArchivedBackup(path=path, archive_date=adate, parsed=parsed))

    if archived:
        return select_current_backup(archived)
    if undated:
        # No dated candidate at all — fall back to the same-date pair chooser
        # over the undated files so a hand-placed Settings.bak/.dat still works.
        return Settings_Selector.select(undated)
    return None


class Import_Workflow:
    """Detection / preview / apply orchestration (pure logic, testable)."""

    # ------------------------------------------------------------------
    # Detection (Requirements 9.1, 14, 15)
    # ------------------------------------------------------------------
    @staticmethod
    def detect_new_backup(config: dict, archive_root) -> Optional[NewBackupInfo]:
        """Detect whether a newer backup should be offered for import.

        Does the archive I/O — discovers the newest archived Settings backup,
        selects the current one from the .bak/.dat pair, parses it — then
        delegates the primary-gated, source-keyed decision to the pure
        ``_decide`` helper (Requirements 14, 15).

        Returns None only when no Settings backup exists or selection failed.
        In every "do not offer" case (stale n-1, undetermined-no-regress,
        non-primary, bound-source mismatch) it returns
        ``NewBackupInfo(available=False)`` with a reason so the caller can
        suppress the prompt.
        """
        settings_dir = Path(archive_root) / "Settings"
        result = _select_current_archived_backup(settings_dir)
        if result is None or result.current is None:
            return None

        info = _decide(result.current, config)
        # Attach the selection's checksum note context is carried by callers;
        # NewBackupInfo already holds the parsed candidate.
        return info

    # ------------------------------------------------------------------
    # Preview (Requirements 8, 9.2, 9.3, 13.3, 14.6/7, 15.5)
    # ------------------------------------------------------------------
    @staticmethod
    def build_preview(parsed: ParsedBackup, config: dict,
                      tier_selections: Optional[dict] = None,
                      selected_keys: Optional[set] = None) -> ImportPreview:
        """Build the preview diff table from a parsed backup and current config.

        Runs the mapper, joins proposed values against the current thresholds
        (analysis_thresholds over DEFAULT_THRESHOLDS) and ``num_cylinders``,
        applies the CHT/EGT/Oil tier selections, runs the strict caution <
        redline guard, and flags a different / bound-mismatch source.

        Per-threshold selection (Req 16): each ``ThresholdChange`` carries a
        ``selected`` flag and a ``selection_key`` (the row identity the UI
        ticks). ``selection_key`` is the threshold ``key`` for ordinary rows
        (including ``num_cylinders``) and ``"vne"`` for BOTH ``vne_*`` rows so
        they form one atomic Vne unit (Req 16.3/16.4). ``selected_keys`` is the
        set of ticked selection keys; when ``None`` every row is selected — the
        fresh, all-checked initial state (Req 16.1/16.2/16.8) and
        backward-compatible with pre-Req-16 callers. The caution<redline guard
        runs over the SELECTED subset (Req 16.6).
        """
        tier_selections = tier_selections or {}
        mapped = Settings_Mapper.map(parsed)
        current = _current_thresholds(config)

        preview = ImportPreview(
            backup_update_value=parsed.update_value,
            parsed=parsed,
        )

        # None => every row selected (fresh all-checked). A concrete set (incl.
        # the empty set for deselect-all) is honored as-is.
        select_all = selected_keys is None

        def _is_selected(selection_key: str) -> bool:
            return True if select_all else selection_key in selected_keys

        # Track proposed values so the guard can compare caution vs redline
        # against proposed-or-current for each concept, over the SELECTED subset.
        proposed_values: dict = {}          # key -> proposed (all rows)
        selected_proposed: dict = {}        # key -> proposed (selected rows only)

        def _record(change: ThresholdChange):
            proposed_values[change.key] = change.proposed
            if change.selected:
                selected_proposed[change.key] = change.proposed
            preview.changes.append(change)

        # --- Direct (non-tier) threshold mappings ---
        for mv in mapped.values:
            # Vne's two keys share the "vne" selection unit (Req 16.3/16.4);
            # every other row is keyed by its own threshold key.
            sel_key = "vne" if mv.threshold_key in ("vne_tas_redline", "vne_ias_redline") else mv.threshold_key
            _record(ThresholdChange(
                key=mv.threshold_key,
                label=_THRESHOLD_LABELS.get(mv.threshold_key, mv.threshold_key),
                current=current.get(mv.threshold_key),
                proposed=mv.value,
                source_sid=mv.source_sid,
                selected=_is_selected(sel_key),
                selection_key=sel_key,
            ))

        # --- Tier-choice concepts (CHT/EGT/Oil Temp) (Req 8.1) ---
        for mv in mapped.tier_choices:
            concept = mv.threshold_key  # "cht" / "egt" / "oil_temp"
            preview.tier_prompts.append(concept)
            caution_key, redline_key = _TIER_CONCEPTS[concept]
            # Default to redline when unspecified (safest single-limit reading).
            choice = str(tier_selections.get(concept, "redline")).lower()
            if choice not in ("caution", "redline"):
                choice = "redline"
            chosen_key = caution_key if choice == "caution" else redline_key
            _record(ThresholdChange(
                key=chosen_key,
                label=_THRESHOLD_LABELS.get(chosen_key, chosen_key),
                current=current.get(chosen_key),
                proposed=mv.value,
                source_sid=mv.source_sid,
                tier=choice,
                selected=_is_selected(chosen_key),
                selection_key=chosen_key,
            ))

        # --- num_cylinders (Req 13.3, 16.2) ---
        if mapped.num_cylinders is not None:
            preview.num_cylinders_proposed = mapped.num_cylinders
            _record(ThresholdChange(
                key="num_cylinders",
                label=_THRESHOLD_LABELS["num_cylinders"],
                current=config.get("num_cylinders"),
                proposed=mapped.num_cylinders,
                selected=_is_selected("num_cylinders"),
                selection_key="num_cylinders",
            ))

        # --- Caution < redline guard over the SELECTED subset (Req 8.2, 16.6) ---
        # For each concept compare the SELECTED caution proposal against the
        # paired redline's selected proposal (when the redline row is also
        # selected) else its current value, and vice-versa. Unselected rows
        # never trigger the block.
        can_apply = True
        for concept, (caution_key, redline_key) in _TIER_CONCEPTS.items():
            caution_selected = caution_key in selected_proposed
            redline_selected = redline_key in selected_proposed
            if not caution_selected and not redline_selected:
                continue
            caution_val = (selected_proposed.get(caution_key)
                           if caution_selected else current.get(caution_key))
            redline_val = (selected_proposed.get(redline_key)
                           if redline_selected else current.get(redline_key))
            if caution_val is None or redline_val is None:
                continue
            if caution_val >= redline_val:
                can_apply = False
                preview.warnings.append(
                    f"{concept}: caution ({caution_val}) must be below "
                    f"redline ({redline_val})"
                )

        # --- Source flagging (Req 14.6/7, 15.5) ---
        identity = extract_source_key(parsed)
        preview.source_key = identity.source_key
        settings_import = config.get("settings_import", {}) or {}
        prior_key, _ = _most_recent_marker(settings_import)
        bound = settings_import.get(BOUND_IMPORT_SOURCE_KEY)
        preview.bound_import_source = bound

        if identity.source_key is None:
            preview.is_different_source = True
            preview.source_note = (
                "This backup's source display could not be identified; "
                "double-check before applying."
            )
        elif prior_key is not None and prior_key != identity.source_key:
            preview.is_different_source = True
            preview.source_note = (
                "This backup is from a different display or aircraft than your "
                "last import."
            )

        # Bound-source mismatch composes an additional rebind note (Req 15.5).
        # Does NOT relax the double-confirm; it does block apply until rebind.
        if (bound is not None and identity.source_key is not None
                and identity.source_key != bound):
            preview.is_bound_source_mismatch = True
            preview.is_different_source = True
            rebind_note = (
                "This primary backup is from a different source than your bound "
                "import source; applying it rebinds the import source."
            )
            if preview.source_note:
                preview.source_note = f"{preview.source_note} {rebind_note}"
            else:
                preview.source_note = rebind_note

        preview.can_apply = can_apply
        return preview

    # ------------------------------------------------------------------
    # Apply (Requirements 9.4-9.6, 10.3, 14.2, 15.3-15.5)
    # ------------------------------------------------------------------
    @staticmethod
    def apply(preview: ImportPreview, confirm1: bool, confirm2: bool,
              config: dict, save=None, selected_keys: Optional[set] = None) -> ApplyResult:
        """Apply the previewed import behind a double-confirm, atomically.

        No-op (returns the config unchanged plus a reason) unless
        ``confirm1 AND confirm2 AND preview.can_apply`` and the bound-source
        gate permits it. On success, assembles the complete next config in
        memory and persists it with a single ``save`` call (defaults to
        ``config.save_config``); tests inject a throwaway ``save`` or pass a
        throwaway dict so no real user config file is touched.

        Per-threshold selection (Req 16.5): ``selected_keys`` is the set of
        ticked selection keys. Only changes whose ``selection_key`` is in the
        set are written; unselected thresholds are left at their current value
        (a frame property over the selected subset). The Vne unit key ``"vne"``
        expands to BOTH ``vne_*`` keys, so it is impossible to write exactly one
        (Req 16.3/16.4). ``selected_keys=None`` writes ALL changes (pre-Req-16
        behavior). An empty set is a no-op that changes no threshold and reports
        "nothing selected to import" (Req 16.7); the double-confirm still gates
        (Req 16.9).

        Only the candidate Source_Key's Import_Marker is refreshed; other
        sources' markers are preserved (R14.2). On the FIRST successful import
        (no bound source yet) it also binds ``bound_import_source`` to this
        primary's Source_Key, never overwriting an existing bound value (R15.3).
        """
        if not (confirm1 and confirm2):
            return ApplyResult(
                applied=False,
                reason="both confirmations are required; no changes made",
                config=config,
            )
        # Deselect-all: an explicit empty selection is a no-op (Req 16.7). This
        # is checked after the double-confirm gate (Req 16.9) but before the
        # can_apply guard, since with nothing selected there is nothing to guard.
        if selected_keys is not None and len(selected_keys) == 0:
            return ApplyResult(
                applied=False,
                reason="nothing selected to import; no changes made",
                config=config,
            )
        if not preview.can_apply:
            return ApplyResult(
                applied=False,
                reason="preview cannot be applied (unresolved conflict); no changes made",
                config=config,
            )
        # Bound-source mismatch: apply is a no-op until the user explicitly
        # rebinds (which would change the bound value in config first) (R15.5).
        settings_import = config.get("settings_import", {}) or {}
        bound = settings_import.get(BOUND_IMPORT_SOURCE_KEY)
        if (bound is not None and preview.source_key is not None
                and preview.source_key != bound):
            return ApplyResult(
                applied=False,
                reason=(
                    "candidate is from a different source than the bound import "
                    "source; explicit rebind required; no changes made"
                ),
                config=config,
            )

        # --- Build the complete next config in memory (atomic) ---
        next_config = copy.deepcopy(config)

        analysis_thresholds = dict(next_config.get("analysis_thresholds", {}) or {})
        updated_thresholds: dict = {}
        for change in preview.changes:
            # Write only the selected subset (Req 16.5). None => all rows. The
            # Vne unit's "vne" selection_key covers BOTH vne_* rows, so testing
            # membership on selection_key expands "vne" to both keys atomically
            # (Req 16.3/16.4). Unselected rows are left at their current value.
            if selected_keys is not None and change.selection_key not in selected_keys:
                continue
            if change.key == "num_cylinders":
                next_config["num_cylinders"] = change.proposed
                continue
            analysis_thresholds[change.key] = change.proposed
            updated_thresholds[change.key] = change.proposed
        next_config["analysis_thresholds"] = analysis_thresholds

        # --- Refresh ONLY the candidate source's Import_Marker (R14.2) ---
        next_si = dict(next_config.get("settings_import", {}) or {})
        marker_key = preview.source_key
        parsed = preview.parsed
        chash = content_hash(parsed) if parsed is not None else None
        backup_name = os.path.basename(parsed.path) if parsed is not None else None
        if marker_key is not None:
            next_si[marker_key] = {
                _MARKER_LAST_UPDATE: preview.backup_update_value,
                _MARKER_CONTENT_HASH: chash,
                _MARKER_BACKUP_NAME: backup_name,
                _MARKER_IMPORTED_AT: datetime.now().isoformat(timespec="seconds"),
            }
            # First successful import binds the source (never overwrite) (R15.3).
            if next_si.get(BOUND_IMPORT_SOURCE_KEY) is None:
                next_si[BOUND_IMPORT_SOURCE_KEY] = marker_key
        next_config["settings_import"] = next_si

        # --- Persist with a single save call (Req 9.5 atomicity) ---
        if save is None:
            from efis_data_manager.config import save_config as save
        save(next_config)

        return ApplyResult(
            applied=True,
            reason="import applied",
            config=next_config,
            updated_thresholds=updated_thresholds,
        )


# ---------------------------------------------------------------------------
# percent-power integration point (single function; Requirements 1.1, 1.2,
# 4.3, 5.2 of the percent-power spec)
# ---------------------------------------------------------------------------

def load_current_settings_sids() -> Optional[tuple[dict[str, str], bool]]:
    """Return ``(sids, temp_units_celsius)`` for the current archived Settings
    backup, or ``None``.

    ``archive_root = Path(load_config()["archive_path"])``. Globs
    ``<archive_root>/Settings/Settings*.bak`` and ``*.dat``, parses each via
    ``Settings_Parser`` (skipping parse failures), and selects the current one
    through the Archive_Date layer (``select_current_backup``: latest
    Archive_Date, then ``Settings_Selector`` for the same-date pair — Req 17).
    On success returns ``(parsed.sids,
    temp_units_celsius)``, where::

        temp_units_celsius = parsed.sids.get("345", "").strip() == "1"

    Returns ``None`` when no archived backup exists, none parses, or selection
    fails (``SelectionResult.current`` is ``None``). Note SID keys in
    ``parsed.sids`` are STRINGS (e.g. "167"). This is the single integration
    point consumed by ``power.py``; it mirrors the archive discovery already
    done by ``Import_Workflow.detect_new_backup``.
    """
    from efis_data_manager.config import load_config

    archive_root = Path(load_config()["archive_path"])
    settings_dir = archive_root / "Settings"
    result = _select_current_archived_backup(settings_dir)
    if result is None or result.current is None:
        return None

    parsed = result.current
    temp_units_celsius = parsed.sids.get(SID_TEMP_UNITS, "").strip() == "1"
    return parsed.sids, temp_units_celsius
