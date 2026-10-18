# Backlog / Future Feature Ideas

Non-urgent ideas and parked work. Not commitments; a scratchpad for the next
versions.

---

# ROADMAP (authoritative — top of file)

This section is the authoritative map of where the project is going. The dated
entries below it are the detailed evidence/record (cockpit tests, investigations)
that these roadmap items draw on; titles down there are chat-derived and may not
match the job name — use THIS section as the index.

## SHIPPED

### Image-based drive write (dd an image instead of rsync-to-mount)  [SHIPPED v1.6.0, 2026-10]
STATUS: DONE. Full spec at `.kiro/specs/image-based-drive-write/`
(requirements + design + tasks, all 13 tasks complete). Released as **v1.6.0**
(MENUBAR_VERSION 0.12.0); opt-in behind config `image_write_enabled` (DEFAULT
OFF). Architecture: compose a FAT32 image locally -> `dd` to the drive's raw
partition slice (`/dev/rdiskNsN`), never mounting the drive -> confirm by
raw-readback hash. This eliminates the FSKit write stall, the ~86% AppleDouble
sidecar tax, AND the screen-lock teardown bug in one architecture (no mount of
the drive = none of the three failure modes exist).

HARDWARE-VALIDATED end to end:
- The GRT EFIS reads a dd'd drive normally (the GO/NO-GO gate: PASS).
- Structural verify of a written drive: 101,694 files / 9.8 GB ChartData, ZERO
  `._*`/`.DS_Store` sidecars, NAV.DB bit-for-bit == source, identity + commit
  markers present.
- Full ~30 GB CHUNKED raw readback-verify ran twice with ZERO resets and
  MATCHING hashes — on a DIRECT Mac USB port.

KEY HARDWARE FINDING: USB bus-drops during sustained raw I/O were caused by the
HENGE DOCK (three cascaded hubs + shared/limited power), NOT the drive or the
code — through the dock the reset threshold fell on each retry (thermal/power
sag). Direct to the MacBook (root controller, full 900 mA) is flawless.
GUIDANCE: use a direct port for large transfers. The chunked write/read
resume-on-reset logic (re-enumerate at the new /dev node, re-guard, re-drive;
_wait_for_reenumeration separate from the dd-retry budget) remains a correct
safeguard for transient resets but is not a substitute for adequate
power/topology. image_write_enabled stays OFF by default until the verify path
is proven on whatever bus a given user has.

Cycle facts measured: ~68% chart content churns per cycle (Plates 79.7%, SEC
74.6%, LO 55.7%), and nav+plates share the FAA 28-day cycle while SEC/LO are
56-day — so most updates are near-full chart writes anyway, which is why the
whole-image dd beats rsync delta-sync here (a genuine delta would still rewrite
most of the tree AND pay the slow sidecar-doubled walk).

## ACTIVE NOW

(Nothing in active development. See PRIORITIZED items below for what's next.)

## PRIORITIZED BIG-PICTURE ITEMS (parked; flesh out next)

### 1. Multi-device handling  [REFINED 2026-10-17]
Draft spec parked at `.kiro/specs/multi-device-settings/` (predates this; will be
rewritten to match the decisions below). For an aircraft with 1-3 GRT displays
(this user: an HXr at Link_ID=1 + a Mini A/P at Link_ID=2, both Mode_S A60670).
SINGLE-AIRCRAFT assumption (multi-aircraft is a separate parked heading).

This item supersedes the older Link_ID-centric framing: the 2026-10-17
conversation reworked it around a THREE-AXIS per-device model, a declared (not
templated) capability profile, a DM-owned setup, and an outbound/inbound split.

IDENTITY & AIRCRAFT VERIFICATION:
- **Position = Link_ID** (SID 387, 1/2/3 = primary/secondary/tertiary), read
  from the drive's settings file via the existing extract_source_key
  (Source_Key = Mode_S:Link_ID). Link_ID is POSITION ONLY — it does NOT imply
  device type/capability (Link_ID 2 could be a Mini OR a second HXr; aircraft
  can have 2-3 HXrs). So capability must be DECLARED, never inferred from
  Link_ID.
- **Mode_S = aircraft identity**, constant across all displays in one aircraft.
  Used to VERIFY a drive belongs to this aircraft: a drive whose Mode_S differs
  from the configured fleet is FOREIGN -> message only, no archive/no action
  (the undetermined/foreign-source path). Mode_S is the future multi-aircraft
  key (SOMEDAY item).

DM SETUP (lives in the DM / menu-bar — the DM owns drive mount/sync/archive;
the dashboard defers drive/device management to it):
- **Device count** (1-3).
- **Per device (user-DECLARED; NO model templates — a per-model capability
  template would mean maintaining a catalog of every GRT display, which goes
  stale; let the user tick capabilities for their actual panel):**
  * **Friendly name** — user-set, default "Device N" / "Link N". Display label
    only; Link_ID is the stable key.
  * **Chart-capable: yes/no** — declared. (A Mini cannot use charts/plates; an
    HXr can.) This single boolean drives BOTH the outbound chart payload AND the
    write mechanism (below). The old per-device "which charts" question is GONE:
    chart PRODUCT selection is already a GLOBAL DM setting (chart_types; the SA
    subscription). An IFR subscriber puts the full set on every chart-capable
    display — that is the point of a backup — so there is no per-device chart
    subset.
- **Analysis data source = exactly ONE user-selected device** (aircraft-level
  radio-select). FDL + logbook import into the analysis DB come from ONLY this
  device. Fully user-selectable with NO capability gating (a user with a
  dual-HXr-ish panel might deliberately record flight data on the Mini). This
  single-source rule (vs per-device opt-in) avoids merging subtly-different
  flight records from two displays and matches the shipped efis-settings-import
  Primary-gate philosophy (one authoritative source for analysis).

OUTBOUND (what the DM PUTS on each device's drive):
- **Charts/plates**: only to chart-capable devices; content = the GLOBAL
  chart_types selection. Not a per-device toggle beyond chart-capable yes/no.
- **NAV.DB / NAV-proc.DB**: ALL devices (HXr and Mini both use nav).
- **Settings**: ALL devices, every mechanism — push the device's MOST-RECENT
  archived settings onto the prepped drive IF NOT ALREADY PRESENT (content-aware
  / idempotent). This keeps a fresh known-good settings copy on every card AND
  converges the two rotating cards toward the same baseline (see the settings
  model in #2). This push IS #2's populate writer, scoped per-device.
- **Write mechanism is DERIVED, not a user toggle**: chart-capable -> image+dd
  (the shipped image-based-drive-write path, worth it for the ~9 GB high-churn
  chart tree); not chart-capable -> small-file sync (a Mini drive is ~18 MB nav
  + small settings; composing/dd-ing a 30 GB image to push 18 MB is absurd). The
  global `image_write_enabled` flag (now exposed in dashboard Settings) becomes
  an advanced override/fallback, not the primary control.

INBOUND (what the DM TAKES from each device's drive):
- **Settings/WP/Plan**: ALWAYS archived, per-device, keyed by Link_ID, in
  per-device subdirs (e.g. `~/EFIS/Archive/Settings/link1/Settings-YYYY-MM-DD.dat`).
  Reuses the v1.5.4 content-aware archive-on-change dedup, scoped PER DEVICE so
  device A never clobbers device B. This is the baseline inbound item (every
  device has settings; they are the Link_ID source + flight-critical) — hence
  "analysis beyond settings" below.
- **FDL + logbook -> analysis DB**: from the ONE designated analysis-source
  device ONLY (see setup). Other devices' drives are not mined for analysis
  data, so the multi-drive same-flight overlap never even enters the DB.

DRIVE<->DEVICE BINDING: resolve which device a drive is for by (a) reading
Link_ID+Mode_S from the drive's existing settings file (populated drive), or
(b) EXPLICIT bind at Prepare time for a blank drive when >1 device is configured
(DM asks which device; store e.g. a `device_link_id` in EFIS_DRIVE_ID.json).

COMPONENT SPLIT (user 2026-10-17):
- **DM (menu-bar)**: device setup, archiving on insert, sync/push to drives,
  restoration WRITE (only the DM mounts/writes drives), divergence warning.
- **Dashboard**: settings archive EXPLORATION, COMPARE, and version SELECTION
  for restoration. Restoration is the one cross-cutting flow: the dashboard
  SELECTS the version, the DM WRITES it to the drive.

Already DONE / reused: extract_source_key / Source_Key + primary-only import
gate (efis-settings-import, shipped), the content-aware archiver (v1.5.4), the
image-write path + `image_write_enabled` (v1.6.x, shipped).

### 2. Settings management  [REFINED 2026-10-17]
Builds on #1's per-device identity. Settings are flight-CRITICAL (Vne, redlines,
limits, cylinder count) — correctness over convenience. The 2026-10-17
conversation SIMPLIFIED this materially from the earlier A/B/C write-up (the
round-trip "were these loaded?" prompt is GONE; the primary undo turns out to be
EFIS-native and needs no DM feature).

GOAL (user's words): two USBs per device in rotation — one in the airplane
(current charts, collecting flight data + enriched logbook, EFIS may save
settings to it) and one at home in the DM (getting latest charts/nav + latest
settings, waiting for the next cycle). If the pilot changes a setting on the
EFIS (deliberately or inadvertently) and flight-test shows it is unacceptable,
they want to RESTORE to the most-recent known-good settings with MINIMUM manual
file bookkeeping.

KEY ASSUMPTION (user): "Backup Settings to USB" is a MANUAL pilot action, never
automatic-on-change. So anything on a USB was deliberately saved and at-least-
flight-test-acceptable at the time. Therefore the DM TRUSTS a returning settings
file as a legitimate new version — NO inadvertent-bad-change guard, NO round-trip
prompt. (The compare function covers after-the-fact "what changed?".)

TWO DISTINCT MEANINGS OF "RESTORE" (do not conflate):
- **EFIS-side "Restore" (the pilot's primary undo, THIS is what "restore" means
  in the goal):** changes made ON the EFIS are bad -> "Restore All Settings"
  loads the USB's current slot back into the EFIS. The EFIS keeps its OWN
  internal config and only loads from the card on this deliberate action. This
  needs NO new DM feature: the known-good is already the pilot's last manual
  save sitting on the in-plane card; the DM's only job is to maintain a good
  settings copy on the card and NEVER clobber it.
- **DM-side "push an older version to a card" (NOT called restore here):** the
  DM writes a chosen older archived version (most-recent-prior or user-picked
  dated) onto a card so it can then be loaded. Different actor/mechanism.

CROSS-CUTTING CONSTRAINT — the CHECKSUM wall (cockpit-confirmed 2026-09-15):
GRT's CHECKSIZE/CHECKSUM algorithm is UNKNOWN and folds in UPDATE. A settings
file whose CHECKSUM does not match its contents is SILENTLY IGNORED (reports
"restored" but keeps old values). So NEVER fabricate/bump UPDATE/CHECKSUM; every
write-to-drive reuses an archived file's OWN original valid UPDATE+CHECKSUM
byte-for-byte. Cockpit-validated write = the chosen file as the SOLE settings
slot on the USB (delete other slot(s)) so "Restore All Settings" loads it by
default. (EDITING a settings file is therefore BLOCKED on this firmware — stale
CHECKSUM = silently ignored = a safety hazard on flight-critical limits.)

THE TWO-CARD DRIFT PROBLEM (named by user; inherent, not fully solvable):
Because the EFIS keeps its own config and saves to whichever card is present,
two rotating cards can hold DIVERGENT save histories. If a good save goes to
card A, then card B (older) is taken out and a bad change saved to it, an
EFIS-side "Restore" from B lands a step too far back. The robust operational
answer is the pilot's discipline: FLIGHT-TEST NEW SETTINGS ON ONE USB ONLY. The
DM cannot reconcile two independent on-card histories it never saw, but it HELPS:
  1. **Propagate latest onto every prepped card** (outbound settings push, #1) —
     shrinks the drift window; cards converge once a good save has come home.
  2. **Divergence WARNING (inbound):** when a card returns with settings OLDER
     than the DM's latest for that device, flag it ("this card is behind the
     latest known-good dated X") so the pilot notices before it bites.

CAPABILITIES:
- **A. Per-device dated archive history** (DM; built on #1 + v1.5.4): archive
  every returning deliberate save, dated + content-hashed, per Link_ID. Trusted
  (no prompt). This is a HISTORY, not a single "latest".
- **B. Settings explorer — READ-ONLY (dashboard): view + compare.** View parses
  a file to SID->value with labels (reuse Settings_Mapper). Compare diffs two
  files (card vs latest, or two dated versions) — labeled SID deltas. NO editing
  (CHECKSUM wall).
- **C. Version restoration = dashboard SELECTS, DM WRITES.** Pick most-recent-
  prior or a dated version in the dashboard; the DM writes it to the
  inserted/bound drive as the sole slot (reuse original UPDATE/CHECKSUM), WRITE-
  NOW one-shot. (This is the DM-side "push an older version", distinct from the
  EFIS-side undo.)
- **Settings-population-to-drive** = the SAME writer as C, sourcing "latest
  archive for device N" instead of a chosen version. Restore is the override
  variant of this one mechanism. (The "latest EFIS/AHRS software on prepare"
  staging remains an OPEN QUESTION / SOMEDAY item — GRT bot protection blocks
  headless software download.)

COMPONENT SPLIT: DM = archive-on-insert, settings push, restoration write,
divergence warning. Dashboard = explore, compare, select-version-for-restore.

OPEN QUESTIONS for the spec: (a) final cockpit confirm that a single-slot file
loads cleanly via "Restore All Settings" (believed yes); (b) source/versioning
of "latest EFIS/AHRS software" for population; (c) exact dashboard UX for
browsing per-device dated versions + the select->DM-write restoration handoff;
(d) how the divergence warning surfaces (DM notification + dashboard badge?).

### 3. Sudo retirement (signed privileged helper)
= chart-sync-stall-fix Phase 2 tasks 14-19 (currently the only open tasks in that
spec): adopt Developer ID code signing + notarization, build a signed
SMAppService/launchd privileged helper, swap the run_privileged backend from the
interim sudoers grant to the helper, retire the sudoers.d file, re-release.
Gated on a paid Apple Developer ID (~$99/yr). The privilege model for
image-based-drive-write (raw dd/readback need root) SHOULD be designed together
with this — same privileged-device-op foundation; do once, not twice.
(TO FLESH OUT.)

### 4. Windows version
LOE analysis done (dated entry below): ~2.5-4 wk full / ~1 wk dashboard-MVP;
shared-core refactor recommended. Parked behind dialing in the Mac version.
(TO FLESH OUT.)

### 5. Natural-language query capability
= efis-data-manager spec tasks 7.1-7.6 (the only open tasks there): ask questions
of the flight-data SQLite DB in natural language (e.g. EGT spread, oil
consumption, fuel-flow trends, leaning events) -> SQL -> formatted result, surfaced
in the menu-bar UI. Open design question: local LLM vs API vs structured NLQ.
(TO FLESH OUT.)

## SOMEDAY / UNPRIORITIZED (await "customer demand" — so far ~nobody has used a release)

### Multi-aircraft support
Extend multi-device identity beyond the single-aircraft assumption: key devices
on the FULL Source_Key (Mode_S:Link_ID) so one install can manage drives/settings
for more than one aircraft. Parked until there is demand; the single-aircraft
model (item #1) is built so Mode_S is already carried and can become a real axis
later without a rewrite.

### EFIS/AHRS software auto-staging for the composer  [DEFERRED 2026-10-03]
The image-based-drive-write composer stages ChartData, nav DB, and settings from
current local sources, but NOT the EFIS/AHRS software uploaders
(HHXRUp-*/MiniUp-*/MiniAHRSUp-*). Reason: the DM's software check
(check_and_download_efis_software) only DETECTS a new version and notifies a URL
for MANUAL download — GRT's site bot/Sucuri protection makes headless download
unreliable, so there is no auto-staged current software to pull. For now the
composer either omits software or carries forward whatever local copy exists;
software currency is the user's manual responsibility (prompted by the DM's
detect+notify). REVISIT options when prioritized: (1) a user-maintained local
software folder the composer stages from; (2) investigate reusing the nav-DB
Playwright/Sucuri-bypass download path for software; (3) carry-forward
last-known. Deferred per user 2026-10-03; not blocking image-based-drive-write.

### (Windows version is item #4 above but is similarly demand-gated.)

---

# DETAILED ENTRIES / EVIDENCE (dated, chat-derived; see Roadmap above for the index)

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

## Windows version — LOE analysis (2026-09-15, FUTURE / parked)

Parked: dial in the Mac version first. Premise: a Windows build drops ALL chart
syncing (Seattle Avionics' own Windows Chart Data Manager handles charts) and
keeps everything else — settings + logbook archiving, flight-data ingest, the
analysis dashboard.

Codebase survey (16k LOC total) — portability:
- PORTS AS-IS (~7.5k LOC, pure Python, no OS coupling): fdl_parser, database
  (SQLite), analysis, gami, power, aux_map, efis_settings, export,
  notification_history. The real logic.
- DASHBOARD PORTS FREE: dashboard/app.py (833 LOC, 29 Flask routes) + 8 HTML
  templates. Flask is cross-platform; biggest single asset, zero change. This is
  the crown jewel for a Windows user.
- MODEST PLATFORM WORK (isolated): config.py one hardcoded
  ~/Library/Application Support path -> %APPDATA% (~0.5d); credential storage
  macOS Keychain (currency.py/app.py) -> `keyring`/DPAPI (~0.5d); usb_monitor
  /Volumes polling -> Windows drive letters D:\ etc (~1-2d, and SIMPLER on
  Windows since it only triggers archive+ingest, not sync).
- REAL WORK: app.py (1823 LOC) is rumps menu-bar -> needs a Windows tray shell
  (pystray) + settings dialog (Tk/PySide) replacing settings_window.py (295 LOC
  PyObjC/AppKit). Logic mostly reusable; UI shell + event wiring rewritten.
  Notifications macOS -> Windows toast (small).
- DROPS ENTIRELY (~7k+ LOC): mount_swap, fskit_diagnostics, repro_harness, most
  of drive_updater, currency (chart download), the FSKit fix, launchd. Windows
  is SIMPLER in the areas kept.

LOE: full feature/UX parity ~2.5-4 weeks focused. Dashboard-centric MVP
(dashboard + simple ingest/archive trigger, skip polished tray UX) ~1 week.

RECOMMENDATION: do NOT fork into two codebases. Extract a shared cross-platform
CORE (pure logic + dashboard + a platform-abstraction layer for
paths/credentials/drive-detection/notifications), then thin macOS (rumps) +
Windows (pystray) front-ends over it. Upfront refactor cost, but kills
double-maintenance and benefits the Mac version too; the Windows front-end
becomes the only net-new surface. That refactor is the single biggest lever on
the estimate.

Open scoping question for when this is picked up: full parity vs
dashboard-centric MVP? Answer swings the estimate ~4x.

## Note: prepare-drive-mount-swap bugfix vs. future Windows version (2026-09-18)

Q asked: for the prepare-drive-mount-swap bugfix, take the path with least rework
should we later build a Windows version.

Assessment: the fix as implemented (v1.5.1) is ALREADY the minimal-rework choice
for Windows portability; no change needed. Reasons:
- The drive_updater half (wrap prepare/adopt populate in msdos_mount) is 100%
  macOS-specific — it only exists to dodge the FSKit/FAT32 stall. A Windows
  build DROPS chart syncing entirely (Seattle Avionics' Windows tool handles
  charts), so prepare/adopt chart-populate + the mount swap are in the
  "drops entirely" bucket. No Windows debt created.
- The app.py provisioning guard (_provisioning_drives/_is_provisioning + the
  race-suppression in _on_efis_drive_mounted) is a platform-agnostic CONCEPT but
  lives in the rumps app shell, which Windows rewrites (pystray) regardless.
- Doing the shared-core refactor NOW just to help a hypothetical Windows version
  would be premature (adds risk to a working fix, zero present benefit). The
  refactor is already captured as the recommended approach in the Windows LOE
  entry above; do it as a deliberate future project, not folded into this bugfix.

Decision: ship the bugfix as written (v1.5.1). Windows portability is unaffected.

## Drive population policy: latest software + settings on prepare, restore override (2026-09-18)

NEW requirement (user). What Prepare Drive / drive population should put on a drive:

DEFAULT (no restore active): put the LATEST of everything on the drive —
  - latest charts (DONE today: scanned + plates families)
  - latest nav DB (DONE today: nav family)
  - latest EFIS/AHRS software (NOT DONE — the HHXRUp*.dat / MiniUp*.dat uploader
    firmware files; populate does not stage these today)
  - latest / current settings file (NOT DONE — populate places NO settings file
    on the drive at all today; settings are only ARCHIVED *from* the drive)

EXCEPTION (restore-settings active): if the user has selected an archived
settings config to restore (the parked efis-settings-management push-to-USB
capability), that CHOSEN settings file goes on the drive INSTEAD of the latest —
placed as a single byte-valid file with the CORRECT UPDATE= so the EFIS treats
it as current/loadable. Mechanics already cockpit-validated (see the .bak/.dat
+ "Restore All Settings" findings earlier in this file): EFIS loads the
highest-UPDATE canonical slot; CHECKSUM folds in UPDATE; place byte-valid file,
never hand-edit. On a fresh prepared drive there is no competing slot so a
single valid file wins; if a newer-UPDATE file already exists on the drive the
restored one must out-rank it (UPDATE correctness) — this is the nuance the user
called out ("the update key needs to be correct in that case").

CURRENT STATE (confirmed in code 2026-09-18): drive_updater populate handles
ONLY 3 families (scanned/plates/nav = charts + nav DB). It does NOT stage
EFIS/AHRS software and does NOT place any settings file. The restore-to-USB
capability (efis-settings-management) is PARKED (designed + cockpit-validated,
never built). So this requirement is a real, multi-part feature gap.

OPEN QUESTIONS to resolve in the spec (do NOT assume):
- Source of "latest EFIS/AHRS software": GRT download site? a local folder the
  user maintains? Which files exactly (HHXRUp-*, MiniUp-*, *proc*)? How is
  "latest" determined/versioned?
- Source of "latest / current settings": most-recent archived settings snapshot
  from ~/EFIS/Archive/Settings, or the live current config? For which device(s)
  (primary HXr vs Mini — ties into the parked multi-device-settings work)?
- Interaction with multi-device-settings (each device's own settings/software).

SEQUENCING: depends on efis-settings-management (restore-to-USB) for the restore
branch. Do NOT fold into the v1.5.1 prepare-drive-mount-swap bugfix (that fix is
done, ships as-is). Spec this separately (e.g. "drive-population-policy" or fold
into efis-settings-management). Menu-bar-tool change -> MENUBAR_VERSION bump on
release.

## BUG: stall watchdog false-trips on rsync's scan/--delete phase (2026-09-30)

SEVERITY: high-ish — makes ROUTINE incremental chart refreshes on a populated
drive spuriously fail ("plates update failed", 120s stall), even on fast USB-3
hardware and even with the v1.5.0 mount_msdos fix in place. Reformatting a drive
only postpones it (a fresh populate has no tree to scan; the NEXT incremental
refresh re-creates the tree and stalls again).

ROOT CAUSE (confirmed in code, not hypothesis): sync_payload's stall watchdog
measures liveness ONLY by growth of rsync's stdout log, which uses
`--out-format=%n` — that emits ONE LINE PER FILE TRANSFERRED and NOTHING during
rsync's initial file-list build + `--delete` reconciliation scan. On a large
existing tree (EFIS_1 plates: ~21.8k files, ~2x with ._* sidecars = ~44k to
stat), that silent scan easily exceeds STALL_TIMEOUT_SECONDS=120, so the
watchdog aborts a HEALTHY rsync that is just scanning. It cannot distinguish
"silently scanning a huge tree" from "genuinely wedged".

EVIDENCE (2026-09-30, EFIS_1 incremental refresh):
- Drive plates=21,803 vs source=21,923 -> only ~120 files to copy (tiny delta),
  0 to delete. So NOT a heavy write; the stall was during the compare/scan.
- Log: swapped to mount_msdos OK -> "Syncing plates..." 15:03:38 -> "plates sync
  stalled: no progress for 120s" 15:05:38. nav (2 files) synced fine right after.
  Teardown (v1.5.1) recovered cleanly. Marker NOT written -> plates left pending.
- Hardware ruled out: device is SanDisk 3.2Gen1 (0781:5583), USB 5 Gb/s link
  (an Ultra Fit) — modern/fast, not a slow old stick.
- This is a DIFFERENT stall from the v1.5.0 FSKit write stall. mount_msdos does
  not help because the phase that stalls produces no stdout regardless of driver.

FIX OPTIONS (spec decision):
1. PREFERRED: add `--info=progress2` (and/or periodic progress) to the rsync
   invocation so rsync emits progress to stdout DURING scan + transfer; the log
   grows, the watchdog sees liveness. Adjust the %n file-count parsing to still
   work (or count from --itemize/stats). Simplest robust fix.
2. Measure liveness by the rsync PROCESS advancing (CPU time / still alive /
   IO), not stdout-byte growth, so a quiet scan is not mistaken for wedged.
3. Scale/raise the timeout, or suppress the watchdog until first byte transfers.
4. Reduce scan cost: prune/suppress ._* sidecars (they ~2x the tree rsync must
   stat), and/or skip `--delete` reconciliation when the family marker is
   present and count+size already match (only reconcile when needed).

Recommend 1 (+ 4 as a perf win). Contained follow-up to chart-sync-stall-fix;
menu-bar-tool change -> MENUBAR_VERSION bump. Until fixed, incremental refreshes
of populated drives are unreliable; a from-scratch prepare works once.

## RESOLVED v1.5.3: stall watchdog + the --info=progress2 launchd gotcha (2026-09-30)

The stall-watchdog false-trip (entry above) is FIXED in v1.5.3. Implementation
note / trap for the future:

- The watchdog now uses TWO liveness signals and aborts only when BOTH are flat
  for STALL_TIMEOUT_SECONDS: (a) rsync stdout-log growth (--out-format=%n, one
  line per transferred file — the transfer phase) and (b) rsync CPU-time advance
  (via `ps -o time= -p <pid>`, no psutil dep). CPU-advance is what proves
  liveness during the SILENT file-list/--delete scan on a large tree, which
  emits zero stdout and used to false-trip the stdout-only watchdog.
- GOTCHA (cost us a bad v1.5.2 release): we initially also added
  `--info=progress2`. It works with Homebrew rsync 3.4.4 (/opt/homebrew/bin,
  first on an INTERACTIVE shell PATH), but the app runs under LAUNCHD, whose
  minimal PATH resolves bare `rsync` to /usr/bin/rsync = openrsync 2.6.9, which
  REJECTS `--info` ("unrecognized option") and aborts the whole transfer. So
  v1.5.2 made populated-drive syncs HARD-FAIL. v1.5.3 drops progress2 entirely;
  CPU-advance covers the scan phase on any rsync version.
- LESSON: validate rsync flag changes against /usr/bin/rsync (the launchd-
  resolved binary), NOT the interactive-shell Homebrew rsync. Field-validated
  live on EFIS_1: a ~48-min / ~16k-file repair rode through with zero false
  stalls (old code died at 120s).

## v1.5.3: entry-side FSKit unmount now settles + retries (2026-09-30)

Symptom: "could not prepare for sync, reinsert and retry" on a freshly mounted
drive. Cause: msdos_mount step 3 (the diskutil unmount of /Volumes/<label> that
STARTS the swap) was single-shot. A just-mounted volume is commonly held for a
second or two by a transient dissenter (Spotlight mds/mdworker indexing the
fresh mount), so the single attempt intermittently dissented and aborted the
whole swap. Fix: new _unmount_fskit_for_swap() mirrors robust_unmount's
settle-then-bounded-retry (SETTLE_WAIT_SECONDS first, then UNMOUNT_MAX_ATTEMPTS
with growing backoff), but deliberately does NOT escalate to `unmount force`
(entry side is still healthy on FSKit; a persistent holder should abort cleanly
and leave the user on the working mount). Field-validated: swap started clean on
EFIS_1 reinsert, no abort.

## BUG: FSKit remount fails when device drops off USB bus during teardown (2026-09-30)  [RESOLVED]

RESOLVED by the image-based-drive-write architecture (SHIPPED v1.6.0): that
write path never mounts the drive (compose local image -> dd to raw slice ->
raw-readback verify), so there is no FSKit mount to tear down and no
loginwindow-dissented unmount/remount to fail — this failure mode cannot occur
on the image-write path by construction. NOTE: the legacy mount-swap write path
(image_write_enabled=False, the current default) still has this teardown
exposure; the candidate fixes below (device-gone detection, a caffeinate/
power-assertion during long syncs) remain valid for that path until image-write
is the default. Original report retained below for reference.

SEVERITY: medium — leaves the drive needing a physical reinsert after an
otherwise-successful sync; data is intact (sync completed + marker written
before teardown).

Observed on EFIS_1 after the ~48-min plates repair (2026-09-30 ~17:36):
1. Sync completed cleanly ("plates sync complete" x2, "Drive update complete").
2. Teardown robust_unmount of the private work mount was DISSENTED by
   loginwindow (PID 604) on all 3 attempts — the screen had locked during the
   long sync. robust_unmount correctly escalated to `diskutil unmount force`
   (succeeded).
3. restore_fskit_mount then failed all 4 attempts: attempt 1 rc=1 ("failed to
   mount"), attempts 2-4 RAISED. `diskutil list disk4` afterwards: "Could not
   find disk for disk4" — the DEVICE NODE WAS GONE. The drive had re-enumerated
   off the bus (likely the force-unmount under the loginwindow lock, or a
   SanDisk Ultra Fit reset after 48 min sustained load).
4. App correctly fell back to "drive needs reinsertion" (no corruption). A
   physical reinsert brought it back clean; .sync_state.json was already cleared
   so NO re-sync triggered — the repair stuck.

CANDIDATE FIXES (spec decision):
- Detect device-gone in restore_fskit_mount (resolve_device_node returns
  None / diskutil can't find the disk) and emit a precise "reinsert the drive"
  message immediately instead of 4 failing/raising retries.
- Hold a power/display-sleep assertion (IOPMAssertionCreateWithName /
  `caffeinate`) for the duration of a long sync so the screen does not lock and
  loginwindow never grabs the volume mid-teardown.
- Consider shortening the teardown window / unmounting before the OS idle-locks.
