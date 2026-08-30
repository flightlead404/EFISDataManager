# EFIS Data Manager

Ground-support automation for GRT HXr / Mini EFIS systems on macOS. It keeps a
GRT chart USB drive current, archives the flight data your EFIS records, and
provides an analysis dashboard for engine and flight data.

## Why

Seattle Avionics does not offer a Mac version of its Chart Data Manager — the
official tool is Windows-only. If you fly behind a GRT EFIS and use a Mac, there
is no supported way to keep your chart/nav data current or to work with the
flight data your EFIS logs. This project fills that gap: plug in your EFIS USB
drive and everything happens automatically, with a companion dashboard for
analyzing what your engine and airframe are doing.

> Independent project, not affiliated with or endorsed by GRT Avionics or
> Seattle Avionics. Use at your own risk; always verify chart currency before
> flight.

## Functionality

**Menu-bar tool (automatic, runs in the background):**
- Detects when a GRT EFIS USB drive is inserted (`EFIS`, `EFIS_1`, ... or any
  drive containing a `GRTCHARTS/` folder).
- Downloads and stages current chart data and **GRT navigation databases** to a
  local USB image. You choose which chart products to pull based on your
  subscription — **VFR sectionals, IFR low, IFR high, approach plates** — so a
  VFR-only pilot isn't downloading (or paying storage for) IFR data.
- Syncs the drive to the current data (delta sync; resilient to interruptions).
- **Prepare Drive**: formats and provisions a fresh USB stick as an EFIS drive.
- **Archives** the data your EFIS writes each flight: FDL engine/flight logs,
  DEMO recordings, snapshots, settings backups, and the logbook.
- Notifications, a "Recent Errors" view, and a Diagnostics panel.

**Analysis dashboard (opens in your browser):**
- **Operations list** — every engine start/stop cycle, flagged flight vs. ground.
- **Flight detail** — stacked, reorderable panels (EGT, CHT, power, flight data,
  and more) with full 1-second resolution, synchronized zoom/pan, a shared
  crosshair, and any parameter selectable into any panel.
- **GAMI lean test** — auto-detects lean sweeps and shows the classic
  EGT-vs-fuel-flow plot with per-cylinder peaks, GAMI spread, and peak order.
- **Trends** — rolling engine-parameter trends across flights.
- **Alerts** — CHT/EGT/oil exceedances and anomalies, with jump-to-timestamp.
- **Oil consumption** — tracked from logbook additions and manual oil-change
  entries, with a consumption-rate chart.
- **CSV export** for flight summaries, trends, oil, and raw FDL data.

## Prerequisites

- **macOS** (Apple Silicon or Intel).
- **Python 3.11 or newer** (`python3 --version`). If you don't have it:
  `brew install python` (install [Homebrew](https://brew.sh) first if needed).
- A **Seattle Avionics chart subscription** (for chart/nav downloads).
- A **GRT EFIS USB drive** (or a blank USB stick to prepare one).

## Install

```bash
# 1. Get the project (a specific tested release)
git clone --branch v0.9.2 --depth 1 https://github.com/flightlead404/EFISDataManager.git
cd EFISDataManager

# 2. Run the installer (creates venv, installs deps + browser, sets up the app)
./install.sh
```

To update later, fetch the newer release tag and re-run the installer:

```bash
git fetch --tags
git checkout v0.9.3   # whichever release you're moving to
./install.sh
```

The installer:
- Creates a Python virtual environment and installs dependencies.
- Installs the Playwright Chromium browser (one-time, ~100 MB — required for
  GRT nav-database checks).
- Installs a menu-bar app to `/Applications/EFIS Data Manager.app` and a login
  item so it starts automatically after you log in. Both are generated with
  **your** home directory — nothing is hardcoded.

## First-time setup

1. Launch **EFIS Data Manager** from `/Applications` (or it starts at next login).
   Look for the `EFIS` icon in the menu bar.
2. **Settings…** — set your **aircraft tail number**, choose your Archive and
   USB Image folders, and select **which chart products to download** (VFR
   sectionals, IFR low, IFR high, approach plates) to match your subscription.
3. **Seattle Avionics Login…** — enter your subscription email and password.
   Credentials are stored in the **macOS Keychain** (see Security below), never
   in the project or in a plaintext config file.
4. **Analysis Dashboard…** — opens the browser dashboard. In its Settings, set
   your **number of cylinders** and engine parameter thresholds.

Then insert your EFIS USB drive: the tool archives your flight data, imports it
for analysis, and brings the drive current. Chart/nav data downloads run on a
background schedule.

## Notes & limitations

- **EFIS software updates** (HXr / Mini display firmware) are detect-and-notify
  only. grtavionics.com is behind bot protection that blocks automated
  downloads; the tool tells you when a new version is available so you can
  download it manually. Nav database updates are fully automated.
- The chart data files, credentials, and analysis database stay **local to your
  Mac**. Nothing is uploaded anywhere.
- First chart sync to a slow USB stick can take a while (many thousands of small
  files). Subsequent syncs are incremental.

## Security & privacy

- **Your Seattle Avionics credentials are stored in the macOS Keychain**, using
  the system `security` service — the same protected store macOS uses for
  Safari and Wi-Fi passwords. They are never written to the project directory,
  the config file, or any log, so there's nothing sensitive to accidentally
  commit or share.
- Everything stays **on your Mac**: chart files, the analysis database, and your
  flight data are all local. The tool talks only to Seattle Avionics and GRT to
  fetch updates; it uploads nothing.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.efisdatamanager.plist
rm ~/Library/LaunchAgents/com.efisdatamanager.plist
rm -rf "/Applications/EFIS Data Manager.app"
# Optional: remove the project folder, ~/EFIS data, and app-support config
```

## Support

This is an early release for testing. Please report issues with the contents of
`~/EFIS/DataManagerLogs/efis_data_manager.log` and the Diagnostics panel output.

## License

Copyright (C) 2026 Martin C. Walker.

EFIS Data Manager is free software: you can redistribute it and/or modify it
under the terms of the **GNU Affero General Public License** as published by the
Free Software Foundation, either version 3 of the License, or (at your option)
any later version. See the [LICENSE](LICENSE) file for the full text.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

The AGPL is a strong copyleft license: anyone who distributes this software or a
modified version — including running a modified version as a network service —
must make the complete corresponding source code available under the same terms.
The source is at https://github.com/flightlead404/EFISDataManager.
