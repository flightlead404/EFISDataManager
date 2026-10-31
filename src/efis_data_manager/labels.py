# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FAT volume label + currency-cycle helpers (relocated from image_composer).

These helpers build the human-facing EFIS card volume label ``C<charts>P<plates>``
from the two currency cycle numbers, and derive those cycle numbers from the
stored currency metadata. They were originally part of the image-dd compose/write
path in ``image_composer``; the post-sync relabel (chart-manifest-sync) reuses
them, so they live here in a small dependency-light module that survives the
retirement of the image-dd path.

FAT volume label from the currency cycles (design "FAT volume label" DECISION)
------------------------------------------------------------------------------

The hardware-harness DECISION (design.md, 2026-10-09) drops the old EFIS_#
scheme and makes the FAT volume label carry BOTH currency cycles:
``C<charts>P<plates>`` (e.g. ``C2609P2610``), because charts/nav run a 56-day
cycle while plates run a 28-day cycle and so differ in general. Device-type /
identity stays in EFIS_DRIVE_ID.json, NOT the label.

IMPORTANT — what ``valid_dates`` actually contains. The cycle numbers are NOT
a 4-digit token inside ``valid_dates``; the 4-digit YYCC cycle lives only in
the download-URL KEY of ``currency.load_cycle_metadata()``. The ``valid_dates``
value is a prose date RANGE, e.g.
  "Thursday, September 3, 2026 through Wednesday, October 28, 2026".
So :func:`extract_cycle` parses the START date of the range and derives the
YYCC cycle from the fixed 28-day cycle anchor below (a bare 4-digit token is
only used as a fallback when there is no prose date, since the prose's only
4-digit tokens are the years and must not be mistaken for a cycle).

Cycle anchor. The Seattle Avionics 28-day cycles are continuous; the first
cycle whose start date falls in 2026 begins 2026-01-22 (verified against the
stored metadata: 2607=>Jul 9, 2608=>Aug 6, 2609=>Sep 3, 2610=>Oct 1, each 28
days apart). From any cycle start date we compute YY (the start year mod 100)
and CC (the 1-based ordinal of that cycle within its start year).
"""

import datetime
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_CYCLE_ANCHOR = datetime.date(2026, 1, 22)  # 2026 cycle 1 (YYCC = 2601)
_CYCLE_DAYS = 28

# Default FAT label when neither cycle is known — a compose-date stamp so the
# volume is still identifiable and never the "NO NAME" newfs_msdos default.
_LABEL_FALLBACK_PREFIX = "EFIS"

# FAT32 volume-label rules: uppercase, A-Z / 0-9 only, at most 11 characters.
_LABEL_MAX_LEN = 11
_LABEL_ALLOWED_RE = re.compile(r"[^A-Z0-9]")

# Month names (full) as they appear in the stored ``valid_dates`` prose.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _cycle_for_start_date(start: datetime.date) -> str:
    """Return the YYCC cycle string for a cycle whose first day is ``start``.

    YY is the two-digit start year; CC is the 1-based ordinal of the cycle
    within its start year, counting the continuous 28-day cycles anchored at
    :data:`_CYCLE_ANCHOR`. The date is snapped to its enclosing cycle boundary
    first, so a mid-range date (or a one-off off-by-a-day source) still maps to
    the right cycle.
    """
    n = (start - _CYCLE_ANCHOR).days // _CYCLE_DAYS
    cycle_start = _CYCLE_ANCHOR + datetime.timedelta(days=_CYCLE_DAYS * n)
    year = cycle_start.year
    # Ordinal within the year: find the first cycle index landing in `year`.
    first_in_year = n
    while True:
        candidate = _CYCLE_ANCHOR + datetime.timedelta(
            days=_CYCLE_DAYS * (first_in_year - 1)
        )
        if candidate.year != year:
            break
        first_in_year -= 1
    ordinal = n - first_in_year + 1
    return f"{year % 100:02d}{ordinal:02d}"


def _parse_start_date(valid_dates: str) -> Optional[datetime.date]:
    """Parse the START date out of a ``valid_dates`` prose range, or None.

    Handles the stored shape "<Weekday>, <Month> <D>, <YYYY> through ...": finds
    the first "<Month> <day>, <year>" triple and returns it as a date. Returns
    None when no such triple is present (so the caller degrades gracefully).
    """
    match = re.search(
        r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", valid_dates
    )
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return datetime.date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def extract_cycle(valid_dates: str) -> Optional[str]:
    """Extract a 4-digit YYCC cycle number from a ``valid_dates`` string.

    Robust to what the field ACTUALLY contains (see the module note above). The
    stored value is a prose date RANGE whose only 4-digit tokens are YEARS
    ("... 2026 through ... 2026"), so a naive 4-digit grab would wrongly return
    the year. Order of attempts:

      1. Parse the START date of the prose range and derive the YYCC cycle from
         the fixed 28-day cycle anchor. This is the real stored shape.
      2. Otherwise, if the string carries a bare 4-digit token whose last two
         digits are a plausible cycle ordinal (01..13), use it directly —
         future-proofing in case the field ever carries a literal YYCC cycle.
         The ordinal guard stops a stray YEAR (ending in e.g. "26") in
         date-like prose from being mistaken for a cycle.
      3. If neither works, return None so the label build degrades gracefully.

    Returns the cycle as a 4-character string (e.g. ``"2610"``) or None.
    """
    if not valid_dates:
        return None
    # 1. Derive from the start date of the prose range (the real stored shape).
    start = _parse_start_date(valid_dates)
    if start is not None:
        return _cycle_for_start_date(start)
    # 2. A standalone 4-digit YYCC token. Guard against grabbing a YEAR out of
    #    date-like prose: a real cycle's last two digits are the 1-based cycle
    #    ordinal (01..13, at most ~13 cycles/year), whereas a year ends in digits
    #    like "26"/"27". Only accept a token whose CC part is a plausible cycle
    #    ordinal, so "... 2026 ..." prose never yields a bogus "2026" cycle.
    token = re.search(r"(?<!\d)(\d{4})(?!\d)", valid_dates)
    if token:
        candidate = token.group(1)
        cc = int(candidate[2:])
        if 1 <= cc <= 13:
            return candidate
    # 3. Unparseable.
    return None


def _sanitize_label(label: str) -> str:
    """Sanitize ``label`` to the FAT32 rules: uppercase, [A-Z0-9], <=11 chars.

    Uppercases, strips every character outside A-Z / 0-9 (spaces, punctuation,
    lowercase after upcasing is a no-op), then truncates to 11 characters. The
    hard strip keeps ``newfs_msdos -v`` from rejecting an invalid label.
    """
    upper = (label or "").upper()
    cleaned = _LABEL_ALLOWED_RE.sub("", upper)
    return cleaned[:_LABEL_MAX_LEN]


def build_volume_label(
    charts_cycle: Optional[str],
    plates_cycle: Optional[str],
    *,
    now: Optional[datetime.date] = None,
) -> str:
    """Build the sanitized FAT volume label from the two currency cycles.

    Format ``C<charts>P<plates>`` (e.g. ``C2609P2610``). Degradation rules:

      * Both cycles known -> ``C<charts>P<plates>``.
      * Only one known -> just that component (``C2609`` or ``P2610``).
      * Neither known -> a compose-date fallback ``EFIS<YYMMDD>`` so the volume
        is still identifiable and never the ``newfs_msdos`` "NO NAME" default.

    The result is always sanitized (uppercase, [A-Z0-9], <=11 chars) so it is a
    legal FAT32 label. ``now`` is injectable for deterministic tests.
    """
    parts = ""
    if charts_cycle:
        parts += f"C{charts_cycle}"
    if plates_cycle:
        parts += f"P{plates_cycle}"
    if not parts:
        today = now or datetime.date.today()
        parts = f"{_LABEL_FALLBACK_PREFIX}{today:%y%m%d}"
    return _sanitize_label(parts)


def _cycles_from_metadata() -> tuple:
    """Return ``(charts_cycle, plates_cycle)`` from the current cycle metadata.

    Reuses the SAME source the "new image ready" notification uses
    (:func:`currency.load_cycle_metadata`, keyed by download URL with
    ``description`` + ``valid_dates``), so the label and the notification
    agree by construction. Classification by description:

      * charts/nav cycle (56-day) = the SEC / LO / Scanned-Charts entry's cycle;
      * plates cycle (28-day)     = the Approach-Plates entry's cycle.

    Each cycle is extracted via :func:`extract_cycle`. Either side degrades to
    None when its entry is absent or its ``valid_dates`` is unparseable, so the
    caller (:func:`build_volume_label`) can fall back. Best-effort: any failure
    reading the metadata yields ``(None, None)``.
    """
    from efis_data_manager import currency

    charts_cycle: Optional[str] = None
    plates_cycle: Optional[str] = None
    try:
        metadata = currency.load_cycle_metadata()
    except Exception as exc:  # metadata is best-effort for labelling only
        logger.debug("labels: could not load cycle metadata for label: %s", exc)
        return (None, None)

    for entry in metadata.values():
        desc = str(entry.get("description", "")).lower()
        valid = entry.get("valid_dates")
        if not valid:
            continue
        cycle = extract_cycle(valid)
        if cycle is None:
            continue
        if "plate" in desc or "approach" in desc:
            # Prefer the newest plates cycle seen (lexically largest YYCC).
            if plates_cycle is None or cycle > plates_cycle:
                plates_cycle = cycle
        elif "sectional" in desc or "low altitude" in desc or "scanned" in desc:
            if charts_cycle is None or cycle > charts_cycle:
                charts_cycle = cycle

    return (charts_cycle, plates_cycle)


# Nav-only fallback label prefixes (R9.4). ``EFISNAV`` is the primary fallback
# when a device friendly name is blank / sanitizes empty; it is reduced to
# ``NAV`` only when appending the MMDD would push the label past the 11-char
# FAT32 limit (so e.g. ``NAV1029`` fits where ``EFISNAV1029`` would not).
_NAV_FALLBACK_PREFIX = "EFISNAV"
_NAV_FALLBACK_PREFIX_SHORT = "NAV"


def _mmdd_from_nav_valid_date(nav_valid_date: Optional[str]) -> str:
    """Return ``MMDD`` (zero-padded month+day) from a GRT ``MM/DD/YYYY`` date.

    The GRT nav DB validity-start date is stored as ``MM/DD/YYYY`` (e.g.
    ``"10/29/2026"`` -> ``"1029"``). Returns an empty string when the input is
    None or does not parse as that form, so the caller degrades to a
    prefix-only label. There is NO year component in the result (R9.5).
    """
    if not nav_valid_date:
        return ""
    try:
        parsed = datetime.datetime.strptime(nav_valid_date.strip(), "%m/%d/%Y")
    except (ValueError, TypeError):
        return ""
    return f"{parsed.month:02d}{parsed.day:02d}"


def build_nav_volume_label(
    friendly_name: Optional[str],
    nav_valid_date: Optional[str],
) -> str:
    """Build the sanitized nav-only FAT volume label ``<PREFIX><MMDD>``.

    For a non-chart-capable (nav-only) drive the label mirrors the device it
    serves plus the nav data's month+day:

      * ``PREFIX`` = :func:`_sanitize_label` of the device ``friendly_name``.
        When the friendly name is blank or sanitizes to empty, fall back to
        ``EFISNAV`` (R9.4).
      * ``MMDD`` = month+day of ``nav_valid_date`` parsed from the GRT
        ``MM/DD/YYYY`` form; empty when the date is None/unparseable. NO year
        component (R9.5).
      * The full ``PREFIX + MMDD`` must be a legal FAT32 label: uppercase,
        ``[A-Z0-9]`` only, at most 11 characters (R9.6). When the ``EFISNAV``
        fallback plus MMDD would exceed 11 chars, the fallback is reduced to
        ``NAV`` (so ``NAV1029`` fits). A too-long USER prefix is simply
        truncated by the final :func:`_sanitize_label` pass.

    Example: ``build_nav_volume_label("Mini A/P", "10/29/2026")`` ->
    ``"MINIAP1029"`` (10 chars).
    """
    mmdd = _mmdd_from_nav_valid_date(nav_valid_date)

    prefix = _sanitize_label(friendly_name or "")
    if not prefix:
        # Blank/empty-sanitizing friendly name -> EFISNAV, reduced to NAV only
        # when the MMDD would push the combined label past the length limit.
        if len(_NAV_FALLBACK_PREFIX) + len(mmdd) > _LABEL_MAX_LEN:
            prefix = _NAV_FALLBACK_PREFIX_SHORT
        else:
            prefix = _NAV_FALLBACK_PREFIX

    # Final pass guarantees [A-Z0-9] and <=11 chars (truncating a long user
    # prefix). The prefix and mmdd are already sanitized, so this is a no-op
    # except for the length clamp in the long-user-prefix case.
    return _sanitize_label(prefix + mmdd)
