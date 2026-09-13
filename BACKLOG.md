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
