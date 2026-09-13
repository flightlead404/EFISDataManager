# Multi-Device Settings Handling & Archiving — Requirements (FIRST DRAFT / PARKED)

> STATUS: Draft for later. Captures the multi-display settings-archival and
> USB-redistribution ideas discussed 2026-09-09 so they are not lost. NOT yet
> refined into full EARS acceptance criteria. Separate from efis-settings-import
> (which only READS the primary HXr's settings into dashboard thresholds).

## Context

Many operators (including this one) fly with MORE THAN ONE GRT display:
- A primary HXr (the authoritative display for analysis/thresholds).
- A backup Mini A/P with a slightly different configuration.

Both displays write settings backups with the SAME filename
(`Settings.bak` / `Settings.dat`, plus `State/Plan/WP`), so their backups
collide on a shared USB. The EFIS can LOAD settings from an arbitrarily-named
file, but SAVES with the fixed name.

Identity signals available in the settings files (from GRT Settings.h):
- SID 387 (SID_DULINK_ID) = Display-Unit Link ID: 0=Auto, 1=Primary, 2-254=other.
  The user's HXr = 1 (Primary).
- SID 321 (SID_EIS_MODEL) = engine-monitor model (describes EIS, not the display).
- SID 1063 = Mode S Address (aircraft, e.g. A60670); SID 1062 = N-number.
- SID 1046-1053 = SID_HXR_DATA_BOX1..8 — HXr-specific (NOT used for gating;
  device-class gating is intentionally avoided — see cross-spec note).
- Source_Key concept (from efis-settings-import): Mode_S_Address ":" Link_ID.

## Scope of THIS spec (multi-device settings archiving + redistribution)

### R1 (draft): Per-device archive naming
- When archiving settings, prepend a USER-DEFINED prefix (<= 5 chars) per device
  so multiple displays' backups do not collide in the archive or on a USB.
- The prefix is configured per device (keyed by Source_Key = ModeS:LinkID).
- Blank prefix is allowed for AT MOST ONE display (others must be non-blank to
  disambiguate). Open question: enforce (reject 2nd blank) vs default (primary
  gets blank).
- Example: HXr -> `Settings.bak`; Mini -> `MINI_Settings.bak`.

### R2 (draft): Keep latest settings on the USB / redistribute to all drives
- Do not strip the settings file from the USB (archiver already copies, not
  moves — keep that).
- Stronger intent: after archiving, ensure the MOST-RECENT known settings for
  EVERY device are present on the USB, overwriting an older copy already there.
- When syncing any USB, place the latest settings for ALL devices onto THAT USB
  (each drive carries every device's newest settings, named per R1), so any
  drive can reload any device.
- These are loadable NAMED copies; the pilot still manually loads the desired
  file on the EFIS (redistribution does not auto-apply).

### R3 (draft): Safety of writing settings back to USB
- Writing settings files to the USB is a drive WRITE. Must not interfere with
  the chart-sync families (drive-sync-integrity), must be safe/atomic, and must
  respect the EFIS newest-by-UPDATE= behavior.
- Open question: automatic on every archive/sync vs an explicit action.

## Open questions
- Where the per-device prefix is set (dashboard settings UI, keyed by Source_Key).
- Enforce vs default for the single blank-prefix rule.
- Auto vs manual redistribution.
- How "latest settings for every device" is tracked (per-Source_Key store of the
  newest archived Settings.bak/.dat, reusing efis-settings-import's parser +
  Source_Key + UPDATE=).

## Explicitly NOT in this spec
- Reading settings into dashboard thresholds (that is efis-settings-import).
- The PRIMARY-DEVICE IMPORT GATE (only import from the Primary HXr) belongs in
  efis-settings-import, NOT here — see note below.

## Cross-spec note (belongs in efis-settings-import, do when resumed)
efis-settings-import must GATE which backup is eligible to import into dashboard
thresholds:
- Only a backup from the PRIMARY display (SID 387 == 1) is an import candidate.
  Do NOT gate on device model/class (users may run any mix: Sport, Horizon,
  HXr, one or two Minis, future models). "Primary" is device-agnostic and is
  the sole discriminator.
- A non-primary backup (SID 387 != 1) is archived but NEVER offered for import
  (status available=false, reason "not the primary display").
- On first import, BIND that primary display's Source_Key (Mode_S:Link_ID) as
  the authoritative import source; thereafter only that bound source is offered
  (prevents another primary in a different aircraft from silently overwriting
  thresholds).
- Edge case to handle: if NO connected/archived backup reports Primary (all
  SID 387 in {0=Auto, 2-254}), no import is offered; consider letting the user
  explicitly designate a source when none is flagged Primary (open question).
