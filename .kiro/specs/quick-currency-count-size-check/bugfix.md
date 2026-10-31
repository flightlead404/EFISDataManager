# Bugfix Requirements Document

## Introduction

The quick drive-currency check decides whether an inserted drive needs a chart sync. For
chart families (scanned, plates) it currently trusts the family's COMMIT MARKER (present,
size-matching, newer-or-equal) plus a cheap "is the family dir non-empty" structural
check. It does NOT compare how many files or how many bytes the card actually holds
against what the current cycle should contain. As a result, a drive that is grossly
incomplete can be declared "current".

Observed 2026-10-31: drive `C2609P2610` reported "Drive Current" while its plates family
was missing 11,345+ tiles (a later cheap count showed the card held 3,980–10,896 plate
files against 22,242 in the source). The commit marker was present and matched, so the
quick check trusted it; a ~2-second file-count comparison would have immediately shown the
drive was not current.

This spec adds a CHEAP, FAST NEGATIVE to the quick currency check: a per-family file count
and total byte size comparison between the card and the current cycle's content. The check
can only REJECT currency early (prove "not current" cheaply); it can NEVER confirm
currency, because an equal count and size can still hide a same-byte-size content change
(the exact case the SHA-256 manifest exists to catch). So a cheap MATCH does not short-
circuit anything — it falls through to the normal (manifest-based) currency/sync path.

### Terminology

- **local manifest** — SSD-side manifest built on DM download/extract (authoritative file
  set + per-file size for the current cycle).
- **card manifest** / **disk manifest** (synonyms) — manifest on the removable USB media.

The cheap check compares the CARD's actual on-disk count+size against the local manifest's
recorded count + summed sizes (the local manifest already carries per-file size, so no
hashing is needed for this tier).

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type DriveCurrencyCheck
  OUTPUT: boolean

  // The quick check declares a chart family current while the card's actual
  // file count / total size differs from the current cycle's expected set.
  RETURN X.quick_check_result[family] = "current"
         AND ( card_file_count(family)  <> expected_file_count(family)
            OR card_total_bytes(family) <> expected_total_bytes(family) )
END FUNCTION
```

### Property Specification (Fix Checking)

```pascal
// Property: Fix Checking - a cheap count/size mismatch rejects currency
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT result.quick_check_result[family] = "not current"
  // (and the family is therefore routed to the sync path)
END FOR
```

Where **F** trusts the commit marker alone and **F'** additionally performs the cheap
count+size comparison and forces "not current" on any mismatch.

### Preservation Goal (Preservation Checking)

```pascal
// Property: Preservation Checking - cheap MATCH never upgrades to "current"
FOR ALL X WHERE (card count and size MATCH the expected set) DO
  // The cheap check is a negative only: a match must NOT by itself declare
  // the family current. The result is whatever the existing (marker + eventual
  // manifest) logic decides — the cheap check neither confirms currency nor
  // bypasses the content-hash check.
  ASSERT F'(X).currency_decision_source <> "cheap_count_size_match"
END FOR
```

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the quick currency check evaluates a chart family THEN it decides "current" from the commit marker (exists, size matches, newer-or-equal) plus a non-empty-structure check, without comparing the card's actual file count or total bytes to the expected set
1.2 WHEN a chart family on the card is materially incomplete (many files missing) but its commit marker is present and matches THEN the quick check declares the family current
1.3 WHEN a drive is declared current on this basis THEN the user may carry a drive to the EFIS believing it holds charts it does not

### Expected Behavior (Correct)

2.1 WHEN the quick currency check evaluates a chart family THEN it SHALL perform a cheap comparison of the card's actual file count and total byte size against the current cycle's expected set (from the local manifest) BEFORE trusting the commit marker

2.1a WHEN counting files on either side for this comparison THEN the system SHALL apply the SAME exclude rules the sync/manifest use — the family commit marker (``ScannedCharts.sqlite`` for scanned, ``Plates.sqlite`` for plates) and macOS metadata (``.DS_Store``, ``._*``) — so the comparison is EXACT. A naive filesystem walk counts the commit marker and transient ``.DS_Store`` files that the manifest excludes, producing a deterministic but spurious off-by-1-or-2 that would make an exact-match gate unreliable. The count/size compared MUST be the manifest-eligible set only. (Observed 2026-10-31: a raw walk showed 79,769 vs the manifest's 79,767; the two extras were exactly ``ScannedCharts.sqlite`` + ``.DS_Store``.)
2.2 WHEN the card's file count or total size does not match the expected set THEN the family SHALL be marked NOT current and routed to the sync path, regardless of the commit marker
2.3 WHEN the cheap comparison MATCHES THEN the family's currency SHALL be decided by the existing/eventual logic (marker and, ultimately, the content-hash manifest) — the cheap match SHALL NOT on its own declare the family current
2.4 WHEN the local manifest needed for the expected count/size is missing THEN the system SHALL build it (per the manifest-only-chart-sync policy) or, if that is out of scope here, SHALL treat the family as not-current (fail safe) rather than skipping the cheap check

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a family legitimately has matching count, size, marker, and (ultimately) manifest content THEN the system SHALL still be able to report it current via the normal path
3.2 WHEN the nav family is checked THEN its existing per-file checksum currency logic SHALL remain unchanged (this spec concerns the chart families' cheap tier only)
3.3 WHEN an interrupted-sync family is flagged in sync-state THEN it SHALL CONTINUE TO be forced not-current (that mechanism is independent of and unaffected by the cheap check)
3.4 WHEN the cheap check runs THEN it SHALL remain inexpensive (count + size only; no content hashing in this tier) so it does not add meaningful time to a normal insert

## Notes (non-authoritative — for design phase)

- Touch point: `check_drive_currency` / `_check_tree_family` in drive_updater.py — add a
  count+size comparison against the local manifest's entries (which carry per-file size)
  before returning "current".
- This is the cheap/fast-negative tier of a layered check: cheap count+size can REJECT
  currency; only the content-hash manifest can CONFIRM it. Keep the two tiers distinct.
- Interaction with manifest-only-chart-sync: if the local manifest is guaranteed/built
  there, this check can always compute the expected count+size. Decide ordering of the two
  specs in design; this one can also stand alone with a fail-safe (treat missing local
  manifest as not-current).
- Validation: a fixture with a correct marker but a short file count must be rejected
  (fast); a fully-matching family must NOT be declared current by the cheap tier alone
  (the decision must come from the manifest/marker path). Keep an exploration test that
  FAILS on current code (short-count family reported current).
- Backlog item: captured 2026-10-31 during chart-capable adopt testing. Not yet scheduled.
