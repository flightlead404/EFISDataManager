# Backlog / Future Feature Ideas

Non-urgent ideas and parked work. Not commitments; a scratchpad for the next
versions.

## efis-settings-import: real-HXr validation findings (2026-09-12)

First real data from the primary HXr (two flights + a genuine settings save with
Vne->TAS and TAS-calibration changes). Net: the feature largely WORKS on real
data; one narrow ordering weakness remains. Validated against the archived
.bak/.dat pair plus 10 prior .bak captures (HXr Link_ID=1, Mini A/P Link_ID=2,
both Mode_S A60670).

VALIDATED / working:
- **.dat archiving fix works.** The 9/12 re-insert captured Settings/State/WP/
  Plan .dat. The fresh descent-save landed in Settings-2026-09-12.DAT
  (889=1 Vne-as-TAS, MaxCHT 400->435, TAS-cal SIDs 551-561 populated,
  CHECKSUM 396643->397384, UPDATE 1->2, 30 SIDs changed). The .bak slot held the
  PRIOR config — exactly GRT's "EFIS replaces the older of the two slots" model.
- **Primary gate (SID 387==1) works** — cleanly IDs the HXr vs the Mini.
- **UPDATE= DOES increment on a real content change (1->2).** GRT's "read UPDATE=
  to pick the newer of .bak/.dat" rule is USABLE for same-concept .bak/.dat
  pairing. Settings_Selector correctly chose the .dat (UPDATE=2), and
  load_current_settings_sids() returned the correct fresh HXr config.
- **GRT CHECKSUM is 1:1 with our content_hash** (same checksum <=> same content);
  usable for content-equality / dedup / regression even though GRT's checksum
  ALGORITHM is unknown.

NARROW WEAKNESS TO FIX (smaller than first thought):
- Cross-capture ORDERING across dates must not rely on UPDATE=. When several
  archived captures share UPDATE=1 (repeated no-change saves), the Selector's
  tiebreak (prefer .dat, then path) can pick an older-dated file. It did NOT
  bite on 9/12 only because the freshest file (the .dat) also had the highest
  UPDATE. Fix: make the ARCHIVE FILENAME DATE (Settings-YYYY-MM-DD) the primary
  cross-capture ordering key; keep UPDATE= for same-concept .bak/.dat pairing;
  use content_hash/CHECKSUM for change/regression detection. EFIS file mtimes are
  FAT junk (1999 / 2025) and unusable.
- Also revisit the source-keyed regression Properties (23,24) that assumed a
  monotonic global UPDATE=; reframe around (Source_Key, archive-date,
  content_hash) rather than UPDATE= magnitude.

Scope: a focused efis-settings-import spec update (Selector + detect_new_backup
ordering + the affected properties) then re-implement. NOT a rewrite; the
parser, Primary gate, mapper, and .dat archiving are all good.

## Multi-device settings handling & archiving  [DRAFT SPEC PARKED]


Parked draft at `.kiro/specs/multi-device-settings/requirements.md`. For
operators with more than one GRT display (this user: primary HXr + backup Mini
A/P). Covers: per-device archive naming with a user-defined <=5-char prefix
(blank allowed for at most one device), keeping the latest settings on the USB,
and redistributing every device's newest settings onto each USB (loadable named
copies; pilot loads manually). Writing settings back to USB must not interfere
with chart-sync (drive-sync-integrity) and must be safe/atomic.

The related import-gate concern is now DONE: efis-settings-import Requirement 15
(shipped, implemented + tested) gates threshold import to ONLY the PRIMARY
display (SID 387==1) — device-agnostic, no model/class check — binds the
authoritative source (Mode_S:Link_ID) on first import, and archives but never
offers a non-primary backup. So this parked spec is now purely about
archiving/naming/USB-redistribution, not import eligibility.

## CORRECTED .bak/.dat model — cockpit test 2026-09-15 (HXr, EFIS_1 scratch key)

Controlled 3-save experiment on a verified-clean drive. Each save changed ONE
telltale SID so the write could be traced. Results OVERTURN the earlier
"dat=current, bak=previous" assumption:

- Save 1 (Vso 48->49, SID 888): wrote **Settings.bak only**, UPDATE=1. No .dat.
- Save 2 (Vs1 52->53, SID 19): wrote **Settings.dat**, UPDATE=2; .bak untouched
  (still UPDATE=1). At this point it LOOKED like "dat=current, bak=previous".
- Save 3 (Vfe 86->87, SID 20): **overwrote Settings.bak** with UPDATE=3 (Vfe=87).
  .dat stayed UPDATE=2 (Vfe=86). i.e. the NEWEST config is now in .BAK.

CONFIRMED MODEL (evidence-backed):
- **UPDATE= is a monotonic save counter** (1,2,3,...), NOT a slot/extension tag.
  It increments every save and travels with whichever file was written.
- The EFIS keeps exactly **TWO slots** (Settings.bak + Settings.dat) and on each
  save **overwrites the slot with the LOWER UPDATE (the older one)**. Classic
  ping-pong / alternating double-buffer.
- Therefore the **current config = the file with the HIGHER UPDATE**, regardless
  of extension. The .dat is NOT reliably "current". First-ever save on a clean
  drive produces .bak only.
- CHECKSUM is content-sensitive (tracked +2 per +1kt here: 397384->386->388) and
  is itself embedded as a SID, as is UPDATE.
- Both files are FULL snapshots (1145 SIDs), not diffs.

IMPACT — bug in shipped code (v1.4.0):
- `select_current_backup()` / Settings_Selector treat .dat as current and .bak as
  previous. That is WRONG whenever .bak has the higher UPDATE (odd number of
  lifetime saves to the drive). It only worked on the 9/12 HXr drive by parity
  luck (that drive's .dat happened to hold the higher UPDATE).
- FIX: choose current = max(UPDATE) across the .bak/.dat pair for a given
  Source_Key/date; use extension only as a tiebreak if UPDATE ties. Re-verify
  the import Selector and the archiver "which slot is current" logic.
- Restore feature: GRT "load" likely selects by highest UPDATE too (CONFIRM in
  Test 3/4). A restored/renamed file may need an UPDATE value that makes it win,
  which conflicts with the "never touch UPDATE/checksum" plan — resolve after
  Test 3/4.

## CHECKSUM correction — cockpit test 2026-09-15 (EFIS_3, save case 5)

The earlier claim "GRT CHECKSUM is 1:1 with our content_hash" is TOO STRONG.
Case-5 evidence: EFIS_3 .bak and .dat had IDENTICAL parsed content (same 1145
SIDs, same content_hash 5e9dd9a64568) but DIFFERENT CHECKSUM:
  .dat UPDATE=2 CHECKSUM=397384
  .bak UPDATE=3 CHECKSUM=397385
=> dUPDATE=1, dCHECKSUM=1. The GRT CHECKSUM FOLDS IN THE UPDATE COUNTER (or a
field that moves in lockstep). So identical settings saved at different UPDATE
values produce different CHECKSUMs.

Corrected rules:
- CHECKSUM is 1:1 with content_hash ONLY when UPDATE is held constant.
- Use content_hash (NOT raw CHECKSUM) for content-equality / dedup / regression.
  Our archiver already dedups on content_hash, so it is correct as-is; but any
  logic that keys on CHECKSUM magnitude must be re-examined.
- Restore feature: placing an archived file BYTE-FOR-BYTE unchanged keeps its
  own CHECKSUM/UPDATE internally consistent, so an on-load checksum validation
  (if any) should pass. Confirm with Test 4 (load a renamed byte-identical file).

Slot model now fully confirmed over 5 controlled saves (see prior entry):
UPDATE is a monotonic per-drive counter; each save overwrites the lower-UPDATE
(older) slot; current = higher UPDATE; extension is irrelevant; first save to a
clean drive makes .bak only; files are full 1145-SID snapshots.

## RESTORE MECHANISM — cockpit test 2026-09-15 (EFIS_3), design-changing

Tried to validate "place a date/device-named file, pilot selects it on load".
RESULT: that mechanism DOES NOT EXIST on this HXr firmware.

What actually happened:
- Copied current most-recent config (Settings.bak, UPDATE 3, Vso=48) byte-for-byte
  to Settings-2026-09-12-HXr.dat (renamed restore candidate).
- On EFIS: changed Vso 48->50 and "saved internally" (NOT "save to USB").
- Chose menu item **"Restore All Settings"** — it just runs, **NO file picker**.
- Vso reverted to 48.
- Post-test disk: Settings.bak U3/Vso48, Settings.dat U2/Vso48, renamed file
  U3/Vso48. NOWHERE is Vso=50, and UPDATE never advanced to 4.

CONCLUSIONS:
1. "Save internally" writes EFIS internal memory ONLY; it does NOT write the USB
   slots. (Matches user: settings retained internally, only read on manual load.)
   A separate "Save Settings to USB" is what writes .bak/.dat (used in saves 1-5).
2. **"Restore All Settings" = load current USB config INTO the EFIS.** No browser.
   It reads the CANONICAL slot files (Settings.bak/Settings.dat) and takes the
   **highest-UPDATE** one (here .bak U3, Vso48). Extension irrelevant.
3. It **ignored** the arbitrarily-named Settings-2026-09-12-HXr.dat entirely.
   => Renamed/date-stamped restore files are NOT selectable. The original restore
   design (pilot picks a nicely-named file) is INVALID on this firmware.

DESIGN IMPACT (restore-to-USB feature / efis-settings-management):
- To restore an OLD config, the tool must make that config the config that
  "Restore All Settings" will load, i.e. it must occupy the canonical slot(s)
  with the winning UPDATE.
- Cleanest path that AVOIDS fabricating UPDATE/CHECKSUM: write the chosen archived
  config into BOTH Settings.bak AND Settings.dat (byte-identical, reusing the
  file's own valid UPDATE/CHECKSUM). Then whichever slot Restore picks, it loads
  our config. NEEDS COCKPIT CONFIRMATION.
- Alternative (worse): set our file's UPDATE higher than the other slot -> forces
  us to compute GRT CHECKSUM (folds in UPDATE) which is UNKNOWN algorithm. Avoid.
- Open Q for next cockpit session: does "Restore All Settings" require BOTH slots
  present, or will a single Settings.dat (or .bak) alone suffice? And confirm the
  both-slots-identical approach loads cleanly.

Also confirmed again: the renamed copy created a macOS ._Settings-...dat sidecar
(FSKit ._* noise); irrelevant to EFIS here but note for restore-write hygiene.

## TERMINOLOGY CORRECTION (2026-09-15) — three distinct EFIS operations

Per user, the EFIS has THREE separate operations (my earlier note conflated the
first two):
1. **Save** = EFIS LOCAL/internal save only. Commits to EFIS memory; does NOT
   write USB. (This is where Vso=50 lived; never hit disk.)
2. **Backup Settings** = the SEPARATE step that WRITES the USB slots
   (Settings.bak/.dat): overwrites the older (lower-UPDATE) slot, increments
   UPDATE. This is what produced saves 1-5 in the slot-model test.
3. **Restore All Settings** = reads USB current config (highest-UPDATE canonical
   slot, .bak or .dat) INTO the EFIS. No file picker; ignores arbitrarily-named
   files.

So the restore-to-USB feature must place the chosen archived config as the
WINNING canonical slot file(s) on the USB so "Restore All Settings" loads it.
Renamed/date-stamped selectable files are not a thing on this firmware.

## RESTORE + CHECKSUM VALIDATION — cockpit test 2026-09-15 (EFIS_3)

Edited Settings.bak (the higher-UPDATE slot, U3) Vso 48->44 WITHOUT recomputing
CHECKSUM (unknown algo), leaving .dat valid (U2, Vso 48). Ran "Restore All
Settings".
RESULT: "settings restored", NO complaint, but **Vso stayed 48** (did NOT take
the edited 44).

Interpretation:
- A checksum-MISMATCHED file is NOT loaded, and NO error is shown -> the EFIS
  silently uses a good config instead. => **Hand-editing a settings file does not
  work.** The restore feature MUST place byte-valid files with correct checksums
  (reuse real archived files unchanged; NEVER edit field values in place).
- IMPORTANT: this also means our earlier "Restore takes highest-UPDATE slot"
  claim was NOT actually proven. That prior test had BOTH slots at Vso=48, so it
  couldn't distinguish "highest UPDATE" from "always .dat". Here the higher-UPDATE
  slot (.bak U3) was IGNORED because its checksum was bad; restore came from a
  good slot (.dat U2, Vso48). So the winning-slot rule is STILL OPEN:
    H-A: Restore validates checksum, picks the newest VALID slot (fell back .bak->.dat).
    H-B: Restore simply reads .dat (extension-based), .bak edit was irrelevant.
  Need a discriminator test (byte-valid files only) next session.

Backup of original EFIS_3 Settings.bak bytes saved at
temp/Settings.bak.ORIG (Vso=48, restore drive to this before flight).

DESIGN LOCK-IN so far:
- Restore feature places ONLY byte-unchanged, checksum-valid archived files.
- No in-place field editing; no fabricated UPDATE/CHECKSUM.
- Still must confirm which slot(s) Restore reads so we place the file correctly
  (likely: write the chosen config to BOTH slots byte-identically to guarantee
  a win regardless of the rule).

## RESTORE DESIGN — VALIDATED 2026-09-15 (EFIS_3): single-file approach WORKS

Test: placed ONE byte-valid archived config as the SOLE Settings.bak (9/12 .bak:
UPDATE1, CKSUM 396643, MaxCHT=400, 889=0/Vne-IAS), removed Settings.dat entirely.
Ran "Restore All Settings".
RESULT: loaded fine, NO error/complaint about missing .dat. On EFIS: MaxCHT->400,
"Change Vne to TAS"->No (889=0). => the single file loaded AND applied.

CONFIRMED RESTORE FEATURE DESIGN (efis-settings-management restore-to-USB):
- Mac tool writes the chosen archived config as the SINGLE settings file on the
  USB, BYTE-FOR-BYTE (its own original valid checksum), and REMOVES the other
  slot (delete Settings.dat if writing .bak, and vice versa) + any ._ sidecar.
- A lone valid Settings.bak (no .dat) restores cleanly. "EFIS has no choice."
- Pilot runs "Restore All Settings"; EFIS loads it. No file picker, no checksum
  fabrication, no UPDATE manipulation, no dependence on slot-selection rule.
- NEVER edit field values in place (checksum mismatch => silently ignored, no
  error, does not load — proven earlier same session).
- After restore, EFIS INTERNAL memory holds the restored (old) config; the pilot
  must be aware they've changed the active config. Feature UX should make the
  date/device of the config being restored explicit.

Drive left byte-exact to pre-test real config (both slots MaxCHT435/889=1, sha
.bak 0abb858bcb648ddb / .dat a8e46a55a470656e). NOTE: after the test the EFIS
BOX still had the old config loaded; user must reload real settings before flight.

## FSKit STALL — regression timeline investigation (2026-09-14)

User Q: why now? synced fine for ~2 weeks, stalls only since a few days ago. Did
we exacerbate it?

EVIDENCE (log from 8/23):
- First stall EVER: 2026-09-06. Clean 8/23 -> 9/05 (no stalls).
- Stalls: 9/06 (x2), 9/12 (x4), 9/14 (x2, live).
- ALL stalls occur during FULL/LARGE populates (9/06, 9/12, 9/14), NEVER on the
  cheap "up to date, no sync needed" mounts. Trigger correlates with WRITE VOLUME.
- EFIS_3 ChartData is currently EMPTY (0 files) -> today is a from-scratch ~100k
  file populate = worst-case FSKit load. Prior 9/12 stalls left it half/not synced.

CODE TIMELINE (git):
- 9/03: 3 sync commits landed: 6298fc0 (rework -> per-family rsync,
  --size-only --modify-window=2, removed -c and fixed 1h timeout), 00b1cbb
  ("fix stall root cause"), 465ebb4 (removed volume-root AppleDouble/dot_clean
  sweep, called ._* "harmless").
- First stall 9/06 = 3 days after the 9/03 rework. Circumstantial, not proof.

DID WE EXACERBATE IT? Careful reading:
- Current sync code ALREADY mitigates the obvious suspects: excludes ._*/.DS_Store,
  --delete-excluded to purge sidecars, and sets COPYFILE_DISABLE=1. Drive shows 0
  ._* sidecars -> excludes work. So the 9/03 "harmless ._*" concern was re-addressed.
- Net: no obvious code line made it worse; mitigations were ADDED. The likely
  contributor is the CHANGED WRITE CADENCE (single big copy -> per-family rsync
  streaming many small writes) provoking the msdos FSKit driver differently, AND
  large from-scratch populates being the trigger.
- Kernel log during stall: com.apple.fskit.msdos clients init/clientDied churn +
  "Denying dirty-tracking opt-in for managed com.apple.fskit.msdos" bracket the
  120s no-progress freeze. Same fingerprint each time. macOS 15.7.9 did NOT fix.

CLEAN TEST TO ATTRIBUTE CAUSE (needs physical drive, not at-airport):
- Run the PRE-9/03 sync code on the same full-populate load. If it also stalls =>
  FSKit driver / write-volume issue, not our rework. If it does NOT stall =>
  our per-family rsync cadence is the exacerbator.

MITIGATION DIRECTIONS FOR THE SPEC (root cause is Apple's FSKit msdos; we can only
work around it tool-side):
- Throttle / chunk writes; add inter-batch fsync + small sleep so the driver
  drains (avoid sustained streaming that seems to wedge it).
- On stall-detect, do a clean unmount+remount cycle and RESUME (we already resume;
  add the remount kick) instead of waiting the full 120s then erroring.
- Consider writing via a single tar/stream then unpack, or larger-granularity
  copies, to change the cadence back toward the pre-rework pattern.
- Investigate mount options (noatime, sync vs async) and whether the legacy
  mount_msdos path (non-FSKit) is available on this macOS to bypass FSKit msdos.
- Keep COPYFILE_DISABLE + ._* excludes (already present).
NEXT: new spec "fskit-stall-mitigation" (menu-bar tool; bump MENUBAR_VERSION).

## FSKit STALL — "why now?" RESOLVED via log+git (2026-09-14)

User Q: we had several good syncs before, then it broke — did we exacerbate it,
or just not notice?

ANSWER: NOT a regression we introduced. It is very likely LATENT/pre-existing
FSKit behavior; two things only arrived ~9/03 that made it VISIBLE:

1. Before 9/03 there were almost no HEAVY syncs. Log 8/23->9/05: nearly every
   mount = "Drive is up to date, no sync needed" (drives already current). The
   stall only manifests during LARGE writes; there basically weren't any.
2. The 120s stall DETECTION ("no progress for 120s") was ADDED 9/03 in commit
   00b1cbb ("fix stall root cause"). Before that, a wedged write logged only a
   vague "interrupted"/silent hang — not recognizable as this specific bug.
3. The ONE heavy populate before the detector (9/03 EFIS_1 full reformat+populate)
   ALREADY thrashed: Syncing scanned -> "Previous sync was interrupted" ->
   verify+repair -> repeat across 16:00/16:39/17:34, ~5h, multiple cycles, before
   finishing. Same signature as the explicit stalls; just pre-detection.
4. First EXPLICIT "sync stalled" ERROR = 9/06 EFIS_3, the first clean full
   populate run entirely under the new detector.

IMPLICATIONS FOR THE SPEC (chart-sync-stall-fix):
- Do NOT frame as a regression; git-bisect "revert to before X" is likely NOT a
  fix (the pre-rework code very probably stalls on the same heavy load too).
- Root cause (FSKit msdos) is still an ASSUMPTION (kernel-log correlation only) —
  the spec must CONFIRM/REFUTE it, not assume it.
- Reproduction is bounded & available: EFIS_3 has 94,893/101,563 files (source
  ~/EFIS/USB/ChartData = 101,563 files / 9.1 GB, both markers present). Drive
  missing plates marker + ~6,670 files (mostly plates). Full-from-scratch also
  reproducible by reformatting EFIS_3 (user has 2 other working drives; EFIS_3 is
  a free test bed).
- Preferred mitigation to evaluate FIRST: mount_msdos. VIABILITY UNCERTAIN —
  /sbin/mount_msdos exists but macOS 15.7.9 loads only com.apple.filesystems.lifs
  (FSKit); msdos.fs is an FSKit module, so mount_msdos may route through the SAME
  lifs path and not bypass it. Needs a spike: manually unmount + mount_msdos with
  options, run heavy write, see if it still stalls.
- Fallbacks if mount_msdos doesn't bypass: throttle/chunk writes + inter-batch
  fsync+drain; stall-detect -> clean unmount/remount -> resume (we already resume).
- Spec name: chart-sync-stall-fix (root-cause-neutral). Menu-bar tool change ->
  bump MENUBAR_VERSION on release.

## FSKit STALL — SPIKE RESULT 2026-09-15: mount_msdos CONFIRMED VIABLE

Ran mount_msdos viability spike on EFIS_3 (disposable). DECISIVE:
- Baseline FSKit mount: (msdos, local, noatime, FSKIT).
- /sbin/mount_msdos triggered `kmutil load -p .../msdosfs.kext` and loaded
  `com.apple.filesystems.msdosfs (1.10)` — the CLASSIC in-kernel kext — alongside
  lifs. Mount options showed NO 'fskit' flag: (msdos, local, noowners).
  => mount_msdos BYPASSES FSKit via the legacy msdosfs kext on macOS 15.7.9.
- Heavy write test: copied 6,000 real plate files, ZERO stalls (never hit 60s
  no-progress). Same drive/files/machine that reliably stalls under FSKit.

CONCLUSIONS:
- ROOT CAUSE CONFIRMED (was hypothesis): the stall is specific to the FSKit
  msdos/lifs path. Classic msdosfs eliminates it. Not just correlation anymore.
- mount_msdos is the PREFERRED mitigation and it WORKS. Throttle/chunk and
  remount-on-stall fallbacks are now contingencies, not the primary path.

DESIGN IMPLICATIONS:
- Sync flow: for a managed EFIS drive, unmount the FSKit mount and remount via
  mount_msdos (classic kext) for the duration of chart writes, then restore.
- mount_msdos requires ROOT (sudo). Need a privilege strategy: the menu-bar app
  runs as the user. Options to design: (a) a small privileged helper /
  SMAppService/launchd daemon; (b) admin auth prompt (AuthorizationServices);
  (c) sudoers entry for the specific mount/unmount commands. Security + install
  UX tradeoffs — cover in design.
- Must handle: device node discovery (/dev/diskNsN), clean unmount of the FSKit
  volume first (diskutil unmount), manual mountpoint mgmt, restoring the normal
  mount + Finder visibility afterward, and failure/rollback (never leave the
  drive unmounted or on a stale mountpoint).
- Keep existing rsync + verify + commit-marker + resumable sync-state unchanged;
  only the MOUNT underneath changes.
- Spike cleanup hit "umount: Resource busy" (had to diskutil mount to recover) —
  design the unmount/remount teardown carefully (lsof/settle/retry).

## Phase 2: signed SMAppService helper + Developer ID (chart-sync-stall-fix)

Shipped v1.5.0 on the INTERIM sudoers backend (Option C): install.sh writes a
narrow /etc/sudoers.d/efis-data-manager NOPASSWD grant for mount_msdos + diskutil
unmount/mount so the mount-swap fix works without a per-sync password. Accepted
interim risk: the mount_msdos grant is broad (sudoers can't constrain its
device/mountpoint args) — fine for a single-user personal Mac, NOT ideal for
wider distribution to less-technical users.

Phase 2 (tasks 14-19 in .kiro/specs/chart-sync-stall-fix/tasks.md), GATED on a
DECISION TO DISTRIBUTE:
- Acquire an Apple Developer Program membership (~$99/year; confirm current price)
  for a Developer ID Application cert + notarization. The free tier's
  Apple Development cert does NOT work for a distributed, notarized helper.
- Build a code-signed SMAppService/launchd privileged helper that VALIDATES
  arguments (removable FAT32 devices + the app's private mountpoint only), so it
  cannot be abused to mount arbitrary images as root — the key safety win over
  the broad sudoers grant.
- Swap the run_privileged backend (sudoers -> signed-helper IPC). The single shim
  seam means the sync engine + mount-swap logic do NOT change.
- Update install.sh to register/bless the helper; RETIRE the sudoers.d grant from
  fresh installs + remove it on upgrade.

Decision driver (recorded 2026-09-15): staying personal-use => interim sudoers is
fine, no spend. Distributing to other pilots => do Phase 2 FIRST (the sudoers
grant is a silent local root-capable primitive a non-technical user can't reason
about, breaks for standard/non-admin users, and lingers after a drag-to-Trash
uninstall). $99/yr buys SAFETY for people who can't assess the risk, not just
cleanliness.

Also pre-existing (unrelated to signing): the app ships UNSIGNED, so users must
right-click->Open past Gatekeeper on first launch. Code signing/notarization
(acquired for Phase 2) would also remove that friction.

## Chart-sync FAT32 small-file tail throughput (chart-sync-stall-fix follow-up)

v1.5.0 fixes the FSKit 120s hard-stall (drive syncs to completion via mount_msdos)
but the FAT32 small-file TAIL is slow: the live acceptance repopulate wrote ~74k
files / 1.4 GB in ~2.4h (mean ~8 files/s, peak 5.8 MB/s). This is FAT32's O(n)
directory-op cost on large dirs (driver-agnostic), plus concurrent find(1) walks
during the run stealing throughput. NOT a stall and NOT a correctness issue —
verify was clean, idempotent re-run current. Follow-up optimization ideas:
throttle/chunk contingency from the design; avoid full-tree walks during a live
sync; faster USB port/stick; accept full-repopulate as a rare one-time cost while
incremental top-ups stay small. Backlog, not a blocker.
