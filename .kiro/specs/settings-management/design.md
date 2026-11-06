# Design Document

## Overview

Settings Management adds three capabilities on top of the shipped multi-device
identity foundation and the existing content-aware archiver:

- **A — Per-device archiving (R1, R2, R11):** when a returning drive resolves to
  a configured device, archive the Settings/WP/Plan families into a per-device,
  per-family location keyed by Link_ID, content-aware (dedup by Content_Hash)
  and non-destructive. A one-time migration adopts the existing flat archives
  into the new per-device layout.
- **B — Explorer / compare (R3, R4):** the dashboard reads archived snapshots
  read-only, renders SID→label values via the existing `Settings_Mapper`, and
  diffs two versions by parsed content (excluding volatile keys).
- **C — Restoration / population / divergence (R5, R6, R7, R8):** a DM-side
  writer places a chosen archived version (restore) or a device's latest
  archived version (populate) onto a bound drive as the SOLE Settings slot,
  reusing the archived file's original bytes verbatim (checksum wall). On a
  returning drive the DM raises a divergence notification when the drive's
  current Settings content does not match the device's latest archived version.

The design reuses, not reinvents: `device_resolution.resolve_drive` (which device
a drive is), `efis_settings` (`Settings_Parser`, `Settings_Selector`,
`extract_source_key`, `content_hash`, `Settings_Mapper`, `parse_archive_date`),
and the `archiver.py` family-selection / snapshot-naming helpers. The component
split holds: the DM (`app.py`, `archiver.py`, a new `settings_writer.py`) owns
archive, write, and the divergence notification; the dashboard
(`dashboard/app.py`, `settings.html`) owns explore/compare and the version
selection that hands a restore request back to the DM.

Scope notes carried from requirements: restoration and population are **Settings
family only** (R5.9, R6.6); archiving covers all three families. Divergence is a
pure Content_Hash mismatch against the latest archived version — never an
"older/newer" verdict — because EFIS-written USB timestamps and UPDATE_Value
cannot order captures the DM did not observe (documented known concern).

## Architecture

```
                 returning drive inserted
                          │
        app.py _on_efis_drive_mounted → _resolve_and_gate_drive
                          │ RESOLVED(outcome: device, link_id)
                          ▼
                 _proceed_resolved ──► _run_archive(mount, link_id)
                          │                     │
                          │                     ▼
                          │        archiver.archive_efis_drive(mount, link_id=...)
                          │          └─ per-device settings archive (A)
                          │                     │
                          ▼                     ▼
             divergence check (C/R7)   per-device archive location
             settings_writer.diverges()   <archive_root>/Settings/link{N}/
                          │
                          ▼
                 _notify("Settings drift", ...)

        dashboard (B + C select)                 DM write (C)
        /settings page, /api/settings/*          settings_writer.py
          history / view / compare               restore_version()  ← R5
          select version → POST restore ───────► populate_latest()  ← R6
                                                 (sole-slot write + byte verify)
```

New module: `src/efis_data_manager/settings_writer.py` — the pure-ish write/diff
core (sole-slot write, byte verify, latest-version lookup, divergence predicate),
callable by both `app.py` (DM actions) and the archiver (archive location).

New module: `src/efis_data_manager/settings_archive.py` — the per-device archive
location + history + migration logic (pure path/scan functions), so both the
archiver and the dashboard API read history the same way.

## Components and Interfaces

### A. Per-device archive location and history (`settings_archive.py`)

The current archiver writes to `archive_root/"Settings"` with names
`<Family>-YYYY-MM-DD[-N].<ext>`. We introduce a per-device subdirectory keyed by
Link_ID and route all settings archiving through it.

```
<archive_root>/Settings/link1/Settings-2026-10-29.dat
<archive_root>/Settings/link1/WP-2026-10-29.bak
<archive_root>/Settings/link2/Settings-2026-10-30.dat
<archive_root>/Settings/unresolved/Settings-2026-09-12.dat   # Link_ID underivable
```

Pure helpers (no I/O side effects beyond reads):

```
def device_settings_dir(archive_root: Path, link_id: str | None) -> Path
    # archive_root/Settings/link{N}; link_id None/"" → archive_root/Settings/unresolved

def family_history(archive_root, link_id, family) -> list[ArchivedSnapshot]
    # scan device_settings_dir for <family>-*.<ext>, parse Archive_Date +
    # sequence (reuse archiver._archive_sequence / parse_archive_date), return
    # newest→oldest. (R2.1)

def latest_version(archive_root, link_id, family) -> ArchivedSnapshot | None
    # family_history(...)[0] or None (R2, R6, R7)
```

`ArchivedSnapshot` dataclass: `path: Path`, `family: str`, `link_id: str|None`,
`archive_date: date`, `sequence: int`. Recency is Archive_Date then sequence —
never mtime/UPDATE (R2.1, R1.7).

### A. Archiver change (`archiver.py`)

`archive_efis_drive` gains an optional `link_id` parameter (default `None` for
backward compatibility). The settings-family loop routes to the per-device
directory when a Link_ID is supplied:

```
def archive_efis_drive(mount_point, progress_callback=None, today=None,
                       link_id: str | None = None) -> dict:
    ...
    settings_dest = device_settings_dir(archive_root, link_id)   # was archive_root/"Settings"
    for family in _ARCHIVED_SETTINGS_FAMILIES:
        _archive_settings_family(mount_point, settings_dest, family, today)
```

`_archive_settings_family` is unchanged internally: it already selects the
current slot, compares Content_Hash against `_most_recent_family_archive`
(now scoped to the per-device dir), and writes a non-destructive dated snapshot
on change. Because `settings_dest` is now per-device, the "most recent archive"
it dedups against is that device's history (R1.4, R1.5, R1.6).

Non-settings archiving (FDL, logbook, DEMO, snapshots, cleanup) is unchanged and
does not become per-device in this spec.

`app.py` passes the resolved Link_ID into the archive call. `_run_archive`
signature grows `link_id`; `_proceed_resolved` passes `outcome.link_id`. The
pre-provisioning archive (`_archive_before_prepare`) and any path without a
resolved device pass `link_id=None` (writes to `unresolved/`, preserving today's
behavior shape without losing data).

### A. Migration (`settings_archive.migrate_flat_archives`, R11)

A one-time, idempotent adoption of pre-existing flat snapshots
(`archive_root/Settings/<Family>-*.<ext>`, i.e. files directly under `Settings/`
not in a `link*/`/`unresolved/` subdir) into the per-device layout:

```
def migrate_flat_archives(archive_root: Path) -> MigrationResult:
    for each flat <Family>-DATE[-N].<ext> directly under Settings/:
        parsed = Settings_Parser.parse(file)           # read-only
        link_id = extract_source_key(parsed).link_id    # derive owner (R11.2)
        dest_dir = device_settings_dir(archive_root, link_id)  # unresolved if None (R11.3)
        dest = _next_available_snapshot_name(dest_dir, family, date_str, ext)  # collision-safe (R11.5)
        move file → dest, preserving bytes + the parsed Archive_Date/sequence (R11.4)
```

- Runs once on DM startup (guarded by a sentinel file/flag so it is idempotent —
  R11.6); re-running after completion is a no-op because no files remain directly
  under `Settings/`.
- Derives the owner from the snapshot's OWN content, not from any configured
  device, so an archive for a Link_ID not currently configured still lands under
  its true `link{N}` (R11.2). Underivable → `unresolved/` (R11.3), never
  discarded, never assigned arbitrarily.
- Uses `shutil.move` (same filesystem under `archive_root`) to preserve bytes; on
  any per-file failure it logs and leaves that file in place (fault tolerance),
  so a partial run is safe to resume.

Decision: **move, not copy.** Adoption relocates the single canonical archive
into its device home. Copying would double every historical snapshot and leave an
ambiguous flat set that future scans would have to keep ignoring. Moving is clean
and the sentinel makes it one-shot.

**Known asymmetry — legacy Plan/WP migrate to `unresolved/` (by design).** The
archiver and the migration attribute ownership by DIFFERENT signals, and for the
Plan/WP families they do not agree:

- The **archiver** (new captures, going forward) resolves ONE `link_id` from the
  returning drive's Settings file and archives *all three* families
  (`Settings`, `WP`, `Plan`) into that drive's `link{N}/` — so Plan/WP are
  attributed by the DRIVE they arrived on, not by their own content.
- The **migration** (one-time, legacy flat files) derives ownership per-file via
  `extract_source_key`, which reads the identity SIDs (`1063` Mode_S / `387`
  Link_ID). Plan and WP files carry the GRT checksum header
  (`CHECKTYPE`/`UPDATE`/`CHECKSIZE`/`CHECKSUM`) but NO identity SIDs — WP is a
  near-empty waypoint-list format and Plan is a tab-delimited plan body — so
  `extract_source_key` returns `None` and they land in `unresolved/` (R11.3).

Net effect: pre-existing flat Plan/WP snapshots migrate to `unresolved/` even
though an identical capture arriving today would be archived under a `link{N}/`.
This is accepted, not fixed: there is no reliable content signal to attribute a
legacy Plan/WP file. Pairing it to a same-dated Settings snapshot is ambiguous
(two devices are routinely archived on the same date, e.g. a `.bak`/`.dat` pair
owned by different displays) and may be impossible (a legacy Plan/WP date can
have no same-dated Settings file at all). It is also harmless: restore,
populate, and divergence are Settings-only (R5.9/R6.6/R7), so `unresolved/`
Plan/WP are archive-only records that are never written back to a card. If
per-device Plan/WP history were ever wanted for legacy files, the only sound
source would be the co-located Settings Link_ID at archive time — a future
design change, out of scope here.

### C. Settings writer (`settings_writer.py`)

The sole-slot, byte-preserving writer shared by restore (R5) and populate (R6),
plus the divergence predicate (R7).

```
def sole_slot_write(archived_path: Path, mount_point: str, family="Settings") -> WriteResult:
    # 1. choose the target slot extension = archived_path's own ext (.bak/.dat)
    # 2. copy archived_path bytes → <mount>/<family><ext>   (shutil.copy2; bytes verbatim, R8)
    # 3. BYTE-VERIFY: sha256(target) == sha256(archived_path); on mismatch, unlink + fail (R5.5/R5.6)
    # 4. remove the OTHER slot (<family>{.dat|.bak}) so the written file is the sole slot (R5.4)
    #    — only after the verify passes, so a failed write never deletes the existing good slot
    # returns WriteResult(status="written"|"failed", written_path, removed_slots, error)

def restore_version(archived_path, mount_point) -> WriteResult        # R5
    return sole_slot_write(archived_path, mount_point, "Settings")

def populate_latest(archive_root, link_id, mount_point) -> WriteResult # R6
    latest = latest_version(archive_root, link_id, "Settings")
    if latest is None: return WriteResult(status="skipped-none")       # R6.4
    if _drive_settings_hash(mount_point) == content_hash(parse(latest.path)):
        return WriteResult(status="skipped-equal")                      # R6.1/R6.2
    return sole_slot_write(latest.path, mount_point, "Settings")

def diverges(archive_root, link_id, mount_point) -> DivergenceResult:  # R7
    latest = latest_version(archive_root, link_id, "Settings")
    if latest is None: return DivergenceResult(diverged=False)          # no baseline → no warning
    drive = _current_settings_slot(mount_point, "Settings")             # reuse archiver helper
    if drive is None: return DivergenceResult(diverged=False)
    diverged = content_hash(drive) != content_hash(parse(latest.path))  # R7.1/R7.4
    return DivergenceResult(diverged, latest.archive_date, link_id)
```

Ordering within `sole_slot_write` is the safety crux (R5.6): write the new slot,
verify its bytes, and only THEN remove the other slot. A failure at any step
leaves at least one valid slot on the drive and never presents a half-written
file as current. The checksum wall (R8) is honored structurally — the function
only ever `copy2`s an archived file's bytes and never parses-then-reserializes,
so UPDATE/CHECKSIZE/CHECKSUM are physically untouched.

Restoration and population target **Settings only**; `sole_slot_write` takes a
family argument but R5.9/R6.6 fix the caller to `"Settings"`.

### C. Divergence confirmation in the DM (R7)

The check MUST run BEFORE the archive. Running it after the archive (the first
implementation) always compares the card against content the archive has just
made the latest, so it can never report a difference.

Flow in `_on_efis_drive_mounted` / `_run_archive` for a resolved device:

1. `settings_writer.diverges(archive_root, link_id, mount_point)` returns a
   `DivergenceResult` extended with `matched_archive_date` — the Archive_Date of
   the most recent archived Settings version (other than the latest) whose
   Content_Hash equals the card's, or `None` when the card matches no archived
   version (content is new).
2. When `diverged`, show a blocking confirmation via the existing
   `_run_alert_on_main_thread` helper (the archive runs on a worker thread; the
   dialog is marshalled to the main thread). Buttons: "Archive settings" (ok) and
   "Skip settings" (cancel). Message names the device label and the latest
   Archive_Date, and adds either "This card matches the version archived
   <matched date>" or "This content has not been archived before".
3. "Skip settings" calls `archive_efis_drive(..., archive_settings=False)`.
   `archive_settings` defaults to `True`; when `False` the settings-family loop
   (Settings, WP, Plan) is skipped and everything else runs unchanged.
4. No prompt when not diverged, when there is no baseline, or when the card has no
   Settings slot (`diverges` already returns `diverged=False` for these).

The dialog blocks the archive thread until answered; chart sync starts after the
archive, so it waits too. Insertion is attended, so no timeout is applied.

### C. Population wiring (R6)

Population is a sync/prepare-time step, not an insert-time one. The natural hook
is the outbound write path that the multi-device foundation left for later; until
that exists, populate is exposed as a DM action (menu item / dashboard-triggered)
that writes the resolved device's latest Settings onto the currently bound drive.
`populate_latest` is idempotent (skips when the drive already carries equal
content, R6.2), so wiring it into a future sync/prepare flow is additive.

### B. Dashboard explorer / compare (`dashboard/app.py`, `settings.html`)

New read-only API endpoints (mirroring the existing `api_settings_import_*`
style), all reading through `settings_archive` + `efis_settings`:

```
GET  /api/settings/devices
       → configured devices (label, link_id) from load_config (R2.2)
GET  /api/settings/history?link_id=&family=Settings
       → [{archive_date, sequence, family, filename}], newest→oldest (R2.1/R2.3)
GET  /api/settings/view?link_id=&family=&name=
       → parse snapshot → [{sid, label, value, unlabeled:bool}] via Settings_Mapper (R3)
GET  /api/settings/compare?link_id=&family=&a=&b=
       → labeled per-SID diff; a/b are archived names, or b="drive" to diff the
         current returning-drive slot (R4.5); excludes UPDATE/CHECKSIZE/CHECKSUM
         (R4.2); equivalent when content_hash equal (R4.4)
POST /api/settings/restore {link_id, family:"Settings", name}
       → hand off to the DM to run settings_writer.restore_version against the
         bound drive (R5.1/R5.2); returns the WriteResult
```

`settings.html` gains a per-device history list, a read-only value view, a
two-version compare table, and a "Restore this version" control that POSTs the
selection. Restoration is dashboard-SELECT, DM-WRITE: the dashboard never writes
the drive; the POST handler invokes the DM write path (same process; the DM owns
mount/write). No edit control is rendered anywhere (R3.4, R8.2).

The SID→label view reuses `Settings_Mapper`; unrecognized SIDs are shown with
their raw value and an "unlabeled" flag (R3.3).

## Data Models

```
ArchivedSnapshot: { path, family, link_id, archive_date: date, sequence: int }
WriteResult:      { status: "written"|"failed"|"skipped-none"|"skipped-equal",
                    written_path: str|None, removed_slots: list[str], error: str|None }
DivergenceResult: { diverged: bool, latest_archive_date: date|None, link_id: str|None,
                    matched_archive_date: date|None }
MigrationResult:  { adopted: int, unresolved: int, collisions: int, errors: list[str] }
```

No config schema change. The archive-root config key (`archive_path`) is reused;
the only on-disk change is the per-device subdirectory layout under `Settings/`
plus a migration sentinel (e.g. `<archive_root>/Settings/.per_device_migrated`).

## Error Handling

- **Parse failures** (returning slot or archived snapshot): caught; the operation
  reports the reason and leaves files unchanged (R9.2, R9.3, R3.5). The archiver
  already wraps each family in try/except and continues (reused).
- **Write/verify failure** (`sole_slot_write`): the new slot is unlinked and the
  OTHER slot is left intact, so the drive always retains a loadable slot; status
  `"failed"` with the error surfaced (R5.6, R9.3). The other slot is removed only
  after a verified write.
- **Migration per-file failure:** logged, file left in place; the run is resumable
  and idempotent (R11.6).
- **No baseline:** divergence and population with no latest archived version are
  no-ops, not errors (R6.4, R7 "no baseline → no warning").
- **Unresolved drive:** no settings archiving, no divergence, no write (R1.10);
  reuses the existing multi-device resolution gating.

## Correctness Properties

Each property is realized as exactly one Hypothesis property test (min 100
examples), tagged `# Feature: settings-management, Property N`.

### Property 1: Per-device archive isolation

*For any* returning drive resolved to a Link_ID, archiving writes new snapshots
only under that Link_ID's per-device directory and never under another Link_ID's.

**Validates: Requirements 1.1**

### Property 2: Content-aware dedup per device

*For any* device and family, re-archiving content whose Content_Hash equals the
device's latest archived version writes no new snapshot, and archiving content
whose Content_Hash differs writes exactly one new snapshot.

**Validates: Requirements 1.4, 1.5**

### Property 3: Non-destructive same-date sequencing

*For any* sequence of N distinct same-date changes for a device and family, the
DM produces N snapshots and never overwrites or discards an existing same-date
snapshot.

**Validates: Requirements 1.6**

### Property 4: Recency ordering ignores mtime and UPDATE

*For any* device/family history, `latest_version` returns the snapshot with the
maximum (Archive_Date, sequence) and never orders by file mtime or UPDATE_Value.

**Validates: Requirements 2.1, 1.7**

### Property 5: Sole-slot write preserves original bytes

*For any* archived settings file, after `sole_slot_write` the written drive slot
is byte-identical to the archived source, so the `UPDATE=`, `CHECKSIZE=`, and
`CHECKSUM=` values are unchanged.

**Validates: Requirements 5.3, 8.1, 8.3**

### Property 6: Sole-slot write leaves exactly one slot

*For any* initial drive slot state, after a successful `sole_slot_write` exactly
the written extension exists for that family on the drive and the other slot is
absent.

**Validates: Requirements 5.4**

### Property 7: Write failure retains a valid slot

*For any* drive with a pre-existing settings slot, if the post-write byte verify
fails, the pre-existing other slot survives unchanged and the result status is
"failed".

**Validates: Requirements 5.6**

### Property 8: Population idempotency

*For any* device and bound drive, `populate_latest` performs no write when the
drive already carries content equal to the device's latest archived version, and
performs exactly one write otherwise.

**Validates: Requirements 6.1, 6.2**

### Property 9: Divergence is a pure hash mismatch

*For any* pair of (drive current Settings content, latest archived version),
`diverges` is True exactly when their Content_Hashes differ, and the decision
never consults file timestamps or UPDATE_Value.

**Validates: Requirements 7.1, 7.4, 7.5**

### Property 11: History match is a pure hash lookup

*For any* device history and returning-card content that diverges from the latest
archived version, `matched_archive_date` is the Archive_Date of the most recent
older archived Settings snapshot whose Content_Hash equals the card's, and `None`
when no snapshot matches, regardless of UPDATE_Value or file timestamps.

**Validates: Requirements 7.3, 7.7**

### Property 10: Migration adoption is faithful and idempotent

*For any* set of flat archive files, each is adopted to the Link_ID derived from
its own parsed content (or the unresolved location when underivable), with bytes,
Archive_Date, and sequence preserved and no collision overwritten, and a second
migration run makes no further change.

**Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.6**

## Testing Strategy

The ten Correctness Properties above are each realized as one Hypothesis
property test (min 100 examples). Example and integration tests cover the
wiring and the matrices.

Example / integration tests:

- Archiver `link_id` routing: a resolved insert archives into `Settings/link{N}/`;
  a `link_id=None` call archives into `Settings/unresolved/` (backward-compatible
  shape).
- `_proceed_resolved` / `_run_archive` pass the resolved Link_ID through, and a
  divergence notification fires on mismatch (mocked `_notify`), not on match.
- Dashboard endpoints: history ordering, read-only view (labeled + unlabeled
  SIDs), compare (archived vs archived, archived vs "drive"), and the restore POST
  handing off to the DM writer (mocked drive).
- Restoration end-to-end against temp dirs: write + byte-verify + sole-slot,
  reusing original bytes; WP/Plan are NOT restored/populated (R5.9/R6.6).
- Fault tolerance: malformed snapshot → reported, unchanged; write verify failure
  → other slot intact.

## Versioning

This spec changes the DM (menu-bar) archive/write path and adds dashboard
endpoints. Per the versioning policy a release bumps `__version__` +
`pyproject.toml` + `MENUBAR_VERSION`; because the dashboard also gains the
explorer/compare UI, `DASHBOARD_VERSION` is bumped as well. Confirm the exact
labels at release time.
