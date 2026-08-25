# Pending Fixes — COMPLETED

All fixes have been applied except Task 11 (deferred instrumentation).

## Completed

1. ✅ .gitignore finalized
2. ✅ GitHub repo created and pushed (https://github.com/flightlead404/EFISDataManager)
3. ✅ `last_downloaded` metadata bug fixed (mtime captured before zip deletion)
4. ✅ Playwright subprocess environment (PLAYWRIGHT_BROWSERS_PATH set explicitly)
5. ✅ Startup check calls `_run_chart_check_auto` (no background-thread UI crash)
6. ✅ Nav DB timer has its own first-tick skip flag
7. ✅ Currency check uses ScannedCharts.sqlite mtime comparison
8. ✅ Interrupted sync resilience (.sync_in_progress state file)
9. ✅ SEC metadata updated in chart_cycles.json
10. ✅ Old Dropbox copy deleted

## Deferred: Task 11 — Instrument Next Chart Cycle

When next chart download happens, log:
- Total files in new extraction
- Files that differ from previous (by name/size)
- Files added/removed

Use this data to decide if format+copy is better than rsync for chart updates.

Implementation approach: before extraction, snapshot the target directory (filename → size dict).
After extraction, compute diff. Log as JSON to ~/EFIS/DataManagerLogs/chart_cycle_diff.json.
