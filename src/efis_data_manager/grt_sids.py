# EFIS Data Manager - GRT HXr EFIS ground support automation.
# Copyright (C) 2026 Martin C. Walker
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GRT Settings SID dictionary built from GRT's own reference files.

Pure and read-only. Loads two verbatim GRT documents shipped under
``efis_data_manager/resources/grt/`` (via ``importlib.resources`` so it works
when installed) and builds, once per process, a dictionary of every Settings
SID with its GRT macro name, a readable label and, where GRT documents it, the
meaning of each value.

Sources
-------
``Settings.h``
    ``#define SID_<NAME> <n>`` lines. ``// N ID's used by SID_X`` comments mean
    ``SID_X`` occupies N consecutive ids starting at its value. ``//#define``
    lines are retired and ignored. The trailing ``STID_*`` /
    ``DEPRECATED_STID_*`` block is the State-file namespace and is excluded.
``DEMO-settings-format.txt``
    Sections titled ``... setting-identifiers:`` (``NNN - Readable Name``) and
    ``... values:`` (``V - Meaning``). Value sections are named, not keyed by
    SID, so ``_VALUE_SECTION_SIDS`` maps each section to the SIDs it covers.

Label precedence: (1) the DEMO doc's readable name; (2) curated power-map
labels (constants from ``power.py``); (3) a humanized ``Settings.h`` name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Optional

logger = logging.getLogger(__name__)

_RESOURCE_DIR = "resources/grt"
_SETTINGS_H = "Settings.h"
_DEMO_DOC = "DEMO-settings-format.txt"


@dataclass(frozen=True)
class SidInfo:
    """Everything the GRT references say about one Settings SID."""

    sid: str                              # e.g. "123"
    grt_name: str                         # "SID_LIM_MAX_RPM" or "SID_POWER_RPM[3]"
    label: str                            # readable label
    values: Optional[dict[str, str]] = None   # raw value -> meaning, when documented


# DEMO "... values:" section title -> SIDs that use it.
_VALUE_SECTION_SIDS: dict[str, tuple[int, ...]] = {
    "EIS Model values": (321,),
    "EIS Temperature Units values": (345,),
    "EIS Fuel Flow Units values": (346,),
    "EIS Baroset Units values": (347,),
    "Aux Function values": (64, 70, 76, 82, 88, 94, 1100, 1105, 1110, 1115, 1120, 1125),
    "Aux Integer/Decimal values": (65, 71, 77, 83, 89, 95, 1102, 1107, 1112, 1117, 1122, 1127),
    "Aux Sensing values": (1350, 1353, 1356, 1359, 1362, 1365, 1381, 1384, 1387, 1390, 1393, 1396),
    "Oil Pressure Sensor values": (1372,),
    "Dial Source values": (573, 598, 673, 698, 1142, 1152, 1162, 1172),
    "Dial Scale values": (574, 599, 674, 699, 1143, 1153, 1163, 1173),
}

# Words kept upper-case when humanizing a macro name.
_ACRONYMS = frozenset({
    "RPM", "EGT", "CHT", "TIT", "EIS", "EFIS", "MAP", "IAS", "TAS", "GPS",
    "AHRS", "AOA", "VOR", "NDB", "ILS", "HSI", "PFD", "CDI", "OAT", "VS",
    "VSI", "ARINC", "CAN", "USB", "FDL", "NAV", "COM", "AP", "SL30", "SL40",
    "WX500", "N1", "N2", "N3", "ID", "UAT", "TIS", "XPDR", "DS", "FP", "HDG",
    "CRS", "FDS",
})
# Words replaced outright when humanizing.
_WORD_OVERRIDES = {"LIM": "Limit", "ADSB": "ADS-B"}

_DEFINE_RE = re.compile(r"^\s*#define\s+(SID_[A-Za-z0-9_]+)\s+(\d+)\b")
_BLOCK_RE = re.compile(r"^\s*//\s*(\d+)\s+ID'?s\s+used\s+by\s+(SID_[A-Za-z0-9_]+)")
_SECTION_RE = re.compile(r"^(.+?)\s+(setting-identifiers|values):\s*$")
_ENTRY_RE = re.compile(r"^(\d+)\s+-\s+(.+?)\s*$")
_ENUM_RE = re.compile(r"\s*\((\d+=[^()]*(?:\[[^\]]*\][^()]*)*)\)")


def _read_resource(name: str) -> str:
    res = resources.files("efis_data_manager").joinpath(_RESOURCE_DIR, name)
    return res.read_text(encoding="utf-8", errors="replace")


def humanize(macro: str) -> str:
    """``SID_LIM_MAX_RPM`` -> ``Limit Max RPM``."""
    name = macro[4:] if macro.startswith("SID_") else macro
    words = []
    for w in (p for p in name.split("_") if p):
        up = w.upper()
        if up in _WORD_OVERRIDES:
            words.append(_WORD_OVERRIDES[up])
        elif up in _ACRONYMS:
            words.append(up)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def _parse_settings_h(text: str) -> list[tuple[str, str, int, Optional[int]]]:
    """Return ``(macro, grt_name, sid, member_index)`` rows in file order.

    ``member_index`` is 1-based for block members, else None. Only ``SID_``
    defines are read, so the ``STID_`` namespace never enters; ``//#define``
    lines do not match the define pattern and are skipped.
    """
    defines: list[list] = []   # [macro, value, block_size or None]
    by_macro: dict[str, list] = {}
    for line in text.splitlines():
        m = _DEFINE_RE.match(line)
        if m:
            entry = [m.group(1), int(m.group(2)), None]
            defines.append(entry)
            by_macro.setdefault(m.group(1), entry)
            continue
        b = _BLOCK_RE.match(line)
        if b and b.group(2) in by_macro:
            by_macro[b.group(2)][2] = int(b.group(1))
    rows: list[tuple[str, str, int, Optional[int]]] = []
    for macro, value, size in defines:
        if size:
            for i in range(size):
                rows.append((macro, f"{macro}[{i + 1}]", value + i, i + 1))
        else:
            rows.append((macro, macro, value, None))
    return rows


def _split_enum(name: str) -> tuple[str, dict[str, str]]:
    """Split ``"G-Meter Mode (3=Off, 0=Auto)"`` into label and value dict."""
    m = _ENUM_RE.search(name)
    if not m:
        return name, {}
    values: dict[str, str] = {}
    for part in m.group(1).split(", "):
        kv = re.match(r"^(\d+)=(.+)$", part.strip())
        if kv:
            values[kv.group(1)] = kv.group(2).strip()
    label = re.sub(r"\s+", " ", (name[:m.start()] + name[m.end():])).strip()
    return label, values


def _parse_demo_doc(text: str):
    """Return ``(names, enum_values, sections)``.

    names: sid -> readable label (inline enumeration removed)
    enum_values: sid -> values parsed from inline enumerations
    sections: section title -> {raw value -> meaning}
    """
    names: dict[int, str] = {}
    enum_values: dict[int, dict[str, str]] = {}
    sections: dict[str, dict[str, str]] = {}
    kind: Optional[str] = None
    title: Optional[str] = None
    for line in text.splitlines():
        line = line.rstrip()
        h = _SECTION_RE.match(line)
        if h and not _ENTRY_RE.match(line):
            kind, title = h.group(2), line[:-1].strip()
            if kind == "values":
                sections.setdefault(title, {})
            continue
        if not line.strip():
            continue
        e = _ENTRY_RE.match(line)
        if not e or kind is None:
            continue
        if kind == "setting-identifiers":
            sid = int(e.group(1))
            if sid in names:
                logger.warning("DEMO doc lists SID %d twice; keeping first", sid)
                continue
            label, vals = _split_enum(e.group(2))
            names[sid] = label
            if vals:
                enum_values[sid] = vals
        else:
            sections[title].setdefault(e.group(1), e.group(2))
    return names, enum_values, sections


def _power_labels() -> dict[int, str]:
    """Curated power-map labels, reusing the SID constants in ``power.py``."""
    from efis_data_manager import power

    labels: dict[int, str] = {int(power.SID_RATED_HP): "Rated HP"}
    for columns, fmt in (
        (power.SID_RPM_COLUMN, "Power Map RPM (row {})"),
        (power.SID_MAP55_COLUMN, "Power Map MAP for 55% (row {})"),
        (power.SID_MAP75_COLUMN, "Power Map MAP for 75% (row {})"),
        (power.SID_ALTITUDE_COLUMN, "Power Map Altitude (row {})"),
        (power.SID_DELTA_HP_COLUMN, "Power Map Delta HP (row {})"),
    ):
        for row, sid in enumerate(columns, start=1):
            labels[int(sid)] = fmt.format(row)
    return labels


@lru_cache(maxsize=1)
def _build() -> dict[str, SidInfo]:
    rows = _parse_settings_h(_read_resource(_SETTINGS_H))
    names, enum_values, sections = _parse_demo_doc(_read_resource(_DEMO_DOC))
    power = _power_labels()

    section_values: dict[int, dict[str, str]] = {}
    for title, sids in _VALUE_SECTION_SIDS.items():
        if title not in sections:
            logger.warning("GRT value section %r not found in DEMO doc", title)
            continue
        for sid in sids:
            section_values[sid] = sections[title]

    out: dict[str, SidInfo] = {}
    for macro, grt_name, sid, member in rows:
        key = str(sid)
        if key in out:
            logger.warning("SID %s defined twice in Settings.h (%s); keeping %s",
                           key, grt_name, out[key].grt_name)
            continue
        if sid in names:
            label = names[sid]
        elif sid in power:
            label = power[sid]
        else:
            label = humanize(macro)
            if member is not None:
                label = f"{label} ({member})"
        values = section_values.get(sid) or enum_values.get(sid)
        out[key] = SidInfo(
            sid=key,
            grt_name=grt_name,
            label=label,
            values=dict(values) if values else None,
        )
    return out


def all_sids() -> dict[str, SidInfo]:
    """Return the full SID dictionary (a copy of the cached mapping)."""
    return dict(_build())


def sid_info(sid) -> Optional[SidInfo]:
    """Return the :class:`SidInfo` for ``sid`` (str or int), or None."""
    return _build().get(str(sid).strip())


def value_meaning(sid, raw_value) -> Optional[str]:
    """Meaning of ``raw_value`` for ``sid`` per GRT's docs, or None if unknown."""
    info = sid_info(sid)
    if info is None or not info.values or raw_value is None:
        return None
    return info.values.get(str(raw_value).strip())
