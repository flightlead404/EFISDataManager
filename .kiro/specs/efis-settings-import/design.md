# Design Document: EFIS Settings Import

## Overview

This feature reads the GRT HXr EFIS's own settings backup files
(`Settings.bak` / `Settings.dat`) from the local archive and derives the
analysis dashboard's alert thresholds (caution / redline limits) from the
aircraft's actual configured limits — replacing today's hand-entered values.
It also introduces three new thresholds the dashboard does not have today
(`rpm_redline`, `cht_cooling_rate_max`, `vne_ias_redline`) and can auto-set the
analysis cylinder count (`num_cylinders`).

Because these thresholds drive safety alerts, the import is **read-only** (it
never modifies the aircraft's backup files) and is **never applied silently**.
When a first or new settings backup is detected, the dashboard offers a preview
of every threshold change (current → proposed) and requires two explicit
confirmations before overwriting (Requirements 9, 10).

Detection is **source-keyed and regression-proof** (Requirement 14). A pilot
rotates USB drives between two GRT EFIS displays and keeps an `n` and `n-1`
drive in rotation, so after importing a newer backup the app may later see an
*older* backup — a stale `n-1` spare, or the backup written by the other
display. The `UPDATE=` counter is monotonic only *within a single display* and
can even reset (a fresh backup can show `UPDATE=1`), so it is a reliable
ordering signal only when comparing backups from the **same source**. Each
display is identified by a Source_Key built from its Mode_S_Address (SID 1063)
and Link_ID (SID 387), with the Flight_ID (SID 1062, the N-number) as a
secondary/human label. The system tracks a per-Source_Key Import_Marker and
offers an import only when the candidate is genuinely newer *for its own
source*; an older spare or the other display's backup never silently regresses
the thresholds.

Detection is also **primary-display gated** (Requirement 15). A candidate is
import-eligible only when it is the Primary_Display — the display whose backup
reports SID 387 (Link_ID / DULINK_ID) equal to `1`. This gate is
**device-agnostic**: it looks *only* at the Link_ID value and never inspects the
display model or class, so it works unchanged across any mix of GRT hardware
(Sport, Horizon, HXr, one or two Minis, or future models). The Primary gate is
evaluated as a **precondition to the Requirement 14 detection** — the
newer-for-source and stale `n-1` rules are applied only *among* candidates that
are already primary-eligible. On the first successful import the app **binds**
the Bound_Import_Source to that primary's Source_Key; thereafter it offers
imports only from that bound source, and a primary from a *different* Source_Key
is never applied silently — the user must explicitly rebind, still through the
Requirement 9 preview + double-confirm. A non-primary candidate is simply not
offered (its badge stays hidden); the Archiver still archives every backup
regardless.

The import is also **per-threshold selectable** (Requirement 16). The preview
opens with every proposed change ticked, but the pilot can untick individual
rows to import some limits (say a new CHT redline) while leaving others (say a
Vne change) at their current dashboard value. Vne is presented as a **single**
selectable row that expands to both underlying keys (`vne_tas_redline` +
`vne_ias_redline`), so the SID-889 mutual exclusivity of Requirement 5 can never
be half-applied. Selection is a one-time, in-session choice: it is never
persisted, and each time the preview is opened it re-initializes with all rows
selected. Because the caution-vs-redline guard now depends on which rows are
selected, toggling a checkbox re-evaluates the guard over the *selected* subset
(the preview is re-fetched server-side), and the double-confirm of Requirement 9
still gates every apply.

Cross-capture ordering is driven by the **Archive_Date** stamped into the
archived filename (`Settings-YYYY-MM-DD`), not by `UPDATE=` and not by file
modification time (Requirement 17). Real HXr data proved `UPDATE=` is *not*
globally monotonic (it stays `1` across many no-change captures and only
increments on a genuine save), and EFIS-written FAT modification times are junk,
so the latest Archive_Date is the only reliable "which capture is newest" signal
across dates. `UPDATE=` is used **only** to disambiguate the `.bak`/`.dat` pair
*within the same date* (Requirement 2.1, 17.3). Whether a newest-dated backup is
actually worth offering is decided by **Content_Hash** (equivalently GRT's
`CHECKSUM`, which the same data proved is 1:1 with content): an identical hash to
the last-imported marker means "nothing new," regardless of date or `UPDATE=`
(Requirement 17.5, refining Requirement 14).

The design adds one new module, `src/efis_data_manager/efis_settings.py`,
housing the parsing/selection/mapping/orchestration components, plus:

- A small extension to `archiver.py` to also archive `.dat` files (Requirement 3).
- Three new entries in `analysis.py` `DEFAULT_THRESHOLDS` and new alert paths in
  `detect_episodes()` / `_check_alerts()` (Requirements 5, 11, 12).
- New dashboard routes and a Settings-page import UI (preview + double-confirm)
  (Requirements 8, 9).
- New config persistence: `analysis_thresholds` additions, `num_cylinders`, and
  a `settings_import` map of per-Source_Key Import_Markers recording the
  last-imported backup for each display (Requirements 9.1, 14).

Percent-power / engine power-map import is explicitly **out of scope** (deferred
to the `percent-power` spec).

### Research notes and honest unknowns

- **FDL sample rate**: The FDL is ~1 Hz (`fdl_parser.py` `FDLRecord` carries a
  per-second `tick`; `database.fdl_data` has one row per sample keyed by ISO
  `timestamp`). The CHT cooling-rate computation is designed around this cadence
  (smoothing/windowing to avoid 1 Hz sensor noise — see Components).
- **SID definitions**: The SID→concept mapping (e.g. 151 = Max CHT, 150 = Max
  CHT Cooling Rate in °/min, 345 = Temp Units, 889 = Convert Vne TAS→IAS, 1055 =
  Num CHT) comes from the requirements Glossary, which cites GRT EIS4000 / HXr
  documentation. These SID numbers are treated as the authoritative contract
  from the requirements; the mapping table is declarative and easy to correct if
  a SID number is later found wrong.
- **Checksum algorithm — UNKNOWN.** GRT's exact `CHECKSIZE=` / `CHECKSUM=`
  algorithm is **not documented to us**. This design does **not** invent a
  formula and present it as fact. Instead, checksum verification is **best
  effort** (see "Checksum verification" below): the parser always reads the
  Checksum_Lines, but the selector only *uses* them when a verifier is known to
  be correct. Absent a confirmed algorithm, selection falls back to the
  `UPDATE=` value and the system logs that the checksum could not be verified.
  The exact algorithm should be confirmed with GRT before enabling strict
  verification.
- **`UPDATE=` is NOT globally monotonic — validated 2026-09-12 (real HXr).**
  An earlier draft assumed `UPDATE=` rose over time and could order backups
  across dates. Real HXr captures disproved this: across a run of no-change
  captures on the same display `UPDATE=` stayed at `1`, and it only incremented
  (`1 → 2`) when a genuine settings change was saved — so it is *not* a
  cross-date "when" signal. The same data confirmed EFIS-written file
  modification times are unreliable FAT timestamps (they do not track capture
  order), and that the GRT `CHECKSUM` line is **1:1 with the computed
  Content_Hash** (equal content ⇒ equal `CHECKSUM`, changed content ⇒ changed
  `CHECKSUM`). The consequences, threaded through the design below (Requirement
  17): cross-capture ordering uses **Archive_Date** as the primary key;
  `UPDATE=` disambiguates only the same-date `.bak`/`.dat` pair; and
  "is there anything new to import" is answered by **Content_Hash / `CHECKSUM`
  equality**, not by `UPDATE=` magnitude. This replaces the earlier
  `UPDATE=`-monotonic assumption everywhere it appeared.

## Architecture

The feature is a read → select → map → preview → apply pipeline. Detection of a
"new backup" is driven from the dashboard (where thresholds live and where a
table + modal UI can be rendered); the menu-bar Archiver's only new
responsibility is to also copy `.dat` files so the pair is present in the
archive.

```mermaid
flowchart TD
    subgraph MenuBar["Menu-bar app (archiver.py)"]
        USB[EFIS USB drive] -->|drive insert| ARCH[Archiver]
        ARCH -->|copy .bak AND .dat, read-only| ARCHIVE[(Date-stamped\nSettings archive)]
    end

    subgraph Dashboard["Dashboard (Flask)"]
        SET[Settings page] -->|GET /api/settings-import/status| SK[Determine candidate\nSource_Key\nSID 1063 + SID 387]
        SK --> PRIMARY{SID 387 == 1\nPrimary?\ndevice-agnostic R15.1}
        PRIMARY -->|no, in 0 or 2-254\nR15.2| SUPPRESS[Suppress prompt\navailable=false\n"not the primary display"]
        PRIMARY -->|yes, Primary| BOUND{Bound_Import_Source\nset AND Source_Key\n!= bound? R15.4/15.5}
        BOUND -->|yes, different primary source| SUPPRESS
        BOUND -->|no bound, or matches bound| DETECT{Marker for\nthis Source_Key?}
        DETECT -->|no marker\nfirst-for-source R14.3| OFFER[Offer import]
        DETECT -->|marker exists| CMP{Content_Hash differs\nfrom marker?\nR14.4/14.5 + R17.5}
        CMP -->|yes, content changed\nR14.4/R17.5| OFFER
        CMP -->|no, identical hash\nstale n-1 or unchanged\nR14.5/R17.5| SUPPRESS
        SK -->|Source_Key undetermined\nR14.8| HASH{Content_Hash differs\nfrom marker?}
        HASH -->|yes| OFFER
        HASH -->|no| SUPPRESS
        OFFER -->|flag if different source\nR14.6, R14.7| WF
        WF[Import_Workflow] --> DATE[select_current_backup\nlatest Archive_Date\nR17.1/17.2]
        DATE --> SEL[Settings_Selector\nsame-date .bak/.dat pair]
        SEL --> P1[Settings_Parser .bak]
        SEL --> P2[Settings_Parser .dat]
        SEL -->|higher UPDATE= within date,\nchecksum best-effort\nR17.3| CUR[Current backup map]
        CUR --> MAP[Settings_Mapper]
        MAP -->|proposed thresholds\n+ num_cylinders| PREVIEW[Preview diff table\nper-row checkboxes\nVne = 1 row R16]
        PREVIEW -->|tier prompt CHT/EGT/Oil\n+ caution<redline guard\nover selected subset R16.6| MODAL[Confirm modal]
        MODAL -->|POST /api/settings-import/apply\nselected_keys + double-confirm\nR16.5/16.9| APPLY[Apply selected subset]
        APPLY -->|save_config: analysis_thresholds,\nnum_cylinders, per-Source_Key\nImport_Marker R14.2,\nbind Bound_Import_Source R15.3| CFG[(config.json)]
    end

    CFG --> AN[analysis.py get_thresholds]
    AN --> EP[detect_episodes / _check_alerts\nrpm_redline, cht_cooling_rate_max,\nvne_ias_redline]
```

### Why the import UI lives in the dashboard Settings page

The requirements require a preview table (current → proposed for each threshold)
and a two-step confirmation with a caution-vs-redline tier prompt (Req 8, 9).
That is a rich, tabular, modal interaction. Three options were considered:

1. **Menu-bar app only.** The menu bar (`rumps`) is a lightweight status menu; it
   has no good affordance for a multi-row diff table or a guarded tier picker.
   Rejected — poor fit for the required preview UX.
2. **Both menu-bar and dashboard.** Detect in the menu bar (it runs the archive
   on drive insert) and confirm in the dashboard. Adds cross-process coupling and
   two code paths for the same guard logic. Rejected as unnecessary complexity.
3. **Dashboard Settings page (chosen).** The thresholds already live on the
   Settings page (`settings.html`, `/api/thresholds`, `/api/config`). The import
   reads and writes exactly those values, so co-locating the "Import limits from
   EFIS backup" action, the diff table, and the confirm modal there keeps all
   threshold reads/writes in one place and reuses the existing config plumbing.

**Decision:** the import UI is a card/action on the dashboard Settings page.
Detection is surfaced there too (a banner/badge when a newer backup is
available for its source), computed server-side from the archive vs. the
per-Source_Key Import_Marker (Requirement 14) and gated on the candidate being
the Primary_Display bound-import source (Requirement 15).
The Archiver's role is limited to copying files (Req 3); it does not drive the
UI. Trade-off: a user who never opens the dashboard will not be prompted — this
is acceptable because thresholds are only meaningful in the dashboard, and the
badge appears the next time Settings is opened.

## Components and Interfaces

All new components live in `src/efis_data_manager/efis_settings.py`. Names match
the requirements Glossary.

### Settings_Parser

Reads one Settings_Backup file into a tolerant, read-only representation.

```python
@dataclass
class ParsedBackup:
    path: str
    sids: dict[str, str]          # SID -> raw value, both strings (Req 1.1)
    update_value: Optional[int]   # from UPDATE=  (Req 1.4)
    checksize: Optional[str]      # raw CHECKSIZE= line value (Req 1.5)
    checksum: Optional[str]       # raw CHECKSUM=  line value (Req 1.5)
    line_count: int
    valid_pairs: int

class Settings_Parser:
    @staticmethod
    def parse(path: str) -> ParsedBackup: ...
        # Opens the file read-only (mode "r", never "w"/"a"), decodes as ASCII
        # with errors="replace" so non-ASCII bytes never raise (Req 1.1, 10.1).
        # For each line: split on the FIRST "=" only. LHS must be a non-empty
        # token to be a SID; RHS is the raw value (Req 1.1). Malformed lines
        # (no "=", empty key) are skipped and parsing continues (Req 1.2, 10.2).
        # UPDATE=, CHECKSIZE=, CHECKSUM= are recognized specially (Req 1.4, 1.5)
        # AND also retained in sids (harmless — mapper ignores unknown SIDs).
        # Unknown SIDs are retained in sids (Req 1.3).
```

- Read-only guarantee: the parser only ever opens files for reading; it never
  writes, truncates, renames, or deletes the source (Req 10.1). It operates on
  a copy in the *archive*, not the USB source, but the read-only contract holds
  regardless of which copy is passed.
- Parse failure: `parse()` raises `SettingsParseError` when the file cannot be
  read (OSError) or contains **zero** valid `KEY=VALUE` pairs (Req 1.6, 6). The
  source is left unmodified in every failure path.
- Tolerance: malformed lines, unknown SIDs, missing `UPDATE=`, and missing
  Checksum_Lines are all handled without raising as long as ≥1 valid pair exists
  (Req 10.2). `update_value` / `checksize` / `checksum` are `None` when absent.

### Checksum verification (best-effort — algorithm unknown)

```python
class ChecksumStatus(Enum):
    VERIFIED = "verified"        # a known algorithm confirmed integrity
    FAILED = "failed"            # a known algorithm was run and mismatched
    UNVERIFIABLE = "unverifiable"  # no known algorithm / missing lines

def verify_checksum(parsed: ParsedBackup) -> ChecksumStatus: ...
```

Because GRT's checksum algorithm is **not documented to us**, `verify_checksum`
is pluggable and defaults to returning `UNVERIFIABLE`. If/when a verified
algorithm is confirmed with GRT, it is implemented here and returns
`VERIFIED` / `FAILED`. This keeps an honest boundary: the code never claims a
file is integrity-checked when it is not. The Settings_Selector treats
`UNVERIFIABLE` as "no integrity signal" and falls back to `UPDATE=` ordering,
logging that the checksum could not be verified (Req 2.3–2.5, 10). We deliberately
do not fabricate a formula.

### Settings_Selector

Given the `.bak` and/or `.dat` for a concept (usually `Settings`), picks the
current backup.

```python
@dataclass
class SelectionResult:
    current: Optional[ParsedBackup]     # chosen file, or None on total failure
    reason: str                         # human-readable selection rationale
    checksum_note: Optional[str]        # e.g. "checksum unverifiable; used UPDATE="

class Settings_Selector:
    @staticmethod
    def select(candidates: list[ParsedBackup]) -> SelectionResult: ...
```

Selection rules (Req 2):

1. Parse all present candidates (Selector receives already-parsed backups).
2. Run `verify_checksum` on each. If **some** verify and **some** fail, restrict
   the candidate set to those that pass (`VERIFIED`), discarding `FAILED`
   (Req 2.4). `UNVERIFIABLE` candidates are retained (no integrity signal;
   best-effort — Req 2.3).
3. If **all** candidates are `FAILED` (a known algorithm ran and every file
   mismatched), return `current=None` with a verification-failure reason
   (Req 2.5). `UNVERIFIABLE`-only sets do **not** trigger this — they proceed on
   `UPDATE=`.
4. Among the surviving candidates, select the one with the highest
   `update_value` (Req 2.1). A present `update_value` beats a `None` one; if all
   are `None`, fall back to a deterministic tiebreak (prefer `.dat`, then path)
   and note it.
5. If only one candidate is present, select it (Req 2.2).

`Settings_Selector.select` remains the **same-date pair chooser**: it is given
the `.bak`/`.dat` candidates for one concept *within a single Archive_Date* and
uses `UPDATE=` (Req 2.1, 17.3) to pick the newer slot. It never orders across
dates — that is the job of the date-grouping layer below (Req 17.1, 17.2).

### Archive_Date ordering layer (Requirement 17)

Cross-capture ordering is a **layer above** `Settings_Selector`, not a change to
its `UPDATE=`-based pair logic. The archive holds many dated captures
(`Settings-YYYY-MM-DD.bak` / `.dat`) accumulated over time; on real HXr data the
`UPDATE=` counter is *not* globally monotonic (it stays `1` across no-change
captures and only bumps `1 → 2` on a genuine save) and file mtimes are unreliable
FAT timestamps, so neither can order backups across dates (Req 17.2, 17.4). The
**Archive_Date parsed from the filename is the primary ordering key** (Req 17.1).

```python
@dataclass(frozen=True)
class ArchivedBackup:
    path: str
    archive_date: date          # parsed from "Settings-YYYY-MM-DD" filename
    parsed: ParsedBackup

def parse_archive_date(path: str) -> Optional[date]:
    # Extract the YYYY-MM-DD datestamp the Archiver stamps into the filename.
    # Returns None if the filename carries no parseable datestamp (that
    # candidate sorts last / is ignored for cross-date ordering).

def select_current_backup(archived: list[ArchivedBackup]) -> SelectionResult:
    # Cross-date + same-date selection (Req 17):
    #   1. Group the archived Settings backups by archive_date.
    #   2. Take the group with the LATEST archive_date as the current capture
    #      (Req 17.1) — never an older date, regardless of any UPDATE= value
    #      an older-dated file may carry (Req 17.2).
    #   3. Within that latest date, hand the .bak/.dat pair to
    #      Settings_Selector.select() to pick the newer slot by UPDATE=
    #      (Req 17.3, consistent with Req 2.1).
    #   4. If only one backup exists for the Source_Key/concept at all, select
    #      it unchanged (Req 17.6, mirroring Req 2.2).
```

**Design decision — layer vs. rewrite.** `Settings_Selector.select()` is kept
usable and unchanged for the same-date pair; a thin `select_current_backup`
grouping helper sits above it and owns cross-date ordering. This keeps the pure,
`UPDATE=`-based pair logic (already validated by Property 6/7) intact while
correcting the cross-date defect in one well-tested place, rather than smearing
date logic through the existing selector.

**Where `load_current_settings_sids()` and detection change.** Today both
`load_current_settings_sids()` (the `power.py` integration point) and
`Import_Workflow.detect_new_backup()` glob `Settings*.bak`/`*.dat`, parse all of
them, and hand the whole mixed set straight to `Settings_Selector.select()` —
which then picks by `UPDATE=` *across dates*, the exact defect Requirement 17
corrects. Both call sites are revised to go through `select_current_backup`:
they still glob and parse, then wrap each `ParsedBackup` in an `ArchivedBackup`
with its `parse_archive_date`, group by Archive_Date, take the latest date, and
select the same-date pair via `Settings_Selector.select()`. The public return
shapes are unchanged (`load_current_settings_sids` still returns
`(sids, temp_units_celsius)`; `detect_new_backup` still returns a
`NewBackupInfo`); only the *which file wins* logic moves from "max `UPDATE=`
across everything" to "latest Archive_Date, then `UPDATE=` within it."

**Content-change / regression detection (refines Requirement 14).** Once the
latest-dated current backup is selected, whether to *offer* it is decided by
Content_Hash, not by `UPDATE=` magnitude. `detect_new_backup` offers an import
when the latest-dated backup for the bound primary Source_Key has a Content_Hash
(equivalently GRT `CHECKSUM`, proven 1:1 with content on real data) that
**differs** from that source's last-imported Import_Marker; an **identical**
Content_Hash means "nothing new to import" and raises no prompt, regardless of
Archive_Date or `UPDATE=` (Req 17.5). This makes the regression-proof detection
of Requirement 14 rest on `(Source_Key, Archive_Date, Content_Hash)` rather than
on `UPDATE_Value` magnitude. The Import_Marker already stores `content_hash`, so
the comparison needs no new persisted field; the `last_update_value` field is
retained for display/diagnostics and same-source `UPDATE=` reporting but is no
longer the offer gate across dates.

### Settings_Mapper

Translates a `ParsedBackup.sids` map into proposed dashboard threshold values,
applying units, rounding, sign, and disabled-value rules.

```python
@dataclass
class MappedValue:
    threshold_key: str          # e.g. "cht_redline", "rpm_redline", "num_cylinders"
    value: float | int
    source_sid: str             # e.g. "151"
    needs_tier_choice: bool = False   # CHT/EGT/Oil single-limit -> user picks tier

@dataclass
class MappedSettings:
    values: list[MappedValue]           # direct, non-tier mappings
    tier_choices: list[MappedValue]     # CHT/EGT/Oil limits awaiting tier pick
    temp_units_celsius: bool            # from SID 345
    vne_is_tas: bool                    # from SID 889 (1 => TAS)
    skipped_disabled: list[str]         # SIDs skipped as Disabled_Value
    num_cylinders: Optional[int]

class Settings_Mapper:
    @staticmethod
    def map(parsed: ParsedBackup) -> MappedSettings: ...
```

**Mapping table (declarative).** Each entry: SID → threshold key, value kind
(`temp`, `speed`, `g`, `voltage`, `pressure`, `rpm`, `rate`, `count`), and
whether it is a single-limit tier choice.

| SID  | Concept              | Threshold key(s)                         | Kind     | Notes / Req |
|------|----------------------|------------------------------------------|----------|-------------|
| 120  | Min Cruise Oil Press | `oil_pressure_low_cruise`                | pressure | Req 4.1 |
| 118  | Max Oil Press        | `oil_pressure_high`                      | pressure | Req 4.1 |
| 121  | Max Oil Temp         | Oil Temp tier (caution/redline)          | temp     | tier — Req 4.2, 8 |
| 151  | Max CHT              | CHT tier (caution/redline)               | temp     | tier — Req 4.3, 8 |
| 144  | Max EGT              | EGT tier (caution/redline)               | temp     | tier — Req 4.4, 8 |
| 147  | Max EGT Span         | `egt_spread_caution`                     | temp     | Req 4.5 |
| 1463 | Min Fuel Press       | `fuel_pressure_low`                      | pressure | Req 4.6 |
| 1464 | Max Fuel Press       | `fuel_pressure_high`                     | pressure | Req 4.6 |
| 353  | Min V Bus1           | `voltage_low`                            | voltage  | Req 4.7 |
| 354  | Max V Bus1           | `voltage_high`                           | voltage  | Req 4.7 |
| 123  | Max RPM              | `rpm_redline`                            | rpm      | Req 4.8, 11 |
| 150  | Max CHT Cooling Rate | `cht_cooling_rate_max`                   | rate     | Req 4.9, 12 |
| 22   | Vne (kt)             | `vne_tas_redline` or `vne_ias_redline`   | speed    | via SID 889 — Req 5 |
| 889  | Convert Vne TAS→IAS  | (selector for the two Vne keys)          | flag     | Req 5.3, 5.4 |
| 331  | G Caution Max        | `g_pos_caution`                          | g        | Req 6.1 |
| 329  | G Max                | `g_pos_limit`                            | g        | Req 6.1 |
| 332  | G Caution Min        | `g_neg_caution`                          | g (neg)  | Req 6.2, 6.3 |
| 330  | G Min                | `g_neg_limit`                            | g (neg)  | Req 6.2, 6.3 |
| 345  | Temp Units           | (unit selector: 0=F, 1=C)                | flag     | Req 7.4, 7.5 |
| 1055 | Num CHT              | `num_cylinders`                          | count    | Req 13.1 |
| 1054 | Num EGT              | `num_cylinders`                          | count    | Req 13.1 |
| 58   | Num Cylinders        | `num_cylinders`                          | count    | fallback — Req 13.2 |

**Units and rounding (Req 7):**

- `temp` kind: if `345 == 1` (Celsius), convert C→F via `F = C * 9/5 + 32`
  before rounding; if `345 == 0` (or absent, default F), no conversion
  (Req 7.4, 7.5). Round to nearest integer (Req 7.3). Cooling `rate` (SID 150,
  °/min) is a *delta*, so it uses the ratio conversion `ΔF = ΔC * 9/5` (offset
  does not apply to a rate); rounded to nearest integer.
- `speed` kind: round to nearest integer (Req 7.3).
- `g` kind: round to one decimal place (Req 7.1). Negative-G SIDs (332, 330)
  preserve their sign so `g_neg_caution` / `g_neg_limit` stay negative (Req 6.3).
  If the EFIS stores negative-G magnitudes as positive numbers, the mapper
  negates them; the invariant is "the stored dashboard value is ≤ 0".
- `voltage` kind: round to one decimal place (Req 7.2).
- `pressure`, `rpm`, `count`: integer.

**Disabled_Value handling (Req 4.10, 5.7, 6.4, 11.3, 12.4):** a SID whose value
equals its Disabled_Value is skipped — the corresponding threshold is left out
of `MappedSettings.values` entirely, so the apply step leaves it unchanged. The
Disabled_Value per SID is part of the declarative table (e.g. `0` for Max EGT
Span, Min RPM, Max CHT Cooling Rate; documented per SID). For `rpm_redline` and
`cht_cooling_rate_max`, "skipped" means the key is absent from config, which the
alerting code treats as "feature off" (see analysis integration).

**Vne mutual exclusivity (Req 5.2–5.4):** exactly one of `vne_tas_redline` /
`vne_ias_redline` is active. If SID 889 == 1 → set `vne_tas_redline` to the
imported Vne and **clear** `vne_ias_redline`; if 889 == 0 → set
`vne_ias_redline` and **clear** `vne_tas_redline`. "Clear" is represented as
setting the inactive key to a disabled sentinel (`9999`, matching the existing
`vne_tas_redline` disable convention in `analysis.py`), so exactly one Vne alert
can fire. If SID 22 is a Disabled_Value, neither is set (Req 5.7).

**Cylinder count (Req 13):** prefer SID 1055 (Num CHT), else 1054 (Num EGT),
else 58 (Num Cylinders); if none present, leave `num_cylinders` unchanged
(Req 13.4). The value flows through the same preview/confirm flow (Req 13.3).

### Source identity (Source_Key) and Content_Hash (Requirement 14)

A small helper on `Settings_Mapper` (or a free function in the same module)
derives the identity of the display that wrote a backup and a content
fingerprint. These feed the source-keyed detection in `Import_Workflow`.

```python
# Identity SIDs (from the Glossary)
SID_MODE_S = "1063"     # Mode_S_Address, ICAO 24-bit hex, e.g. "A60670"
SID_LINK_ID = "387"     # Link_ID / inter-display link, e.g. "1"
SID_FLIGHT_ID = "1062"  # Flight_ID / N-number (secondary label)

@dataclass
class SourceIdentity:
    source_key: Optional[str]   # "A60670:1"; None when undetermined (Req 14.8)
    mode_s: Optional[str]       # SID 1063 if present
    link_id: Optional[str]      # SID 387 if present
    flight_id: Optional[str]    # SID 1062 if present (label only)

def extract_source_key(parsed: ParsedBackup) -> SourceIdentity:
    # Reads SID 1063 (Mode_S_Address) and SID 387 (Link_ID) from parsed.sids.
    # Source_Key = "<mode_s>:<link_id>" when both present.
    # Graceful fallback (Req 14.1, Glossary Source_Key):
    #   - only Mode_S present   -> source_key = mode_s        (e.g. "A60670")
    #   - only Link_ID present  -> source_key = ":<link_id>"  (weak identity)
    #   - neither present/readable -> source_key = None (undetermined -> Req 14.8)
    # Flight_ID (SID 1062) is carried for display but never part of the key,
    # because the N-number can be re-used/changed and is not display-specific.

def content_hash(parsed: ParsedBackup) -> str:
    # Content_Hash: SHA-256 over the parsed SID/value content, computed from the
    # canonicalized (sorted-by-SID) "SID=value" pairs so it is order-independent
    # and reproducible. Volatile / non-configuration lines are EXCLUDED so the
    # hash tracks the settings content, not incidental churn:
    #   - UPDATE=            (the monotonic counter; ordering handled separately)
    #   - CHECKSIZE=, CHECKSUM=  (integrity lines, not settings content)
    # All remaining SID=value pairs (including identity SIDs 1063/387/1062) are
    # included, so two backups with identical settings hash equal even if their
    # UPDATE= counters differ.
```

- **Source_Key form:** `"<Mode_S_Address>:<Link_ID>"`, e.g. `"A60670:1"`. This
  is the map key used by the `settings_import` Import_Marker map (see Data
  Models) and by every branch of `detect_new_backup`.
- **Graceful fallback (Req 14.1, Glossary):** when one identity SID is missing
  the key degrades to whatever identity SIDs are present; when *no* identity SID
  is readable the Source_Key is `None` (undetermined), which routes detection
  through the Content_Hash fallback (Req 14.8) so ambiguous identity can never
  cause a silent regression.
- **Content_Hash scope:** the hash deliberately excludes `UPDATE=`,
  `CHECKSIZE=`, and `CHECKSUM=` so it reflects only the configuration content.
  It is the secondary signal for the undetermined-source case and is also
  stored in every Import_Marker so "did the content actually change" can be
  answered independently of the `UPDATE=` counter.

### Import_Workflow

Orchestrates detection, preview, tier prompts, guards, and the double-confirm
apply. Pure/decidable logic lives here (testable without Flask); the dashboard
routes are thin wrappers.

```python
@dataclass
class ThresholdChange:
    key: str                    # threshold key or "num_cylinders"
    label: str                  # human label (reuses settings.html thresholdLabels)
    current: Optional[float]    # current dashboard value (None if unset)
    proposed: Optional[float]   # proposed imported value
    source_sid: Optional[str]
    tier: Optional[str] = None  # "caution" | "redline" when user chose a tier
    selected: bool = True       # ticked for import (default all-selected) — R16.1
    selection_key: str = ""     # UI row identity selected_keys refers to; equals
                                # `key` for ordinary rows, "vne" for both vne_*
                                # ThresholdChanges (the atomic Vne unit) — R16.3/16.4

@dataclass
class NewBackupInfo:
    available: bool                 # True => an import may be offered (R14.3/4/8, R15)
    source_key: Optional[str]       # candidate Source_Key, None if undetermined
    is_first_for_source: bool       # no prior marker for this Source_Key (R14.3)
    is_different_source: bool       # differs from most-recent prior import (R14.6/7)
    candidate_update_value: Optional[int]        # candidate UPDATE= (R14.4/5)
    source_last_imported_update: Optional[int]   # that source's marker UPDATE=
    candidate_content_hash: Optional[str]        # Content_Hash of candidate
    is_primary: bool = False        # candidate SID 387 == 1 (Primary_Display) — R15.1
    link_id: Optional[str] = None   # candidate SID 387 value (0/1/2-254) — R15.1/15.2
    bound_import_source: Optional[str] = None    # currently bound Source_Key, None if unbound — R15.3/15.4
    is_bound_source_mismatch: bool = False       # primary but Source_Key != bound_import_source — R15.5
    reason: str = ""                # human-readable why offered / suppressed
    parsed: Optional[ParsedBackup] = None        # the selected candidate

@dataclass
class ImportPreview:
    changes: list[ThresholdChange]
    tier_prompts: list[str]     # concepts needing a tier decision: CHT/EGT/Oil Temp
    warnings: list[str]         # e.g. caution >= redline conflicts
    checksum_note: Optional[str]
    backup_update_value: Optional[int]
    source_key: Optional[str]              # candidate Source_Key (R14.1)
    is_different_source: bool = False      # flag "from a different display/aircraft" (R14.6, 14.7)
    source_note: Optional[str] = None      # e.g. "This backup is from a different display or aircraft than your last import."
    can_apply: bool = False
    # Per-threshold selection (R16): each change carries selected/selection_key
    # above; the guard and apply operate over the selected subset. Selection is
    # NOT persisted (fresh all-selected each build — R16.8) and is passed to
    # build_preview/apply as a selected_keys argument rather than stored here.

class Import_Workflow:
    @staticmethod
    def detect_new_backup(config: dict, archive_root: Path) -> Optional[NewBackupInfo]:
        # Primary-gated (Requirement 15), then source-keyed, regression-proof
        # detection (Requirement 14). The R15 gates are evaluated BEFORE the R14
        # decision, so the newer-for-source and stale n-1 rules apply only among
        # import-eligible (Primary, bound-source) candidates (R15.8).
        #
        # 0.  Select the current archived Settings backup via the Archive_Date
        #     layer (select_current_backup: latest Archive_Date, then
        #     Settings_Selector for the same-date .bak/.dat pair — R17.1-17.4) and
        #     parse it into the candidate ParsedBackup.
        #
        # 0a. PRIMARY GATE (R15.1, R15.2) — device-agnostic:
        #       link_id = candidate.sids.get("387")   # Link_ID / DULINK_ID
        #       is_primary = (link_id == "1")
        #     This checks ONLY SID 387; it never inspects the display model/class
        #     (no HXr/Sport/Mini/Horizon-specific SID) so eligibility is identical
        #     across any hardware mix (R15.1). If NOT primary (link_id in
        #     {0, 2-254}, or absent):
        #       => return NewBackupInfo(available=False, is_primary=False,
        #               link_id=link_id,
        #               reason="not the primary display")   # R15.2
        #     Archiver behavior is unaffected — this only suppresses the import
        #     offer; every backup is still archived (R15.2).
        #
        # 0b. BOUND-SOURCE GATE (R15.3, R15.4, R15.5):
        #       bound = config["settings_import"].get(BOUND_IMPORT_SOURCE_KEY)
        #       candidate Source_Key = extract_source_key(candidate) (R14.1)
        #     - bound is None (not yet bound): eligible — the first successful
        #       import will bind Bound_Import_Source to this primary's Source_Key
        #       (see apply, R15.3).
        #     - bound is set AND Source_Key == bound: eligible; continue to R14.
        #     - bound is set AND Source_Key != bound (a DIFFERENT primary source):
        #         => available=False, is_bound_source_mismatch=True,
        #            bound_import_source=bound,
        #            reason="primary backup from a different source than the bound
        #                    import source; explicit rebind required"   # R15.5
        #       Never applied silently. The user may explicitly rebind (change the
        #       Bound_Import_Source), after which it flows through the normal
        #       preview + double-confirm of Requirement 9 (R15.5).
        #
        # 1.  candidate Source_Key = extract_source_key(candidate) (R14.1).
        # 2.  markers = config["settings_import"]  # map Source_Key -> Import_Marker
        #     (the reserved BOUND_IMPORT_SOURCE_KEY entry is not a Source_Key marker)
        #
        # REQUIREMENT 17 REFINEMENT (cross-date ordering + content-hash offer):
        # step 0 selects the candidate via the Archive_Date layer
        # (select_current_backup): the LATEST-Archive_Date capture wins, with
        # UPDATE= only disambiguating the same-date .bak/.dat pair (R17.1-17.4).
        # The offer decision below is then made on CONTENT_HASH, not UPDATE=
        # magnitude, because real HXr UPDATE= is not globally monotonic. Read
        # every "update_value >/<= marker.last_update_value" test below as its
        # refined form "content_hash != / == marker.content_hash" (R17.5); the
        # UPDATE= comparisons describe the as-built pre-Req-17 code and are
        # superseded by the Content_Hash gate for the cross-date offer decision.
        #
        # Decision (returns NewBackupInfo; available drives whether to offer),
        # evaluated ONLY for candidates that passed 0a + 0b (R15.8):
        #   a. Source_Key determined:
        #      - no marker for this Source_Key
        #            => available=True, is_first_for_source=True (R14.3)
        #      - marker exists AND candidate.content_hash != marker.content_hash
        #        (was: candidate.update_value > marker.last_update_value)
        #            => available=True (R14.4 as refined by R17.5)
        #      - marker exists AND candidate.content_hash == marker.content_hash
        #        (was: candidate.update_value <= marker.last_update_value)
        #            => available=False, suppress prompt (R14.5 as refined by
        #               R17.5) — the stale n-1 AND the newer-date-but-unchanged case
        #      - is_different_source = (Source_Key != most-recently-imported Source_Key)
        #            (R14.6, 14.7 — flagged in the preview; never a silent overwrite)
        #   b. Source_Key undetermined (None) (R14.8):
        #      - fall back to Content_Hash compare against the marker of the most
        #        recent prior import:
        #            available=True iff candidate.content_hash != marker.content_hash
        #      - if there is no prior import at all, treat as first import
        #            => available=True
        #      - never regress on ambiguous identity: when content is identical,
        #        available=False.
        #
        # No-primary case (R15.6, R15.7): if the selected candidate is not a
        # Primary_Display it is suppressed at step 0a. When NO archived candidate
        # anywhere reports Primary (every candidate's SID 387 is in {0, 2-254}),
        # detect_new_backup offers no import and NEVER falls back to selecting a
        # non-primary candidate as the source (R15.6). Explicit user designation
        # of a source when none is Primary is an OPTIONAL / DEFERRED behavior
        # (R15.7): it is not offered automatically, and would require an explicit
        # user action to designate a non-primary source before any import — not
        # implemented in this pass, documented here as the intended extension
        # point.
        #
        # Returns None only when no Settings backup exists / selection failed.
        # In all "do not offer" cases (stale n-1, undetermined-no-regress,
        # non-primary, bound-source mismatch) it returns
        # NewBackupInfo(available=False) with a reason, so the caller can suppress
        # the prompt (R14.5, 14.8, R15.2, R15.5).

    @staticmethod
    def build_preview(parsed: ParsedBackup, config: dict,
                      tier_selections: dict[str, str],
                      selected_keys: Optional[set[str]] = None) -> ImportPreview:
        # Runs Settings_Mapper, joins proposed values against current config
        # thresholds, applies tier_selections to CHT/EGT/Oil single limits,
        # runs the caution<redline guard (Req 8.2), builds the change list
        # (Req 9.2, 9.3, 13.3). can_apply=False if any unresolved conflict.
        #
        # Per-threshold selection (Req 16): each ThresholdChange carries a
        # `selected` flag and a `selection_key` (the row identity the UI ticks).
        # selected_keys is the set of ticked selection keys; when None (the
        # default), EVERY row is selected — the fresh, all-checked initial state
        # (Req 16.1, 16.2, 16.8), and backward-compatible with the pre-Req-16
        # callers. num_cylinders is a selectable row like any other (Req 16.2).
        # Vne is a SINGLE selectable row keyed "vne" that spans BOTH vne_*
        # ThresholdChanges (Req 16.3, 16.4): the two rows share selection_key
        # "vne", so ticking/unticking them is atomic and it is impossible to
        # select exactly one.
        #
        # The caution<redline guard now runs over the SELECTED subset (Req 16.6):
        # for each concept it compares the selected caution proposal against the
        # selected-or-current redline (and vice-versa), so unticking a row that
        # would have conflicted clears the block. Because selection changes the
        # guard outcome, the route re-fetches the preview with the current
        # selected_keys whenever a checkbox toggles (see Dashboard routes).
        # Sets source_key = extract_source_key(parsed) and, when it differs from
        # the most-recent prior import's Source_Key, sets is_different_source and
        # a source_note flagging "from a different display/aircraft than your
        # last import" (Req 14.6, 14.7). This is informational; it does NOT
        # relax the double-confirm gate (Req 14.9).
        # When a Bound_Import_Source is set and source_key differs from it, the
        # preview composes an additional rebind note ("this primary backup is
        # from a different source than your bound import source; applying it
        # rebinds the import source") on top of the is_different_source note. The
        # preview still cannot be applied to a mismatched bound source without an
        # explicit user rebind, and the double-confirm gate is unchanged
        # (Req 15.5).

    @staticmethod
    def apply(preview: ImportPreview, confirm1: bool, confirm2: bool,
              config: dict, selected_keys: Optional[set[str]] = None) -> ApplyResult:
        # Requires confirm1 AND confirm2 (Req 9.4, 9.5). On decline of either,
        # returns unchanged config (no-op, Req 9.6, 10.3).
        #
        # Per-threshold selection (Req 16.5): `selected_keys` is the set of
        # ticked selection keys. apply writes ONLY the changes whose
        # selection_key is in the set and leaves every unselected threshold at
        # its current dashboard value (a frame property over the selected
        # subset). When selected_keys is None, ALL changes are written —
        # preserving the pre-Req-16 behavior and keeping apply pure/testable
        # (the flag lives in the argument, not baked into the preview). The Vne
        # unit's selection_key "vne" expands to BOTH underlying keys, so
        # selecting Vne writes vne_tas_redline AND vne_ias_redline together,
        # preserving SID-889 exclusivity (one real value, the other 9999); it is
        # never possible to write exactly one (Req 16.3, 16.4).
        #
        # Deselect-all (Req 16.7): if selected_keys is an empty set, apply is a
        # no-op that changes no threshold and returns a reason ("nothing
        # selected to import"); the double-confirm still gates (Req 16.9).
        #
        # On success, writes only the SELECTED analysis_thresholds additions +
        # num_cylinders (when its row is selected) atomically via save_config
        # (Req 9.5), AND persists/refreshes the per-Source_Key
        # Import_Marker for preview.source_key — recording that source's
        # last_update_value, content_hash (Content_Hash), backup_name, and
        # imported_at (Req 14.2). Markers for other Source_Keys are left intact,
        # so importing from one display never clears the other's marker.
        #
        # Bound_Import_Source (Req 15.3, 15.4, 15.5): on the FIRST successful
        # import (no bound source persisted yet), apply also persists
        # config["settings_import"][BOUND_IMPORT_SOURCE_KEY] = preview.source_key,
        # binding the import source to this primary's Source_Key. Thereafter apply
        # is a no-op for any primary candidate whose Source_Key != the bound value
        # unless the user has explicitly rebound the Bound_Import_Source first;
        # such an explicit rebind still requires confirm1 AND confirm2 (Req 15.5,
        # composed with Req 9.4). Binding never overwrites an existing bound
        # source implicitly.
```

**Per-threshold selection and the Vne unit (Req 16):** each `ThresholdChange`
gains two fields — `selected: bool` (defaults True) and
`selection_key: str` (the row identity the UI ticks and that `selected_keys`
references). For an ordinary change the `selection_key` is just its threshold
`key` (e.g. `"cht_redline"`, `"rpm_redline"`, `"num_cylinders"`), so ticking a
row maps 1:1 to writing that key.

Vne is the one grouped row. The mapper emits **two** `ThresholdChange`s for Vne —
the active key at the imported value and the inactive key at the `9999` sentinel
(preserving SID-889 exclusivity, Req 5.3/5.4). `build_preview` gives **both** the
same `selection_key = "vne"`, so from the UI's perspective they are a single
checkbox row (rendered once, labelled "Vne"), and `selected_keys` carries the one
token `"vne"` for the pair. When `apply` filters by `selected_keys`, membership
is tested on `selection_key`, so selecting `"vne"` writes both `vne_*` keys
together and deselecting it writes neither — it is structurally impossible to
apply exactly one of the two (Req 16.3, 16.4). Every other row remains
independently selectable, including `num_cylinders` (Req 16.2).

The initial selection is **fresh and all-checked** every time the preview opens
and is **never persisted** (Req 16.1, 16.8): `build_preview(selected_keys=None)`
marks every row `selected=True`, and no selection state is stored in config or
the marker. A subsequent preview open starts all-checked again.

The **caution<redline guard runs over the selected subset** (Req 16.6). For each
CHT/EGT/Oil-Temp concept the guard compares the *selected* caution proposal
against the *selected-or-current* redline (and the reverse), instead of over all
proposals. Unticking a row that would have conflicted therefore clears the block;
ticking one back re-imposes it. Because selection thus changes `can_apply`, it is
a server-side recompute: the preview is re-fetched with the current
`selected_keys` on every checkbox toggle (see Dashboard routes), and the apply
endpoint re-validates with the same `selected_keys` so the client and server
agree on the guarded subset.

**Deselect-all (Req 16.7):** an empty `selected_keys` set makes `apply` a no-op
that changes no `Dashboard_Threshold` and reports "nothing selected to import."
Selection never bypasses either confirmation — the double-confirm of Requirement
9 still gates a selected-subset apply exactly as it gates an all-rows apply
(Req 16.9).

**Tier prompt + guard (Req 8):** CHT, EGT, and Oil Temp each expose a single EFIS
max (SIDs 151, 144, 121) but the dashboard has both a caution and a redline tier.
The workflow asks the user, per concept, whether the imported limit maps to the
Caution_Tier or the Redline_Tier (Req 8.1). After applying the choice, it checks
that the resulting caution is **strictly below** the redline for that concept
(comparing against the other tier's current-or-proposed value); if
`caution >= redline`, it emits a warning and sets `can_apply = False` until the
user resolves it (Req 8.2). The G-meter is mapped both-tiers-directly with no
prompt (caution SIDs → caution keys, max/min SIDs → limit keys) (Req 8.3).

**Atomicity (Req 9.5, 10.3):** `apply` builds the complete next-config in memory
(current config + all confirmed changes) and persists it with a single
`save_config` call. Either every confirmed change lands or, on any failure before
the write, none do and the reason is reported.

### Archiver extension (Requirement 3)

`archive_efis_drive` already copies `Settings.bak`, `State.bak`, `WP.bak`,
`Plan.bak` via `_copy_with_datestamp` (size-based skip). The change adds the
`.dat` siblings to the same loop:

```python
for bak_name in ["Settings.bak", "State.bak", "WP.bak", "Plan.bak",
                 "Settings.dat", "State.dat", "WP.dat", "Plan.dat"]:
    ...
```

`_copy_with_datestamp` already copies without deleting the source (Req 3.2) and
skips when a same-size dated copy exists (Req 3.3). No other archiver change is
needed; `.dat` files land in the same date-stamped `Settings/` archive dir
(Req 3.1).

### analysis.py integration (Requirements 5, 11, 12)

Three new `DEFAULT_THRESHOLDS` entries (see Data Models) and new alert paths:

- **`rpm_redline` (Req 11):** in `detect_episodes()`, add a high-direction
  episode on the `rpm1` column with `redline == threshold` (warning-only, like
  Vne) so any sample at/above Max RPM opens a warning episode (Req 11.1). In
  `_check_alerts()`, add a flight-summary line when `stats.max_rpm >= rpm_redline`
  (Req 11.2). Both are gated on the threshold being set (not the disabled
  sentinel); if unset, no RPM alert (Req 11.3). `FlightStats.max_rpm` already
  exists.

- **`vne_ias_redline` (Req 5.6):** mirror the existing `vne_tas_redline` path.
  In `detect_episodes()`, when `vne_ias_redline < 9999`, add a high episode on
  `indicated_airspeed`; when `vne_tas_redline < 9999`, keep the existing
  `true_airspeed` episode. Because the mapper clears the inactive key to `9999`,
  exactly one runs (Req 5.5, 5.6). `_check_alerts()` gains the IAS summary line
  mirroring the existing TAS one, using `stats.max_indicated_airspeed`.

- **`cht_cooling_rate_max` (Req 12):** shock-cooling detection. This is a *rate*
  of decrease of CHT in °/min. The FDL is ~1 Hz, so per-sample first differences
  are noisy. Design:
  - Track the **hottest cylinder at each instant** (same
    `cyl_series(cht_cols, 100)` construction already used for CHT episodes), so
    a single monitored CHT series is produced.
  - Smooth with a short trailing window and compute the cooling rate over a
    sliding **W-second window** (default 10 s): `rate = (cht[t-W] - cht[t]) / (W/60)`
    in °/min (positive = cooling). Using a multi-second window instead of the raw
    1 Hz difference suppresses single-sample noise while still catching a genuine
    shock-cooling ramp (Req 12.2). W is an internal constant (documented in code);
    it is not a user threshold.
  - Feed the rate series into a high-direction episode against
    `cht_cooling_rate_max` (caution severity; shock-cooling has no separate
    redline). Add a flight-summary alert when the max cooling rate over the
    flight exceeds the threshold (Req 12.3).
  - If `cht_cooling_rate_max` is unset, skip the entire computation (Req 12.4).

  The rate detection reuses `_detect_episodes_series` by passing the derived
  `(timestamp, cooling_rate)` series with `direction="high"`.

  Concretely, the cooling-rate series is built by first smoothing the
  hottest-cylinder CHT with a **5 s trailing average**, then measuring the rate
  over a **W = 10 s** window against the nearest earlier smoothed sample that
  falls within a **2 s** pairing tolerance; the series-start warmup (no full
  window yet, or an earlier endpoint lacking a full smoothing window) yields no
  rate sample rather than an amplified noisy one — giving 1 Hz noise immunity.
  These are internal constants (`_CHT_COOLING_SMOOTH_S`, `_CHT_COOLING_WINDOW_S`,
  `_CHT_COOLING_WINDOW_TOL_S`), not user thresholds. `FlightStats` gains a
  `max_cht_cooling_rate` field (°/min, positive = cooling; the max windowed rate
  over the flight) that the summary alert compares against `cht_cooling_rate_max`.

  Two new **episode deadband** keys accompany the new alert paths (recovery
  margins for the hysteresis engine, alongside the existing
  `episode_deadband_temp` / `_oil_press` / `_fuel_press`):
  `episode_deadband_rpm` (default **50 RPM**) for the RPM-redline episode and
  `episode_deadband_cht_cooling` (default **5 °/min**) for the cooling-rate
  episode. Both are read from `analysis_thresholds` with those defaults and are
  not part of the EFIS import (they tune episode merging, not a limit value).

### Dashboard routes (Requirements 8, 9)

New Flask routes in `dashboard/app.py` (thin wrappers over `Import_Workflow`):

- `GET /api/settings-import/status` → source-aware detection JSON (Req 9.1,
  14):

  ```json
  {
      "available": true,
      "source_key": "A60670:1",
      "is_primary": true,
      "bound_import_source": "A60670:1",
      "is_first_for_source": false,
      "is_different_source": true,
      "candidate_update_value": 2,
      "source_last_imported_update": 1,
      "reason": "Latest-dated backup for A60670:1 has changed content since last import"
  }
  ```

  `available` reflects the R15 primary/bound gates AND the R14 decision (offer
  iff the candidate is a Primary_Display whose Source_Key matches the bound
  source or none is bound yet, AND it is first-for-source, or strictly newer for
  its source, or the undetermined-source content-hash+update case);
  `source_key` is the candidate Source_Key (`null` when undetermined);
  `is_primary` reflects SID 387 == 1 (Req 15.1); `bound_import_source` is the
  currently bound Source_Key or `null` when unbound (Req 15.3, 15.4);
  `is_different_source` flags a backup from a different display/aircraft than the
  last import (Req 14.6, 14.7); `candidate_update_value` vs.
  `source_last_imported_update` lets the UI show the comparison for *that*
  source. Drives the "new backup" badge on Settings. Thin wrapper over
  `Import_Workflow.detect_new_backup` — it returns `available: false` (with a
  `reason`) for the stale `n-1` and undetermined-no-regress cases (Req 14.5,
  14.8), for a non-primary candidate (`reason: "not the primary display"`,
  Req 15.2), and for a primary whose Source_Key differs from the bound source
  (`reason` naming the bound-source mismatch, Req 15.5) — so the badge stays
  hidden in all those cases.
- `GET /api/settings-import/preview?tier_cht=&tier_egt=&tier_oil=&selected_keys=`
  → `ImportPreview` as JSON: the diff table (each `ThresholdChange` with current
  + proposed + tier + `selected` + `selection_key`), tier prompts, conflict
  warnings, checksum note, and `can_apply` (Req 9.2, 9.3, 8). The optional
  `selected_keys` query param (comma-separated, or repeated) carries the
  currently-ticked selection keys — the `"vne"` unit token and threshold keys
  such as `"num_cylinders"`. **When `selected_keys` is absent, the preview opens
  with every row selected** (the fresh, all-checked initial state — Req 16.1,
  16.8), which is also how the very first preview fetch behaves. When present,
  `build_preview` re-runs the caution<redline guard over just the selected
  subset (Req 16.6), so the UI re-fetches this endpoint with the updated
  `selected_keys` whenever a checkbox toggles — exactly mirroring how the
  `tier_*` params already trigger a re-fetch.
- `POST /api/settings-import/apply` → applies only when both confirmations are
  true and there are no unresolved conflicts; returns the updated thresholds or
  an error reason (Req 9.4–9.6, 10.3, 16.5, 16.7, 16.9). Reuses `save_config`.
  Request body:

  ```json
  {
      "confirm1": true,
      "confirm2": true,
      "tier_selections": { "cht": "redline", "egt": "caution", "oil_temp": "redline" },
      "selected_keys": ["cht_redline", "rpm_redline", "vne", "num_cylinders"]
  }
  ```

  `selected_keys` is the set of ticked selection keys (the `"vne"` unit token and
  individual threshold/`num_cylinders` keys). It is **optional and
  backward-compatible**: when omitted or `null`, the endpoint applies **all**
  changes (pre-Req-16 behavior); when present, only the selected rows are written
  and the `"vne"` token expands to both `vne_*` keys (Req 16.3–16.5). An **empty**
  `selected_keys` array is a valid deselect-all that yields a no-op with a
  "nothing selected to import" reason (Req 16.7). The endpoint re-validates the
  guard with the same `selected_keys` before writing, so it cannot apply a subset
  the guard would block (Req 16.6, 16.9).

### Settings page UI (Requirements 8, 9)

`settings.html` gains an "Import limits from EFIS backup" card:

- A banner/badge shown when `/api/settings-import/status` reports `available`.
- A "Review import…" button that fetches the preview and renders a table:
  columns *Import?*, *Threshold*, *Current*, *Imported* (reusing the existing
  `thresholdLabels` map, extended with labels for the three new keys and
  `num_cylinders`).
- A leading **checkbox column** (Req 16.1): every row is rendered with a
  checkbox, **default checked**, bound to the row's `selection_key`. A
  **select-all / select-none** affordance in the column header ticks or unticks
  every row at once. `num_cylinders` gets its own checkbox row like any other
  change (Req 16.2).
- **Vne is a single checkbox row** (Req 16.3, 16.4): the two `vne_*`
  `ThresholdChange`s that share `selection_key = "vne"` are collapsed into one
  displayed row (labelled "Vne") with one checkbox. Ticking it imports both
  underlying keys; the UI never shows two separate Vne checkboxes, so a partial
  Vne selection is not expressible.
- On any checkbox toggle (including select-all/none), the UI **re-fetches the
  preview** with the current `selected_keys` so the caution<redline guard
  re-runs over the selected subset and `can_apply` / the warnings update
  (Req 16.6). Selection is one-time: the table opens fresh all-checked each time
  the preview is opened and nothing is persisted between sessions (Req 16.8).
- The apply POST includes the current `selected_keys`; if every row is unticked,
  the Apply action is disabled/blocked and the UI shows "nothing selected to
  import" (Req 16.7).
- Tier selectors (Caution / Redline radio) for CHT, EGT, Oil Temp; re-fetches
  the preview when changed so the caution<redline guard re-runs (Req 8.1, 8.2).
- An explicit overwrite warning ("Applying will overwrite your current alert
  thresholds") (Req 9.3), then a **first** "Apply import" button and a **second**
  confirmation dialog before the POST (Req 9.4).
- On success, re-fetches `/api/thresholds` + `/api/config` so the existing form
  reflects the imported values.

## Data Models

### New `DEFAULT_THRESHOLDS` entries (`analysis.py`)

```python
# RPM redline (engine over-speed). Disabled sentinel: absent/None.
"rpm_redline": 2700,            # RPM (typical Lycoming redline; set from EFIS)

# CHT shock-cooling rate limit, degrees F per minute (SID 150). GRT EIS4000
# expresses this in °/min. Disabled when unset. Default off (large) until imported.
"cht_cooling_rate_max": 60,    # °F/min

# IAS-based Vne companion to vne_tas_redline. Exactly one Vne key is active at a
# time (the other holds the 9999 disabled sentinel). Default disabled.
"vne_ias_redline": 9999,       # KIAS; 9999 = disabled
```

- `vne_tas_redline` (existing) and `vne_ias_redline` (new) are **mutually
  exclusive**: the mapper sets one to the imported value and the other to `9999`
  (disabled). Default ships with `vne_tas_redline = 200` active and
  `vne_ias_redline = 9999` inactive, preserving today's behavior until an import
  flips it based on SID 889.
- `rpm_redline` and `cht_cooling_rate_max` are gated so an unset/sentinel value
  produces no alert (Req 11.3, 12.4). For consistency with the existing `9999`
  pattern, "unset" for these is represented by omitting the key from
  `analysis_thresholds` (defaults apply) or by the mapper skipping a
  Disabled_Value.
- Two **episode-deadband** tuning keys support the new alert paths (not
  imported from the EFIS; they only tune episode merging): `episode_deadband_rpm`
  (default **50 RPM**) and `episode_deadband_cht_cooling` (default **5 °/min**),
  read from `analysis_thresholds` with those defaults. `FlightStats` also gains
  `max_cht_cooling_rate` (°/min, positive = cooling): the max windowed
  cooling rate over the flight, computed on the hottest-cylinder CHT series with
  a **5 s** trailing smooth over a **W = 10 s** window (**2 s** pairing
  tolerance) and a series-start warmup guard for 1 Hz noise immunity — see
  "analysis.py integration".

### Config changes (`config.py` / `config.json`)

- `analysis_thresholds`: gains any of `rpm_redline`, `cht_cooling_rate_max`,
  `vne_ias_redline`, `vne_tas_redline`, plus the existing engine/airspeed/G keys,
  when imported (Req 9.5). Unmapped/disabled keys are left as-is.
- `num_cylinders`: existing top-level key, auto-set from SID 1055/1054/58
  (Req 13).
- **New** `settings_import` map of per-Source_Key Import_Markers (records what
  was last imported *from each display* so regression-proof, source-keyed
  detection works — Req 9.1, 14.2). The old single-object marker is **replaced**
  by a map keyed by Source_Key:

  ```json
  "settings_import": {
      "bound_import_source": "A60670:1",
      "A60670:1": {
          "last_update_value": 2,
          "content_hash": "…",
          "backup_name": "Settings-2026-08-13.dat",
          "imported_at": "2026-08-13T10:22:00"
      },
      "A60670:2": {
          "last_update_value": 1,
          "content_hash": "…",
          "backup_name": "Settings-2026-08-11.dat",
          "imported_at": "2026-08-11T09:05:00"
      }
  }
  ```

  The reserved top-level `bound_import_source` key (the Bound_Import_Source, a
  Source_Key string such as `"A60670:1"`) sits **alongside** the per-Source_Key
  Import_Markers within the same `settings_import` map (Req 15.3). It is a
  *reserved* key name that a Source_Key can never collide with, because a
  Source_Key is always either `"<Mode_S_Address>:<Link_ID>"`, a bare
  Mode_S_Address, or `":<Link_ID>"` — none of which is the literal string
  `bound_import_source`. In code this reserved name is the constant
  `BOUND_IMPORT_SOURCE_KEY = "bound_import_source"`, and detection/apply skip it
  when iterating Source_Key markers. Its value is the Source_Key that imports are
  currently bound to; imports are offered only from candidates whose Source_Key
  equals it (Req 15.4), and a primary from any other Source_Key requires an
  explicit rebind (Req 15.5).

  Each remaining entry is an Import_Marker keyed by the candidate's Source_Key
  (Mode_S_Address `:` Link_ID, e.g. `A60670:1`), storing that source's
  `last_update_value` (its last-imported UPDATE_Value), `content_hash`
  (Content_Hash), `backup_name`, and `imported_at`. The two entries above are
  the pilot's two displays sharing one aircraft (`A60670`), distinguished by
  Link_ID.

  **Detection per Source_Key (Req 14, refined by Req 17.5):** the candidate is
  the **latest-Archive_Date** backup for its Source_Key (selected by
  `select_current_backup`, not by cross-date `UPDATE=`), and the offer decision
  rests on Content_Hash rather than `UPDATE=` magnitude:
  - No entry for the candidate's Source_Key ⇒ first import for that source ⇒
    offer (Req 14.3).
  - Entry exists AND candidate `content_hash` **differs** from that entry's
    `content_hash` ⇒ offer — there is genuinely new content for that source
    (Req 14.4 as refined by Req 17.5).
  - Entry exists AND candidate `content_hash` **equals** that entry's
    `content_hash` ⇒ do **not** offer; nothing new, regardless of Archive_Date
    or `UPDATE=` (Req 14.5 as refined by Req 17.5) — covers both the stale `n-1`
    spare and a newer-dated but unchanged capture.
  - Candidate Source_Key differs from every stored key ⇒ distinct source; may be
    offered but flagged as a different display/aircraft (Req 14.6, 14.7).
  - Candidate Source_Key undetermined ⇒ fall back to comparing `content_hash`
    against the most-recent prior marker; offer only when content differs
    (Req 14.8).

  `last_update_value` is retained in each marker for display/diagnostics and
  same-source `UPDATE=` reporting, and still disambiguates the same-date
  `.bak`/`.dat` pair (Req 17.3); it is no longer the cross-date offer gate, since
  real HXr `UPDATE=` is not globally monotonic (see "Research notes and honest
  unknowns").

  On a successful apply, only the candidate source's entry is written/refreshed
  (Req 14.2); other sources' markers are preserved.

  **Migration:** a legacy single-object `settings_import` (with
  `last_update_value` / `last_backup_sha256` / …) is treated as an untagged
  marker under a synthetic "unknown source" key; the first successful
  source-keyed import writes a properly keyed entry alongside it. Detection
  tolerates the legacy shape by routing it through the undetermined-source path.

  **`bound_import_source` migration (Req 15.3):** an **absent**
  `bound_import_source` key means "not yet bound" — the import source has never
  been established. In that state the Primary gate alone governs eligibility
  (any Primary_Display candidate is eligible), and the first successful Primary
  import writes `bound_import_source` = that primary's Source_Key. Existing
  configs upgraded from a pre-Req-15 build therefore start unbound and bind on
  the next successful Primary import; no explicit migration step is required.

### fdl_data columns used (read-only, existing)

`rpm1`, `cht1..cht6`, `indicated_airspeed`, `true_airspeed`, `timestamp`
(ISO 8601). No schema change — the new alerts read existing columns.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

The parser, selector, mapper, and workflow-decision logic are pure functions
with clear input/output, so property-based testing applies well. Alerting
integration in `analysis.py` is tested by generating synthetic time series and
asserting the alert fires exactly when the (mathematically defined) condition
holds. File-copy behavior of the Archiver and the concrete SID→key table
lookups are example/integration tests (see Testing Strategy), not properties.

### Property 1: Parse round-trip preserves SID map

*For any* map of SID→value rendered as `KEY=VALUE` lines, parsing the rendered
text SHALL produce a SID map equal to the original (values compared as strings),
and every valid pair SHALL appear in the parsed map.

**Validates: Requirements 1.1, 1.3**

### Property 2: Parser tolerates garbage and never crashes

*For any* input consisting of valid `KEY=VALUE` pairs interleaved with arbitrary
malformed lines (no `=`, empty key, non-ASCII bytes) in any order, the parser
SHALL return without raising, SHALL recover every valid pair, and SHALL default
missing `UPDATE=` / `CHECKSIZE=` / `CHECKSUM=` to `None` — provided at least one
valid pair exists.

**Validates: Requirements 1.2, 10.2**

### Property 3: Parser is read-only on the source

*For any* input file (well-formed or garbage, including inputs that trigger a
parse failure), the SHA-256 of the source file SHALL be identical before and
after `parse()`, and the source SHALL still exist.

**Validates: Requirements 1.6, 10.1**

### Property 4: Empty-of-valid-pairs input fails cleanly

*For any* input containing zero valid `KEY=VALUE` lines, `parse()` SHALL raise
`SettingsParseError` and SHALL leave the source unmodified.

**Validates: Requirements 1.6**

### Property 5: UPDATE / checksum extraction

*For any* integer `n` and any strings `s1`, `s2`, a backup containing
`UPDATE=n`, `CHECKSIZE=s1`, `CHECKSUM=s2` SHALL parse to `update_value == n`,
`checksize == s1`, `checksum == s2`.

**Validates: Requirements 1.4, 1.5**

### Property 6: Selector picks the higher UPDATE_Value

*For any* pair of candidate backups with distinct `update_value`s and no
integrity signal (all `UNVERIFIABLE`), the selector SHALL choose the candidate
with the higher `update_value`; *for any* single candidate, the selector SHALL
choose it.

**Validates: Requirements 2.1, 2.2**

### Property 7: Selector honors integrity verification when available

*For any* candidate set where at least one is `VERIFIED` and at least one is
`FAILED`, the selector SHALL choose a `VERIFIED` candidate (even if it has a
lower `update_value`); *for any* set where every candidate is `FAILED`, the
selector SHALL select nothing (`current is None`) and report a verification
failure; a set with only `UNVERIFIABLE` candidates SHALL NOT be treated as a
verification failure and SHALL proceed on `UPDATE=`.

**Validates: Requirements 2.3, 2.4, 2.5**

### Property 8: Mapper ignores unrecognized SIDs

*For any* parsed backup, every `threshold_key` produced by the mapper SHALL
belong to the known mapping table; no unrecognized SID SHALL ever appear as an
output key.

**Validates: Requirements 1.3, 4.10**

### Property 9: Disabled values are skipped

*For any* mapped engine, airspeed, or G-meter SID whose value equals its
Disabled_Value, the mapper SHALL omit the corresponding threshold key from its
output (leaving the dashboard threshold unchanged on apply).

**Validates: Requirements 4.10, 5.7, 6.4**

### Property 10: Temperature unit conversion

*For any* temperature SID value `v`: when SID 345 == 1 (Celsius) the mapped
threshold SHALL equal `round(v * 9/5 + 32)`; when SID 345 == 0 or absent
(Fahrenheit) it SHALL equal `round(v)`. For the CHT cooling *rate* (SID 150,
°/min), the Celsius conversion SHALL use the delta form `round(v * 9/5)` (no
+32 offset).

**Validates: Requirements 7.3, 7.4, 7.5, 12.1**

### Property 11: One-decimal rounding for G and voltage

*For any* G-load or voltage SID value, the mapped threshold SHALL be rounded to
exactly one decimal place (`round(v, 1) == mapped`).

**Validates: Requirements 7.1, 7.2**

### Property 12: Negative-G sign preserved

*For any* negative-G SID value (SIDs 332, 330), the mapped `g_neg_caution` /
`g_neg_limit` SHALL be stored as a value `<= 0`.

**Validates: Requirements 6.3**

### Property 13: Vne TAS/IAS mutual exclusivity

*For any* Vne value and SID 889 in {0, 1}: when 889 == 1 the mapper SHALL set
`vne_tas_redline` to the imported (rounded) Vne and set `vne_ias_redline` to the
disabled sentinel `9999`; when 889 == 0 it SHALL do the reverse. Exactly one of
the two SHALL be active (below the sentinel) after mapping.

**Validates: Requirements 5.1, 5.3, 5.4**

### Property 14: Active airspeed limit alerts iff its airspeed reaches it

*For any* airspeed time series, when `vne_tas_redline` is the active limit the
dashboard SHALL raise the Vne warning iff `max(true_airspeed) >= vne_tas_redline`;
when `vne_ias_redline` is the active limit it SHALL raise the Vne warning iff
`max(indicated_airspeed) >= vne_ias_redline`.

**Validates: Requirements 5.5, 5.6**

### Property 15: Cylinder-count precedence

*For any* combination of SIDs 1055, 1054, 58 present/absent with distinct
values, `num_cylinders` SHALL be taken from 1055 if present, else 1054 if
present, else 58 if present, else left unchanged (mapper yields `None`).

**Validates: Requirements 13.1, 13.2, 13.4**

### Property 16: Preview completeness

*For any* mapped backup, the preview SHALL contain exactly one
`ThresholdChange` per mapped key (including `num_cylinders` when set), each
carrying both a `current` and a `proposed` value.

**Validates: Requirements 9.2, 13.3**

### Property 17: Caution-below-redline guard

*For any* set of proposed CHT/EGT/Oil-Temp tier selections, the preview
`can_apply` SHALL be `True` iff every resulting caution value is strictly less
than its corresponding redline value; when any caution `>=` its redline, the
preview SHALL include a warning and `can_apply` SHALL be `False`.

**Validates: Requirements 8.2**

### Property 18: Apply is gated on double-confirmation and no-op otherwise

*For any* `(confirm1, confirm2)` in {True, False}² and any preview, `apply` SHALL
write the config iff `confirm1 AND confirm2 AND preview.can_apply`; in every
other case it SHALL leave the config byte-identical to the input and report a
reason.

**Validates: Requirements 9.4, 9.6, 10.3**

### Property 19: Apply updates only mapped keys (frame property)

*For any* current config and any confirmed preview, after a successful `apply`
every mapped key SHALL equal its proposed value and every non-mapped config key
SHALL be unchanged from the input config.

**Validates: Requirements 9.5**

### Property 21: RPM redline alert

*For any* `rpm1` time series, when `rpm_redline` is set the dashboard SHALL
produce a warning episode AND a flight-summary alert iff
`max(rpm1) >= rpm_redline`; when `rpm_redline` is unset it SHALL produce no RPM
alert for any series.

**Validates: Requirements 11.1, 11.2, 11.3**

### Property 22: CHT shock-cooling rate alert

*For any* hottest-cylinder CHT time series, when `cht_cooling_rate_max` is set
the dashboard SHALL produce a cooling-rate episode AND a flight-summary alert
iff the maximum windowed cooling rate (°/min) exceeds `cht_cooling_rate_max`;
when unset it SHALL produce no cooling-rate alert for any series (including steep
descents).

**Validates: Requirements 12.2, 12.3, 12.4**

### Property 23: Source-keyed detection — offered iff first-for-source or strictly newer

*For any* map of per-Source_Key Import_Markers and any candidate backup with a
**determined** Source_Key, `detect_new_backup` SHALL report `available == True`
iff there is no marker for that Source_Key (first import for the source), OR the
candidate's `update_value` is strictly greater than that Source_Key's marker
`last_update_value`.

**Validates: Requirements 14.1, 14.3, 14.4**

### Property 24: Regression-proof — not offered when not newer for its source (stale n-1)

*For any* candidate backup with a **determined** Source_Key for which a marker
exists, when the candidate's `update_value` is less than or equal to that
Source_Key's marker `last_update_value`, `detect_new_backup` SHALL report
`available == False` and SHALL suppress the prompt — regardless of any other
Source_Key's marker. In particular, re-seeing an older `n-1` spare or the other
display's backup SHALL never be offered as an import.

**Validates: Requirements 14.5**

### Property 25: Different source is flagged and never silently overwrites

*For any* candidate whose determined Source_Key differs from every Source_Key
with a prior Import_Marker (or differs from the most-recent prior import), when
an import is offered the resulting `ImportPreview` SHALL set
`is_different_source == True` with a source note, and `apply` SHALL still
require both confirmations and the caution<redline guard before writing any
threshold (no silent overwrite).

**Validates: Requirements 14.6, 14.7, 14.9**

### Property 26: Undetermined source falls back to content-hash + update and never regresses

*For any* candidate whose Source_Key is undetermined (identity SIDs missing or
unreadable) and any most-recent prior Import_Marker, `detect_new_backup` SHALL
report `available == True` iff there is no prior marker, OR the candidate's
Content_Hash differs from the marker's `content_hash` AND the candidate's
`update_value` is greater than the marker's `last_update_value`; in every other
case it SHALL report `available == False` (never regressing on ambiguous
identity).

**Validates: Requirements 14.8**

> Interaction note (R15 + R14.8): Because the Primary gate (Requirement 15.1) is
> evaluated first and requires SID 387 == 1, a *genuinely* undetermined
> Source_Key (returned only when neither Mode_S nor Link_ID is readable) cannot
> reach the R14.8 fallback through the full `detect_new_backup` pipeline — a
> candidate with no readable Link_ID is already suppressed as non-primary. The
> undetermined-source rule is still implemented in the pure decision helper (it
> is correct and regression-proof on its own terms) and is exercised by forcing
> the branch in tests. This is intended: the Primary gate is strictly more
> conservative, and the R14.8 math remains the safety net for any future path
> (e.g. an explicitly user-designated source per Requirement 15.7) that could
> present an eligible-but-unidentified candidate.

### Property 27: Successful apply refreshes only the candidate source's marker

*For any* current `settings_import` map and any successfully applied import with
candidate Source_Key `k`, after `apply` the marker at key `k` SHALL record the
candidate's `update_value`, Content_Hash, backup name, and an import timestamp,
AND every other Source_Key's marker SHALL be unchanged.

**Validates: Requirements 14.2**

### Property 28: Primary gate — import offered only for the Primary_Display

*For any* candidate backup, `detect_new_backup` SHALL report `available == True`
only if the candidate's SID 387 (Link_ID / DULINK_ID) equals `1`; *for any*
candidate whose SID 387 is in `{0, 2-254}` (or absent), it SHALL report
`available == False` with a "not the primary display" reason, regardless of the
candidate's `UPDATE=` value or any Import_Marker state.

**Validates: Requirements 15.1, 15.2**

### Property 29: Primary gate is device-agnostic

*For any* candidate backup whose SID 387 equals `1`, import eligibility SHALL NOT
depend on any device-model or device-class SID: adding, removing, or changing
any non-identity, model-specific SID (e.g. an HXr-only SID) SHALL NOT change the
`available` result, holding the Link_ID, Source_Key, and marker state fixed.

**Validates: Requirements 15.1**

### Property 30: Bound-source binding and exclusivity

*For any* sequence beginning with a first successful import from a Primary
candidate with Source_Key `k`, after `apply` the persisted
`bound_import_source` SHALL equal `k`; and thereafter *for any* Primary
candidate whose Source_Key differs from `k`, `detect_new_backup` SHALL report
`available == False` (bound-source mismatch, pending explicit rebind) AND `apply`
SHALL be a no-op that leaves the config byte-identical unless the user has
explicitly rebound `bound_import_source` and provided both confirmations.

**Validates: Requirements 15.3, 15.4, 15.5**

### Property 31: No primary present ⇒ no offer and no non-primary source

*For any* candidate set in which every candidate has SID 387 in `{0, 2-254}`
(no Primary_Display anywhere), `detect_new_backup` SHALL offer no import
(`available == False` for every candidate) and SHALL NOT select any non-primary
candidate as the import source (`bound_import_source` SHALL remain unset).

**Validates: Requirements 15.6, 15.7**

### Property 32: Primary gate composes with Requirement 14 detection

*For any* candidate set, the newer-for-source and stale `n-1` rules
(Requirements 14.4, 14.5) SHALL be evaluated only among candidates that are
import-eligible under the Primary + bound-source gate; in particular a
non-primary candidate (SID 387 != 1) whose `UPDATE=` exceeds its Source_Key's
marker SHALL NOT be offered, and a Primary candidate that is newer for its
source SHALL still only be offered when it also passes the bound-source gate.

**Validates: Requirements 15.8**

### Property 33: Preview initializes fresh with every row selected

*For any* parsed backup and config, `build_preview` with `selected_keys=None`
SHALL mark every `ThresholdChange` as `selected == True` (including the
`num_cylinders` row and the single Vne unit row), and building the preview again
SHALL again yield an all-selected state — no per-threshold selection is carried
over between builds.

**Validates: Requirements 16.1, 16.2, 16.8**

### Property 34: Apply writes exactly the selected subset (frame property)

*For any* preview and any subset `S` of its rows' selection keys, after a
double-confirmed `apply(selected_keys=S)` every threshold whose `selection_key`
is in `S` SHALL equal its proposed value (the Vne unit key `"vne"` expanding to
both `vne_tas_redline` and `vne_ias_redline`), and every threshold not selected
SHALL be byte-identical to its current value in the input config.

**Validates: Requirements 16.5**

### Property 35: Vne unit is atomic

*For any* preview containing the Vne unit, selecting `"vne"` SHALL apply BOTH
`vne_tas_redline` and `vne_ias_redline` preserving SID-889 exclusivity (exactly
one below the `9999` sentinel and the other at `9999`), and there SHALL exist no
`selected_keys` set that causes `apply` to write exactly one of the two `vne_*`
keys.

**Validates: Requirements 16.3, 16.4**

### Property 36: Guard evaluates over the selected subset

*For any* preview and any subset `S` of selection keys, `build_preview(selected_keys=S)`
SHALL set `can_apply == True` iff no *selected* caution proposal is `>=` its
paired redline value (comparing against the paired tier's selected proposal when
that tier is also selected, else its current value); when a selected caution
`>=` its paired redline the preview SHALL include a warning and set
`can_apply == False`.

**Validates: Requirements 16.6**

### Property 37: Deselect-all is a no-op

*For any* double-confirmed preview, `apply` with an empty `selected_keys` set
SHALL leave the config byte-identical to the input and report a reason
indicating there is nothing selected to import.

**Validates: Requirements 16.7**

### Property 38: Cross-date selection uses the latest Archive_Date

*For any* set of archived backups for a single Source_Key spanning multiple
Archive_Dates with arbitrary `UPDATE_Value`s (including all-equal or an older
date carrying a higher/equal `UPDATE=`), `select_current_backup` SHALL select a
backup from the latest Archive_Date and SHALL never select one from an earlier
date, regardless of `UPDATE_Value`.

**Validates: Requirements 17.1, 17.2**

### Property 39: Content-hash decides whether an import is offered

*For any* candidate that is the latest-dated backup for its Source_Key and any
prior Import_Marker for that source, `detect_new_backup` SHALL offer an import
iff the candidate's Content_Hash differs from the marker's `content_hash`, and
SHALL treat an identical Content_Hash as "nothing new to import" (no offer),
independent of Archive_Date and `UPDATE_Value`.

**Validates: Requirements 17.5**

## Error Handling

- **Unreadable / empty backup (Req 1.6, 6):** `Settings_Parser.parse` raises
  `SettingsParseError` on OSError or zero valid pairs. The Import_Workflow
  catches it, reports the reason to the UI, and leaves all thresholds unchanged
  (Req 10.3).
- **Malformed lines / unknown SIDs / missing UPDATE or checksum (Req 10.2):**
  handled silently by the parser — skipped lines, retained unknown SIDs,
  `None` for missing UPDATE/checksum. Never raises when ≥1 valid pair exists.
- **Read-only guarantee (Req 10.1):** the parser opens files in read mode only;
  it never writes, truncates, renames, or deletes. The Archiver copies (never
  moves) settings files, preserving the USB source (Req 3.2). Property 3 asserts
  the source hash is unchanged across parse, including failure paths.
- **Checksum verification failure (Req 2.5):** if a *known* verifier is enabled
  and all candidates fail, the selector returns `current=None` with a failure
  reason and the workflow aborts without changing thresholds. When the algorithm
  is unknown (`UNVERIFIABLE`), the system proceeds on `UPDATE=` and logs a note
  that the checksum could not be verified — it never silently claims integrity.
- **Tier conflict (Req 8.2):** `caution >= redline` sets `can_apply=False` and
  surfaces a warning; the apply endpoint refuses until resolved.
- **Decline / partial confirmation (Req 9.6, 10.3):** any missing confirmation,
  failed guard, or explicit decline yields a no-op with a reported reason
  (Property 18).
- **Atomic apply (Req 9.5, 14.2):** the next config is assembled in memory and
  written with a single `save_config`; a failure before the write leaves config
  untouched. On success, only the candidate Source_Key's Import_Marker is
  refreshed; other sources' markers are preserved.
- **Stale / older drive (Req 14.5):** when a candidate's `update_value` is `<=`
  the marker for its own Source_Key (an older `n-1` spare, or the other
  display's older backup), `detect_new_backup` returns `available=False` with a
  reason and the status route reports the badge as hidden. This is a normal,
  expected outcome — not an error — and the thresholds are left untouched. The
  UI shows no prompt so the pilot cannot accidentally regress.
- **Undetermined Source_Key (Req 14.8):** when identity SIDs 1063/387 are
  missing or unreadable, the Source_Key is `None`. Detection does not fail;
  instead it falls back to comparing the candidate's Content_Hash and
  `update_value` against the most-recent prior Import_Marker and offers an
  import only when content differs AND `update_value` is greater. If either
  condition fails (or the content is identical), the system does **not** offer —
  it never regresses on ambiguous identity. The `reason`/`source_note` conveys
  that the source could not be identified so the pilot can double-check before
  confirming. In the current pipeline this path is only reachable when the
  Primary gate has already passed (SID 387 == 1) but the Source_Key is otherwise
  unresolved; a candidate with no readable Link_ID is suppressed earlier as
  non-primary (Req 15.1).
- **Different source (Req 14.6, 14.7):** an offer whose Source_Key differs from
  the most-recent prior import is surfaced with an explicit note that the backup
  is from a different display or aircraft; it still passes through the full
  preview + double-confirm flow and never silently overwrites.
- **Nothing selected to import (Req 16.7):** an apply with an empty
  `selected_keys` set is a **normal, expected no-op — not an error**: no
  threshold is written and the reason "nothing selected to import" is reported.
  A partially-selected apply whose selected subset still trips the
  caution<redline guard is refused with the guard warning until the user unticks
  the offending row or resolves the tiers (Req 16.6), consistent with the
  Req 8.2 block-on-conflict behavior.
- **Unchanged content across dates (Req 17.5):** when the latest-dated backup's
  Content_Hash equals the source's last-imported marker, `detect_new_backup`
  returns `available=False` ("nothing new to import") and the badge stays
  hidden. This is a **normal outcome — not an error**: a newer Archive_Date with
  identical settings must not re-prompt or regress. `UPDATE=` magnitude is not
  consulted for the cross-date offer decision (real HXr `UPDATE=` is not
  globally monotonic); it is used only to pick the same-date `.bak`/`.dat` slot.
- **Non-primary candidate suppressed (Req 15.1, 15.2):** when the selected
  candidate's SID 387 is not `1` (i.e. in `{0, 2-254}` or absent),
  `detect_new_backup` returns `available=False` with the reason "not the primary
  display" and the status route reports the badge as hidden. This is a **normal,
  expected outcome — not an error**: the Archiver still archives the backup, and
  no threshold is touched. No primary anywhere (every candidate SID 387 in
  `{0, 2-254}`) likewise yields no offer, and the app never selects a
  non-primary as the source (Req 15.6); explicit designation of a source when
  none is primary is an optional/deferred action requiring an explicit user
  step (Req 15.7).
- **Bound-source mismatch (Req 15.4, 15.5):** when a Primary candidate's
  Source_Key differs from the persisted `bound_import_source`,
  `detect_new_backup` returns `available=False` with a reason naming the
  mismatch, and `apply` is a no-op for that candidate. The import is offered only
  after the user **explicitly rebinds** the Bound_Import_Source; even then it
  still passes through the full preview + double-confirm flow (Req 9) and is
  never applied silently. Binding on the first successful import never overwrites
  an already-bound source implicitly.
- **Legacy marker migration:** a pre-R14 single-object `settings_import` is
  handled by the undetermined-source path (no per-key entry yet), so it can
  neither crash detection nor cause a regression; the first source-keyed import
  writes a properly keyed marker.
- **Alerting robustness:** the new RPM / IAS-Vne / cooling-rate paths reuse the
  existing `_detect_episodes_series` engine and its `None`-tolerant column
  handling; missing columns or all-`None` series produce no episodes rather than
  errors.

## Testing Strategy

### Dual approach

- **Property-based tests** verify the universal properties above (parser
  round-trips and tolerance, selector ordering/verification, mapper
  conversions/skips/mutual-exclusivity, workflow gating/frame, source-keyed
  regression-proof detection (Properties 23–27), the Primary-display import gate
  (Properties 28–32), per-threshold selection and the Vne unit (Properties
  33–37), cross-date Archive_Date ordering and content-hash offer detection
  (Properties 38–39), and the alerting fire-iff-condition properties).
- **Unit / example tests** verify concrete cases: each SID→threshold-key table
  entry (Req 4.1–4.9, 5.1, 6.1, 6.2), the presence of both Vne keys in
  `DEFAULT_THRESHOLDS` (Req 5.2), the tier-prompt set for CHT/EGT/Oil (Req 8.1),
  no-prompt-for-G (Req 8.3), overwrite-warning messaging (Req 9.3), and boundary
  values (exactly-at-threshold, exactly-at-Disabled_Value).
- **Archiver integration tests** (Req 3): set up a temp mount directory with
  `Settings.dat` / `State.dat` / etc., run `archive_efis_drive` against it, and
  assert the dated `.dat` copies appear, the source is unchanged (hash before ==
  after), and a pre-existing same-size dated copy is skipped. These are I/O
  wiring, not properties.

### Property test configuration

- Library: **Hypothesis** (Python). The repo targets Python 3 with pytest
  (`.pytest_cache` present). Do not hand-roll property testing.
- Each property test runs a **minimum of 100 iterations** (Hypothesis
  `max_examples>=100`).
- Each property test carries a comment tag referencing its design property:
  `# Feature: efis-settings-import, Property N: <property text>`.
- Each correctness property (Properties 1–39, including the source-keyed
  detection Properties 23–27, the Primary-gate Properties 28–32, the
  per-threshold selection Properties 33–37, and the cross-date ordering /
  content-hash Properties 38–39) is implemented by a **single** property-based
  test. (Property numbering has a historical gap at 20 — preserved, not
  renumbered.)

### Generators (Hypothesis strategies)

- **Backup text**: strategies producing dicts of SID→value, rendered to
  `KEY=VALUE` text, plus injected garbage lines (empty, no-`=`, whitespace-only,
  non-ASCII bytes) and optional `UPDATE=` / `CHECKSIZE=` / `CHECKSUM=` lines.
- **SID values**: bounded numeric strategies per kind (temps, speeds, G in
  ±ranges, voltages, RPM, cooling rate, cylinder counts 4/6), including each
  SID's Disabled_Value for Property 9.
- **Verification status**: a stub `verify_checksum` injected into the selector
  returning `VERIFIED` / `FAILED` / `UNVERIFIABLE` so Property 7 is testable
  without GRT's (unknown) algorithm.
- **Time series**: `(timestamp, value)` sequences at 1 Hz for `rpm1`,
  `true_airspeed`, `indicated_airspeed`, and hottest-CHT, with controlled
  slopes for the cooling-rate property (steep descents that do/do not exceed the
  windowed rate, plus noisy-but-flat series to confirm noise immunity).
- **Config**: current-config dicts with arbitrary unrelated keys to exercise the
  frame property (Property 19).
- **Source identity + markers**: strategies producing Mode_S_Address (SID 1063),
  Link_ID (SID 387), and Flight_ID (SID 1062) — present, partially present, or
  absent — to build candidates with determined and undetermined Source_Keys, and
  `settings_import` maps of per-Source_Key Import_Markers with varied
  `last_update_value` / `content_hash`. These drive the source-keyed detection
  properties (23–27): first-for-source, strictly-newer, stale `n-1`
  (`update <= marker`), different-source flagging, and the undetermined-source
  content-hash+update fallback.
- **Primary gate + bound source**: strategies producing SID 387 (Link_ID) across
  the full range `{0=Auto, 1=Primary, 2..254=other}` — so both the primary
  (`== 1`) and non-primary (`in {0, 2-254}`) branches are exercised — combined
  with a `settings_import` map that may or may not carry a
  `bound_import_source`, and with candidate Source_Keys that match or differ
  from the bound source. An **optional device-class SID** (a model-specific SID
  that is present or absent independent of Link_ID) is generated to prove the
  device-agnostic property: toggling it must not change eligibility. These
  strategies drive the Primary-gate properties (28–32): primary-only offer,
  device-agnostic eligibility, bound-source binding + exclusivity, no-primary ⇒
  no offer, and composition with the Requirement 14 rules.
- **Per-change selection subsets**: strategies producing arbitrary subsets `S`
  of a preview's row selection keys — including the empty set (deselect-all),
  the full set (default), and subsets that do or do not include the `"vne"` unit
  token — to drive the selection properties (33–37): all-selected initialization,
  the selected-subset frame property, Vne-unit atomicity (no subset yields
  exactly one `vne_*` key), the guard-over-subset property, and the deselect-all
  no-op. Tier selections are co-generated so the guard sees selected and
  unselected caution/redline combinations.
- **Multi-date archived captures**: strategies producing several
  `Settings-YYYY-MM-DD.{bak,dat}` captures for one Source_Key across distinct
  Archive_Dates, with `UPDATE_Value`s that are **equal**, ascending, and
  descending relative to date order (so an older date may carry a higher/equal
  `UPDATE=`), plus same-date `.bak`/`.dat` pairs with differing `UPDATE=`. These
  exercise Property 38 (latest Archive_Date wins regardless of `UPDATE=`;
  same-date pair resolved by `UPDATE=`) and, combined with Content_Hash-varying
  vs. Content_Hash-identical captures against a prior Import_Marker, Property 39
  (offer iff content differs, independent of date/`UPDATE=`).

### Alerting tests

The RPM, IAS-Vne, and cooling-rate properties (14, 21, 22) are exercised by
generating synthetic FDL rows into a temp SQLite DB (matching the real
`fdl_data` schema) and running `detect_episodes` / `get_flight_stats` — the same
functions the dashboard calls — then asserting the alert fires exactly when the
mathematical condition holds. This keeps the tests aligned with production code
paths rather than mocking the analysis engine.
