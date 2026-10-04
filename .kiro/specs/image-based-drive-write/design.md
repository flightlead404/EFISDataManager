# Design: Image-Based Drive Write (investigation)

## Overview

INVESTIGATION / design-first. This document establishes the proposed
architecture and the OPEN QUESTIONS that must be validated (several on real EFIS
hardware and on scratch drives) BEFORE committing to an implementation.
Requirements and tasks follow once the open questions resolve.

**Problem.** The current chart write path (`chart-sync-stall-fix`) rsync-delta-
copies ~9.1 GB / ~101,692 small files to a 30 GB FAT32 USB drive through a mount
swap: the FSKit `/Volumes/` mount is unmounted and the drive remounted via
`/sbin/mount_msdos` for the write, then the FSKit mount is restored. Three
problems, all rooted in **mounting the FAT32 volume through macOS**:

1. **FSKit write stall** — writing through the FSKit mount hangs; the swap exists
   solely to avoid it.
2. **AppleDouble sidecar tax** — macOS writes a `._*` companion for most files on
   the FAT32 volume regardless of rsync `--exclude ._*` and `COPYFILE_DISABLE=1`
   (the mount layer creates them, not rsync). Measured ~87,702 sidecars on a
   101,692-file drive = **~+86% files**.
3. **Screen-lock teardown fragility** — after a long write the screen locks,
   `loginwindow` dissents the unmount, the force-unmount can drop the device off
   the bus, and the FSKit remount fails → "drive needs reinsertion". Breaks the
   "insert and walk away" use case.

**Measured cycle churn** (full content-hash of consecutive Seattle Avionics
cycles, 2026-10-01): Plates 79.7%, SEC 74.6%, LO 55.7%; **~68% overall**. Map
tiles change at identical byte-size (same-size-but-changed: SEC 828, LO 394,
Plates 62), so size is not a safe unchanged signal. Delta-sync therefore skips
only ~32% while paying full detection cost over the slow, sidecar-doubled drive
tree — a weak trade. A full/bulk write is justified most cycles.

**Chosen approach.** Compose the ENTIRE drive filesystem as a FAT32 image on the
local SSD, then write it to the drive's raw partition slice in one sequential
`dd`. No macOS mount of the USB drive is ever required, which eliminates all
three root problems. Completion is confirmed by raw readback (no mount, no
screen-lock dependency). Image composition is decoupled from drive presence
(triggered on new nav/charts/software), so the slow per-file work happens once
on local SSD, not per-drive over USB.

## Architecture

Three stages, the first fully decoupled from any drive:

1. **Composer (drive-independent).** Maintains a local FAT32 image
   (`EFIS_USB.img`) sized to the target partition. On new nav DB / charts /
   software, (re)builds the image contents: ChartData (scanned/plates/nav),
   identity file, nav DB, settings, software uploaders. Built via
   `/sbin/newfs_msdos` + a local populate with AppleDouble suppressed. The commit
   marker / sync sentinel is written into the image LAST.
2. **Writer (on drive insert / on demand).** Rigorously validates the target
   device node, then `dd` the composed image onto the raw partition slice
   `/dev/diskNsN`.
3. **Verifier.** Raw readback hash of the device vs. the composed image hash.
   Pass = done; fail = report incomplete (no corruption of our own state).

Strategy comparison that led to this choice:

| Strategy | FSKit stall | Sidecars | Screen-lock teardown | New dep |
|---|---|---|---|---|
| A. rsync + mount_msdos (current) | avoided via swap | STILL created | STILL present | none |
| B. bulk cp/ditto/tar to mount_msdos | avoided via swap | STILL created | STILL present | none |
| C. mtools mcopy to raw device | gone | gone | gone | mtools (brew) |
| **D. local FAT image + dd (CHOSEN)** | **gone** | **gone** | **gone** | **none** |
| E. prune sidecars / skip --delete | n/a | FUTILE | present | none |

D uses only system-native tools (`newfs_msdos`, `dd`, `hdiutil`), moves slow
per-file work to local SSD, and makes the USB write one fast sequential transfer.

## Components and Interfaces

(Proposed — shapes to be confirmed during the investigation.)

- **ImageComposer** — `compose(image_path, sources) -> ComposeResult`. Builds/
  refreshes the local FAT32 image from the current ChartData/nav/settings/
  software sources; writes the completion marker into the image last; returns the
  image path + a content hash of the finished image. No drive interaction.
- **ImageWriter** — `write(image_path, device_node) -> WriteResult`. Validates
  `device_node` (destructive-grade guard, see Data Models / Error Handling), then
  `dd` the image to the raw slice. Privileged (reuses/extends the
  `mount_swap.run_privileged` allow-list).
- **WriteVerifier** — `verify(image_path, device_node) -> bool`. Raw readback of
  the device compared to the image (full or bounded hash — see open question 8).
  Pure read; no mount.
- **Device-node guard** — extends `mount_swap.validate_device_node` (currently:
  `/dev/diskNsN` syntactic check + removable + FAT32 via diskutil) with
  destructive-operation checks: expected size, expected volume label / identity,
  and explicit confirmation.
- Retire or fall back from `msdos_mount` (the mount swap) per open question 10.

## Data Models

- **ComposeResult**: `{ image_path: str, image_sha256: str, byte_size: int,
  families_included: list[str], composed_at: iso8601 }`.
- **WriteResult**: `{ device_node: str, bytes_written: int, verified: bool,
  status: "complete" | "incomplete" | "aborted", error: Optional[str] }`.
- **DeviceGuardInfo** (for the destructive guard): `{ device_node, is_removable,
  is_fat32, slice_size_bytes, volume_label, identity_id }` — all must match
  expectation before a `dd` is permitted.
- No new durable per-family sync-state is required for the chart write: the unit
  of work is the whole image (verified or not), replacing the current
  `.sync_state.json` interrupted-family tracking for this path.

## Correctness Properties

### Property 1: No mount of the target drive
The write + verify path performs no `diskutil mount` / FSKit mount of the USB
drive; it only `dd`-writes and raw-reads `/dev/diskNsN`. This eliminates the
screen-lock teardown dependency by construction.

### Property 2: Marker atomicity
The completion marker exists on the drive IFF the full image was written: it is
baked into the image and (optionally) the FAT signature is written last, so a
partial `dd` never presents a complete-looking volume.

### Property 3: Verified completion
`WriteResult.status == "complete"` IFF raw readback of the device matches the
composed image hash.

### Property 4: Destructive-write safety
A `dd` is issued ONLY to a device node that passes the full guard (removable +
FAT32 + expected size + expected label/identity + explicit confirmation); any
mismatch refuses the write.

### Property 5: Composition is drive-independent
`compose(...)` touches no `/dev/disk` and no `/Volumes` USB mount; it can run
with no drive attached.

## Error Handling

- **Wrong/unverifiable device node** → refuse the `dd` (raise, like the current
  `PrivilegeError`); never write. Highest-priority guard.
- **`dd` interrupted (USB pulled)** → verify fails; report "incomplete, re-write
  needed"; our local image and state are untouched. The drive is left with an
  invalid/partial FS (optionally guaranteed unrecognizable via last-written FAT
  signature) rather than a silently-partial valid FS.
- **Readback mismatch** → status "incomplete"; surface clear next step.
- **Compose failure** → keep the previous good image; do not write a bad image.
- **Privileged call failure** → surface the safe-next-step guidance (consistent
  with the current mount-swap error contract).

## Testing Strategy

- **Hardware GO/NO-GO** (manual, scratch drive + real EFIS): compose → `dd` →
  read in the actual HXr/Mini. This gates the whole approach (open question 1).
- **Scratch-drive bake-off** (manual harness, like the existing `repro_harness`):
  time compose+dd+verify vs the current rsync+swap on the same data; capture
  real numbers (open questions 7, 8).
- **Device-guard unit tests** (headless): the destructive guard refuses
  non-removable / non-FAT32 / wrong-size / wrong-label / unconfirmed nodes;
  accepts only a fully-matching node. (Pure logic, mock diskutil — mirrors the
  existing `test_mount_swap_privileged` pattern.)
- **Composer unit/integration tests**: image built from a fixture source tree
  contains exactly the expected files + marker; no AppleDouble in the image;
  content hash stable.
- **Verifier tests**: readback-match returns complete; injected mismatch /
  truncation returns incomplete.
- Property/example tests for P2–P5 where expressible headlessly; the raw `dd`
  and real-hardware reads are covered by the manual harness, not CI.

## Open questions (must resolve before requirements/tasks)

Hardware/correctness: (1) does the EFIS read a `dd`'d image correctly — GO/NO-GO;
(2) partition-slice vs whole-disk target while preserving the partition table +
identity; (3) image sizing to the slice geometry; (4) does local composition add
sidecars into the image and does it matter.

Safety (highest priority): (5) full destructive-grade device-node guard; (6)
privilege model for raw `dd`/readback (ties into parked Phase-2 signed helper).

Performance (measure first): (7) is compose+dd+verify actually faster than
rsync+swap, enough to justify the rewrite; (8) raw readback-verify cost over USB
and whether a bounded/sentinel verify is acceptable.

Scope/fit: (9) full-rewrite acceptable at ~68% churn (confirm no important tiny-
top-up case needs a secondary path); (10) is the mount-swap fully retired or kept
as a rollout fallback.

## Validation result (2026-10-03) — GO

Real-EFIS read test PASSED: a GRT EFIS read a dd'd drive normally. Combined with
the Mac-side validation (compose + dd to raw slice + raw-readback hash match),
Strategy D is confirmed viable end to end. Proceed to requirements/tasks.

Finding from the test — COMPOSITION SOURCING (new requirement): the test image
carried a STALE nav.db because it was hand-composed by copying EFIS_3's existing
on-drive files (its nav.db was the Sep-6 cycle). The dd/read was perfect; the
input was wrong. The real composer MUST source each component from its CURRENT
authoritative source, not from whatever happened to be on a drive:
  - ChartData  <- current chart source (~/EFIS/USB/ChartData)
  - nav.db / NAV-proc.DB  <- current nav source the DM maintains (NOT an old drive)
  - settings (Settings/WP/Plan)  <- per-device latest archive (or restore choice) [item #2]
  - EFIS/AHRS software uploaders  <- current software source [open question: where]
  - identity (EFIS_DRIVE_ID.json)  <- the drive's own, or newly minted on prepare
A per-component "current source" manifest should be an explicit part of the
composer design so no component is ever stale.

## Composer auto-refresh, notification, and component staging (2026-10-03)

### Auto-refresh trigger + frequency
The composer rebuilds the local image when NEW data lands. DECISION: event-driven
off the DM's EXISTING data checks, NOT a separate polling timer. When the DM's
chart check (every 12h), nav-DB check (24h), or software check (24h) completes
AND reports new data downloaded, it fires a composer refresh. This is naturally
bounded by the shortest DM cadence (12h charts) — satisfying "composer frequency
<= smallest DM check frequency" — with no redundant timer to drift. (If an
explicit floor is ever wanted: config composer_check_interval_hours capped at the
min DM interval; start event-driven.)

### "New image available" notification — DECISION: persistent menu item + menu-bar badge
Today the only mechanism is rumps.notification() = a TRANSIENT banner (easy to
miss). The user wants visible + permanent. Chosen (both are existing app idioms,
fully app-controlled, persist until acted on):
- **Persistent menu item** (primary): a menu line reflecting image state, e.g.
  "USB image current" -> "New image ready (nav 10/1 + plates 2610)" when a fresh
  image is composed; stays until the user acts. Mirrors the existing
  "Download Charts" menu-item pattern. Make it ACTIONABLE ("New image ready ->
  prepare a drive").
- **Menu-bar icon/title badge** (glanceable): badge the menu-bar glyph when a new
  image is ready (the app already swaps self.title for drive state).
Rejected: C) sticky native UNUserNotificationCenter (more work, macOS controls
dwell anyway); D) dashboard banner is a nice-to-have complement, later.
Notification should name what's new, specifically nav (NAV.DB + NAV-proc.DB) and
chart cycle.

### Component staging sources (refines the composition manifest)
- ChartData: current chart source (~/EFIS/USB/ChartData). STAGED locally today.
- nav.db / NAV-proc.DB: current nav source — check_and_download_nav_db() DOES
  download the current nav DB locally, so a current source EXISTS. (The test
  image's stale nav was a hand-compose mistake, not a gap.)
- settings (Settings/WP/Plan): per-device LATEST archive, or the restore choice
  (item #2). Clear source.
- EFIS/AHRS software uploaders (HHXRUp-*/MiniUp-*/MiniAHRSUp-*): DEFERRED
  (user 2026-10-03) — OUT of immediate composer scope. The DM cannot auto-stage
  current software (check only DETECTS + notifies a URL; GRT bot protection
  blocks headless download). For v1 the composer omits software or carries
  forward whatever local copy exists; software currency stays the user's manual
  responsibility. Revisit later — see BACKLOG "EFIS/AHRS software auto-staging
  for the composer".
- identity (EFIS_DRIVE_ID.json): the drive's own, or newly minted on prepare;
  extended with device_link_id for the multi-device binding (roadmap #1).

## Non-goals

- Not changing what chart data is sourced/downloaded.
- Not resolving Phase-2 signed-helper distribution beyond noting the privilege
  model depends on it.
