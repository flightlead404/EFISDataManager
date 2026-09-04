# Getting Started

A five-minute quick start for EFIS Data Manager on macOS. For the full guide —
how everything works, options, security, and limitations — see
[README.md](README.md).

## 1. Install

1. Download `EFISDataManager.zip` from the
   [latest release](https://github.com/flightlead404/EFISDataManager/releases/latest)
   and unzip it (a folder appears, e.g. in Downloads).
2. Open that folder and **double-click `install.command`**.
3. A Terminal window opens and sets everything up (Python, dependencies, the
   chart-checker browser, the menu-bar app, and a login item). You may be asked
   for your Mac password, and on a new Mac to approve Apple's "Command Line
   Tools" download. That's expected. It can take a few minutes the first time.
4. When it says **Install complete**, you can close the window.

First launch may show a macOS security prompt because the app isn't
notarized — see **First launch security note** in the README if macOS blocks it.

## 2. Set it up (one time)

Click the small attitude-indicator (PFD) icon in the menu bar (top-right), then:

1. **Settings…** — set your **aircraft tail number**, choose your **Archive**
   and **USB Image** folders, and tick **which chart products** you subscribe to
   (VFR sectionals, IFR low/high, approach plates).
2. **Seattle Avionics Login…** — enter your chart-subscription email and
   password. These are stored in the macOS Keychain, never in a file.
3. **Analysis Dashboard…** (optional) — opens the browser dashboard; in its
   Settings set your number of cylinders and engine thresholds.

## 3. Prepare your EFIS USB drive (one time per drive)

A drive is only managed once you've prepared or adopted it — nothing happens to
a drive on its own until then.

1. Insert your USB drive.
2. Menu bar → **Prepare Drive…**
3. **Select Drive to Prepare** — type the exact name of your drive from the list.
4. Choose the path the app offers:
   - **Blank drive** → **Start clean**: reformats as FAT32/MBR, labels it, and
     copies your current charts + nav data. You'll confirm before anything is
     erased.
   - **Previously-used GRT drive** (from the Windows tool or an older version) →
     **Adopt & update** (non-destructive — keeps your charts, just brings them
     current) or **Start clean**.
   - **Already managed** → **Update** (incremental) or **Start clean**.
5. If the drive holds un-archived flight data or logbooks, you'll be asked to
   **Import first** (archive it) or **Erase** before it's overwritten.

A full first-time populate copies the entire chart set and can take a while;
the menu bar shows progress. Leave the drive connected until it finishes.

## 4. Everyday use

Just plug in your prepared drive. The tool automatically:

- archives the flight data your EFIS wrote (logs, snapshots, settings, logbook)
  and imports it for analysis, and
- brings the drive's charts and nav data current.

Chart and nav-database update checks also run on a background schedule, so your
local copy stays fresh between insertions.

## Using more than one drive

Many owners rotate drives (one current, an n‑1 spare in the airplane, one at the
Mac). Each managed drive carries its own identity file, so the app tracks them
independently. Give each drive a unique label when you prepare it, and connect
only one EFIS drive at a time (two at once is unsupported — see the README).

## If something looks stuck

- A large first-time sync is slow by nature. macOS writes a small hidden
  companion file next to each chart on FAT32/exFAT drives, which roughly doubles
  the file count on the initial populate (unavoidable, and harmless to the
  EFIS). The menu bar shows it's working, and routine updates afterward are much
  faster.
- If a drive genuinely stops responding mid-sync, the app now detects the stall
  and reports it rather than hanging — re-insert the drive, or try a different
  USB port, cable, or drive.

## More

Full documentation, security details, and uninstall steps are in
[README.md](README.md). Questions or problems: open an issue on the
[GitHub repository](https://github.com/flightlead404/EFISDataManager).
