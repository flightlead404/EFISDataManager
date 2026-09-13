# Requirements Document

## Introduction

The EFIS Data Manager menu-bar app posts macOS notifications for many events:
scheduled currency notices ("Charts Current", "Drive Current"), archive/eject/import
results, chart download progress, and various failures. These notifications
auto-dismiss before the user can read them, and there is no way to review them
afterward. The application log records everything, but the log is not user-facing.

This feature adds a rolling, reviewable **notification history**. Every notification
the app posts is also captured (title, message, timestamp) into a bounded in-memory
ring buffer, regardless of severity, and made reviewable from the menu bar. Posting is
centralized through a single helper so that no call site is missed. Optionally, the
most-recent entries persist to disk so they survive an app restart, and the dashboard
Alerts page may surface the history.

This is a promotion of the "Notifications history" BACKLOG item
(BACKLOG.md, requested 2026-09-09).

## Glossary

- **Menu_Bar_App**: The `rumps.App` subclass `EFISDataManagerApp` in
  `src/efis_data_manager/app.py`. The component that posts all user-facing macOS
  notifications and owns the notification history.
- **Notification**: A single user-facing macOS notice, currently posted via
  `rumps.notification(title, subtitle, message, ...)`. In this app the first
  argument is consistently the constant title `"EFIS Data Manager"`, the second is
  the short headline (subtitle), and the third is the body message.
- **Notification_Entry**: A recorded item in the history consisting of a `title`
  (the notice headline), a `message` (the body text), and a `timestamp`
  (capture time in local time).
- **Notification_History**: The bounded, most-recent-first ring buffer of
  `Notification_Entry` items owned by the `Menu_Bar_App`.
- **Notify_Helper**: The single internal method (referred to as `self._notify(title, message)`)
  through which all notifications are posted. It both posts the OS notification and
  appends a `Notification_Entry` to the `Notification_History`.
- **History_Cap**: The maximum number of `Notification_Entry` items retained
  (specified as 50).
- **RecentErrorHandler**: The existing WARNING+-only logging ring buffer used as the
  design reference (see Known Inputs). It is a separate mechanism and is not modified
  by this feature.

## Known Inputs (cited from the codebase)

These facts were confirmed by reading the code and drive the requirements below.

- **Notifications are posted directly in many places.** `rumps.notification(...)`
  appears **57 times** in `src/efis_data_manager/app.py` (confirmed via
  `grep -c "rumps.notification" src/efis_data_manager/app.py`). Representative call
  sites include: unmanaged drive detected (~line 239), EFIS drive detected (~line 367),
  drive ejected (~lines 399, 470), eject failures (~lines 474, 479), drive verify
  (~lines 535, 542, 547), archive complete/errors/failed (~lines 587, 591, 611),
  flight data ready (~line 597), drive update flow (~lines 674, 690, 703, 717, 737,
  743, 751), "busy" guards (~lines 832, 841, 850, 907), chart check/download
  (~lines 869, 877, 892, 897, 924, 934, 938, 943, 961), and more. Every call uses the
  literal title `"EFIS Data Manager"` with a headline + body. Because posting is
  scattered, capture must be centralized to guarantee nothing is missed.

- **An existing "Recent Errors" ring buffer exists — WARNING+ only.**
  `RecentErrorHandler(logging.Handler)` is defined at `src/efis_data_manager/app.py:34`,
  instantiated as the shared `_recent_error_handler = RecentErrorHandler(capacity=10)`
  at line 59, and wired into `logging.basicConfig(...)` as a handler (line 68). It:
  - is constructed with `level=logging.WARNING`, so it only retains WARNING and
    above — narrower than this feature, which captures **all** notifications (INFO included);
  - stores strings in a `collections.deque(maxlen=capacity)` (lines 40-41);
  - formats each record as `"[YYYY-MM-DD HH:MM:SS] LEVEL: message"` using
    `datetime.fromtimestamp(record.created)` (local time);
  - exposes `get_recent() -> list[str]` (line 54).
  Its menu-bar review surface is a menu item `"Recent Errors..."` (declared at line 152)
  with handler `show_recent_errors` (line 1510) that shows a `rumps.alert` titled
  `"Recent Errors (N)"` with entries **most-recent-first** (`reversed(errors)`), and an
  empty-state alert reading "No errors or warnings logged this session." The
  Notification_History mirrors this shape (deque, local-time timestamps, most-recent-first
  review, empty-state message) but is broader in scope (all severities).

- **A persistence location convention already exists.** `src/efis_data_manager/config.py`
  defines `APP_SUPPORT_DIR = ~/Library/Application Support/EFISDataManager` and stores
  `config.json` there (line 23). This is the appropriate location for an optional
  history persistence file.

- **The dashboard has an Alerts surface.** `src/efis_data_manager/dashboard/app.py`
  serves `/alerts` (`alerts_page`, line 87 → `alerts.html`) and `/api/alerts`
  (`api_alerts`, line 503), which currently return detected flight-data **anomalies**.
  This is a candidate optional secondary surface for the notification history; the
  anomaly/alerts computation itself is out of scope (see Out of Scope).

## Requirements

### Requirement 1: Centralized capture of every notification

**User Story:** As a pilot using the menu-bar app, I want every notification the app
shows to be recorded, so that I can review notices I missed regardless of type or severity.

#### Acceptance Criteria

1. THE Menu_Bar_App SHALL provide a single Notify_Helper method `self._notify(title, message)` that posts the macOS notification and appends a corresponding Notification_Entry to the Notification_History.
2. WHEN the Menu_Bar_App posts a Notification through the Notify_Helper, THE Menu_Bar_App SHALL append a Notification_Entry containing the title, the message, and a capture timestamp to the Notification_History.
3. THE Menu_Bar_App SHALL route all user-facing notification posting in `app.py` through the Notify_Helper, replacing direct `rumps.notification(...)` calls.
4. THE Menu_Bar_App SHALL record a Notification_Entry for a Notification of any severity, including informational (INFO) notices such as "Charts Current" and "Drive Current".
5. WHEN a Notification is recorded, THE Menu_Bar_App SHALL preserve the original title and message text without truncation for storage purposes.

### Requirement 2: Bounded history size

**User Story:** As a user, I want the history to stay small, so that memory use is bounded and I only see recent, relevant notices.

#### Acceptance Criteria

1. THE Notification_History SHALL retain at most History_Cap (50) Notification_Entry items.
2. WHEN appending a Notification_Entry would exceed History_Cap, THE Menu_Bar_App SHALL discard the oldest Notification_Entry so that the count does not exceed History_Cap.
3. THE Menu_Bar_App SHALL retain the History_Cap most-recent Notification_Entry items when discarding.

### Requirement 3: Review from the menu bar (primary)

**User Story:** As a user, I want to open a "Recent Notifications" view from the menu bar, so that I can read notices that auto-dismissed.

#### Acceptance Criteria

1. THE Menu_Bar_App SHALL provide a menu item ("Recent Notifications...") that opens a view of the Notification_History.
2. WHEN the user opens the Recent Notifications view, THE Menu_Bar_App SHALL display the recorded Notification_Entry items ordered most-recent-first.
3. WHEN the user opens the Recent Notifications view, THE Menu_Bar_App SHALL display, for each Notification_Entry, the title, the message, and the timestamp.
4. IF the Notification_History contains no Notification_Entry items, THEN THE Menu_Bar_App SHALL display an empty-state message indicating no notifications have been recorded.

### Requirement 4: Review from the dashboard (optional / secondary)

**User Story:** As a user, I want to optionally review notification history in the dashboard, so that I can see it alongside flight alerts.

#### Acceptance Criteria

1. WHERE the dashboard notification-history surface is enabled, THE Dashboard SHALL present the recorded Notification_Entry items ordered most-recent-first, each showing title, message, and timestamp.
2. WHERE the dashboard notification-history surface is enabled, THE Dashboard SHALL leave the existing `/api/alerts` anomaly computation and its output unchanged.

> Requirement 4 is a **should/optional** tier. Requirement 3 (menu-bar review) is the primary, required review surface. Requirement 4 may be deferred without blocking the feature.

### Requirement 5: Ordering, timestamps, and empty state

**User Story:** As a user, I want notifications shown newest-first with readable local times, so that I can quickly find what just happened.

#### Acceptance Criteria

1. THE Menu_Bar_App SHALL order the Notification_History for review with the most-recently-recorded Notification_Entry first.
2. THE Menu_Bar_App SHALL record and display each Notification_Entry timestamp in the local time zone.
3. THE Menu_Bar_App SHALL format each displayed timestamp as `YYYY-MM-DD HH:MM:SS`.
4. IF the Notification_History is empty when a review surface is opened, THEN THE review surface SHALL show an explicit empty-state message rather than a blank view.

### Requirement 6: Persistence across restart (optional / should)

**User Story:** As a user, I want recent notifications to survive an app restart, so that I do not lose notices when the app relaunches.

#### Acceptance Criteria

1. WHERE persistence is enabled, WHEN a Notification_Entry is recorded, THE Menu_Bar_App SHALL persist the History_Cap (50) most-recent Notification_Entry items to a JSON file at `~/Library/Application Support/EFISDataManager/notification_history.json`.
2. WHERE persistence is enabled, WHEN the Menu_Bar_App launches, THE Menu_Bar_App SHALL load the persisted Notification_Entry items into the Notification_History, preserving most-recent-first ordering.
3. WHERE persistence is enabled, IF the persistence file is missing or contains invalid JSON, THEN THE Menu_Bar_App SHALL start with an empty Notification_History and continue operating without error.
4. WHERE persistence is enabled, THE Menu_Bar_App SHALL persist at most History_Cap (50) Notification_Entry items to the file.

> Requirement 6 is a **should/optional** tier. The in-memory history (Requirements 1-3, 5) is the required core; persistence may be deferred.

## Out of Scope

- **Not changing which notifications are posted.** This feature does not add, remove,
  or alter the set of notifications the app posts, nor their titles, messages, or timing.
- **No new alert logic.** No new detection, thresholds, or alerting behavior is introduced.
- **No changes to the dashboard anomaly/alerts computation.** The existing `/alerts`
  page, `/api/alerts` endpoint, and their flight-data anomaly logic remain unchanged.
- **Not modifying the existing "Recent Errors" ring buffer.** `RecentErrorHandler` and
  the "Recent Errors..." menu item are used only as a design reference and are left intact.
- **No notification acknowledgement, filtering, search, or deletion UI** beyond the
  review surface(s) described above.
