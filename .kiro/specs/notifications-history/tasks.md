# Implementation Plan: Notifications History

## Overview

Add a bounded, most-recent-first notification history that captures every macOS
notification the menu-bar app posts (title, message, local-time timestamp) and
makes it reviewable from a new "Recent Notifications..." menu item. The value of
the feature is "nothing is missed," so capture is centralized: every one of the
57 direct `rumps.notification("EFIS Data Manager", headline, body)` calls in
`src/efis_data_manager/app.py` is mechanically replaced with
`self._notify(headline, body)`, leaving exactly one `rumps.notification(` call
(inside `_notify`) as the single posting point.

Work is ordered bottom-up so the property-tested core comes first. The pure
buffer logic lives in a NEW module `src/efis_data_manager/notification_history.py`
(`NotificationHistory` + a pure `format_entry_line` helper) that imports nothing
from `rumps`/`AppKit`, so all six correctness properties are driven by Hypothesis
in a headless `tests/test_notification_history.py`. Only after the pure core is
testable do we wire `app.py`: the `self._history` instance + `_notify` +
optional-persistence helpers in `__init__`, then the "Recent Notifications..."
menu item and `show_recent_notifications` handler, then the mechanical
replacement of all 57 call sites (that large edit comes last so it builds on the
already-present `_notify`).

The three `app.py` edits touch the SAME file and are therefore serialized
(wiring → menu/handler → 57-call replacement). Property-based tests use
**Hypothesis** (already in `requirements.txt`, min 100 iterations each, tagged
`# Feature: notifications-history, Property N`), and each design property
(Properties 1–6) is implemented as exactly one property-based test. Persistence
(Requirement 6) is the **should/optional** tier; the dashboard surface
(Requirement 4) is **deferred** and intentionally has no tasks here.

## Tasks

- [x] 1. Create the pure `notification_history.py` module
  - [x] 1.1 Implement `NotificationEntry`, `NotificationHistory`, and `format_entry_line`
    - Create `src/efis_data_manager/notification_history.py` importing only the
      stdlib (`collections.deque`, `datetime`); NO `rumps`/`AppKit` imports.
    - Add `CAP = 50` (History_Cap) and a lightweight `NotificationEntry` with
      `title: str`, `message: str`, `timestamp: str` (preformatted local time).
    - Implement `NotificationHistory(cap: int = CAP)` backed by
      `collections.deque(maxlen=cap)` in insertion order (oldest left, newest
      right). `add(self, title, message, when=None)`: coerce `title`/`message`
      with `str(...)`; build the timestamp from `when` (default
      `datetime.now()`, local time) formatted `YYYY-MM-DD HH:MM:SS`; append the
      entry so the deque's `maxlen` discards the oldest once the cap is exceeded.
    - `get_recent(self) -> list[NotificationEntry]` returns
      `list(reversed(self._entries))` (newest at index 0).
    - `to_serializable(self) -> list[dict]` returns a JSON-ready list of
      `{title, message, timestamp}` dicts, most-recent-first, at most `cap` items.
    - `from_serializable(cls, data, cap=CAP)` (classmethod): tolerant — non-list
      input or malformed items yield a valid (possibly empty) history and NEVER
      raise; receives newest-first data and re-appends in oldest-first order so
      `maxlen` keeps the newest `cap` when the input is over-length.
    - Add pure `format_entry_line(entry) -> str` returning
      `f"{entry.timestamp} — {entry.title} — {entry.message}"`.
    - _Requirements: 1.4, 1.5, 2.1, 2.2, 2.3, 5.1, 5.2, 5.3, 6.1, 6.2, 6.4_

  - [x]* 1.2 Write property test that add preserves title and message
    - **Property 1: Add preserves title and message without truncation**
    - **Validates: Requirements 1.5, 1.4**

  - [x]* 1.3 Write property test for the bounded cap
    - **Property 2: Bounded cap**
    - **Validates: Requirements 2.1**

  - [x]* 1.4 Write property test for FIFO eviction of the oldest entries
    - **Property 3: FIFO eviction retains the newest 50 in order**
    - **Validates: Requirements 2.2, 2.3**

  - [x]* 1.5 Write property test for most-recent-first ordering
    - **Property 4: Most-recent-first ordering**
    - **Validates: Requirements 5.1, 3.2**

  - [x]* 1.6 Write property test for the serialize/deserialize round-trip
    - **Property 5: Serialize/deserialize round-trip preserves entries, order, and cap**
    - **Validates: Requirements 6.1, 6.2, 6.4**

  - [x]* 1.7 Write property test that a robust load never raises
    - **Property 6: Robust load never raises**
    - **Validates: Requirements 6.3**

  - [x]* 1.8 Write example tests for `format_entry_line` and the timestamp format
    - `format_entry_line` for a known entry contains the timestamp, title, and
      message separated by ` — ` as designed. `add(when=<known datetime>)`
      yields a timestamp string matching `YYYY-MM-DD HH:MM:SS` for that local
      time. Add an INFO-style notice (e.g. "Charts Current") and confirm it is
      retained (no severity filter, unlike `RecentErrorHandler`).
    - _Requirements: 1.4, 3.3, 5.2, 5.3_

- [x] 2. Wire the history instance, `_notify`, and persistence into `app.py`
  - [x] 2.1 Add `self._history`, `_notify`, and the persistence helpers in `EFISDataManagerApp`
    - In `src/efis_data_manager/app.py`, import `NotificationHistory` and
      `format_entry_line` from `.notification_history`. In `__init__`,
      instantiate `self._history = NotificationHistory()` — loading from disk
      first when persistence is enabled (`self._history = self._load_history()`).
    - Add `_notify(self, title, message)`: post
      `rumps.notification("EFIS Data Manager", title, message)` exactly as today,
      then `self._history.add(title, message)`; when persistence is enabled call
      `self._save_history()`. Recording/persisting is best-effort and MUST NOT
      prevent the notification from posting.
    - Add the optional persistence tier (Req 6): `_load_history()` reads
      `~/Library/Application Support/EFISDataManager/notification_history.json`
      (dir is `config.APP_SUPPORT_DIR`), tolerating a missing file or invalid
      JSON by catching `json.JSONDecodeError`/`OSError` and returning
      `NotificationHistory.from_serializable([])` (empty). `_save_history()`
      writes `self._history.to_serializable()` (≤ 50 items) wrapped in
      try/except, logging a warning on `OSError` without interrupting posting.
    - _Requirements: 1.1, 1.2, 6.1, 6.2, 6.3, 6.4_

  - [ ]* 2.2 Write tests for the `_notify` posting+recording contract and persistence I/O
    - `_notify` contract: with `rumps.notification` monkeypatched to a mock,
      calling `_notify(headline, body)` calls the mock once with
      `("EFIS Data Manager", headline, body)` and leaves one history entry
      `{title: headline, message: body}`. If importing `app.py` headless is
      impractical (it pulls in `rumps`/`AppKit`), test the recording contract
      directly against `NotificationHistory` plus the documented arg→field
      mapping instead — so this is not a blocker.
    - Persistence round-trip (temp file): after adds the file holds the same
      entries as `get_recent()` (≤ 50); loading that file reconstructs the same
      history; a missing file and a file with invalid JSON both load to an empty
      history without error.
    - _Requirements: 1.1, 1.2, 6.1, 6.2, 6.3_

- [x] 3. Add the "Recent Notifications..." menu item and handler in `app.py`
  - [x] 3.1 Insert the menu item and implement `show_recent_notifications`
    - In `src/efis_data_manager/app.py`, insert `"Recent Notifications..."` into
      the `self.menu` list immediately BEFORE `"Recent Errors..."` so the two
      review surfaces sit together. Bind it via the `@rumps.clicked("Recent
      Notifications...")` decorator (mirroring `show_recent_errors`).
    - Implement `show_recent_notifications(self, _)`: read
      `entries = self._history.get_recent()`; on empty, `rumps.alert(title=
      "Recent Notifications", message="No notifications have been recorded this
      session.", ok="OK")`; otherwise join `format_entry_line(e)` for each entry
      (already newest-first) with `"\n\n"` and show
      `rumps.alert(title=f"Recent Notifications ({len(entries)})",
      message=body, ok="OK")`.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 5.4_

  - [ ]* 3.2 Write example tests for the handler formatting and empty state
    - For a history with several entries, the composed body lists lines via
      `format_entry_line` in `get_recent()` (newest-first) order. With an empty
      history the handler produces the empty-state message, not a blank body.
      If `app.py` cannot be imported headless, assert against the same
      formatting/empty-state logic exercised through `NotificationHistory` +
      `format_entry_line`.
    - _Requirements: 3.2, 3.3, 3.4, 5.4_

- [x] 4. Replace the 57 direct `rumps.notification(...)` call sites with `self._notify(...)`
  - [x] 4.1 Mechanically route all posting through `_notify`
    - In `src/efis_data_manager/app.py`, replace every
      `rumps.notification("EFIS Data Manager", <headline>, <body>)` with
      `self._notify(<headline>, <body>)`, carrying `<headline>` and `<body>`
      (including multi-line f-strings and `str(e)[:100]` truncations) over
      verbatim so message text is unchanged. Leave the `rumps.notification(`
      call inside `_notify` as the sole remaining one; do NOT touch the unrelated
      `rumps.alert(...)` calls in `show_recent_errors`, diagnostics, or about.
    - _Requirements: 1.3_

  - [x]* 4.2 Write a static test asserting a single `rumps.notification(` call remains
    - Read `src/efis_data_manager/app.py` and assert the count of
      `rumps.notification(` occurrences is exactly 1 — proving no call site was
      missed and all posting is centralized through `_notify`.
    - _Requirements: 1.3_

- [x] 5. Final checkpoint - full suite
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster
  MVP; core implementation tasks are never optional.
- Each task references specific requirement sub-clauses and, for test tasks, the
  exact design property number it implements.
- All six design properties map to exactly one property-based test:
  P1 → 1.2; P2 → 1.3; P3 → 1.4; P4 → 1.5; P5 → 1.6; P6 → 1.7.
- Property-based tests use Hypothesis (`max_examples >= 100`) and carry a
  `# Feature: notifications-history, Property N: …` tag. Hypothesis is ALREADY in
  `requirements.txt`, so no dependency-bump task is needed. Tests import only
  `notification_history` (no `rumps`/`AppKit`) so they run headless.
- The pure `NotificationHistory` buffer is the property-tested core (ordering,
  cap, round-trip, robust load). UI wiring (`_notify` posting, menu rendering,
  empty state), timestamp formatting, and file I/O are side-effect concerns
  covered by example/integration tests, not properties.
- The `_notify` posting+recording contract (task 2.2) and the handler formatting
  (task 3.2) are tested against `NotificationHistory` + the documented arg→field
  mapping when importing `app.py` headless is impractical (it pulls in the macOS
  UI stack), per the design — so those tests are never a blocker.
- **Persistence (Requirement 6) is the should/optional tier:** implemented in
  task 2.1 (`_load_history`/`_save_history` + `notification_history.json`) and
  covered by task 2.2; it may be deferred without blocking the required in-memory
  core (Requirements 1, 2, 3, 5).
- **Dashboard surface (Requirement 4) is deferred** and intentionally has NO
  tasks here; when built it would render `NotificationHistory.get_recent()` on
  the Alerts page while leaving the `/api/alerts` anomaly computation unchanged.
- The three `app.py` edits (tasks 2.1, 3.1, 4.1) touch the same file and are
  ordered wiring → menu/handler → 57-call replacement; the replacement comes last
  so it can call the already-present `_notify`.
- This change adds a new module and edits the menu-bar tool (`app.py`) only; on
  release, bump versions per the versioning policy (menu bar changed, so
  `MENUBAR_VERSION`).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "2.1"] },
    { "id": 2, "tasks": ["2.2", "3.1"] },
    { "id": 3, "tasks": ["3.2", "4.1"] },
    { "id": 4, "tasks": ["4.2"] }
  ]
}
```
