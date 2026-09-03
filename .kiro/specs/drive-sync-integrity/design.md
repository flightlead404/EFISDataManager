# Design Document

## Overview

This design reworks the drive updater from a single monolithic rsync into
per-family sync jobs with commit-marker atomicity, count+size verification,
idempotent/resumable behavior, and explicit interruption handling (eject,
sleep, dock removal). The routine currency check stays cheap because the
updater now guarantees that a family's commit marker only appears once that
family's payload is fully in place.

The work lives primarily in `src/efis_data_manager/drive_updater.py`, with
supporting changes in `src/efis_data_manager/app.py` (orchestration, sleep
notifications, interrupted-sync handling, error reporting) and a small amount
in the menu (`Verify Drive`).

## Product families

Three families, each a self-describing job:

| Family | Payload | Commit marker | Currency test |
|--------|---------|---------------|---------------|
| scanned | `ChartData/` minus `Plates/` and minus `ScannedCharts.sqlite` | `ChartData/ScannedCharts.sqlite` | marker mtime+size |
| plates | `ChartData/Plates/` minus `Plates.sqlite` | `ChartData/Plates/Plates.sqlite` | marker mtime+size |
| nav | `NAV.DB`, `NAV-proc.DB` | the file(s) themselves | per-file checksum |

Rationale: the scanned set (SEC/LO/HI, ~5.5 GB) and the plates set (~3.6 GB)
are sourced and revised independently and each already ships its own SQLite
manifest. Splitting them gives independent commit points and lets one be
current while the other is mid-sync or failed. NAV.DB is a single standalone
file from a different source, so a checksum is both the currency test and the
verification — cheap and exact.

## Architecture

### Job model

Introduce a `SyncJob` abstraction (a dataclass + functions, not necessarily a
heavy class):

```
SyncJob:
    name: str                 # "scanned" | "plates" | "nav"
    payload_specs: list       # source/dest roots + excludes (for tree jobs)
    marker_src: str | None    # local commit marker path (None for nav)
    marker_dst: str | None    # drive commit marker path
    kind: "tree" | "files"    # tree = rsync a directory; files = per-file copy
```

`update_drive(mount_point, families=None, progress_callback=None)` builds the
jobs for the requested families (default: all) and runs each via
`run_sync_job(job, mount_point, ...)`. Each job returns a per-family result:

```
JobResult:
    name: str
    status: "current" | "updated" | "failed" | "aborted"
    files_updated: int
    errors: list[str]
    verified: bool
```

`update_drive` aggregates job results into:

```
{"jobs": {name: JobResult, ...}, "errors": [...], "aborted": bool}
```

### Per-job sequence (tree job)

1. **Preflight**: confirm mount present + writable; enough free space (compare
   local family size to drive free space).
2. **Record interrupted-sync marker** for (drive, family) BEFORE copying.
3. **Payload sync** excluding the commit marker:
   - `rsync -r --delete --size-only --modify-window=2 --exclude <marker>`
     from `local/` to `drive/`.
   - Replace `-c` with size+mtime delta. `--modify-window=2` absorbs
     FAT/exFAT 2-second timestamp granularity.
   - No fixed wall-clock timeout; use a liveness watchdog (see Interruption).
4. **Verify payload** (count + size) local vs drive over the family tree,
   excluding the marker. Any mismatch -> job failed, log, do NOT write marker.
5. **Write commit marker**: copy marker file, then flush (`os.fsync` on the
   dest fd; best-effort `sync` for FAT).
6. **Clear** the interrupted-sync marker for this family.
7. Return JobResult(status = updated/current, verified=True).

### Per-job sequence (nav / files job)

1. Preflight (mount, writable).
2. Record interrupted marker.
3. For each nav file: compare checksum (or size+mtime fast path, then checksum
   confirm) local vs drive; copy if different; fsync.
4. Verify by checksum.
5. Clear interrupted marker.

NAV files have no separate "marker" — the file *is* the marker; a matching
checksum means current.

## Currency check (quick)

`check_drive_currency(mount_point)` becomes per-family and marker-based:

- scanned: exists(drive `ScannedCharts.sqlite`) AND drive mtime >= local mtime
  AND size matches; also verify `LO/`, `SEC/` exist and are non-empty (cheap
  structural sanity).
- plates: same against `Plates/Plates.sqlite`; verify `Plates/` non-empty.
- nav: checksum(local NAV.DB) == checksum(drive NAV.DB) (single small-ish file;
  fast). Same for NAV-proc.DB if present.
- If an interrupted-sync marker exists for the drive, the family it covers is
  reported NOT current regardless of marker state, forcing verify+repair.

Returns per-family staleness so `update_drive` can run only the stale families.

## Exhaustive verification and repair

`verify_drive(mount_point, families=None, deep=False)`:

- For each family, walk local and drive trees (excluding marker), build
  `{relpath: size}` maps, compare:
  - missing on drive, extra on drive, size mismatch.
- Default depth is count + size (`deep=False`). `deep=True` would add content
  hashing (opt-in; not used routinely).
- Returns per-family discrepancy lists.

`repair` = feed discrepancies back into the same rsync job (idempotent), then
re-verify. Because rsync with `--delete` + size compare already converges the
tree, "repair" is essentially "run the job again", which is why idempotency is
the core property.

Triggers:
- User menu **"Verify Drive"** -> `verify_drive(deep=False)` and report.
- Startup / next mount with an interrupted marker -> verify+repair the covered
  families before clearing the marker and declaring current.

## Drive identity (multi-drive rotation)

**Problem.** All prior state (interrupted-sync record, currency gating) keys on
the *mount path* (`/Volumes/EFIS_1`). macOS derives that path from the volume
label and disambiguates duplicate labels with a history/timing-dependent numeric
suffix, so a single physical drive mounts at `/Volumes/EFIS` one session and
`/Volumes/EFIS_1` the next, and identically-labeled drives collide on the same
paths in an unpredictable order. Mount path is therefore not a stable per-drive
identity. In an N-drive rotation this causes one drive's interrupted-sync state
to be mis-attributed to another on swap.

**Solution: an app-owned identity file at the volume root**, keyed on an
app-generated UUID. "UUID plus something else" = the UUID plus a `kind`
discriminator, so a drive the user later repurposes (or that was never ours) is
not mistaken for a managed chart drive.

### Identity file: `EFIS_DRIVE_ID.json` (visible, volume root)

```
{
  "schema_version": 1,
  "id": "<app-generated-uuid4>",         // the sync-state key
  "kind": "efis-chart-drive",            // the "something else"
  "volume_uuid": "<OS VolumeUUID at write>",  // cross-check only
  "label": "<volume label at write>",         // cross-check only
  "created": "<iso>",
  "prepared_by": "EFISDataManager <version> on <host>",
  "last_sync_started": "<iso|null>",
  "last_sync_completed": "<iso|null>",
  "last_sync_result": "clean|aborted|failed|null",
  "last_sync_families": ["scanned", "plates", "nav"],
  "sync_count": 0,
  "data_cycle": "<chart/nav cycle id|null>"
}
```

**Visible, not hidden — deliberate.** A hidden dotfile survives `rm -rf
/Volumes/EFIS/*` (most shells do not glob dotfiles), which would leave a
"synced" identity on a drive whose charts were just wiped. A visible file shares
the data's lifecycle: wipe the charts and the identity goes too, so the app
re-adopts and re-syncs. The HXr tolerates extra files at the volume root.

**Identity never decides currency.** The identity file governs *which drive this
is* and *provenance/telemetry only*. "Is this drive current?" is always answered
by the on-drive commit markers + payload (Req 3/5/6). So even a stale or lying
identity file cannot cause a needed sync to be skipped — the marker/payload
check still forces a correct resync. This is what makes the `rm -rf *` foot-gun
harmless.

### Detection is identity-only (no label matching)

**A drive is "ours" if and only if it carries our identity file** with
`kind == "efis-chart-drive"`. The volume label plays NO role in detection — the
old `EFIS`/`EFIS_N` regex is removed entirely (it was a personal labeling
convention, not a GRT-ecosystem signal, and mount paths reshuffle anyway).
`is_efis_drive` is renamed `is_managed_drive` and checks only for a valid
identity file.

`resolve_drive_id(mount_point) -> str | None`:
1. `wait_for_mount_ready(mount_point)` first (mount-readiness race).
2. If `EFIS_DRIVE_ID.json` exists and parses with `kind == "efis-chart-drive"`,
   return its `id`. Opportunistically capture `VolumeUUID` and warn on mismatch.
3. Else return `None`. **There is no lazy adoption on mount.** A drive without
   our identity file is not ours; the USB monitor takes no automatic action on
   it. Adoption happens ONLY through the explicit Prepare Drive flow below.

### Mount auto-action is identity-gated

The USB monitor's mount handler auto-archives + auto-syncs a drive ONLY when
`resolve_drive_id` returns an id (identity file present). A never-before-seen
drive — even one with a `GRTCHARTS/` folder or a chart payload — triggers no
automatic action. This is the deliberate "not ours until the identity JSON is
written" model: first contact with any drive is an explicit user action.

### Prepare Drive: provisioning, adoption, and data safety

`GRTCHARTS/` (and/or `ChartData/`) is used ONLY here, as the signal that a drive
is a *previously-used GRT chart drive* that can be adopted rather than
reformatted. Prepare Drive inspects the selected volume and branches:

- **No identity file, has `GRTCHARTS/`/`ChartData/`** (Windows-tool drive, or an
  older app version): offer
  - **Start clean** — reformat (destructive) + write identity + full populate.
  - **Adopt & update** — NON-destructive: write the identity file onto the
    existing drive, then `update_drive` (incremental; reuses everything that
    already matches, syncs only the delta). This is the migration path into our
    ecosystem for an existing drive without re-copying ~9 GB.
- **No identity file, blank / no GRTCHARTS**: only "Start clean" applies.
- **Has our identity file**: already managed; a normal update (optionally offer
  re-clean).

**Flight-data safety (both paths).** Before provisioning, Prepare Drive checks
whether the drive holds unarchived flight data / logbooks (FDL, DEMO, snapshots,
settings, logbook — what `archive_efis_drive` handles). If so, it prompts:
**Import/Archive** (run the archive first, preserving the data) vs **Erase**
(discard). This gates BOTH "Start clean" (destructive) and "Adopt & update"
(may overwrite settings), so flight data is never silently lost.

Prepare Drive still sets a cosmetic label (`EFIS_<suffix>` by default) purely
for Finder clarity; nothing keys on it.

### Provenance updates

The identity file's telemetry fields are updated atomically (temp + `rename`)
around the commit-marker step: `last_sync_started` at job begin,
`last_sync_completed` / `last_sync_result` / `sync_count` / `data_cycle` after a
clean verified pass. `data_cycle` is derived from the source image's markers
(the chart/nav cycle being synced) and lets a later feature surface "drive is N
cycles behind" without introducing per-drive roles.

### Failure-safe behavior

If `resolve_drive_id` returns `None` (unreadable root, a read error, or simply a
drive with no identity file), the app does NOT consult or apply any
interrupted-sync state (which is keyed by id) and logs a WARNING where relevant.
Currency decisions never depend on the identity file — the on-drive commit
markers + payload remain the sole source of truth. No drive is ever bricked by
an identity-resolution failure.

### No roles; every managed drive refreshed

There are no per-drive roles. Any MANAGED drive (identity file present) that
mounts — including the n-1 emergency drive brought back from the airplane — is
archived and refreshed to current. Labels are cosmetic; a distinct label per
drive is a documented convenience only.

### Migration note

Because detection is now identity-only, users whose drives were previously
auto-detected by label must **adopt each drive once** (Prepare Drive →
"Adopt & update", non-destructive) before auto-sync resumes for it. Documented
in the README and release notes.

## Interruption handling

### Durable interrupted-sync marker

Replace the single `~/EFIS/DataManagerLogs/.sync_in_progress` string file with
a small JSON state file, e.g. `~/EFIS/DataManagerLogs/.sync_state.json`:

```
{"mount": "/Volumes/EFIS_1", "families": ["scanned"], "started": "<iso>"}
```

Written before payload copy; a family entry removed as each job completes;
file removed when empty. On next mount/launch, if this file names the current
drive, the listed families are forced through verify+repair.

### Mount-presence watchdog

While a tree job runs, a watchdog thread polls `os.path.ismount(mount_point)`
(and that the path still exists) every ~2 s. If the mount vanishes, it
terminates the rsync subprocess (`proc.terminate()`/`kill()`), causing the job
to abort. The job then:
- marks status "aborted",
- leaves the interrupted marker in place,
- logs an ERROR ("EFIS drive removed during <family> sync"),
- sets an informative status.

Because rsync writes to a temp name and renames per file, an aborted rsync does
not leave half-written files under real names for the files it hadn't started;
the file in flight may be partial, which the next verify+repair catches and
re-copies. `--delete` is only applied on a clean pass.

### Sleep / wake

Register for `NSWorkspace` notifications on the shared workspace notification
center:
- `NSWorkspaceWillSleepNotification` -> set a "sleeping" flag; the watchdog and
  job treat this like an at-risk condition and stop the current rsync safely,
  leaving the interrupted marker. We do NOT try to keep an rsync alive across
  sleep.
- `NSWorkspaceDidWakeNotification` -> if a drive is still mounted and an
  interrupted marker exists, trigger verify+repair (resume).

This makes sleep a special case of "interrupted then resume", relying on the
idempotent job rather than trying to freeze I/O.

### Idempotency guarantees

- Payload rsync with size+mtime + `--delete` converges to source on each run.
- Commit marker is only written after a clean, verified payload pass, so a
  partial run never advertises currency.
- Re-running a job is safe and cheap when already current (rsync no-ops).

## Error reporting

- Every appended error in a job SHALL also be logged at ERROR (hard failures)
  or WARNING (recoverable/aborted), so Recent Errors mirrors status (Req 8).
- A large-but-successful transfer produces status "updated"/"current", never an
  error. The old 1-hour-timeout-as-error path is removed with the timeout.
- Status transitions: "Updating <family>..." -> per-family completion ->
  aggregate "Idle"/"Drive current" or "<family> failed"/"Drive removed during
  update". No sticky stale error strings: on any successful terminal state the
  status returns to idle/current.

## Data / API changes

- `drive_updater.check_drive_currency` -> returns per-family staleness (keeps a
  backward-compatible top-level `is_current`/`stale_items` for existing callers,
  plus `families` detail).
- `drive_updater.update_drive(mount_point, families=None, progress_callback=)`
  -> returns `{"jobs": {...}, "errors": [...], "aborted": bool}`.
- New `drive_updater.verify_drive(mount_point, families=None, deep=False)`.
- New helper: `sync_state` read/write in a small module or within drive_updater.
- `app.py`: NSWorkspace sleep/wake observers; interrupted-marker-aware mount
  handler; a `Verify Drive` menu action; error logging alignment; watchdog
  wiring; per-family status text.
- `prepare_drive` uses the new `update_drive` and reports per-family results.

## Components and Interfaces

### drive_updater.py

- `check_drive_currency(mount_point) -> dict`
  Per-family quick check. Returns backward-compatible
  `{"is_current": bool, "stale_items": [str], "message": str, "families": {name: {"current": bool, "reason": str}}}`.
- `update_drive(mount_point, families=None, progress_callback=None) -> dict`
  Runs the requested family jobs. Returns
  `{"jobs": {name: JobResult}, "errors": [str], "aborted": bool}`.
- `verify_drive(mount_point, families=None, deep=False) -> dict`
  Exhaustive count+size walk (deep adds hashing). Returns per-family
  discrepancies `{name: {"missing": [...], "extra": [...], "size_mismatch": [...]}}`.
- `run_sync_job(job, mount_point, sync_state, progress_callback, is_aborted) -> JobResult`
  Internal per-job driver implementing the commit-marker sequence.
- `prepare_drive(volume_path, progress_callback=None) -> dict`
  Unchanged signature; internally uses the new `update_drive` and reports
  per-family results.

### sync_state (helper in drive_updater)

Keyed by drive `id` (Req 10). Signatures take a `drive_id` rather than a mount
path:

- `read_sync_state() -> dict | None` (v2 id-keyed map; discards legacy formats)
- `begin_family(drive_id, family, mount=None)` — record a family in-progress.
- `complete_family(drive_id, family)` — remove a family; prune empty entries.
- `pending_families(drive_id) -> list[str]` — families needing verify+repair.

### drive_identity (new helpers in drive_updater)

- `resolve_drive_id(mount_point) -> str | None` — read/adopt `EFIS_DRIVE_ID.json`
  at the volume root; adopt recognized-but-unmarked drives; None if not ours /
  unreadable (callers fail safe).
- `read_identity(mount_point) -> dict | None` / `write_identity(mount_point, data)`
  — atomic (temp + rename) read/write of the identity file.
- `update_identity_provenance(mount_point, **fields)` — merge telemetry fields
  (`last_sync_*`, `sync_count`, `data_cycle`) atomically.
- `IDENTITY_FILENAME = "EFIS_DRIVE_ID.json"`, `IDENTITY_KIND = "efis-chart-drive"`.

Callers (`check_drive_currency`, `run_sync_job`/`update_drive`, and the app's
mount handler) resolve the drive id once per operation and pass it to the
sync_state helpers instead of the mount path.

### app.py (orchestration)

- NSWorkspace observers: `receiveSleepNotification_`,
  `receiveWakeNotification_` registered on
  `NSWorkspace.sharedWorkspace().notificationCenter()`.
- `_run_drive_update` rewritten to: quick check -> run only stale families ->
  per-family status -> aggregate result -> error logging.
- `_on_efis_drive_mounted`: if `pending_families(mount)` is non-empty, run
  verify+repair before declaring current.
- New menu action `Verify Drive` -> `verify_drive` + report.
- A mount-presence watchdog thread with an `is_aborted()` predicate passed into
  `run_sync_job`.

## Data Models

### JobResult

```
@dataclass
class JobResult:
    name: str                 # "scanned" | "plates" | "nav"
    status: str               # "current" | "updated" | "failed" | "aborted"
    files_updated: int
    errors: list[str]
    verified: bool
```

### SyncJob

```
@dataclass
class SyncJob:
    name: str
    kind: str                 # "tree" | "files"
    payload_root_local: str   # for tree jobs
    payload_root_drive: str
    excludes: list[str]       # includes the marker filename for tree jobs
    marker_src: str | None
    marker_dst: str | None
    files: list[str]          # for "files" jobs (nav)
```

### Sync state file (`~/EFIS/DataManagerLogs/.sync_state.json`)

Keyed by drive `id` (Req 10) so an arbitrary number of drives are tracked
independently and a swap never mis-attributes state:

```
{
  "schema_version": 2,
  "drives": {
    "<drive-id-A>": {
      "families": ["scanned", "plates"],
      "started": "2026-09-02T10:36:48-04:00",
      "mount": "/Volumes/EFIS_1"     // last-seen mount, informational only
    },
    "<drive-id-B>": { "families": ["nav"], "started": "...", "mount": "..." }
  }
}
```

The legacy single-`mount` v1 format (and the even older `.sync_in_progress`
string) is discarded on first read of the new code, since it cannot be reliably
mapped to a drive id when the drive may be absent; a genuinely-interrupted drive
is re-detected by its markers/payload on next mount and repaired.

### Currency result (per-family detail)

```
{
  "is_current": false,
  "stale_items": ["plates (drive older)"],
  "message": "1 family needs updating.",
  "families": {
    "scanned": {"current": true,  "reason": "marker matches"},
    "plates":  {"current": false, "reason": "drive marker older"},
    "nav":     {"current": true,  "reason": "checksum matches"}
  }
}
```

## Correctness Properties

Property 1: Marker implies completeness. If a family's drive marker exists and
is newer-or-equal to the local marker (mtime+size), then the family's payload
on the drive matches local by count+size. Holds because the marker is only
written after a verified payload pass.

**Validates: Requirements 2.1, 2.2, 2.3, 5.1, 5.3**

Property 2: No false-current after interruption. If a sync was interrupted for a
family, the quick check reports that family not-current until verify+repair
clears the interrupted state.

**Validates: Requirements 3.5, 6.3, 7.5**

Property 3: Idempotent convergence. Running `update_drive` N times (N >= 1),
possibly with interruptions between runs, yields the same final drive state as
one clean run.

**Validates: Requirements 7.6, 9.1**

Property 4: Family independence. A failure or abort in one family never
invalidates another family's already-written marker.

**Validates: Requirements 1.2, 1.4**

Property 5: Error/status agreement. Every error surfaced in the status line has
a matching log record at level >= WARNING.

**Validates: Requirements 8.1, 8.3**

Property 6: Identity-stable attribution. Interrupted-sync state is keyed by the
drive's app-generated `id`; swapping which physical drive occupies a given mount
path never causes one drive's pending families to be read or cleared against
another drive.

**Validates: Requirements 10.2, 10.5, 10.6, 10.7**

These map to tests 1-7 (Properties 1-5) and the identity tests (Property 6) in
the Testing strategy.

## Error Handling

- **Mount disappears mid-sync**: watchdog aborts rsync; JobResult.status =
  "aborted"; interrupted marker retained; ERROR logged
  ("EFIS drive removed during <family> sync"); status set to
  "Drive removed during update".
- **Sleep during sync**: treated as at-risk; current rsync stopped safely;
  interrupted marker retained; resume via wake observer or next mount.
- **Verification mismatch**: JobResult.status = "failed"; marker NOT written;
  WARNING logged with counts; family remains stale so next run repairs.
- **Insufficient free space (preflight)**: job fails fast with ERROR; no
  partial copy attempted.
- **rsync non-zero exit**: stderr captured (truncated) into errors and logged
  at ERROR.
- **NAV checksum mismatch after copy**: ERROR logged; nav marked failed.
- **No sticky errors**: any successful terminal job resets status to
  idle/current; error strings are not left on the status line after a later
  success.

## Testing strategy

Because tests were removed from the repo, add focused unit tests under a local
`tests/` tree (git-archive export-ignored so they stay out of releases):

1. **Commit-marker atomicity**: simulate payload copy then abort before marker;
   assert drive marker unchanged/absent and quick check reports stale.
2. **Per-family independence**: fail the plates job; assert scanned marker still
   valid and scanned reported current.
3. **Quick currency check**: fabricate local/drive marker mtimes/sizes and
   NAV.DB checksums; assert per-family staleness. Include plates-stale-while-
   scanned-current (the gap this fixes).
4. **Verify (count+size)**: build local vs drive trees with a missing file, an
   extra file, and a size mismatch; assert discrepancies detected; after repair
   (re-run) assert clean.
5. **Idempotency**: run job twice; second run reports no changes.
6. **Interruption**: mock mount disappearance mid-job (ismount -> False);
   assert job aborts, marker not written, interrupted state persists, error
   logged at ERROR.
7. **Error/status consistency**: assert every errors[] entry has a matching log
   record at >= WARNING.

Use temporary directories for "local image" and "drive"; rsync runs against
real temp dirs. Mount/sleep are mocked. No physical USB needed.

## Migration / compatibility

- Existing populated drives: size+mtime delta means no forced re-copy; markers
  already present are reinterpreted per-family. First run may re-copy files
  whose mtimes differ beyond the modify-window; acceptable one-time cost.
- Files outside known families remain untouched (no `--delete` outside family
  roots).
- The old `.sync_in_progress` file, if present, is migrated/treated as an
  interrupted marker for the named mount on first launch.

## Risks and tradeoffs

- **size+mtime vs checksum**: size+mtime can miss a same-size, same-mtime
  content change. For SA chart tiles this is effectively impossible (new cycles
  change names/sizes/mtimes), and the opt-in deep verify covers paranoia. This
  is the deliberate tradeoff that removes the hour-long sync.
- **rsync abort partial file**: a file in flight at abort may be partial; caught
  by next verify+repair. Acceptable given idempotency.
- **Sleep = stop+resume** rather than survive-in-place: simpler and far more
  reliable than trying to keep USB I/O alive across sleep.
