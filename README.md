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

- **A Mac** running macOS (Apple Silicon or Intel).
- A **Seattle Avionics chart subscription** (for chart/nav downloads).
- A **GRT EFIS USB drive** (or a blank USB stick to prepare one).
- An internet connection for the first install.

You do **not** need to install Python or anything else yourself — the installer
sets up everything it needs.

## Install (recommended, no technical steps)

1. Go to the [latest release](https://github.com/flightlead404/EFISDataManager/releases/latest)
   and download **`EFISDataManager.zip`** (under "Assets").
2. In your **Downloads** folder, double-click the ZIP to unzip it. You'll get a
   folder named `EFISDataManager`.
3. Open that folder and **right-click `install.command` → Open**. (Right-click →
   Open is needed the first time because the app isn't from the App Store — see
   [First launch security note](#first-launch-security-note) below. A normal
   double-click may be blocked by macOS.)
4. A Terminal window opens and the installer runs. Follow the on-screen prompts:
   - On a new Mac, macOS may ask to install **Command Line Tools** — click
     **Install** and accept.
   - You may be asked for your **Mac password** (to install Homebrew/Python).
   - The installer then downloads everything and sets up the app. The one-time
     browser download is ~100 MB, so give it a few minutes.
5. When it says **"All done,"** the **EFIS Data Manager** icon appears in your
   menu bar (top-right) and in your Applications folder.

That's it. The installer handles Homebrew, Python, the app dependencies, the
menu-bar app, and starting it at login — all using **your** home folder, nothing
hardcoded.

### First launch security note

Because this is a free, un-notarized app (not distributed through the App
Store), macOS Gatekeeper will warn you the first time. This is expected:

- For `install.command`: **right-click it → Open**, then click **Open** in the
  dialog. You only need to do this once.
- If macOS still blocks it, go to **System Settings → Privacy & Security**,
  scroll down, and click **Open Anyway**.

### Install with git (for developers)

If you're comfortable with the terminal and have git:

```bash
git clone --branch v0.9.6 --depth 1 https://github.com/flightlead404/EFISDataManager.git
cd EFISDataManager
./install.sh
```

To update later:

```bash
git fetch --tags
git checkout v0.9.7   # whichever release you're moving to
./install.sh
```

## First-time setup

1. Launch **EFIS Data Manager** from `/Applications` (or it starts at next login).
   Look for the small attitude-indicator (PFD) icon in the menu bar, top-right.
2. **Settings…** — set your **aircraft tail number**, choose your Archive and
   USB Image folders, and select **which chart products to download** (VFR
   sectionals, IFR low, IFR high, approach plates) to match your subscription.
3. **Seattle Avionics Login…** — enter your subscription email and password.
   Credentials are stored in the **macOS Keychain** (see Security below), never
   in the project or in a plaintext config file.
4. **Analysis Dashboard…** — opens the browser dashboard. In its Settings, set
   your **number of cylinders** and engine parameter thresholds.

## How to use

Once set up, the tool lives in your menu bar and works mostly on its own.

**Everyday use — just plug in your drive.** When you insert your EFIS USB drive,
the tool automatically archives the flight data your EFIS wrote (FDL logs, DEMO
recordings, snapshots, settings, logbook), imports it for analysis, and brings
the drive's charts and nav data current. Chart/nav update checks also run on a
background schedule, so the local copy stays fresh between insertions.

**USB drive naming — how a drive is recognized.** The tool treats a mounted
volume as an EFIS drive if **either** of these is true:

- The volume is named **`EFIS`** (case-insensitive). A trailing number with an
  optional space, underscore, or hyphen is also accepted, so macOS's automatic
  suffixes and rotating drives work too: `EFIS`, `EFIS1`, `EFIS 2`, `EFIS_3`,
  `EFIS-4`. Names with extra words (like `EFIS_BACKUP`) are **not** matched.
- **or** the volume contains a **`GRTCHARTS/`** folder at its root — so an
  already-provisioned GRT drive is detected regardless of its name.

**Preparing a fresh USB stick (Prepare Drive…).** To turn a blank or repurposed
USB stick into an EFIS drive, use the menu bar's **Prepare Drive…** item. It:

1. Lists your removable volumes and asks you to type the exact name of the one
   to use (internal and Time Machine drives are excluded).
2. Asks for a second explicit confirmation — **this erases everything on the
   drive.**
3. Reformats the entire drive as **FAT32** with an **MBR** partition map (the
   layout GRT EFIS expects) and **names the volume `EFIS` for you**.
4. Creates the `GRTCHARTS/` folder and populates the drive with your current
   chart and nav data.

You don't name the drive yourself when using Prepare Drive — it's labeled `EFIS`
automatically. Manual naming only matters if you format a stick yourself instead
of using Prepare Drive; in that case, format it FAT32 and name it `EFIS`.

> **Prepare Drive is destructive.** It erases the whole target disk. Double-check
> the volume name before confirming, and never point it at a drive holding data
> you want to keep.

**Other menu items:** Settings…, Seattle Avionics Login…, Analysis Dashboard…,
Diagnostics… (versions, paths, and a self-check), plus a Recent Errors view.
Quitting from the menu is logged.

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
